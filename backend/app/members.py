import re
from datetime import datetime
from uuid import uuid4

from .commerce import result
from .identity import get_binding
from .store import execute, many, one

SIZE = re.compile(r"我(?:平时|通常|一直|现在|以后)?(?:都)?穿\s*(XXL|XL|XS|S|M|L)\s*(?:码|号)(?![A-Za-z])", re.I)
COLOR = re.compile(r"我(?:平时|通常|一直|现在|以后)?(?:更)?喜欢\s*([黑白蓝红灰绿])色")
PURPOSE = re.compile(r"我(?:平时|通常|一直|现在|以后)(?:主要|喜欢|常)?(越野|跑步|训练|健身)")
FORGET = re.compile(r"(?:别再记|不要记|忘掉|删除)(?:住)?我(?:的)?(尺码|颜色偏好|运动偏好)")
TEMPORARY = re.compile(r"(?:给|帮|替)(?:朋友|别人|家人|同事)|代购|这次|临时")


def memory_command(text):
    if TEMPORARY.search(text):
        return None
    if match := FORGET.match(text):
        return {"尺码": "size", "颜色偏好": "color", "运动偏好": "use_case"}[match[1]], None
    for topic, pattern in (("size", SIZE), ("color", COLOR), ("use_case", PURPOSE)):
        if match := pattern.match(text):
            return topic, match[1].upper() if topic == "size" else match[1] + "色" if topic == "color" else match[1]
    return None


def valid_preference(topic, value):
    return value in {
        "size": ("XS", "S", "M", "L", "XL", "XXL"),
        "color": ("黑色", "白色", "蓝色", "红色", "灰色", "绿色"),
        "use_case": ("越野", "跑步", "训练", "健身"),
    }.get(topic, ())


async def observe_membership(conn, event):
    if event["conversation"]["kind"] == "group":
        await execute(conn, """
            INSERT INTO group_memberships (connection_id,group_id,sender_external_id)
            VALUES (:connection,:group_id,:sender) ON CONFLICT DO NOTHING
        """, connection=event["connection_id"], group_id=event["conversation"]["external_id"],
             sender=event["sender_external_id"])


async def locked_profile(conn, event, event_id):
    return await one(conn, """
        SELECT p.* FROM member_bindings b JOIN member_profiles p ON p.merchant_member=b.member_id
        JOIN inbox_events e ON e.id=:event AND b.updated_at<=e.received_at
        WHERE b.connection_id=:connection AND b.sender_external_id=:sender AND p.deleted_at IS NULL
            AND (CAST(:private_id AS text) IS NULL OR b.private_external_id=:private_id)
        FOR SHARE OF b FOR UPDATE OF p
    """, connection=event["connection_id"], sender=event["sender_external_id"], event=event_id,
         private_id=event["conversation"]["external_id"] if event["conversation"]["kind"] == "private" else None)


async def remember(conn, event, event_id, intent, outcome, subject):
    profile = await locked_profile(conn, event, event_id)
    if not profile:
        return False
    occurred = datetime.fromisoformat(event["occurred_at"])
    kind = intent.kind
    command = memory_command(intent.evidence) if kind == "memory_update" else None
    fresh = await one(conn, """
        INSERT INTO member_timeline (profile_id,event_id,kind,topic,occurred_at)
        VALUES (:profile,:event,:kind,:topic,:at) ON CONFLICT DO NOTHING RETURNING event_id
    """, profile=profile["id"], event=event_id, kind=kind, topic=subject, at=occurred)
    if not fresh:
        return True
    if command:
        topic, value = command
        updated = await one(conn, """
            INSERT INTO member_memories (id,profile_id,kind,topic,value,status,event_id,source_at)
            VALUES (:id,:profile,'preference',:topic,:value,:status,:event,:at)
            ON CONFLICT (profile_id,kind,topic) DO UPDATE SET value=EXCLUDED.value,status=EXCLUDED.status,
                event_id=EXCLUDED.event_id,source_at=EXCLUDED.source_at,updated_at=now(),version=member_memories.version+1
            WHERE member_memories.source_at < EXCLUDED.source_at RETURNING id
        """, id=uuid4(), profile=profile["id"], topic=topic, value=value,
             status="confirmed" if value else "deleted", event=event_id, at=occurred)
        if updated:
            await execute(conn, "UPDATE member_profiles SET version=version+1 WHERE id=:id", id=profile["id"])
    elif subject and outcome in {"answered", "unavailable"} and kind == "inventory_query" and not TEMPORARY.search(intent.evidence):
        await candidate(conn, profile["id"], event_id, subject, occurred)
    return True


async def candidate(conn, profile_id, event_id, topic, occurred):
    return await one(conn, """
        INSERT INTO member_memories (id,profile_id,kind,topic,value,status,event_id,source_at,expires_at)
        VALUES (:id,:profile,'interest',:topic,:topic,'candidate',:event,:at,:at+interval '30 days')
        ON CONFLICT (profile_id,kind,topic) DO UPDATE SET event_id=EXCLUDED.event_id,source_at=EXCLUDED.source_at,
            expires_at=EXCLUDED.expires_at,updated_at=now(),version=member_memories.version+1
        WHERE member_memories.status='candidate' AND member_memories.source_at < EXCLUDED.source_at RETURNING id
    """, id=uuid4(), profile=profile_id, topic=topic, event=event_id, at=occurred)


async def memory_reply(engine, event, intent):
    if not memory_command(intent.evidence):
        return result("clarification", "请明确告诉我你希望记住或删除的本人尺码、颜色或运动偏好。", reason="MEMORY_UNCLEAR")
    identity = await get_binding(engine, event["connection_id"], event["sender_external_id"])
    if not identity or (event["conversation"]["kind"] == "private"
                        and identity["private_external_id"] != event["conversation"]["external_id"]):
        return result("unavailable", "请先完成身份核验，再修改本人偏好。", reason="UNBOUND")
    return result("answered", "已记录你的偏好设置。", authorization={
        "id": str(identity["id"]), "version": identity["version"],
    }) if event["conversation"]["kind"] == "private" else {
        **result("answered", "已记录你的偏好设置。"), "memory_identity": {
            "id": str(identity["id"]), "version": identity["version"],
        },
    }


async def guide_preferences(engine, event):
    if event["conversation"]["kind"] != "private":
        return {}, None
    async with engine.connect() as conn:
        rows = await many(conn, """
            SELECT m.topic,m.value,p.id,p.version,b.id AS binding_id,b.version AS binding_version
            FROM member_bindings b JOIN member_profiles p ON p.merchant_member=b.member_id
            JOIN member_memories m ON m.profile_id=p.id
            WHERE b.connection_id=:connection AND b.sender_external_id=:sender AND b.private_external_id=:scope
                AND p.deleted_at IS NULL AND m.kind='preference' AND m.status='confirmed'
                AND (m.expires_at IS NULL OR m.expires_at>now())
        """, connection=event["connection_id"], sender=event["sender_external_id"],
             scope=event["conversation"]["external_id"])
    if not rows:
        return {}, None
    row = rows[0]
    return {r["topic"]: r["value"] for r in rows}, {
        "id": str(row["binding_id"]), "version": row["binding_version"],
        "profile_id": str(row["id"]), "profile_version": row["version"],
    }


async def profile_current(conn, grant):
    return await one(conn, """
        SELECT id FROM member_profiles WHERE id=:id AND version=:version AND deleted_at IS NULL FOR SHARE
    """, id=grant["profile_id"], version=grant["profile_version"])


async def profile_rows(conn):
    return await many(conn, """
        SELECT p.id,p.version,p.deleted_at,ARRAY(
            SELECT DISTINCT b.sender_external_id FROM member_bindings b WHERE b.member_id=p.merchant_member
            ORDER BY b.sender_external_id
        ) AS identities,ARRAY(
            SELECT DISTINCT g.group_id FROM member_bindings b JOIN group_memberships g
              ON g.connection_id=b.connection_id AND g.sender_external_id=b.sender_external_id
            WHERE b.member_id=p.merchant_member AND g.active ORDER BY g.group_id
        ) AS groups,(SELECT max(t.occurred_at) FROM member_timeline t WHERE t.profile_id=p.id) AS last_activity
        FROM member_profiles p WHERE p.deleted_at IS NULL ORDER BY last_activity DESC NULLS LAST,p.id
    """)
