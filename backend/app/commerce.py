import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from .identity import get_binding
from .merchant import ORDER_ID, PRODUCTS, merchant_for_connection, official_service_url

COMMERCE_KINDS = {"inventory_query", "product_advice", "public_promotion", "personal_query"}
SIZES = re.compile(r"(?<![A-Za-z0-9])(?:XXL|XL|XS|S|M|L)(?![A-Za-z0-9])", re.I)
COLOR = re.compile(r"(黑|白|蓝|红|灰|绿)(?:色|款)")
BUDGET = re.compile(r"(?:预算(?:改(?:成|到|为)?|提高到|降低到|是|为)?\s*|[¥￥]\s*)(\d{1,6}(?:\.\d{1,2})?)|(\d{1,6}(?:\.\d{1,2})?)\s*元(?:以内|以下|之内|左右)")
PURPOSES = ("越野", "跑步", "训练", "日常", "健身")
OFFERS = ("春季旧活动", "状态冲突活动", "会员日活动", "会员日券", "会员日")
PERSONAL_OFFER = re.compile(r"我的(?:优惠券|券|权益)|我(?:能领|可以领|能参加|有资格)|我(?:是否)?有.{0,8}(?:券|权益|资格)|本人(?:优惠|权益|资格)")
ORDER_SELECTION = re.compile(r"(?:选|查|看)?第([一二三四五1-5])(?:笔|单|个)")


class CommerceHints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_names: list[str] = Field(default_factory=list, max_length=3)
    color: str | None = None
    size: str | None = None
    use_case: str | None = None
    promotion_name: str | None = None
    order_id: str | None = None
    personal_kind: Literal["order", "eligibility"] | None = None


def fixture_hints(text):
    names = [item.name for item in PRODUCTS if any(name in text for name in (item.name, *item.aliases))]
    return CommerceHints(product_names=names, personal_kind="eligibility" if PERSONAL_OFFER.search(text) else None)


def is_commerce_followup(text):
    return bool(ORDER_SELECTION.search(text) or ORDER_ID.search(text) or BUDGET.search(text)
                or COLOR.search(text) or SIZES.search(text))


def has_shopping_use(text):
    return len(text) <= 32 and any(purpose in text for purpose in PURPOSES)


def safe_preview(text, conversation_kind):
    if conversation_kind == "private":
        return "会员私密消息"
    return ORDER_ID.sub("[订单编号已隐藏]", text)


def checks(operation, code, source="application"):
    return {"operation": operation, "result_code": code, "source": source,
            "public_reference": None, "checked_at": datetime.now(UTC).isoformat()}


def result(outcome, message, *, reason=None, evidence=None, context=None, subject=None, authorization=None):
    return {"outcome": outcome, "answer": message, "reason": reason, "evidence": [],
            "checks": evidence or [], "commerce_context": context,
            "public_subject": subject, "authorization": authorization}


def money(cents):
    return f"{Decimal(cents) / 100:.2f}".rstrip("0").rstrip(".") + " 元"


def failure_message(code, entity):
    return {
        "NOT_FOUND": f"没有查到匹配的{entity}，请核对名称或规格。",
        "UNAVAILABLE": f"{entity}接口尚未接入，当前无法确认。",
        "TEMPORARY_FAILURE": f"{entity}接口暂时不可用，当前无法确认。",
        "CONFLICT": f"{entity}的规则和状态存在冲突，暂时无法确认，可以回复「找人工」。",
        "EXPIRED": f"该{entity}已经结束。",
    }.get(code, f"当前无法确认{entity}。")


def build_context(text, hints, previous, public_reference, *, guide):
    state = {**previous}
    if re.search(r"(?:替|帮|给)(?:朋友|别人|家人|同事)|代购", text):
        state = {key: state[key] for key in ("products",) if key in state}
        state["shopping_for"] = "other"
    elif re.search(r"(?:给|我)(?:我)?自己(?:买|用|穿)", text) and state.get("shopping_for") == "other":
        state = {key: state[key] for key in ("products",) if key in state}
    mentioned = [name for name in hints.product_names if name in text]
    if not mentioned and hints.product_names:
        mentioned = fixture_hints(text).product_names
    if mentioned:
        state["products"] = mentioned[:2]
    elif public_reference and public_reference not in state.get("products", []):
        state["products"] = [public_reference]
    if color := COLOR.search(text):
        state["color"] = color.group(1) + "色"
    elif hints.color and hints.color in text:
        state["color"] = hints.color
    if size := SIZES.search(text):
        state["size"] = size.group(0).upper()
    elif hints.size and hints.size in text:
        state["size"] = hints.size
    if budget := BUDGET.search(text):
        state["budget_cents"] = int(Decimal(budget.group(1) or budget.group(2)) * 100)
    use_case = next((item for item in PURPOSES if item in text), None)
    if use_case and guide:
        state["use_case"] = use_case
    elif hints.use_case and hints.use_case in text and guide:
        state["use_case"] = hints.use_case
    offer_name = next((item for item in OFFERS if item in text), None)
    if offer_name or (hints.promotion_name and hints.promotion_name in text):
        state["promotion_name"] = offer_name or hints.promotion_name
    return state


class PublicState(TypedDict, total=False):
    kind: str
    connection: str
    context: dict
    products: list
    evidence: list[dict]
    outcome: dict


def build_commerce_graph():
    from langchain_core.tools import tool
    from langgraph.graph import END, START, StateGraph

    @tool
    async def read_catalog(connection: str, names: list[str]):
        """Read public catalog records for the requested products."""
        return await merchant_for_connection(connection).catalog(names)

    @tool
    async def read_inventory(connection: str, product_id: str, color: str | None, size: str | None):
        """Read current stock for an explicitly selected product and optional variant."""
        return await merchant_for_connection(connection).inventory(product_id, color, size)

    @tool
    async def read_promotion(connection: str, name: str | None):
        """Read the public rule and current state for a promotion."""
        return await merchant_for_connection(connection).promotion(name)

    async def catalog(state):
        ctx, kind = state["context"], state["kind"]
        names = ctx.get("products", [])
        if kind == "inventory_query" and len(names) != 1:
            return {"outcome": result("clarification", "请告诉我具体款式，也可以一起提供颜色和尺码。",
                                      reason="MISSING_PRODUCT", context=ctx)}
        if kind == "product_advice" and not names and not ctx.get("use_case"):
            return {"outcome": result("clarification", "你主要用来跑步、越野还是训练？有预算上限也可以一起告诉我。",
                                      reason="MISSING_USE_CASE", context=ctx)}
        if kind == "public_promotion" and not names:
            return {"products": [], "evidence": []}
        reading = await read_catalog.ainvoke({"connection": state["connection"], "names": names})
        evidence = [reading.evidence("catalog")]
        if reading.code != "OK":
            return {"outcome": result("unavailable", failure_message(reading.code, "商品"),
                                      reason=reading.code, evidence=evidence, context=ctx)}
        found = reading.value
        if kind == "product_advice":
            if ctx.get("use_case") and not names:
                found = [p for p in found if ctx["use_case"] in p.use_cases]
            if "budget_cents" in ctx:
                found = [p for p in found if p.price_cents <= ctx["budget_cents"]]
            if not found:
                return {"outcome": result("clarification", "当前没有找到符合用途和预算的商品。你愿意调整预算或款式吗？",
                                          reason="NO_BUDGET_MATCH", evidence=evidence, context=ctx)}
        return {"products": found[:2], "evidence": evidence}

    async def render(state):
        ctx, kind = state["context"], state["kind"]
        products, evidence = state["products"], list(state["evidence"])
        connection = state["connection"]
        subject = products[0].name if len(products) == 1 else None
        chunks = []
        if kind != "public_promotion":
            for product in products:
                reading = await read_inventory.ainvoke({"connection": connection, "product_id": product.id,
                                                        "color": ctx.get("color"), "size": ctx.get("size")})
                evidence.append(reading.evidence("inventory"))
                if reading.code == "OK":
                    available = [f"{v.color} {v.size} 有货" for v in reading.value if v.quantity > 0]
                    unavailable = [f"{v.color} {v.size} 暂时缺货" for v in reading.value if v.quantity == 0]
                    stock = "，".join(available + unavailable)
                else:
                    stock = failure_message(reading.code, "库存")
                if kind == "inventory_query":
                    return {"outcome": result("answered" if reading.code == "OK" else "unavailable",
                        f"{product.name}：{stock}。当前标价 {money(product.price_cents)}。",
                        reason=None if reading.code == "OK" else reading.code, evidence=evidence, context=ctx, subject=subject)}
                chunks.append(f"{product.name}（{money(product.price_cents)}）：{product.description} {stock}。")
        if kind == "public_promotion" or ctx.get("promotion_name"):
            offer = await read_promotion.ainvoke({"connection": connection, "name": ctx.get("promotion_name")})
            evidence.append(offer.evidence("promotion"))
            if offer.code == "OK":
                chunks.append(f"{offer.value.name}：{offer.value.public_rule}")
                if products:
                    chunks.extend(f"{p.name}{'在' if p.id in offer.value.product_ids else '不在'}适用商品范围内。" for p in products)
            elif kind == "public_promotion":
                return {"outcome": result("answered" if offer.code == "EXPIRED" else "unavailable",
                    failure_message(offer.code, "活动"), reason=offer.code, evidence=evidence, context=ctx, subject=subject)}
            else:
                chunks.append(failure_message(offer.code, "优惠"))
        if link := official_service_url():
            chunks.append(f"办理或购买请前往品牌官方入口：{link}")
        return {"outcome": result("answered", "\n".join(chunks), evidence=evidence, context=ctx, subject=subject)}

    graph = StateGraph(PublicState)
    graph.add_node("read_commerce_catalog", catalog)
    graph.add_node("verify_stock_and_offer", render)
    graph.add_edge(START, "read_commerce_catalog")
    graph.add_conditional_edges("read_commerce_catalog",
                                lambda state: END if "outcome" in state else "verify_stock_and_offer")
    graph.add_edge("verify_stock_and_offer", END)
    return graph.compile()


def is_personal_promotion(text, hints):
    return bool(PERSONAL_OFFER.search(text) or hints.personal_kind == "eligibility")


def order_reference(text, hints, context):
    found = ORDER_ID.search(text)
    if found:
        return found.group().upper()
    if hints.order_id and hints.order_id in text:
        return hints.order_id
    if match := ORDER_SELECTION.search(text):
        digit = match.group(1)
        index = int(digit) if digit.isdigit() else "一二三四五".index(digit) + 1
        # The displayed choices are safe identifiers only, and authorization is checked again below.
        choices = context.get("order_choices", [])
        return choices[index - 1] if len(choices) >= index else None
    return context.get("selected_order") if re.search(r"这单|那单|这一笔|刚才", text) else None


async def personal_query(engine, event, intent, context, text):
    merchant = merchant_for_connection(event["connection_id"])
    identity = await get_binding(engine, event["connection_id"], event["sender_external_id"])
    link = official_service_url()
    if not identity:
        message = "查询本人订单或权益前，请先完成会员身份核验。"
        message += f" 官方服务入口：{link}" if link else " 当前核验入口未配置，可回复「找人工」。"
        return result("unavailable", message, reason="UNBOUND", evidence=[checks("identity", "UNBOUND")])
    private = event["conversation"]["kind"] == "private"
    right_channel = private and identity["private_external_id"] == event["conversation"]["external_id"]
    if not right_channel:
        reason = "PRIVATE_CHANNEL_REQUIRED"
        if not identity["private_external_id"] and not link and intent.condition is None:
            return result("handoff", None, reason=reason, evidence=[checks("identity", reason)],
                          authorization={"id": str(identity["id"]), "version": identity["version"]})
        if identity["private_external_id"]:
            message = "请在已核验的私密会话向助手查询本人订单或权益。"
        else:
            message = f"当前私密查询入口不可用。请前往品牌官方入口：{link}" if link else "当前私密查询入口不可用，可回复「找人工」。"
        return result("unavailable", message, reason=reason, evidence=[checks("identity", reason)])

    auth = {"id": str(identity["id"]), "version": identity["version"]}
    if context.get("order_grant") != auth:
        context = {}
    evidence = [checks("identity", "VERIFIED")]
    member = identity["member_id"]
    hints = intent.commerce or CommerceHints()
    if is_personal_promotion(text, hints) and intent.condition is None:
        offer_name = next((name for name in OFFERS if name in text), hints.promotion_name)
        offer = await merchant.promotion(offer_name)
        evidence.append(offer.evidence("promotion"))
        if offer.code != "OK":
            return result("answered" if offer.code == "EXPIRED" else "unavailable", failure_message(offer.code, "活动"),
                          reason=offer.code, evidence=evidence, authorization=auth)
        eligibility = await merchant.eligibility(member, offer.value.id)
        evidence.append(eligibility.evidence("eligibility"))
        if eligibility.code in {"OK", "INELIGIBLE"}:
            message = f"你符合{offer.value.name}的当前参与资格。" if eligibility.value else f"你目前不符合{offer.value.name}的参与资格。"
            return result("answered", message, reason=None if eligibility.value else "INELIGIBLE",
                          evidence=evidence, authorization=auth)
        return result("unavailable", failure_message(eligibility.code, "会员权益"), reason=eligibility.code,
                      evidence=evidence, authorization=auth)
    order_id = order_reference(text, hints, context)
    if not order_id and ORDER_SELECTION.search(text):
        return result("clarification", "该序号没有对应的本人订单，请先查询订单列表，或直接回复订单编号。",
                      reason="ORDER_SELECTION_REQUIRED", evidence=evidence, authorization=auth)
    condition = context.get("pending_condition") if order_id else None
    if intent.condition:
        condition = "mismatch" if re.search(r"寄错|发错|货不对|错发|商品不符", text) else "unspecified"
    reading = await merchant.orders(member, order_id)
    evidence.append(reading.evidence("order"))
    if reading.code in {"FORBIDDEN", "NOT_FOUND"} or (reading.code == "OK" and not reading.value):
        return result("unavailable", "未查到可供你查询的订单。请核对编号，或回复「找人工」。",
                      reason="FORBIDDEN" if reading.code == "FORBIDDEN" else "NOT_FOUND", evidence=evidence, authorization=auth)
    if reading.code != "OK":
        return result("unavailable", failure_message(reading.code, "订单物流") + " 如需协助，可回复「找人工」。",
                      reason=reading.code, evidence=evidence, authorization=auth)
    orders = reading.value
    if len(orders) > 1:
        choices = "\n".join(f"{index}. {order.id} · {order.product}" for index, order in enumerate(orders[:5], 1))
        return result("clarification", f"查到多笔本人订单，请回复订单编号或第几笔：\n{choices}",
                      reason="ORDER_SELECTION_REQUIRED", evidence=evidence,
                      context={"order_choices": [order.id for order in orders[:5]], "order_grant": auth,
                               "pending_condition": condition}, authorization=auth)
    order = orders[0]
    if condition:
        if condition != "mismatch":
            return result("clarification", "请说明需要核实什么情况，再决定是否转人工。",
                          reason="UNVERIFIED_CONDITION", evidence=evidence, authorization=auth)
        if order.mismatch_confirmed:
            return result("handoff", None, reason="CONFIRMED_ORDER_MISMATCH", evidence=evidence,
                          context={"selected_order": order.id, "order_grant": auth}, authorization=auth)
        return result("answered", "当前订单记录没有确认寄错，尚未转人工。如有疑问可直接回复「找人工」。",
                      reason="CONDITION_NOT_MET", evidence=evidence,
                      context={"selected_order": order.id, "order_grant": auth}, authorization=auth)
    return result("answered", f"订单 {order.id}（{order.product}）：{order.status}。{order.tracking_summary}",
                  evidence=evidence, context={"selected_order": order.id, "order_grant": auth}, authorization=auth)


async def commerce_outcome(engine, graph, event, intent, matter, public_reference, text, *, preferences=None):
    context = matter["commerce_context"]
    if intent.kind == "personal_query" or intent.condition == "requires_business_verification" or (
        intent.kind == "public_promotion" and is_personal_promotion(text, intent.commerce or CommerceHints())
    ):
        return await personal_query(engine, event, intent, context, text)
    current = build_context(text, intent.commerce or CommerceHints(), context, public_reference,
                            guide=intent.kind == "product_advice")
    read_context = {**(preferences or {}), **current} if current.get("shopping_for") != "other" else current
    output = await graph.ainvoke({"kind": intent.kind, "connection": event["connection_id"], "context": read_context})
    outcome = output["outcome"]
    outcome["commerce_context"] = current
    outcome["used_preferences"] = read_context != current
    return outcome
