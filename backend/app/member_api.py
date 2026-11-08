from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from .auth import Operator, Writer
from .http import ApiError
from .ingress import Input, require_fixture
from .members import profile_rows, valid_preference
from .operator_writes import check_version, operator_write
from .store import execute, json_data, many, one

router = APIRouter()


class Versioned(Input):
    expected_version: int = Field(ge=1)


class MemoryChange(Versioned):
    action: Literal["confirm", "correct", "delete"]
    value: str | None = Field(default=None, max_length=100)


class Membership(Input):
    connection_id: Literal["fixture-channel"]
    group_id: str = Field(min_length=1, max_length=255)
    sender_external_id: str = Field(min_length=1, max_length=255)
    active: bool


@router.post("/dev/group-memberships", dependencies=[Depends(require_fixture)])
async def fixture_membership(data: Membership, request: Request):
    async with request.app.state.database.begin() as conn:
        return dict(await one(conn, """
            INSERT INTO group_memberships (connection_id,group_id,sender_external_id,active)
            VALUES (:connection_id,:group_id,:sender_external_id,:active)
            ON CONFLICT (connection_id,group_id,sender_external_id) DO UPDATE SET active=EXCLUDED.active,updated_at=now()
            RETURNING *
        """, **data.model_dump()))


def summary(row):
    return {**dict(row), "status": "verified" if row["identities"] else "unbound"}


@router.get("/members")
async def members(request: Request, operator: Operator, group_id: str | None = None):
    async with request.app.state.database.connect() as conn:
        rows = await profile_rows(conn)
    return {"items": [summary(row) for row in rows if not group_id or group_id in row["groups"]]}


@router.get("/members/{profile_id}")
async def member_detail(profile_id: UUID, request: Request, operator: Operator):
    async with request.app.state.database.connect() as conn:
        row = next((item for item in await profile_rows(conn) if item["id"] == profile_id), None)
        if not row:
            raise ApiError(404, "NOT_FOUND", "会员档案不存在。")
        memories = await many(conn, """
            SELECT m.id,m.kind,m.topic,m.value,m.status,m.version,m.event_id,m.source_at,m.expires_at,
                msg.preview AS evidence,
                (SELECT matter_id FROM matter_inputs WHERE message_id=msg.id ORDER BY item_index LIMIT 1) AS matter_id
            FROM member_memories m LEFT JOIN messages msg ON msg.event_id=m.event_id
              AND msg.occurred_at >= now()-interval '90 days'
            WHERE m.profile_id=:id ORDER BY m.updated_at DESC
        """, id=profile_id)
        timeline = await many(conn, """
            SELECT t.kind,t.topic,t.event_id,t.occurred_at,msg.preview,
                   (SELECT matter_id FROM matter_inputs WHERE message_id=msg.id ORDER BY item_index LIMIT 1) AS matter_id
            FROM member_timeline t LEFT JOIN messages msg ON msg.event_id=t.event_id
              AND msg.occurred_at >= now()-interval '90 days'
            WHERE t.profile_id=:id AND t.occurred_at>=now()-interval '365 days' ORDER BY t.occurred_at DESC LIMIT 100
        """, id=profile_id)
    return {"member": summary(row), "memories": list(map(dict, memories)), "timeline": list(map(dict, timeline))}


@router.post("/members/{profile_id}/memories/{memory_id}")
async def change_memory(profile_id: UUID, memory_id: UUID, data: MemoryChange, request: Request, operator: Writer):
    async def apply(conn):
        profile = await one(conn, "SELECT id FROM member_profiles WHERE id=:id AND deleted_at IS NULL FOR UPDATE", id=profile_id)
        row = await one(conn, "SELECT * FROM member_memories WHERE id=:id AND profile_id=:profile",
                        id=memory_id, profile=profile_id) if profile else None
        if not row:
            raise ApiError(404, "NOT_FOUND", "记忆条目不存在。")
        check_version(row, data.expected_version)
        if data.action == "confirm" and (row["status"] != "candidate" or row["expires_at"] <= datetime.now(UTC)):
            raise ApiError(409, "INVALID_STATE", "只有有效的候选标签可以确认。")
        if data.action == "correct":
            valid = valid_preference(row["topic"], data.value) if row["kind"] == "preference" else bool(data.value)
            if not valid or row["status"] == "deleted":
                raise ApiError(422, "VALIDATION_ERROR", "请为当前条目提供有效的纠正值。")
        deleted = data.action == "delete"
        updated = await one(conn, """
            UPDATE member_memories SET value=:value,status=:status,version=version+1,updated_at=now(),source_at=now(),
                expires_at=NULL,event_id=CASE WHEN :deleted THEN NULL ELSE event_id END
            WHERE id=:id RETURNING id,kind,topic,value,status,version,expires_at
        """, value=None if deleted else data.value if data.action == "correct" else row["value"],
             status="deleted" if deleted else "confirmed", deleted=deleted, id=memory_id)
        await execute(conn, "UPDATE member_profiles SET version=version+1 WHERE id=:id", id=profile_id)
        if deleted:
            await execute(conn, "DELETE FROM api_writes WHERE action=:action AND operation_key<>:key",
                          action=f"member-memory:{profile_id}:{memory_id}", key=request.headers["Idempotency-Key"])
        return dict(updated)
    return await operator_write(request, operator, f"member-memory:{profile_id}:{memory_id}", data.model_dump(), apply)


@router.post("/members/{profile_id}/delete")
async def delete_member(profile_id: UUID, data: Versioned, request: Request, operator: Writer):
    async def apply(conn):
        row = await one(conn, "SELECT version FROM member_profiles WHERE id=:id AND deleted_at IS NULL FOR UPDATE", id=profile_id)
        if not row:
            raise ApiError(404, "NOT_FOUND", "会员档案不存在。")
        check_version(row, data.expected_version)
        await execute(conn, "UPDATE member_profiles SET deleted_at=now(),version=version+1 WHERE id=:id", id=profile_id)
        await execute(conn, "DELETE FROM member_memories WHERE profile_id=:id", id=profile_id)
        await execute(conn, "DELETE FROM member_timeline WHERE profile_id=:id", id=profile_id)
        await execute(conn, "UPDATE participation SET answer=NULL WHERE profile_id=:id", id=profile_id)
        await execute(conn, "DELETE FROM api_writes WHERE action LIKE :action", action=f"member-memory:{profile_id}:%")
        return {"id": profile_id, "version": row["version"] + 1, "status": "deleted"}
    return await operator_write(request, operator, f"delete-member:{profile_id}", data.model_dump(), apply)


class AudienceFilters(Input):
    group_id: str | None = Field(default=None, max_length=255)
    tag: str | None = Field(default=None, max_length=100)
    interest: str | None = Field(default=None, max_length=100)
    recent_days: int | None = Field(default=None, ge=1, le=365)


class AudienceSave(AudienceFilters):
    name: str = Field(min_length=1, max_length=100)
    confirmed_member_ids: list[UUID] = Field(min_length=1, max_length=500)


async def preview(conn, filters):
    rows = await profile_rows(conn)
    signals = await many(conn, """
        SELECT profile_id,value,status FROM member_memories
        WHERE kind='interest' AND status IN ('candidate','confirmed') AND (expires_at IS NULL OR expires_at>now())
    """)
    cutoff = datetime.now(UTC) - timedelta(days=filters.recent_days) if filters.recent_days else None
    items = []
    for row in rows:
        facts = [signal for signal in signals if signal["profile_id"] == row["id"]]
        reasons = []
        if not row["identities"]:
            reasons.append("无当前有效的身份绑定")
        if filters.group_id and filters.group_id not in row["groups"]:
            reasons.append("当前不属于指定群")
        if cutoff and (not row["last_activity"] or row["last_activity"] < cutoff):
            reasons.append("指定时间内无可核实互动")
        if filters.tag and not any(item["status"] == "confirmed" and item["value"] == filters.tag for item in facts):
            reasons.append("缺少匹配的已确认标签")
        if filters.interest and not any(item["value"] == filters.interest for item in facts):
            reasons.append("没有仍有效的候选或已确认意向")
        matched = not reasons
        items.append({"id": row["id"], "identities": row["identities"], "matched": matched,
                      "reasons": reasons or ["符合本次筛选条件"]})
    return {"items": items, "matched_count": sum(item["matched"] for item in items)}


@router.get("/audiences/preview")
async def audience_preview(request: Request, operator: Operator, group_id: str | None = Query(None, max_length=255),
                           tag: str | None = Query(None, max_length=100), interest: str | None = Query(None, max_length=100),
                           recent_days: int | None = Query(None, ge=1, le=365)):
    async with request.app.state.database.connect() as conn:
        return await preview(conn, AudienceFilters(group_id=group_id, tag=tag, interest=interest, recent_days=recent_days))


@router.get("/audiences")
async def audiences(request: Request, operator: Operator):
    async with request.app.state.database.connect() as conn:
        rows = await many(conn, "SELECT * FROM audiences ORDER BY created_at DESC")
    return {"items": list(map(dict, rows))}


@router.post("/audiences")
async def save_audience(data: AudienceSave, request: Request, operator: Writer):
    async def apply(conn):
        filters = AudienceFilters(**data.model_dump(exclude={"name", "confirmed_member_ids"}))
        current = await preview(conn, filters)
        selected = list(dict.fromkeys(data.confirmed_member_ids))
        if not set(selected) <= {row["id"] for row in current["items"] if row["matched"]}:
            raise ApiError(409, "INVALID_STATE", "部分会员不再符合条件，请重新预览。")
        return dict(await one(conn, """
            INSERT INTO audiences (id,name,filters,confirmed_member_ids)
            VALUES (:id,:name,CAST(:filters AS jsonb),:members) RETURNING *
        """, id=uuid4(), name=data.name, filters=json_data(filters.model_dump()), members=selected))
    return await operator_write(request, operator, "save-audience", data.model_dump(mode="json"), apply)
