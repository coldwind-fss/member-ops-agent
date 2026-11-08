import re
from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, model_validator

from .commerce import result
from .ingress import Input
from .members import locked_profile
from .store import execute, many, one

ZONE = ZoneInfo("Asia/Shanghai")
ACTIVITY_COMMAND = re.compile(r"报名|打卡|问卷")


class ActivityConfig(Input):
    title: str = Field(min_length=1, max_length=80)
    group_id: str | None = Field(default=None, min_length=1, max_length=255)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    rule: str = Field(min_length=1, max_length=2000)
    checkin: bool = False
    question: str | None = Field(default=None, min_length=1, max_length=500)
    allow_makeup: bool = False
    allow_edit: bool = False

    @model_validator(mode="after")
    def check_window(self):
        if self.starts_at >= self.ends_at:
            raise ValueError("结束时间必须晚于开始时间")
        return self


def activity_request(text):
    if re.search(r"不想|不报名|取消报名|怎么|规则|可以报名吗|是什么|是否", text):
        return None
    command = ACTIVITY_COMMAND.search(text)
    if not command:
        return None
    if command[0] == "问卷" and not re.search(r"问卷\s*[:：]", text):
        return None
    return {"报名": "register", "打卡": "checkin", "问卷": "survey"}[command[0]]


def effective_status(activity, now):
    if activity["status"] in {"published", "paused"}:
        if now >= activity["ends_at"]:
            return "ended"
        if activity["status"] == "published":
            return "scheduled" if now < activity["starts_at"] else "open"
    return activity["status"]


async def activity_information(engine, event, text):
    if not re.search(r"活动|报名|打卡|问卷|挑战", text):
        return None
    async with engine.connect() as conn:
        rows = await many(conn, """
            SELECT a.* FROM activities a WHERE a.status<>'draft' AND (
                a.group_id IS NULL OR (:kind='group' AND a.group_id=:scope) OR (:kind='private' AND EXISTS (
                    SELECT 1 FROM member_bindings b JOIN group_memberships g
                    ON g.connection_id=b.connection_id AND g.sender_external_id=b.sender_external_id
                    WHERE b.connection_id=:connection AND b.sender_external_id=:sender AND b.private_external_id=:scope
                      AND b.member_id IS NOT NULL AND g.group_id=a.group_id AND g.active)))
        """, connection=event["connection_id"], sender=event["sender_external_id"], kind=event["conversation"]["kind"],
             scope=event["conversation"]["external_id"])
    matching = [item for item in rows if item["title"] in text]
    if len(matching) != 1:
        return None
    activity = matching[0]
    status = effective_status(activity, datetime.now(ZONE))
    state = {"scheduled": "待开始", "open": "进行中", "paused": "已暂停", "ended": "已结束", "cancelled": "已取消"}[status]
    rule = activity["config"]["rule"]
    return result("answered", f"{activity['title']} · {state}\n{rule}", subject=activity["title"])


async def activity_result(conn, event, event_id, text):
    action = activity_request(text)
    if not action:
        return result("clarification", "请说明你要报名、打卡或提交哪场活动。", reason="ACTIVITY_UNCLEAR")
    profile = await locked_profile(conn, event, event_id)
    if not profile:
        return result("unavailable", "参与前请先完成会员核验。", reason="UNBOUND")
    private = event["conversation"]["kind"] == "private"
    if action == "survey" and not private:
        return result("unavailable", "请通过核验后的私密会话提交问卷。", reason="PRIVATE_CHANNEL_REQUIRED")
    group = event["conversation"]["external_id"] if not private else None
    rows = await many(conn, """
        SELECT * FROM activities WHERE status<>'draft' AND (group_id IS NULL OR CAST(:group AS text) IS NULL OR group_id=:group)
        ORDER BY starts_at DESC
    """, group=group)
    explicit = [row for row in rows if row["title"] in text]
    now = datetime.now(ZONE)
    choices = explicit or [row for row in rows if effective_status(row, now) == "open"]
    if len(choices) != 1:
        return result("clarification", "请补充活动名称，我再核对参与条件。", reason="ACTIVITY_UNCLEAR")
    activity = await one(conn, "SELECT * FROM activities WHERE id=:id FOR SHARE", id=choices[0]["id"])
    command = text.replace(activity["title"], "", 1).strip(" ，。！？!?")
    if action == "register" and not re.fullmatch(r"(?:(?:帮我|给我|我想|我要|请帮我)\s*)?报名", command):
        return result("clarification", "确认参加时，请回复「报名」加活动名称。", reason="ACTIVITY_UNCLEAR")
    if action == "checkin" and not re.fullmatch(r"(?:(?:我来|我要|今日|今天|补)\s*)?打卡(?:\s*\d{4}-\d{2}-\d{2})?", command):
        return result("clarification", "请回复活动名称加「打卡」。", reason="ACTIVITY_UNCLEAR")
    if activity["group_id"] and not await one(conn, """
        SELECT 1 FROM member_bindings b JOIN group_memberships g
          ON b.connection_id=g.connection_id AND b.sender_external_id=g.sender_external_id
        WHERE b.member_id=:member AND g.group_id=:group AND g.active
    """, member=profile["merchant_member"], group=activity["group_id"]):
        return result("unavailable", "当前身份不在活动指定群的可参与范围。", reason="ACTIVITY_INELIGIBLE")
    config = activity["config"]
    if (action == "checkin" and not config["checkin"]) or (action == "survey" and not config["question"]):
        return result("unavailable", "此活动未开放该提交方式。", reason="ACTIVITY_ACTION_DISABLED")
    period = ""
    if action == "checkin":
        period = now.date().isoformat()
        if match := re.search(r"\d{4}-\d{2}-\d{2}", text):
            try:
                day = date.fromisoformat(match[0])
            except ValueError:
                return result("clarification", "请提供正确的打卡日期。", reason="INVALID_PERIOD")
            if (day > now.date() or day < activity["starts_at"].astimezone(ZONE).date()
                    or (day < now.date() and not config["allow_makeup"])):
                return result("unavailable", "该日期不可补打卡。", reason="INVALID_PERIOD")
            period = day.isoformat()
    params = dict(activity=activity["id"], profile=profile["id"], action=action, period=period)
    previous = await one(conn, """
        SELECT * FROM participation WHERE event_id=:event AND action=:action
        UNION ALL
        SELECT * FROM participation WHERE activity_id=:activity AND profile_id=:profile AND action=:action AND period=:period
        LIMIT 1
    """, event=event_id, **params)
    label = {"register": "报名", "checkin": "打卡", "survey": "问卷"}[action]
    if previous and (previous["event_id"] == event_id or action != "survey" or not config["allow_edit"]):
        return result("answered", f"这次{label}已登记。", reason="ALREADY_SUBMITTED")
    status = effective_status(activity, now)
    if status != "open":
        description = {"scheduled": "尚未开始", "ended": "已结束", "paused": "已暂停", "cancelled": "已取消"}[status]
        return result("unavailable", f"活动{description}，当前无法{label}。", reason=f"ACTIVITY_{status.upper()}")
    if action != "register" and not await one(conn, """
        SELECT 1 FROM participation WHERE activity_id=:activity AND profile_id=:profile AND action='register'
    """, **params):
        return result("unavailable", "请先报名，再提交打卡或问卷。", reason="REGISTRATION_REQUIRED")
    answer = re.split(r"问卷\s*[:：]", text, maxsplit=1)[-1].strip() if action == "survey" else None
    if action == "survey" and (not answer or len(answer) > 2000):
        return result("clarification", "请提交 1 到 2000 字的问卷内容。", reason="INVALID_SURVEY")
    if previous:
        await execute(conn, """
            UPDATE participation SET answer=:answer,event_id=:event,activity_version=:version,updated_at=now() WHERE id=:id
        """, answer=answer, event=event_id, version=activity["version"], id=previous["id"])
    else:
        await execute(conn, """
            INSERT INTO participation (id,activity_id,activity_version,profile_id,action,period,event_id,answer)
            VALUES (:id,:activity,:version,:profile,:action,:period,:event,:answer)
        """, id=uuid4(), version=activity["version"], event=event_id, answer=answer, **params)
    return result("answered", f"{activity['title']}的{label}已登记。")
