import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from .activities import effective_status
from .delivery import reply_channel_for
from .member_api import AudienceFilters, preview
from .queue import enqueue, fence, finish
from .store import execute, many, one

SUCCESS = {"sent", "delivered"}


async def group_settings(conn, connection, group, *, lock=False):
    if not group:
        return {"reply_enabled": True, "outreach_enabled": True, "welcome_text": None, "care_text": None, "reply_since": None}
    return await one(conn, """
        SELECT * FROM outreach_groups WHERE connection_id=:connection AND group_id=:group_id
    """ + (" FOR UPDATE" if lock else ""), connection=connection, group_id=group) or {
        "reply_enabled": True, "outreach_enabled": False, "welcome_text": None, "care_text": None, "reply_since": None,
    }


async def active_in_group(conn, connection, group, sender):
    return bool(await one(conn, """
        SELECT 1 FROM group_memberships WHERE connection_id=:connection AND group_id=:group_id
            AND sender_external_id=:sender AND active FOR SHARE
    """, connection=connection, group_id=group, sender=sender))


async def profile_binding(conn, profile, connection, group=None):
    return await one(conn, """
        SELECT b.id,b.version,b.sender_external_id,b.member_id,b.private_external_id
        FROM member_bindings b JOIN member_profiles p ON p.merchant_member=b.member_id
        WHERE p.id=:profile AND p.deleted_at IS NULL AND b.connection_id=:connection
        ORDER BY EXISTS (SELECT 1 FROM group_memberships g WHERE g.connection_id=b.connection_id
            AND g.sender_external_id=b.sender_external_id AND g.group_id=:group_id AND g.active) DESC,
            b.private_external_id IS NOT NULL DESC,b.updated_at DESC,b.id LIMIT 1
    """, profile=profile, connection=connection, group_id=group)


async def schedule(conn, *, key, kind, connection, group, body, at, expires, sender=None, profile=None, binding=None,
                   plan=None, plan_version=None, subscription=None, event=None, order=None, memory=None):
    row = await one(conn, """
        INSERT INTO outreach_records (id,operation_key,kind,connection_id,group_id,body,scheduled_at,expires_at,
            sender_external_id,profile_id,binding_id,binding_version,plan_id,plan_version,subscription_id,event_id,
            order_id,memory_id)
        VALUES (:id,:key,:kind,:connection,:group_id,:body,:at,:expires,:sender,:profile,:binding,:binding_version,
            :plan,:plan_version,:subscription,:event,:order_id,:memory) ON CONFLICT (operation_key) DO NOTHING RETURNING *
    """, id=uuid4(), key=key, kind=kind, connection=connection, group_id=group, body=body, at=at, expires=expires,
         sender=sender or (binding["sender_external_id"] if binding else None), profile=profile,
         binding=binding["id"] if binding else None, binding_version=binding["version"] if binding else None,
         plan=plan, plan_version=plan_version, subscription=subscription, event=event, order_id=order, memory=memory)
    if row:
        await enqueue(conn, "outreach", f"send:{key}", f"outreach:{row['id']}", {"record_id": str(row["id"])}, run_after=at)
    return row


async def audience_candidates(conn, config):
    if config["mode"] == "group":
        return []
    audience = await one(conn, "SELECT * FROM audiences WHERE id=:id", id=config["audience_id"])
    if not audience:
        return []
    filters = AudienceFilters(**audience["filters"])
    result = await preview(conn, filters)
    allowed = set(audience["confirmed_member_ids"])
    rows = []
    for item in result["items"]:
        if item["id"] not in allowed:
            continue
        binding = await profile_binding(conn, item["id"], config["connection_id"], config["group_id"])
        if not binding or not await active_in_group(conn, config["connection_id"], config["group_id"], binding["sender_external_id"]):
            item = {**item, "matched": False, "reasons": ["已退出目标群或缺少有效渠道身份"]}
        rows.append(item)
    return rows


async def update_record(conn, item, state, *, reason=None, reference=None, evidence=None):
    row = await one(conn, """
        UPDATE outreach_records SET state=:state,reason=:reason,
            provider_ref=COALESCE(:reference,provider_ref),evidence=COALESCE(:evidence,evidence),
            dispatch_at=CASE WHEN :state IN ('accepted','sent','delivered') THEN COALESCE(dispatch_at,now()) ELSE dispatch_at END,
            version=version+1,updated_at=now()
        WHERE id=:id RETURNING *
    """, id=item["id"], state=state, reason=reason, reference=reference, evidence=evidence)
    if item["subscription_id"] and state in SUCCESS:
        await execute(conn, """
            UPDATE subscriptions SET status='fulfilled',version=version+1
            WHERE id=:id AND status='active'
        """, id=item["subscription_id"])
    if item["memory_id"] and state in SUCCESS:
        await execute(conn, """
            UPDATE outreach_suggestions SET status='closed',result='contacted',version=version+1,updated_at=now()
            WHERE memory_id=:id AND status='approved'
        """, id=item["memory_id"])
    return row


async def record_permission(conn, item, now):
    connection, group, sender = item["connection_id"], item["group_id"], item["sender_external_id"]
    kind = item["kind"]
    daily_limit = 1
    if item["expires_at"] <= now:
        return "expired", "SCHEDULE_EXPIRED", None
    if item["plan_id"]:
        plan = await one(conn, "SELECT * FROM outreach_plans WHERE id=:id FOR SHARE", id=item["plan_id"])
        if plan["status"] != "approved" or plan["version"] != item["plan_version"]:
            return "cancelled", "PLAN_CHANGED", None
        daily_limit = plan["config"].get("daily_limit", 1)
        if plan["config"]["activity_id"]:
            activity = await one(conn, "SELECT * FROM activities WHERE id=:id FOR SHARE", id=plan["config"]["activity_id"])
            if not activity or effective_status(activity, now) not in {"scheduled", "open"}:
                return "cancelled", "ACTIVITY_UNAVAILABLE", None
            baseline = await one(conn, """
                SELECT snapshot->'config' AS config FROM activity_versions
                WHERE activity_id=:id AND created_at<=:approved ORDER BY version DESC LIMIT 1
            """, id=activity["id"], approved=plan["updated_at"])
            if not baseline or baseline["config"] != activity["config"]:
                return "cancelled", "ACTIVITY_RULE_CHANGED", None
    binding = None
    if item["profile_id"]:
        binding = await one(conn, """
            SELECT b.private_external_id,b.sender_external_id,b.member_id FROM member_bindings b
            JOIN member_profiles p ON p.merchant_member=b.member_id
            WHERE b.id=:binding AND b.version=:version AND p.id=:profile AND p.deleted_at IS NULL FOR SHARE OF b FOR UPDATE OF p
        """, binding=item["binding_id"], version=item["binding_version"], profile=item["profile_id"])
        if not binding:
            return "cancelled", "BINDING_CHANGED", None
    if sender and group and not await active_in_group(conn, connection, group, sender):
        return "cancelled", "MEMBER_LEFT", None
    if item["plan_id"] and item["profile_id"]:
        eligible = {row["id"] for row in await audience_candidates(conn, plan["config"]) if row["matched"]}
        if item["profile_id"] not in plan["approved_members"] or item["profile_id"] not in eligible:
            return "cancelled", "NO_LONGER_ELIGIBLE", None
    if item["subscription_id"]:
        subscription = await one(conn, "SELECT * FROM subscriptions WHERE id=:id FOR SHARE", id=item["subscription_id"])
        if subscription["status"] != "active" or subscription["expires_at"] <= now:
            return "cancelled", "SUBSCRIPTION_ENDED", None
        stock = await one(conn, """
            SELECT quantity FROM outreach_stock WHERE connection_id=:connection AND product_id=:product_id
                AND color=:color AND size=:size FOR SHARE
        """, connection=connection, **{name: subscription[name] for name in ("product_id", "color", "size")})
        if not stock or stock["quantity"] <= 0:
            return "cancelled", "STOCK_UNAVAILABLE", None
    if kind == "care":
        state = await one(conn, """
            SELECT state FROM outreach_orders WHERE connection_id=:connection AND order_id=:order_id FOR SHARE
        """, connection=connection, order_id=item["order_id"])
        if state and state["state"] != "order.signed":
            return "cancelled", "ORDER_NOT_ELIGIBLE", None
        if await one(conn, "SELECT 1 FROM care_optouts WHERE profile_id=:profile", profile=item["profile_id"]):
            return "cancelled", "CARE_OPT_OUT", None
    if item["memory_id"]:
        need = await one(conn, """
            SELECT m.status,m.expires_at,s.status AS followup_status FROM member_memories m
            JOIN outreach_suggestions s ON s.memory_id=m.id WHERE m.id=:id FOR SHARE OF m,s
        """, id=item["memory_id"])
        if not need or need["status"] not in {"candidate", "confirmed"} or need["followup_status"] != "approved" or (
                need["expires_at"] and need["expires_at"] <= now):
            return "cancelled", "INTEREST_ENDED", None
    settings = await group_settings(conn, connection, group, lock=True)
    if not settings["outreach_enabled"]:
        return "defer", "OUTREACH_PAUSED", None
    if kind in {"plan", "followup"}:
        count = await one(conn, """
            SELECT count(*) AS total FROM outreach_records WHERE id<>:id AND kind IN ('plan','followup')
            AND state IN ('sending','accepted','sent','delivered','unknown')
            AND COALESCE(dispatch_at,updated_at) >= (date_trunc('day',now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai')
            AND (profile_id=:profile OR (CAST(:profile AS uuid) IS NULL AND profile_id IS NULL
                AND connection_id=:connection AND group_id=:group_id))
        """, id=item["id"], profile=item["profile_id"], connection=connection, group_id=group)
        if count["total"] >= daily_limit:
            return "cancelled", "DAILY_FREQUENCY", None
    if kind == "welcome":
        recent = await one(conn, """
            SELECT 1 FROM outreach_records WHERE connection_id=:connection AND group_id=:group_id
                AND sender_external_id=:sender AND kind='welcome' AND id<>:id
                AND state IN ('sending','accepted','sent','delivered','unknown')
                AND COALESCE(dispatch_at,updated_at)>now()-interval '24 hours'
        """, connection=connection, group_id=group, sender=sender, id=item["id"])
        if recent:
            return "cancelled", "WELCOME_FREQUENCY", None
    channel = reply_channel_for(connection)
    destination = binding["private_external_id"] if binding else group
    if not destination or channel is None:
        return "manual", "CHANNEL_UNAVAILABLE", None
    return "send", None, (channel, "private" if binding else "group", destination)


async def process_outreach(engine, job):
    async with engine.begin() as conn:
        if not await fence(conn, job):
            return
        item = await one(conn, "SELECT * FROM outreach_records WHERE id=:id FOR UPDATE", id=job["payload"]["record_id"])
        if item["state"] == "sending":
            await update_record(conn, item, "unknown", reason="DELIVERY_NEEDS_RECONCILIATION")
        if item["state"] != "pending":
            await finish(conn, job)
            return
        now = datetime.now(UTC)
        state, reason, target = await record_permission(conn, item, now)
        if state == "defer":
            await finish(conn, job, "pending", reason, retry_at=min(now + timedelta(seconds=30), item["expires_at"]))
            return
        if state != "send":
            await update_record(conn, item, state, reason=reason)
            await finish(conn, job)
            return
        await execute(conn, """
            UPDATE outreach_records SET state='sending',attempts=attempts+1,version=version+1,updated_at=now(),dispatch_at=now() WHERE id=:id
        """, id=item["id"])
    channel, conversation_kind, destination = target
    try:
        async with asyncio.timeout(15):
            state, reference = await channel.send(operation_key=item["operation_key"], conversation_kind=conversation_kind,
                                                  conversation_id=destination, member_id=item["sender_external_id"] or "",
                                                  in_reply_to=None, text=item["body"])
    except (TimeoutError, OSError):
        state, reference = "unknown", None
    async with engine.begin() as conn:
        if not await fence(conn, job):
            return
        current = await one(conn, "SELECT * FROM outreach_records WHERE id=:id FOR UPDATE", id=item["id"])
        if current["state"] != "sending":
            await finish(conn, job)
            return
        if state == "retryable" and current["attempts"] == 1:
            await update_record(conn, current, "pending", reason="TEMPORARY_FAILURE")
            await finish(conn, job, "pending", "TEMPORARY_FAILURE")
            return
        if state == "retryable":
            state = "failed"
        if state not in SUCCESS | {"accepted", "failed"}:
            state = "unknown"
        await update_record(conn, current, state, reason="DELIVERY_NEEDS_RECONCILIATION" if state == "unknown" else None,
                            reference=reference)
        await finish(conn, job)
