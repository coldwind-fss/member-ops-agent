import base64
import binascii
import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from .auth import Operator, Writer
from .http import ApiError
from .queue import enqueue
from .store import execute, json_data, many, one

router = APIRouter()
MATTER_FIELDS = "id,version,status,conversation_kind,conversation_external_id,participant_label,subject,created_at,updated_at"
HANDOFF_FIELDS = "id,matter_id,version,status,summary,reason,notification_state,resolution,created_at,updated_at"
Limit = Annotated[int, Query(ge=1, le=100)]
MatterStatus = Literal["open", "waiting_member", "human_handling", "closed"]
HandoffStatus = Literal["pending", "resolved"]


def decode_cursor(cursor, filters):
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if data["filters"] != filters:
            raise ValueError
        at = datetime.fromisoformat(data["at"])
        if at.tzinfo is None:
            raise ValueError
        return at, UUID(data["id"])
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeError):
        raise ApiError(422, "VALIDATION_ERROR", "分页游标无效或筛选条件已改变。") from None


async def page(engine, source, fields, filters, limit, cursor, *, order_at="created_at"):
    conditions = [f"{field}=:{field}" for field in filters]
    params = {**filters, "limit": limit + 1}
    if cursor:
        params["at"], params["cursor_id"] = decode_cursor(cursor, filters)
        conditions.append(f"({order_at},id)<(:at,:cursor_id)")
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    async with engine.connect() as conn:
        rows = await many(conn, f"SELECT {fields} FROM {source}{where} ORDER BY {order_at} DESC,id DESC LIMIT :limit", **params)
    items = [dict(row) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        row = items[-1]
        data = json_data({"filters": filters, "at": row[order_at].isoformat(), "id": str(row["id"])})
        next_cursor = base64.urlsafe_b64encode(data.encode()).decode()
    return {"items": items, "next_cursor": next_cursor}


@router.get("/matters")
async def list_matters(request: Request, operator: Operator, limit: Limit = 20, cursor: str | None = None,
                       status: MatterStatus | None = None,
                       conversation_external_id: Annotated[str | None, Query(min_length=1, max_length=255)] = None):
    filters = {key: value for key, value in {"status": status, "conversation_external_id": conversation_external_id}.items()
               if value is not None}
    return await page(request.app.state.database, "matters", MATTER_FIELDS, filters, limit, cursor)


@router.get("/matters/{matter_id}")
async def get_matter(matter_id: UUID, request: Request, operator: Operator):
    async with request.app.state.database.connect() as conn:
        matter = await one(conn, f"SELECT {MATTER_FIELDS} FROM matters WHERE id=:id", id=matter_id)
        if not matter:
            raise ApiError(404, "NOT_FOUND", "会话事项不存在。")
        messages = await many(conn, """
            SELECT id,kind,preview,occurred_at,delivery_state FROM messages msg
            WHERE msg.matter_id=:id OR EXISTS (
                SELECT 1 FROM matter_inputs i WHERE i.matter_id=:id AND i.message_id=msg.id
            )
            ORDER BY occurred_at,id
        """, id=matter_id)
        evidence = await many(conn, """
            SELECT DISTINCT k.id AS knowledge_id,k.version,k.title FROM answer_evidence e
            JOIN agent_runs r ON r.id=e.run_id JOIN knowledge k ON k.id=e.knowledge_id AND k.version=e.version
            WHERE r.matter_id=:id ORDER BY k.title,k.version
        """, id=matter_id)
        runs = await many(conn, """
            SELECT id,intents,outcome,error_code,recorded_at FROM agent_runs WHERE matter_id=:id ORDER BY recorded_at,id
        """, id=matter_id)
        handoff = await one(conn, "SELECT id FROM handoffs WHERE matter_id=:id", id=matter_id)
    return {"matter": dict(matter), "messages": list(map(dict, messages)), "evidence": list(map(dict, evidence)),
            "runs": list(map(dict, runs)), "handoff_id": handoff["id"] if handoff else None}


@router.get("/handoffs")
async def list_handoffs(request: Request, operator: Operator, limit: Limit = 20,
                        cursor: str | None = None, status: HandoffStatus | None = None):
    return await page(request.app.state.database, "handoffs", HANDOFF_FIELDS,
                      {"status": status} if status else {}, limit, cursor)


class ResolveHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_version: int = Field(ge=1)
    resolution: str = Field(min_length=1, max_length=4000)
    resume_agent: bool = False


@router.post("/handoffs/{handoff_id}/resolve")
async def resolve_handoff(handoff_id: UUID, data: ResolveHandoff, request: Request, operator: Writer):
    key = request.headers.get("Idempotency-Key", "")
    if not 8 <= len(key) <= 128:
        raise ApiError(400, "BAD_REQUEST", "请提供有效的 Idempotency-Key。")
    action = f"resolve-handoff:{handoff_id}"
    request_data = json_data(data.model_dump())
    async with request.app.state.database.begin() as conn:
        previous = await one(conn, """
            INSERT INTO api_writes (operator_id,action,operation_key,request)
            VALUES (:operator,:action,:key,CAST(:body AS jsonb))
            ON CONFLICT DO NOTHING RETURNING operation_key
        """, operator=operator.operator_id, action=action, key=key, body=request_data)
        if not previous:
            saved = await one(conn, """
                SELECT response,request=CAST(:body AS jsonb) AS same FROM api_writes
                WHERE operator_id=:operator AND action=:action AND operation_key=:key
            """, operator=operator.operator_id, action=action, key=key, body=request_data)
            if not saved["same"]:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "此请求键已用于其他内容。")
            return saved["response"]
        linked = await one(conn, "SELECT matter_id FROM handoffs WHERE id=:id", id=handoff_id)
        if not linked:
            raise ApiError(404, "NOT_FOUND", "待办不存在。")
        matter = await one(conn, "SELECT * FROM matters WHERE id=:id FOR UPDATE", id=linked["matter_id"])
        handoff = await one(conn, "SELECT * FROM handoffs WHERE id=:id FOR UPDATE", id=handoff_id)
        if handoff["version"] != data.expected_version:
            raise ApiError(409, "VERSION_CONFLICT", "待办已更新，请刷新。", {"current_version": handoff["version"]})
        if handoff["status"] != "pending" or matter["status"] != "human_handling":
            raise ApiError(409, "INVALID_STATE", "当前事项无法按此操作处理。")
        row = await one(conn, f"""
            UPDATE handoffs SET status='resolved',resolution=:resolution,version=version+1,updated_at=now()
            WHERE id=:id RETURNING {HANDOFF_FIELDS}
        """, id=handoff_id, resolution=data.resolution)
        version = matter["version"] + 1
        await execute(conn, """
            UPDATE matters SET status=:status,version=:version,updated_at=now() WHERE id=:id
        """, status="open" if data.resume_agent else "closed", version=version, id=matter["id"])
        if data.resume_agent:
            await enqueue(conn, "resume", f"resume:{handoff_id}:{row['version']}", f"matter:{matter['id']}",
                          {"matter_id": str(matter["id"]), "expected_version": version,
                           "cycle": str(handoff["notification_delivery_id"]), "resolution": data.resolution})
        result = jsonable_encoder(dict(row))
        await execute(conn, """
            UPDATE api_writes SET response=CAST(:response AS jsonb)
            WHERE operator_id=:operator AND action=:action AND operation_key=:key
        """, operator=operator.operator_id, action=action, key=key, response=json_data(result))
        return result
