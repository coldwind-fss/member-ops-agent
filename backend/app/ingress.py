import os
from datetime import UTC, datetime
from hmac import compare_digest
from ipaddress import ip_address
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_serializer

from .commerce import safe_preview
from .http import ApiError
from .queue import enqueue
from .store import execute, json_data, one

router = APIRouter()


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Conversation(Input):
    kind: Literal["group", "private"]
    external_id: str = Field(min_length=1, max_length=255)


class Message(Input):
    external_id: str = Field(min_length=1, max_length=255)
    text: str = Field(max_length=12000)
    mentions_bot: bool
    reply_to_external_id: str | None = Field(min_length=1, max_length=255)
    image_present: bool = False


class Inbound(Input):
    connection_id: str = Field(min_length=1, max_length=255)
    external_event_id: str = Field(min_length=1, max_length=255)
    event_type: Literal["message.received"]
    occurred_at: AwareDatetime
    conversation: Conversation
    sender_external_id: str = Field(min_length=1, max_length=255)
    message: Message

    @field_serializer("occurred_at")
    def serialize_time(self, value: datetime):
        return value.astimezone(UTC).isoformat()


def lane_for(data):
    return json_data([data["connection_id"], data["conversation"]["kind"],
                      data["conversation"]["external_id"], data["sender_external_id"]])


def require_fixture(request: Request):
    configured = os.getenv("DEV_EVENT_TOKEN")
    if not configured or not request.client or not ip_address(request.client.host).is_loopback:
        raise ApiError(404, "NOT_FOUND", "测试事件入口未启用。")
    supplied = request.headers.get("X-Dev-Token", "")
    if not compare_digest(supplied.encode(), configured.encode()):
        raise ApiError(401, "UNAUTHENTICATED", "测试事件凭据无效。")


@router.post("/dev/inbound-events", status_code=202, dependencies=[Depends(require_fixture)])
async def ingest(event: Inbound, request: Request):
    if event.connection_id != "fixture-channel":
        raise ApiError(403, "FORBIDDEN", "此测试连接未配置。")
    payload = event.model_dump(mode="json")
    if not event.message.image_present:
        payload["message"].pop("image_present")
    data = json_data(payload)
    async with request.app.state.database.begin() as conn:
        scope = event.conversation.external_id
        # 先按参与者串行接收，为 Worker 建立可靠的同事项处理顺序。
        lane = lane_for(payload)
        await execute(conn, "SELECT pg_advisory_xact_lock(hashtextextended(:lane, 0))", lane=lane)
        existing = await one(conn, """
            SELECT e.id, e.redacted_at IS NOT NULL OR e.payload=CAST(:payload AS jsonb) AS same FROM inbox_events e
            JOIN messages m ON m.event_id=e.id AND m.kind='inbound'
            WHERE e.connection_id=:connection
              AND (e.external_event_id=:external OR (m.conversation_kind=:kind AND m.conversation_external_id=:scope AND m.external_id=:message))
            ORDER BY (e.external_event_id=:external) DESC LIMIT 1
        """, payload=data, connection=event.connection_id, external=event.external_event_id,
             kind=event.conversation.kind, scope=scope, message=event.message.external_id)
        if existing:
            if not existing["same"]:
                raise ApiError(409, "EVENT_CONFLICT", "事件或消息编号已被不同内容使用。")
            return {"event_id": existing["id"], "duplicate": True}
        event_id = uuid4()
        new = await one(conn, """
            INSERT INTO inbox_events (id, connection_id, external_event_id, payload)
            VALUES (:id, :connection, :external, CAST(:payload AS jsonb))
            ON CONFLICT (connection_id, external_event_id) DO NOTHING RETURNING id
        """, id=event_id, connection=event.connection_id, external=event.external_event_id, payload=data)
        if not new:
            duplicate = await one(conn, """
                SELECT id, redacted_at IS NOT NULL OR payload=CAST(:payload AS jsonb) AS same FROM inbox_events
                WHERE connection_id=:connection AND external_event_id=:external
            """, connection=event.connection_id, external=event.external_event_id, payload=data)
            if not duplicate["same"]:
                raise ApiError(409, "EVENT_CONFLICT", "事件编号已被不同内容使用。")
            return {"event_id": duplicate["id"], "duplicate": True}
        message = await one(conn, """
            INSERT INTO messages (id,event_id,kind,connection_id,conversation_kind,conversation_external_id,external_id,preview,occurred_at)
            VALUES (:id,:id,'inbound',:connection,:kind,:scope,:external,:preview,:at)
            ON CONFLICT (connection_id, conversation_kind, conversation_external_id, external_id) DO NOTHING RETURNING id
        """, id=event_id, connection=event.connection_id, kind=event.conversation.kind, scope=scope, external=event.message.external_id,
             preview=safe_preview(event.message.text, event.conversation.kind), at=event.occurred_at)
        if not message:
            raise ApiError(409, "EVENT_CONFLICT", "消息编号已被其他事件使用。")
        await enqueue(conn, "inbound", f"inbound:{event_id}", lane, {"event_id": str(event_id)})
    return {"event_id": event_id, "duplicate": False}
