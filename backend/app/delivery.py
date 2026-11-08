import asyncio
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

from .identity import binding_current
from .members import profile_current
from .queue import fence, finish
from .store import execute, one


class ReplyChannel(Protocol):
    async def send(self, *, operation_key: str, conversation_kind: str, conversation_id: str,
                   member_id: str, in_reply_to: str | None, text: str) -> tuple[str, str | None]: ...


class FixtureReplyChannel:
    async def send(self, *, operation_key, conversation_kind, conversation_id, member_id, in_reply_to, text):
        state = os.getenv("FIXTURE_REPLY_RESULT", "sent")
        return state, f"fixture-{operation_key}" if state in {"sent", "accepted", "delivered"} else None


def reply_channel_for(connection_id: str) -> ReplyChannel | None:
    if connection_id == "fixture-channel" and os.getenv("DEV_EVENT_TOKEN"):
        return FixtureReplyChannel()
    return None


def send_email(item):
    host, sender, recipient = (os.getenv(name) for name in ("SMTP_HOST", "SMTP_FROM", "OPERATOR_EMAIL"))
    if not all((host, sender, recipient)):
        return "failed", None
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, recipient, "私域运营助手 · 人工待办"
    origin = os.getenv("OPERATOR_ORIGIN", "http://127.0.0.1:5173").rstrip("/")
    message.set_content(
        f"会话：{item['conversation_external_id']}\n会员：{item['participant_label']}\n"
        f"转交原因：{item['reason']}\n问题摘要：{item['summary']}\n"
        f"查看事项：{origin}/?matter={item['matter_id']}"
    )
    dispatch_started = False
    try:
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=10) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if os.getenv("SMTP_USERNAME"):
                smtp.login(os.environ["SMTP_USERNAME"], os.getenv("SMTP_PASSWORD", ""))
            dispatch_started = True
            smtp.send_message(message)
        return "accepted", None
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused):
        return "failed", None
    except (OSError, smtplib.SMTPException):
        return ("unknown" if dispatch_started else "retryable"), None


async def deliver(item):
    if item["kind"] == "reply":
        channel = reply_channel_for(item["connection_id"])
        if channel is None:
            return "failed", None
        return await channel.send(operation_key=item["operation_key"], conversation_kind=item["conversation_kind"],
                                  conversation_id=item["conversation_external_id"], member_id=item["participant_label"],
                                  in_reply_to=item["in_reply_to"], text=item["private_body"] or item["preview"])
    if os.getenv("NOTIFICATION_MODE") == "fixture" and os.getenv("DEV_EVENT_TOKEN"):
        result = os.getenv("FIXTURE_NOTIFICATION_RESULT", "sent")
        return result, f"fixture-notification-{item['id']}" if result == "sent" else None
    return await asyncio.to_thread(send_email, item)


async def record(conn, item, state, provider_ref=None):
    await execute(conn, """
        UPDATE deliveries SET state=:state,provider_ref=:reference,updated_at=now(),
            private_body=CASE WHEN :state='pending' THEN private_body ELSE NULL END WHERE id=:id
    """, id=item["id"], state=state, reference=provider_ref)
    if item["message_id"]:
        await execute(conn, """
            UPDATE messages SET delivery_state=:state,external_id=:reference WHERE id=:id
        """, id=item["message_id"], state=state, reference=provider_ref)
    if item["handoff_id"]:
        await execute(conn, """
            UPDATE handoffs SET notification_state=:state,updated_at=now()
            WHERE id=:id AND notification_delivery_id=:delivery
        """, id=item["handoff_id"], delivery=item["id"], state=state)


async def process_delivery(engine, job):
    async with engine.begin() as conn:
        if not await fence(conn, job):
            return
        item = await one(conn, """
            SELECT d.*,m.version,m.connection_id,m.conversation_kind,m.conversation_external_id,m.participant_label,
                   h.summary,h.reason,h.status AS handoff_status,h.notification_delivery_id,msg.preview,msg.in_reply_to,
                   msg.occurred_at AS reply_created_at
            FROM deliveries d JOIN matters m ON m.id=d.matter_id
            LEFT JOIN messages msg ON msg.id=d.message_id
            LEFT JOIN handoffs h ON h.id=d.handoff_id WHERE d.id=:id FOR UPDATE OF d,m
        """, id=job["payload"]["delivery_id"])
        if item["state"] == "sending":
            await record(conn, item, "unknown")
            await finish(conn, job, error="DELIVERY_NEEDS_RECONCILIATION")
            return
        if item["state"] != "pending":
            await finish(conn, job)
            return
        binding_revoked = item["binding_id"] and not await binding_current(conn, item["binding_id"], item["binding_version"])
        profile_changed = item["profile_id"] and not await profile_current(conn, item)
        group_paused = item["kind"] == "reply" and item["conversation_kind"] == "group" and await one(conn, """
            SELECT 1 FROM outreach_groups WHERE connection_id=:connection AND group_id=:group_id
                AND (NOT reply_enabled OR reply_since>:created) FOR SHARE
        """, connection=item["connection_id"], group_id=item["conversation_external_id"], created=item["reply_created_at"])
        if (binding_revoked or profile_changed or group_paused or (item["kind"] == "reply" and item["version"] != item["expected_version"])
                or (item["kind"] == "notification" and
                    (item["handoff_status"] != "pending" or item["notification_delivery_id"] != item["id"]))):
            await record(conn, item, "cancelled")
            await finish(conn, job)
            return
        await execute(conn, "UPDATE deliveries SET state='sending',attempt_started_at=now() WHERE id=:id", id=item["id"])

    state, reference = await deliver(item)
    if state not in {"accepted", "sent", "delivered", "failed", "unknown", "retryable"}:
        state, reference = "unknown", None
    async with engine.begin() as conn:
        if not await fence(conn, job):
            return
        if state == "retryable" and job["generation"] == 1:
            await record(conn, item, "pending")
            await finish(conn, job, "pending", "DELIVERY_TEMPORARILY_UNAVAILABLE")
            return
        if state == "retryable":
            state = "failed"
        await record(conn, item, state, reference)
        await finish(conn, job, error=f"DELIVERY_{state.upper()}" if state in {"unknown", "failed"} else None)
