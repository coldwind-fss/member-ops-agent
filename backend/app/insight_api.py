from datetime import date, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request
from pydantic import Field

from .auth import Operator, Writer
from .http import ApiError
from .ingress import Input
from .member_api import Versioned
from .members import candidate
from .operator_writes import check_version, operator_write
from .store import many, one

router = APIRouter()
ZONE = ZoneInfo("Asia/Shanghai")


class GroupConfig(Input):
    connection_id: str = Field(min_length=1, max_length=255)
    group_id: str = Field(min_length=1, max_length=255)
    enabled: bool


@router.get("/insights/groups")
async def groups(request: Request, operator: Operator):
    async with request.app.state.database.connect() as conn:
        rows = await many(conn, """
            SELECT known.connection_id,known.group_id,COALESCE(g.enabled,false) AS enabled
            FROM (SELECT connection_id,group_id FROM group_memberships
                  UNION SELECT connection_id,group_id FROM analysis_groups) known
            LEFT JOIN analysis_groups g USING (connection_id,group_id)
            ORDER BY connection_id,group_id
        """)
    return {"items": list(map(dict, rows))}


@router.post("/insights/groups")
async def configure_group(data: GroupConfig, request: Request, operator: Writer):
    async def apply(conn):
        return dict(await one(conn, """
            INSERT INTO analysis_groups (connection_id,group_id,enabled) VALUES (:connection_id,:group_id,:enabled)
            ON CONFLICT (connection_id,group_id) DO UPDATE SET enabled=EXCLUDED.enabled,
                updated_at=CASE WHEN analysis_groups.enabled<>EXCLUDED.enabled THEN now() ELSE analysis_groups.updated_at END
            RETURNING *
        """, **data.model_dump()))
    return await operator_write(request, operator, "analysis-group", data.model_dump(), apply)


@router.get("/insights")
async def report(request: Request, operator: Operator, connection_id: str = Query(min_length=1, max_length=255),
                 group_id: str = Query(min_length=1, max_length=255), day: date | None = None):
    today = datetime.now(ZONE).date()
    day = day or today
    if day > today or day < today - timedelta(days=364):
        raise ApiError(422, "VALIDATION_ERROR", "日报可查询最近 365 天。")
    start = datetime.combine(day, datetime.min.time(), tzinfo=ZONE)
    end = start + timedelta(days=1)
    async with request.app.state.database.connect() as conn:
        configured = await one(conn, """
            SELECT enabled,updated_at FROM analysis_groups WHERE connection_id=:connection AND group_id=:group_id
        """, connection=connection_id, group_id=group_id)
        rows = await many(conn, """
            SELECT s.id,s.topic,s.category,s.status,s.version,s.occurred_at,s.matter_id,m.preview,
                   CASE WHEN s.profile_id IS NOT NULL AND p.deleted_at IS NULL THEN s.profile_id END AS profile_id
            FROM group_signals s LEFT JOIN messages m ON m.event_id=s.event_id AND m.occurred_at>=now()-interval '90 days'
            LEFT JOIN member_profiles p ON p.id=s.profile_id
            WHERE s.connection_id=:connection AND s.group_id=:group_id AND s.occurred_at>=:start AND s.occurred_at<:end
            ORDER BY s.occurred_at DESC,s.id
        """, connection=connection_id, group_id=group_id, start=start, end=end)
        progress = await many(conn, """
            SELECT a.title,p.action,count(*) AS count FROM participation p JOIN activities a ON a.id=p.activity_id
            WHERE a.group_id=:group_id AND p.created_at>=:start AND p.created_at<:end GROUP BY a.title,p.action
            ORDER BY a.title,p.action
        """, group_id=group_id, start=start, end=end)
        unresolved = await one(conn, """
            SELECT count(DISTINCT s.matter_id) AS count FROM group_signals s JOIN matters m ON m.id=s.matter_id
            WHERE s.connection_id=:connection AND s.group_id=:group_id AND s.occurred_at>=:start AND s.occurred_at<:end
                AND m.status IN ('waiting_member','human_handling')
        """, connection=connection_id, group_id=group_id, start=start, end=end)
        snapshot = await one(conn, """
            SELECT generated_at,summary FROM daily_reports WHERE connection_id=:connection AND group_id=:group_id AND day=:day
        """, connection=connection_id, group_id=group_id, day=day)
        metrics = await one(conn, """
            SELECT count(*) AS consultations,count(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM messages msg WHERE msg.matter_id=m.id AND msg.kind='assistant'
                        AND msg.delivery_state IN ('sent','delivered'))) AS answered,
                count(h.id) AS handoffs,count(h.id) FILTER (WHERE h.status='pending') AS pending,
                avg(extract(epoch FROM (h.updated_at-h.created_at))) FILTER (WHERE h.status='resolved') AS mean_resolution_seconds
            FROM matters m LEFT JOIN handoffs h ON h.matter_id=m.id
            WHERE m.connection_id=:connection AND m.conversation_kind='group' AND m.conversation_external_id=:group_id
                AND m.created_at>=:start AND m.created_at<:end
        """, connection=connection_id, group_id=group_id, start=start, end=end)
        deliveries = await many(conn, """
            SELECT state,count(*) AS count FROM outreach_records WHERE connection_id=:connection AND group_id=:group_id
                AND scheduled_at>=:start AND scheduled_at<:end GROUP BY state ORDER BY state
        """, connection=connection_id, group_id=group_id, start=start, end=end)
    topics = {}
    for row in rows:
        topics.setdefault(row["topic"], []).append(dict(row))
    return {"day": day, "group_id": group_id, "enabled": bool(configured and configured["enabled"]),
            "coverage_since": configured["updated_at"] if configured and configured["enabled"] else None,
            "unresolved": unresolved["count"], "activity_progress": list(map(dict, progress)),
            "daily_snapshot": dict(snapshot) if snapshot else None,
            "service_metrics": dict(metrics), "delivery_counts": list(map(dict, deliveries)),
            "items": [{"topic": topic, "count": len(signals), "signals": signals} for topic, signals in topics.items()]}


class SignalChange(Versioned):
    action: Literal["confirm", "correct", "close", "candidate"]
    topic: str | None = Field(default=None, min_length=1, max_length=100)


@router.post("/insights/signals/{signal_id}")
async def change_signal(signal_id: UUID, data: SignalChange, request: Request, operator: Writer):
    async def apply(conn):
        signal = await one(conn, "SELECT * FROM group_signals WHERE id=:id FOR UPDATE", id=signal_id)
        if not signal:
            raise ApiError(404, "NOT_FOUND", "复盘信号不存在。")
        check_version(signal, data.expected_version)
        if signal["status"] == "closed":
            raise ApiError(409, "INVALID_STATE", "此信号已关闭。")
        if data.action == "correct" and not data.topic:
            raise ApiError(422, "VALIDATION_ERROR", "请提供纠正后的主题。")
        if data.action == "candidate":
            profile = await one(conn, """
                SELECT p.id FROM member_profiles p JOIN member_bindings b ON p.merchant_member=b.member_id
                JOIN inbox_events e ON e.id=:event AND e.payload->>'sender_external_id'=b.sender_external_id
                WHERE p.id=:profile AND b.connection_id=:connection AND p.deleted_at IS NULL
                    AND e.redacted_at IS NULL FOR SHARE OF b FOR UPDATE OF p
            """, event=signal["event_id"], profile=signal["profile_id"], connection=signal["connection_id"])
            if signal["category"] != "interest" or not profile or datetime.now(ZONE) - signal["occurred_at"] >= timedelta(days=30):
                raise ApiError(409, "INVALID_STATE", "信号缺少有效的个人证据。")
            topic = data.topic or signal["topic"]
            if not await candidate(conn, profile["id"], signal["event_id"], topic, signal["occurred_at"]):
                raise ApiError(409, "INVALID_STATE", "该候选已处理，旧信号不能恢复删除的候选。")
        return dict(await one(conn, """
            UPDATE group_signals SET status=:status,topic=COALESCE(:topic,topic),version=version+1 WHERE id=:id
            RETURNING id,topic,status,version
        """, id=signal_id, status="closed" if data.action == "close" else "confirmed",
             topic=data.topic if data.action == "correct" else None))
    return await operator_write(request, operator, f"group-signal:{signal_id}", data.model_dump(), apply)
