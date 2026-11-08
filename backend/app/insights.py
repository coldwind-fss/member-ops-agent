import re
from datetime import datetime, time
from uuid import uuid4
from zoneinfo import ZoneInfo

from .members import observe_membership
from .queue import fence
from .store import execute, one

COMPLAINT = re.compile(r"我要投诉|投诉这个|(?:收到|到手|刚穿|穿了|已经|发现).{0,14}(?:开胶|破损|质量问题|严重掉色)|(?:开胶|破损|寄错|发错货|破)了")
TOPICS = (
    ("商品洗护", r"怎么洗|洗护|烘干|缩水|保养"),
    ("库存与尺码", r"有货|缺货|库存|尺码|补货"),
    ("价格与购买", r"多少钱|什么价|价格|怎么买|想买"),
    ("活动与优惠", r"活动|优惠|怎么领|满减|打卡"),
    ("订单物流", r"物流|订单|快递|签收"),
    ("售后规则", r"退换|怎么退|售后"),
)
ZONE = ZoneInfo("Asia/Shanghai")


async def publish_daily_reports(engine, now=None):
    current = (now or datetime.now(ZONE)).astimezone(ZONE)
    if current.hour < 18:
        return
    start = datetime.combine(current.date(), time.min, tzinfo=ZONE)
    cutoff = datetime.combine(current.date(), time(18), tzinfo=ZONE)
    async with engine.begin() as conn:
        await execute(conn, """
            INSERT INTO daily_reports (connection_id,group_id,day,summary)
            SELECT g.connection_id,g.group_id,:day,jsonb_build_object('topics',COALESCE((
                SELECT jsonb_object_agg(bucket.topic,bucket.total) FROM (
                    SELECT s.topic,count(*) AS total FROM group_signals s WHERE s.connection_id=g.connection_id
                        AND s.group_id=g.group_id AND s.occurred_at>=:start AND s.occurred_at<:cutoff GROUP BY s.topic
                ) bucket), '{}'::jsonb),'cutoff',CAST(:cutoff AS timestamptz))
            FROM analysis_groups g WHERE g.enabled AND g.updated_at<:cutoff ON CONFLICT DO NOTHING
        """, day=current.date(), start=start, cutoff=cutoff)


def group_signal(text):
    if COMPLAINT.search(text) and not re.search(r"如果|要是|会不会|有没有|没有开胶|没有破损|不投诉|没开胶|没破损", text):
        return "质量与售后反馈", "complaint"
    for topic, pattern in TOPICS:
        if re.search(pattern, text):
            category = "interest" if topic in {"库存与尺码", "价格与购买"} else "question"
            return topic, category
    return None


async def observe_group(engine, job, event_id, event):
    if event["conversation"]["kind"] != "group":
        return None
    async with engine.begin() as conn:
        if not await fence(conn, job):
            return None
        await observe_membership(conn, event)
        if not await one(conn, """
            SELECT 1 FROM analysis_groups g JOIN inbox_events e ON e.id=:event
            WHERE g.connection_id=:connection AND g.group_id=:group_id AND g.enabled
                AND e.received_at>=g.updated_at AND e.redacted_at IS NULL
        """, event=event_id, connection=event["connection_id"], group_id=event["conversation"]["external_id"]):
            return None
        signal = group_signal(event["message"]["text"])
        if not signal:
            return None
        topic, category = signal
        await execute(conn, """
            INSERT INTO group_signals (id,event_id,connection_id,group_id,topic,category,occurred_at,profile_id)
            VALUES (:id,:event,:connection,:group_id,:topic,:category,:at,(
                SELECT p.id FROM member_bindings b JOIN member_profiles p ON p.merchant_member=b.member_id
                JOIN inbox_events e ON e.id=:event AND b.updated_at<=e.received_at
                WHERE b.connection_id=:connection AND b.sender_external_id=:sender AND p.deleted_at IS NULL
            )) ON CONFLICT (event_id) DO NOTHING
        """, id=uuid4(), event=event_id, connection=event["connection_id"], group_id=event["conversation"]["external_id"],
             topic=topic, category=category, at=datetime.fromisoformat(event["occurred_at"]), sender=event["sender_external_id"])
        return category


async def expire_sources(engine, checkpointer):
    async with engine.begin() as conn:
        await execute(conn, """
            UPDATE messages SET preview='原始内容已到期'
            WHERE occurred_at < now()-interval '90 days' AND preview<>'原始内容已到期'
        """)
        await execute(conn, """
            UPDATE inbox_events SET payload=payload #- '{message,text}',redacted_at=now()
            WHERE redacted_at IS NULL AND received_at < now()-interval '90 days'
                AND NOT EXISTS (SELECT 1 FROM jobs WHERE operation_key='inbound:'||inbox_events.id
                                AND state IN ('pending','running'))
        """)
        expired = (await execute(conn, """
            UPDATE matters SET commerce_context='{}'::jsonb,subject='原始内容已到期'
            WHERE updated_at<now()-interval '90 days' AND status<>'human_handling'
                AND subject<>'原始内容已到期' RETURNING id
        """)).scalars().all()
        await execute(conn, """
            UPDATE jobs SET payload=payload-'decision' WHERE kind='inbound' AND state IN ('done','failed')
                AND created_at<now()-interval '90 days' AND payload ? 'decision'
        """)
        await execute(conn, """
            UPDATE participation SET answer=NULL WHERE answer IS NOT NULL AND updated_at<now()-interval '365 days'
        """)
        await execute(conn, """
            UPDATE handoffs SET summary='原始内容已到期' WHERE status='resolved' AND created_at<now()-interval '90 days'
                AND summary<>'原始内容已到期'
        """)
        await execute(conn, """
            DELETE FROM member_timeline WHERE occurred_at<now()-interval '365 days'
        """)
    for matter_id in expired:
        await checkpointer.adelete_thread(str(matter_id))
