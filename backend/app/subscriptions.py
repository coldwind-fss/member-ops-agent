import re

from .commerce import build_context, CommerceHints, result
from .members import locked_profile
from .merchant import merchant_for_connection
from .store import execute, one
from uuid import uuid4

OPTOUT = re.compile(r"^(?:我)?(?:不想要|不需要|不要|取消)(?:再)?(?:接收)?购后关怀[。！! ]*$")


def subscription_intent(text):
    if OPTOUT.fullmatch(text):
        return "unsubscribe"
    if re.search(r"不想|不要|取消|退订|不用", text) and re.search(r"到货|补货|提醒|订阅", text):
        return "unsubscribe"
    if re.search(r"到货提醒我|补货提醒我|到货通知我|补货通知我|订阅.{0,40}(?:到货|补货)|(?:到货|补货)(?:了|后)?(?:请|就|再)?(?:叫|通知|提醒)我", text):
        return "subscribe"
    return None


async def prepare_subscription(event, intent, matter, reference, text):
    action = subscription_intent(text)
    if action is None and intent.kind == "unsubscribe" and re.fullmatch(r"取消提醒|退订", text):
        action = "unsubscribe"
    if action is None:
        return result("clarification", "请明确是要订阅还是取消到货提醒，并注明商品、颜色和尺码。", reason="SUBSCRIPTION_UNCLEAR")
    if OPTOUT.fullmatch(text):
        return {**result("ignored", None), "subscription_change": {"action": "optout"}}
    context = build_context(text, intent.commerce or CommerceHints(), matter["commerce_context"], reference, guide=False)
    names = context.get("products", [])
    if len(names) != 1 or not context.get("color") or not context.get("size"):
        return result("clarification", "请补充具体商品、颜色和尺码，例如「风行跑步上衣黑色 XL 到货提醒我」。",
                      reason="MISSING_VARIANT", context=context)
    merchant = merchant_for_connection(event["connection_id"])
    catalog = await merchant.catalog(names)
    if catalog.code != "OK" or len(catalog.value) != 1:
        return result("unavailable", "当前无法核实该商品，请核对名称后重试。", reason=catalog.code,
                      evidence=[catalog.evidence("catalog")], context=context)
    product = catalog.value[0]
    if action == "subscribe":
        inventory = await merchant.inventory(product.id, context["color"], context["size"])
        if inventory.code != "OK":
            return result("unavailable", "当前无法核实该规格，请核对颜色和尺码。", reason=inventory.code,
                          evidence=[inventory.evidence("inventory")], context=context)
    return {**result("ignored", None, context=context, subject=product.name), "subscription_change": {
        "action": action, "product_id": product.id, "product_name": product.name,
        "color": context["color"], "size": context["size"],
    }}


async def cancel_subscription(conn, sub_id):
    return await one(conn, """
        UPDATE subscriptions SET status='cancelled',version=version+1 WHERE id=:id AND status='active' RETURNING *
    """, id=sub_id)


async def apply_subscription(conn, event, event_id, command):
    connection, sender = event["connection_id"], event["sender_external_id"]
    action = command["action"]
    profile = await locked_profile(conn, event, event_id)
    if not profile:
        return result("unavailable", "请先完成会员核验，再操作本人提醒。", reason="UNBOUND")
    if action == "optout":
        await execute(conn, """
            INSERT INTO care_optouts (profile_id) VALUES (:profile)
            ON CONFLICT (profile_id) DO UPDATE SET updated_at=now()
        """, profile=profile["id"])
        return result("answered", "已记录你的购后关怀偏好。")
    params = dict(profile=profile["id"], **{name: command[name] for name in ("product_id", "color", "size")})
    current = await one(conn, """
        SELECT s.*,b.version AS current_version,b.member_id FROM subscriptions s
        JOIN member_bindings b ON b.id=s.binding_id WHERE s.profile_id=:profile AND s.product_id=:product_id
            AND s.color=:color AND s.size=:size AND s.status='active'
    """, **params)
    if action == "unsubscribe":
        if current:
            await cancel_subscription(conn, current["id"])
        return result("answered", "这个规格的到货提醒已取消。" if current else "这个规格当前没有待生效的到货提醒。")
    if current and current["expires_at"] > await now(conn) and current["binding_version"] == current["current_version"] and current["member_id"]:
        return result("answered", "这个规格的到货提醒已登记。", reason="ALREADY_SUBSCRIBED")
    if current:
        await cancel_subscription(conn, current["id"])
    binding = await one(conn, """
        SELECT id,version FROM member_bindings WHERE connection_id=:connection AND sender_external_id=:sender
    """, connection=connection, sender=sender)
    await execute(conn, """
        INSERT INTO subscriptions (id,profile_id,binding_id,binding_version,connection_id,group_id,product_id,
            product_name,color,size,event_id,expires_at)
        VALUES (:id,:profile,:binding,:version,:connection,:group_id,:product_id,:name,:color,:size,:event,now()+interval '90 days')
    """, id=uuid4(), binding=binding["id"], version=binding["version"], connection=connection,
         group_id=event["conversation"]["external_id"] if event["conversation"]["kind"] == "group" else None,
         name=command["product_name"], event=event_id, **params)
    return result("answered", "这个规格的到货提醒已登记，有效期 90 天。")


async def now(conn):
    return (await one(conn, "SELECT now() AS at"))["at"]
