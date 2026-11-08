from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from pydantic import AwareDatetime, Field, model_validator

from .auth import Operator, Writer
from .business_events import confirmed_order
from .http import ApiError
from .ingress import Input
from .member_api import Versioned
from .operator_writes import check_version, operator_write
from .operations import Limit, page
from .outreach import SUCCESS, audience_candidates, profile_binding, record_permission, schedule, update_record
from .store import execute, json_data, many, one
from .subscriptions import cancel_subscription
from .working_hours import next_work_time

router = APIRouter(prefix="/outreach")
ExternalID = Annotated[str, Field(min_length=1, max_length=255)]
RecordState = Literal["pending", "sending", "accepted", "sent", "delivered", "manual", "unknown",
                      "failed", "cancelled", "expired"]


class GroupSettings(Input):
    connection_id: ExternalID
    group_id: ExternalID
    reply_enabled: bool
    analysis_enabled: bool
    outreach_enabled: bool
    welcome_text: str | None = Field(default=None, min_length=1, max_length=2000)
    care_text: str | None = Field(default=None, min_length=1, max_length=2000)


@router.get("/groups")
async def groups(request: Request, operator: Operator):
    async with request.app.state.database.connect() as conn:
        rows = await many(conn, """
            SELECT g.connection_id,g.group_id,COALESCE(o.reply_enabled,true) AS reply_enabled,
                COALESCE(o.outreach_enabled,false) AS outreach_enabled,COALESCE(a.enabled,false) AS analysis_enabled,
                o.welcome_text,o.care_text
            FROM (SELECT connection_id,group_id FROM group_memberships UNION SELECT connection_id,group_id FROM outreach_groups
                UNION SELECT connection_id,group_id FROM analysis_groups) g
            LEFT JOIN outreach_groups o USING (connection_id,group_id)
            LEFT JOIN analysis_groups a USING (connection_id,group_id)
            ORDER BY g.group_id
        """)
    return {"items": list(map(dict, rows))}


@router.post("/groups")
async def save_group(data: GroupSettings, request: Request, operator: Writer):
    async def apply(conn):
        row = await one(conn, """
            INSERT INTO outreach_groups (connection_id,group_id,reply_enabled,outreach_enabled,welcome_text,care_text,reply_since)
            VALUES (:connection_id,:group_id,:reply_enabled,:outreach_enabled,:welcome_text,:care_text,
                CASE WHEN :reply_enabled THEN NULL ELSE now() END)
            ON CONFLICT (connection_id,group_id) DO UPDATE SET reply_enabled=EXCLUDED.reply_enabled,
                outreach_enabled=EXCLUDED.outreach_enabled,welcome_text=EXCLUDED.welcome_text,care_text=EXCLUDED.care_text,
                outreach_since=CASE WHEN outreach_groups.outreach_enabled<>EXCLUDED.outreach_enabled THEN now()
                    ELSE outreach_groups.outreach_since END,
                reply_since=CASE WHEN outreach_groups.reply_enabled<>EXCLUDED.reply_enabled THEN now()
                    ELSE outreach_groups.reply_since END RETURNING *
        """, **data.model_dump(exclude={"analysis_enabled"}))
        await execute(conn, """
            INSERT INTO analysis_groups (connection_id,group_id,enabled) VALUES (:connection,:group_id,:enabled)
            ON CONFLICT (connection_id,group_id) DO UPDATE SET enabled=EXCLUDED.enabled,
                updated_at=CASE WHEN analysis_groups.enabled<>EXCLUDED.enabled THEN now() ELSE analysis_groups.updated_at END
        """, connection=data.connection_id, group_id=data.group_id, enabled=data.analysis_enabled)
        return {**dict(row), "analysis_enabled": data.analysis_enabled}
    return await operator_write(request, operator, "outreach-group", data.model_dump(), apply)


class Plan(Input):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=4000)
    connection_id: ExternalID
    group_id: ExternalID
    mode: Literal["group", "personal"]
    scheduled_at: AwareDatetime
    expires_at: AwareDatetime
    audience_id: UUID | None = None
    activity_id: UUID | None = None
    daily_limit: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def coherent(self):
        if self.scheduled_at >= self.expires_at:
            raise ValueError("过期时间必须晚于计划时间")
        if (self.mode == "personal") != (self.audience_id is not None):
            raise ValueError("个人触达需选择已确认人群")
        if self.daily_limit != 1 and self.activity_id is None:
            raise ValueError("仅关联活动的计划可调整每日频率")
        return self


async def snapshot(conn, row):
    await execute(conn, """
        INSERT INTO outreach_plan_versions (plan_id,version,snapshot) VALUES (:id,:version,CAST(:snapshot AS jsonb))
    """, id=row["id"], version=row["version"], snapshot=json_data(dict(row)))


async def validate_links(conn, config):
    if not await one(conn, "SELECT 1 FROM outreach_groups WHERE connection_id=:c AND group_id=:g",
                     c=config.connection_id, g=config.group_id):
        raise ApiError(422, "VALIDATION_ERROR", "请先在触达设置登记目标群。")
    if config.audience_id and not await one(conn, "SELECT 1 FROM audiences WHERE id=:id", id=config.audience_id):
        raise ApiError(404, "NOT_FOUND", "人群不存在。")
    if config.activity_id:
        activity = await one(conn, "SELECT group_id FROM activities WHERE id=:id", id=config.activity_id)
        if not activity or activity["group_id"] not in {None, config.group_id}:
            raise ApiError(422, "VALIDATION_ERROR", "活动不存在或不适用于目标群。")


@router.get("/plans")
async def plans(request: Request, operator: Operator):
    async with request.app.state.database.connect() as conn:
        rows = await many(conn, "SELECT * FROM outreach_plans ORDER BY created_at DESC")
    return {"items": list(map(dict, rows))}


@router.post("/plans")
async def create_plan(data: Plan, request: Request, operator: Writer):
    async def apply(conn):
        await validate_links(conn, data)
        row = await one(conn, """
            INSERT INTO outreach_plans (id,title,config) VALUES (:id,:title,CAST(:config AS jsonb)) RETURNING *
        """, id=uuid4(), title=data.title, config=data.model_dump_json())
        await snapshot(conn, row)
        return dict(row)
    return await operator_write(request, operator, "create-plan", data.model_dump(mode="json"), apply)


@router.get("/plans/{plan_id}/preview")
async def plan_preview(plan_id: UUID, request: Request, operator: Operator):
    async with request.app.state.database.connect() as conn:
        row = await one(conn, "SELECT config FROM outreach_plans WHERE id=:id", id=plan_id)
        if not row:
            raise ApiError(404, "NOT_FOUND", "运营计划不存在。")
        config = row["config"]
        items = await audience_candidates(conn, config)
    return {"mode": config["mode"], "group_id": config["group_id"], "items": items,
            "matched_count": sum(item["matched"] for item in items)}


class PlanChange(Versioned):
    action: Literal["edit", "approve", "cancel"]
    config: Plan | None = None
    member_ids: list[UUID] = Field(default_factory=list, max_length=500)


@router.post("/plans/{plan_id}")
async def change_plan(plan_id: UUID, data: PlanChange, request: Request, operator: Writer):
    async def apply(conn):
        row = await one(conn, "SELECT * FROM outreach_plans WHERE id=:id FOR UPDATE", id=plan_id)
        if not row:
            raise ApiError(404, "NOT_FOUND", "运营计划不存在。")
        check_version(row, data.expected_version)
        if data.action == "approve" and row["status"] != "draft":
            raise ApiError(409, "INVALID_STATE", "只有待确认的草稿可以批准。")
        if data.action == "cancel" and row["status"] == "cancelled":
            raise ApiError(409, "INVALID_STATE", "计划已取消。")
        if data.action == "edit" and data.config is None:
            raise ApiError(422, "VALIDATION_ERROR", "编辑时请提供完整计划配置。")
        config = data.config if data.action == "edit" else Plan.model_validate(row["config"])
        await validate_links(conn, config)
        selected = list(dict.fromkeys(data.member_ids))
        targets = []
        now = datetime.now(UTC)
        if data.action == "approve":
            if config.scheduled_at < now - timedelta(minutes=1) or config.expires_at <= now:
                raise ApiError(409, "INVALID_STATE", "计划时间已过，请调整后重新确认。")
            if config.mode == "personal":
                eligible = {item["id"] for item in await audience_candidates(conn, config.model_dump(mode="json")) if item["matched"]}
                if not selected or not set(selected) <= eligible:
                    raise ApiError(409, "INVALID_STATE", "批准名单含已失效成员，请重新预览。")
                for profile in selected:
                    binding = await profile_binding(conn, profile, config.connection_id, config.group_id)
                    if binding is None:
                        raise ApiError(409, "INVALID_STATE", "批准名单的渠道身份已变化。")
                    targets.append((profile, binding))
            elif selected:
                raise ApiError(422, "VALIDATION_ERROR", "群公告不接受个人名单。")
            else:
                targets.append((None, None))
        state = {"edit": "draft", "approve": "approved", "cancel": "cancelled"}[data.action]
        version = row["version"] + 1
        updated = await one(conn, """
            UPDATE outreach_plans SET title=:title,config=CAST(:config AS jsonb),status=:state,
                approved_members=:members,version=:version,updated_at=now() WHERE id=:id RETURNING *
        """, id=plan_id, title=config.title, config=config.model_dump_json(), state=state,
             version=version, members=selected if state == "approved" else [])
        await snapshot(conn, updated)
        for profile, binding in targets:
            await schedule(conn, key=f"plan:{plan_id}:{version}:{profile or 'group'}", kind="plan",
                           connection=config.connection_id, group=config.group_id, profile=profile, binding=binding,
                           plan=plan_id, plan_version=version, body=config.body, at=config.scheduled_at,
                           expires=min(config.expires_at, config.scheduled_at + timedelta(minutes=5)))
        return dict(updated)
    return await operator_write(request, operator, f"change-plan:{plan_id}", data.model_dump(mode="json"), apply)


class DraftRequest(Input):
    activity_id: UUID | None = None
    knowledge_id: UUID | None = None
    topic: str = Field(min_length=1, max_length=160)


@router.post("/draft")
async def suggest_draft(data: DraftRequest, request: Request, operator: Writer):
    async def apply(conn):
        sections = [data.topic]
        sources = []
        if data.activity_id:
            activity = await one(conn, """
                SELECT * FROM activities WHERE id=:id AND status IN ('published','draft') AND ends_at>now()
            """, id=data.activity_id)
            if not activity:
                raise ApiError(404, "NOT_FOUND", "没有可用于撰写的活动。")
            sections += [activity["title"], activity["config"]["rule"]]
            sources.append({"kind": "activity", "id": activity["id"], "version": activity["version"]})
        if data.knowledge_id:
            row = await one(conn, """
                SELECT * FROM knowledge WHERE id=:id AND valid_from<=now() AND (valid_until IS NULL OR valid_until>now())
                ORDER BY version DESC LIMIT 1
            """, id=data.knowledge_id)
            if not row:
                raise ApiError(404, "NOT_FOUND", "没有可用的知识版本。")
            sections.append(row["body"])
            sources.append({"kind": "knowledge", "id": row["id"], "version": row["version"]})
        return {"body": "\n".join(sections)[:4000], "sources": sources}
    return await operator_write(request, operator, "suggest-draft", data.model_dump(mode="json"), apply)


@router.get("/subscriptions")
async def subscriptions(request: Request, operator: Operator):
    async with request.app.state.database.connect() as conn:
        rows = await many(conn, """
            SELECT s.id,s.profile_id,s.product_name,s.color,s.size,s.version,s.group_id,s.expires_at,s.created_at,
              CASE WHEN s.status='active' AND s.expires_at<=now() THEN 'expired'
                   WHEN s.status='active' AND (b.version<>s.binding_version OR b.member_id IS NULL OR p.deleted_at IS NOT NULL)
                   THEN 'invalid' ELSE s.status END AS status
            FROM subscriptions s JOIN member_bindings b ON b.id=s.binding_id JOIN member_profiles p ON p.id=s.profile_id
            ORDER BY s.created_at DESC
        """)
    return {"items": list(map(dict, rows))}


@router.post("/subscriptions/{sub_id}/cancel")
async def cancel(sub_id: UUID, data: Versioned, request: Request, operator: Writer):
    async def apply(conn):
        row = await one(conn, "SELECT version,status FROM subscriptions WHERE id=:id FOR UPDATE", id=sub_id)
        if not row:
            raise ApiError(404, "NOT_FOUND", "订阅不存在。")
        check_version(row, data.expected_version)
        result = await cancel_subscription(conn, sub_id)
        if result is None:
            raise ApiError(409, "INVALID_STATE", "订阅已经结束。")
        return {"id": sub_id, "version": result["version"], "status": result["status"]}
    return await operator_write(request, operator, f"cancel-subscription:{sub_id}", data.model_dump(), apply)


@router.get("/suggestions")
async def suggestions(request: Request, operator: Operator):
    async with request.app.state.database.connect() as conn:
        rows = await many(conn, """
            SELECT m.id,m.profile_id,m.topic,m.value,m.source_at,m.expires_at,m.event_id,m.status AS memory_status,
                COALESCE(s.version,1) AS version,
                CASE WHEN (m.status='deleted' OR m.expires_at<=now() OR p.deleted_at IS NOT NULL)
                          AND COALESCE(s.status,'open')<>'closed' THEN 'expired' ELSE COALESCE(s.status,'open') END AS status,
                s.result,msg.preview,
                (SELECT i.matter_id FROM matter_inputs i WHERE i.message_id=msg.id ORDER BY item_index LIMIT 1) AS matter_id
            FROM member_memories m JOIN member_profiles p ON p.id=m.profile_id
            LEFT JOIN outreach_suggestions s ON s.memory_id=m.id
            LEFT JOIN messages msg ON msg.event_id=m.event_id AND msg.occurred_at>=now()-interval '90 days'
            WHERE m.kind='interest' AND (m.status<>'deleted' OR s.memory_id IS NOT NULL)
            ORDER BY m.source_at DESC
        """)
    return {"items": list(map(dict, rows))}


class SuggestionChange(Versioned):
    action: Literal["approve", "close"]
    connection_id: ExternalID = "fixture-channel"
    group_id: ExternalID | None = None
    body: str | None = Field(default=None, min_length=1, max_length=2000)
    reason: Literal["resolved", "declined", "purchased", "expired"] | None = None


@router.post("/suggestions/{memory_id}")
async def change_suggestion(memory_id: UUID, data: SuggestionChange, request: Request, operator: Writer):
    async def apply(conn):
        memory = await one(conn, "SELECT * FROM member_memories WHERE id=:id AND kind='interest' FOR UPDATE", id=memory_id)
        if not memory:
            raise ApiError(404, "NOT_FOUND", "跟进线索不存在。")
        row = await one(conn, "SELECT * FROM outreach_suggestions WHERE memory_id=:id", id=memory_id)
        check_version(row or {"version": 1}, data.expected_version)
        if row and (row["status"] == "closed" or data.action == "approve"):
            raise ApiError(409, "INVALID_STATE", "当前线索无法执行该操作。")
        now = datetime.now(UTC)
        if data.action == "approve":
            if not data.body or not data.group_id:
                raise ApiError(422, "VALIDATION_ERROR", "请填写确认的跟进文案和群。")
            if memory["status"] == "deleted" or (memory["expires_at"] and memory["expires_at"] <= now):
                raise ApiError(409, "INVALID_STATE", "线索已失效。")
            binding = await profile_binding(conn, memory["profile_id"], data.connection_id, data.group_id)
            if not binding:
                raise ApiError(409, "INVALID_STATE", "缺少可核验的会员身份。")
            await schedule(conn, key=f"followup:{memory_id}", kind="followup", connection=data.connection_id,
                           group=data.group_id, profile=memory["profile_id"], binding=binding, memory=memory_id,
                           body=data.body, at=now, expires=min(memory["expires_at"] or now + timedelta(days=7), now + timedelta(days=7)))
        elif not data.reason:
            raise ApiError(422, "VALIDATION_ERROR", "请选择跟进结束原因。")
        saved = await one(conn, """
            INSERT INTO outreach_suggestions (memory_id,version,status,result) VALUES (:id,:version,:state,:result)
            ON CONFLICT (memory_id) DO UPDATE SET version=EXCLUDED.version,status=EXCLUDED.status,
                result=EXCLUDED.result,updated_at=now() RETURNING *
        """, id=memory_id, version=data.expected_version + 1,
             state="approved" if data.action == "approve" else "closed", result=data.reason)
        return dict(saved)
    return await operator_write(request, operator, f"suggestion:{memory_id}", data.model_dump(mode="json"), apply)


class CareRequest(Input):
    connection_id: ExternalID
    group_id: ExternalID
    profile_id: UUID
    order_id: ExternalID
    body: str = Field(min_length=1, max_length=4000)


@router.post("/care")
async def manual_care(data: CareRequest, request: Request, operator: Writer):
    engine = request.app.state.database
    async with engine.connect() as conn:
        binding = await profile_binding(conn, data.profile_id, data.connection_id, data.group_id)
    order = await confirmed_order(data.connection_id, binding["member_id"], data.order_id) if binding else None
    if not order:
        raise ApiError(409, "INVALID_STATE", "暂不能核实本人订单的签收状态。")

    async def apply(conn):
        now = datetime.now(UTC)
        key = f"care:{data.connection_id}:{data.order_id}"
        row = await schedule(conn, key=key, kind="care", connection=data.connection_id, group=data.group_id,
                             profile=data.profile_id, binding=binding, order=data.order_id, body=data.body,
                             at=next_work_time(now) or now, expires=now + timedelta(days=7))
        if row is None:
            row = await one(conn, "SELECT * FROM outreach_records WHERE operation_key=:key", key=key)
        return {name: row[name] for name in ("id", "state", "version", "scheduled_at")}
    return await operator_write(request, operator, "manual-care", data.model_dump(mode="json"), apply)


RECORD_FIELDS = """
id,operation_key,kind,connection_id,group_id,sender_external_id,profile_id,plan_id,plan_version,
subscription_id,memory_id,event_id,state,reason,version,attempts,provider_ref,evidence,scheduled_at,expires_at,created_at,updated_at
"""


@router.get("/records")
async def records(request: Request, operator: Operator, limit: Limit = 20, cursor: str | None = None,
                  state: RecordState | None = None):
    source = """(SELECT r.id,r.operation_key,r.kind,r.connection_id,r.group_id,r.sender_external_id,r.profile_id,
        r.plan_id,r.plan_version,r.subscription_id,r.memory_id,r.event_id,
        CASE WHEN r.state IN ('pending','manual') AND s.status='cancelled' THEN 'cancelled' ELSE r.state END AS state,
        CASE WHEN r.state IN ('pending','manual') AND s.status='cancelled' THEN 'SUBSCRIPTION_ENDED' ELSE r.reason END AS reason,
        r.version,r.attempts,r.provider_ref,r.evidence,r.scheduled_at,r.expires_at,r.created_at,r.updated_at
        FROM outreach_records r LEFT JOIN subscriptions s ON s.id=r.subscription_id) AS records"""
    return await page(request.app.state.database, source, RECORD_FIELDS, {"state": state} if state else {}, limit, cursor)


class Resolution(Versioned):
    state: Literal["accepted", "sent", "delivered", "failed", "cancelled"]
    evidence: str = Field(min_length=4, max_length=1000)
    provider_ref: ExternalID | None = None


@router.post("/records/{record_id}/resolve")
async def resolve(record_id: UUID, data: Resolution, request: Request, operator: Writer):
    async def apply(conn):
        item = await one(conn, "SELECT * FROM outreach_records WHERE id=:id FOR UPDATE", id=record_id)
        if not item:
            raise ApiError(404, "NOT_FOUND", "触达记录不存在。")
        check_version(item, data.expected_version)
        if item["state"] not in {"manual", "unknown", "accepted"}:
            raise ApiError(409, "INVALID_STATE", "当前记录无需人工核实。")
        if item["state"] == "manual" and data.state in SUCCESS | {"accepted"}:
            state, reason, _ = await record_permission(conn, item, datetime.now(UTC))
            if state not in {"send", "manual"}:
                raise ApiError(409, "INVALID_STATE", f"当前条件已变化：{reason}")
        if item["state"] != "manual" and data.state in SUCCESS | {"accepted"} and not (data.provider_ref or item["provider_ref"]):
            raise ApiError(422, "VALIDATION_ERROR", "请填写可核对的供应商回执编号。")
        row = await update_record(conn, item, data.state, reference=data.provider_ref, evidence=data.evidence)
        return {name: row[name] for name in ("id", "state", "version", "provider_ref", "evidence")}
    return await operator_write(request, operator, f"resolve-outreach:{record_id}", data.model_dump(), apply)
