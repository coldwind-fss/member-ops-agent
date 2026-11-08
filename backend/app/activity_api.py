from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request

from .activities import ActivityConfig, effective_status
from .auth import Operator, Writer
from .http import ApiError
from .member_api import Versioned
from .operator_writes import check_version, operator_write
from .store import execute, json_data, many, one

router = APIRouter()


def view(row):
    return {**dict(row), "effective_status": effective_status(row, datetime.now(UTC))}


async def save_snapshot(conn, row):
    await execute(conn, """
        INSERT INTO activity_versions (activity_id,version,snapshot)
        VALUES (:id,:version,CAST(:snapshot AS jsonb))
    """, id=row["id"], version=row["version"], snapshot=json_data(view(row)))


@router.get("/activities")
async def list_activities(request: Request, operator: Operator):
    async with request.app.state.database.connect() as conn:
        rows = await many(conn, """
            SELECT a.*,(SELECT count(*) FROM participation p WHERE p.activity_id=a.id AND p.action='register') AS registrations
            FROM activities a ORDER BY created_at DESC
        """)
    return {"items": [view(row) for row in rows]}


@router.post("/activities")
async def create_activity(data: ActivityConfig, request: Request, operator: Writer):
    async def apply(conn):
        row = await one(conn, """
            INSERT INTO activities (id,title,group_id,starts_at,ends_at,status,config)
            VALUES (:id,:title,:group,:start,:end,'draft',CAST(:config AS jsonb))
            ON CONFLICT (title) DO NOTHING RETURNING *
        """, id=uuid4(), title=data.title, group=data.group_id, start=data.starts_at, end=data.ends_at,
             config=json_data(data.model_dump(mode="json")))
        if not row:
            raise ApiError(409, "INVALID_STATE", "已有同名活动。")
        await save_snapshot(conn, row)
        return view(row)
    return await operator_write(request, operator, "create-activity", data.model_dump(mode="json"), apply)


@router.get("/activities/{activity_id}")
async def get_activity(activity_id: UUID, request: Request, operator: Operator):
    async with request.app.state.database.connect() as conn:
        row = await one(conn, "SELECT * FROM activities WHERE id=:id", id=activity_id)
        if not row:
            raise ApiError(404, "NOT_FOUND", "活动不存在。")
        history = await many(conn, "SELECT version,snapshot,created_at FROM activity_versions WHERE activity_id=:id ORDER BY version DESC",
                             id=activity_id)
        records = await many(conn, """
            SELECT p.id,p.activity_version,CASE WHEN m.deleted_at IS NULL THEN p.profile_id END AS profile_id,
                   p.action,p.period,p.created_at,p.updated_at
            FROM participation p JOIN member_profiles m ON m.id=p.profile_id
            WHERE p.activity_id=:id ORDER BY p.updated_at DESC
        """, id=activity_id)
    return {"activity": view(row), "versions": list(map(dict, history)), "participation": list(map(dict, records)),
            "progress": {action: sum(record["action"] == action for record in records)
                         for action in ("register", "checkin", "survey")}}


class ActivityChange(Versioned):
    action: Literal["edit", "publish", "pause", "resume", "cancel"]
    config: ActivityConfig | None = None


@router.post("/activities/{activity_id}")
async def change_activity(activity_id: UUID, data: ActivityChange, request: Request, operator: Writer):
    async def apply(conn):
        row = await one(conn, "SELECT * FROM activities WHERE id=:id FOR UPDATE", id=activity_id)
        if not row:
            raise ApiError(404, "NOT_FOUND", "活动不存在。")
        check_version(row, data.expected_version)
        now = datetime.now(UTC)
        status = effective_status(row, now)
        allowed = {"draft": {"edit", "publish", "cancel"}, "scheduled": {"edit", "pause", "cancel"},
                   "open": {"edit", "pause", "cancel"}, "paused": {"edit", "resume", "cancel"}}
        if data.action not in allowed.get(status, set()):
            raise ApiError(409, "INVALID_STATE", "活动在当前阶段无法执行该操作。")
        config = data.config if data.action == "edit" else ActivityConfig.model_validate(row["config"])
        if config is None:
            raise ApiError(422, "VALIDATION_ERROR", "编辑需要提供完整活动规则。")
        if data.action == "edit" and row["status"] != "draft" and row["starts_at"] <= now:
            if (config.starts_at != row["starts_at"] or config.allow_makeup != row["config"]["allow_makeup"]
                    or config.allow_edit != row["config"]["allow_edit"]):
                raise ApiError(409, "INVALID_STATE", "活动开始后不能更改开始时间、补打卡或问卷修改许可。")
        if data.action in {"publish", "resume"} and config.ends_at <= now:
            raise ApiError(409, "INVALID_STATE", "活动已结束。")
        if await one(conn, "SELECT id FROM activities WHERE title=:title AND id<>:id", title=config.title, id=activity_id):
            raise ApiError(409, "INVALID_STATE", "已有同名活动。")
        target = {"publish": "published", "pause": "paused", "resume": "published", "cancel": "cancelled"}.get(data.action, row["status"])
        updated = await one(conn, """
            UPDATE activities SET status=:status,version=version+1,title=:title,group_id=:group,
                starts_at=:start,ends_at=:end,config=CAST(:config AS jsonb) WHERE id=:id RETURNING *
        """, id=activity_id, status=target, title=config.title, group=config.group_id, start=config.starts_at,
             end=config.ends_at, config=json_data(config.model_dump(mode="json")))
        await save_snapshot(conn, updated)
        return view(updated)
    return await operator_write(request, operator, f"change-activity:{activity_id}", data.model_dump(mode="json"), apply)
