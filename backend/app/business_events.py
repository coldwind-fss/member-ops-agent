from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import AwareDatetime, Field

from .http import ApiError
from .ingress import Input, require_fixture
from .merchant import merchant_for_connection
from .outreach import SUCCESS, group_settings, schedule, update_record
from .queue import enqueue, fence, finish
from .store import execute, json_data, many, one
from .working_hours import next_work_time

router = APIRouter()
ExternalID = Annotated[str, Field(min_length=1, max_length=255)]


class Event(Input):
    connection_id: Literal["fixture-channel"]
    external_event_id: ExternalID
    occurred_at: AwareDatetime
    historical: bool = False


class MembershipEvent(Event):
    event_type: Literal["member.joined", "member.left"]
    group_id: ExternalID
    sender_external_id: ExternalID


class StockEvent(Event):
    event_type: Literal["stock.changed"]
    product_id: ExternalID
    color: ExternalID
    size: ExternalID
    quantity: int = Field(ge=0)


class OrderEvent(MembershipEvent):
    event_type: Literal["order.signed", "order.returned", "order.cancelled", "order.after_sale"]
    order_id: ExternalID


class ReceiptEvent(Event):
    event_type: Literal["delivery.receipt"]
    operation_key: ExternalID
    state: Literal["accepted", "sent", "delivered", "failed"]
    provider_ref: ExternalID


@router.post("/dev/business-events", status_code=202, dependencies=[Depends(require_fixture)])
async def fixture_event(data: Annotated[MembershipEvent | StockEvent | OrderEvent | ReceiptEvent, Field(discriminator="event_type")],
                        request: Request):
    payload = data.model_dump(mode="json")
    key = {"connection": data.connection_id, "external": data.external_event_id, "payload": json_data(payload)}
    async with request.app.state.database.begin() as conn:
        saved = await one(conn, """
            INSERT INTO business_events (id,connection_id,external_event_id,event_type,payload)
            VALUES (:id,:connection,:external,:kind,CAST(:payload AS jsonb))
            ON CONFLICT DO NOTHING RETURNING id
        """, id=uuid4(), kind=data.event_type, **key)
        if not saved:
            previous = await one(conn, """
                SELECT id,payload=CAST(:payload AS jsonb) AS same FROM business_events
                WHERE connection_id=:connection AND external_event_id=:external
            """, **key)
            if not previous["same"]:
                raise ApiError(409, "EVENT_CONFLICT", "事件编号已被不同内容使用。")
            return {"event_id": previous["id"], "duplicate": True}
        await enqueue(conn, "business_event", f"business:{saved['id']}", f"business:{data.connection_id}",
                      {"event_id": str(saved["id"])})
        return {"event_id": saved["id"], "duplicate": False}


async def latest_membership(conn, event, at):
    return await one(conn, """
        INSERT INTO group_memberships (connection_id,group_id,sender_external_id,active,source_at)
        VALUES (:connection,:group_id,:sender,:active,:at)
        ON CONFLICT (connection_id,group_id,sender_external_id)
        DO UPDATE SET active=EXCLUDED.active,updated_at=now(),source_at=EXCLUDED.source_at
        WHERE group_memberships.source_at IS NULL OR group_memberships.source_at<EXCLUDED.source_at
        RETURNING active
    """, connection=event["connection_id"], group_id=event["group_id"], sender=event["sender_external_id"],
         active=event["event_type"] == "member.joined", at=at)


async def membership(conn, event, event_id, at, now):
    row = await latest_membership(conn, event, at)
    if not row or not row["active"]:
        return
    connection, group = event["connection_id"], event["group_id"]
    settings = await group_settings(conn, connection, group)
    expires = at + timedelta(hours=24)
    if settings["outreach_enabled"] and settings["welcome_text"] and at >= settings["outreach_since"] and now < expires:
        await schedule(conn, key=f"welcome:{event_id}", kind="welcome", connection=connection, group=group,
                       sender=event["sender_external_id"], body=settings["welcome_text"], at=now, expires=expires, event=event_id)


async def stock_changed(conn, event, event_id, at, now):
    params = {key: event[key] for key in ("connection_id", "product_id", "color", "size")}
    previous = await one(conn, """
        SELECT quantity FROM outreach_stock WHERE connection_id=:connection_id AND product_id=:product_id
        AND color=:color AND size=:size
    """, **params)
    updated = await one(conn, """
        INSERT INTO outreach_stock VALUES (:connection_id,:product_id,:color,:size,:quantity,:at)
        ON CONFLICT (connection_id,product_id,color,size) DO UPDATE SET quantity=EXCLUDED.quantity,occurred_at=EXCLUDED.occurred_at
        WHERE outreach_stock.occurred_at<EXCLUDED.occurred_at RETURNING quantity
    """, quantity=event["quantity"], at=at, **params)
    if not updated or updated["quantity"] <= 0 or (previous and previous["quantity"] > 0):
        return
    rows = await many(conn, """
        SELECT s.*,b.sender_external_id FROM subscriptions s JOIN member_bindings b ON b.id=s.binding_id
        WHERE s.connection_id=:connection_id AND s.product_id=:product_id AND s.color=:color AND s.size=:size
            AND s.status='active' AND s.expires_at>now() AND s.created_at<=:at
            AND NOT EXISTS (SELECT 1 FROM outreach_records r WHERE r.subscription_id=s.id
                AND r.state IN ('pending','sending','manual','unknown','accepted'))
    """, at=at, **params)
    for row in rows:
        await schedule(conn, key=f"restock:{row['id']}:{event_id}", kind="restock", connection=row["connection_id"],
                       group=row["group_id"], profile=row["profile_id"],
                       binding={"id": row["binding_id"], "version": row["binding_version"], "sender_external_id": row["sender_external_id"]},
                       body=f"{row['product_name']} {row['color']} {row['size']} 已有库存，请通过品牌官方入口查看。",
                       at=now, expires=row["expires_at"], subscription=row["id"], event=event_id)


async def order_binding(conn, connection, sender):
    return await one(conn, """
        SELECT b.id,b.version,b.sender_external_id,b.member_id,p.id AS profile_id
        FROM member_bindings b JOIN member_profiles p ON p.merchant_member=b.member_id
        WHERE b.connection_id=:connection AND b.sender_external_id=:sender AND p.deleted_at IS NULL
    """, connection=connection, sender=sender)


async def confirmed_order(connection, member_id, order_id):
    reading = await merchant_for_connection(connection).orders(member_id, order_id)
    if reading.code == "OK" and len(reading.value) == 1:
        order = reading.value[0]
        if order.status == "已签收" and not order.mismatch_confirmed:
            return order
    return None


async def order_event(conn, event, event_id, at, now, verified):
    connection, group, order_id = event["connection_id"], event["group_id"], event["order_id"]
    if event["event_type"] == "order.signed" and not verified:
        return
    row = await one(conn, """
        INSERT INTO outreach_orders VALUES (:connection,:order_id,:sender,:state,:at)
        ON CONFLICT (connection_id,order_id) DO UPDATE SET state=EXCLUDED.state,occurred_at=EXCLUDED.occurred_at
        WHERE outreach_orders.occurred_at<EXCLUDED.occurred_at AND outreach_orders.sender_external_id=EXCLUDED.sender_external_id
        RETURNING state
    """, connection=connection, order_id=order_id, sender=event["sender_external_id"], state=event["event_type"], at=at)
    if not row or row["state"] != "order.signed":
        return
    binding, product, care_note = verified
    await execute(conn, """
        INSERT INTO outreach_suggestions (memory_id,status,result)
        SELECT id,'closed','purchased' FROM member_memories WHERE profile_id=:profile AND kind='interest'
            AND status IN ('candidate','confirmed') AND topic=:product
        ON CONFLICT (memory_id) DO UPDATE SET status='closed',result='purchased',
            version=outreach_suggestions.version+1,updated_at=now() WHERE outreach_suggestions.status<>'closed'
    """, profile=binding["profile_id"], product=product)
    settings = await group_settings(conn, connection, group)
    if not settings["outreach_enabled"] or not settings["care_text"] or at < settings["outreach_since"]:
        return
    await schedule(conn, key=f"care:{connection}:{order_id}", kind="care", connection=connection, group=group,
                   profile=binding["profile_id"], binding=binding, order=order_id, event=event_id,
                   body=f"{settings['care_text']}\n{product}：{care_note}" if care_note else f"{settings['care_text']}\n商品：{product}",
                   at=next_work_time(now) or now, expires=at + timedelta(days=7))


async def receipt(conn, event):
    item = await one(conn, """
        SELECT * FROM outreach_records WHERE operation_key=:key AND connection_id=:connection FOR UPDATE
    """, key=event["operation_key"], connection=event["connection_id"])
    if not item or item["state"] not in {"sending", "unknown", "accepted", "sent"}:
        return
    state = event["state"]
    if item["state"] == "sent" and state != "delivered":
        return
    if item["provider_ref"] and item["provider_ref"] != event["provider_ref"]:
        return
    await update_record(conn, item, state, reference=event["provider_ref"], evidence=f"receipt:{event['external_event_id']}")


async def process_business_event(engine, job):
    event_id = job["payload"]["event_id"]
    async with engine.connect() as conn:
        stored = await one(conn, "SELECT payload FROM business_events WHERE id=:id", id=event_id)
        event = stored["payload"]
        kind = event["event_type"]
        binding = await order_binding(conn, event["connection_id"], event["sender_external_id"]) if (
            kind == "order.signed" and not event["historical"]) else None
    verified = None
    if binding:
        order = await confirmed_order(event["connection_id"], binding["member_id"], event["order_id"])
        if order:
            async with engine.connect() as conn:
                notes = await many(conn, """
                    SELECT body FROM knowledge WHERE product=:product AND topic='care' AND valid_from<=now()
                        AND (valid_until IS NULL OR valid_until>now()) ORDER BY version DESC LIMIT 2
                """, product=order.product)
            verified = binding, order.product, notes[0]["body"] if len(notes) == 1 else None
    async with engine.begin() as conn:
        if not await fence(conn, job):
            return
        if not event["historical"]:
            at = datetime.fromisoformat(event["occurred_at"])
            now = datetime.now(UTC)
            if kind in {"member.joined", "member.left"}:
                await membership(conn, event, event_id, at, now)
            elif kind == "stock.changed":
                await stock_changed(conn, event, event_id, at, now)
            elif kind.startswith("order."):
                await order_event(conn, event, event_id, at, now, verified)
            elif kind == "delivery.receipt":
                await receipt(conn, event)
        await finish(conn, job)
