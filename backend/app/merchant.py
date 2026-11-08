import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Generic, Literal, Protocol, TypeVar

Operation = Literal["identity", "catalog", "inventory", "order", "promotion", "eligibility"]
Status = Literal["OK", "NOT_FOUND", "FORBIDDEN", "UNAVAILABLE", "TEMPORARY_FAILURE", "CONFLICT", "EXPIRED", "INELIGIBLE"]
T = TypeVar("T")
OPERATIONS: tuple[Operation, ...] = ("identity", "catalog", "inventory", "order", "promotion", "eligibility")
ORDER_ID = re.compile(r"(?<![A-Z0-9-])ORD-[A-Z0-9]+(?:-[A-Z0-9]+)*(?![A-Z0-9-])", re.IGNORECASE)


@dataclass(frozen=True)
class Reading(Generic[T]):
    code: Status
    source: str
    checked_at: datetime
    value: T | None = None
    public_reference: str | None = None

    def evidence(self, operation: Operation):
        return {"operation": operation, "result_code": self.code, "source": self.source,
                "public_reference": self.public_reference, "checked_at": self.checked_at.isoformat()}


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    aliases: tuple[str, ...]
    use_cases: tuple[str, ...]
    description: str
    price_cents: int


@dataclass(frozen=True)
class Variant:
    product_id: str
    color: str
    size: str
    quantity: int


@dataclass(frozen=True)
class Order:
    id: str
    product: str
    status: str
    tracking_summary: str
    mismatch_confirmed: bool = False


@dataclass(frozen=True)
class Offer:
    id: str
    name: str
    public_rule: str
    product_ids: tuple[str, ...]


class Merchant(Protocol):
    mode: str
    source: str
    available: tuple[Operation, ...]

    async def verify(self, proof: str) -> Reading[str]: ...
    async def catalog(self, names: list[str]) -> Reading[list[Product]]: ...
    async def inventory(self, product_id: str, color: str | None, size: str | None) -> Reading[list[Variant]]: ...
    async def orders(self, member_id: str, order_id: str | None) -> Reading[list[Order]]: ...
    async def promotion(self, name: str | None) -> Reading[Offer]: ...
    async def eligibility(self, member_id: str, offer_id: str) -> Reading[bool]: ...


PRODUCTS = (
    Product("wind-top", "风行跑步上衣", ("风行上衣", "风行跑步衫"), ("跑步", "日常"),
            "轻薄透气，适合日常跑步。", 19900),
    Product("trail-vest", "越野训练背心", ("越野背心",), ("越野", "跑步", "训练"),
            "无袖活动空间大，适合越野和高强度训练。", 29900),
    Product("light-belt", "轻量跑步腰包", ("轻量腰包", "跑步腰包"), ("跑步", "越野"),
            "适合收纳小件随身物品。", 9900),
)
VARIANTS = (
    Variant("wind-top", "黑色", "M", 7), Variant("wind-top", "黑色", "L", 0),
    Variant("wind-top", "蓝色", "L", 4), Variant("trail-vest", "黑色", "M", 3),
    Variant("wind-top", "黑色", "XL", 0),
    Variant("trail-vest", "白色", "L", 2),
)
ORDERS = {
    "demo-a": (
        Order("ORD-DEMO-A101", "风行跑步上衣", "运输中", "包裹已离开发货城市。"),
        Order("ORD-DEMO-A102", "越野训练背心", "已签收", "物流显示已签收。"),
    ),
    "demo-b": (Order("ORD-DEMO-B201", "越野训练背心", "已签收", "已核实配送商品不符。", True),),
}
MEMBER_DAY = Offer("member-day", "会员日活动", "指定款满 199 元可用会员日券，每单限一张，不与其他优惠叠加。",
                   ("wind-top", "trail-vest"))


class FixtureMerchant:
    mode = "fixture"
    source = "fixture-commerce"
    available = OPERATIONS

    def result(self, code, value=None, ref=None):
        return Reading(code, self.source, datetime.now(UTC), value, ref)

    async def verify(self, proof):
        members = {"wp02-proof-a": "demo-a", "wp02-proof-b": "demo-b", "wp02-proof-c": "demo-c"}
        if proof in members:
            return self.result("OK", members[proof])
        return self.result("FORBIDDEN")

    async def catalog(self, names):
        matches = [item for item in PRODUCTS if not names or
                   any(name.strip() in (item.id, item.name, *item.aliases) for name in names)]
        return self.result("OK", matches) if matches else self.result("NOT_FOUND")

    async def inventory(self, product_id, color, size):
        if product_id == "light-belt":
            return self.result("TEMPORARY_FAILURE", ref=product_id)
        matches = [item for item in VARIANTS if item.product_id == product_id
                   and (not color or item.color == color) and (not size or item.size == size)]
        return self.result("OK", matches, product_id) if matches else self.result("NOT_FOUND", ref=product_id)

    async def orders(self, member_id, order_id):
        owned = ORDERS.get(member_id, ())
        if order_id is None:
            return self.result("OK", list(owned)) if owned else self.result("NOT_FOUND")
        matches = [order for order in owned if order.id == order_id]
        if matches:
            return self.result("OK", matches)
        if any(order.id == order_id for orders in ORDERS.values() for order in orders):
            return self.result("FORBIDDEN")
        return self.result("NOT_FOUND")

    async def promotion(self, name):
        if name in (None, "member-day", "会员日", "会员日活动", "会员日券", "当前活动"):
            return self.result("OK", MEMBER_DAY, MEMBER_DAY.id)
        if name in ("春季旧活动", "spring-old"):
            return self.result("EXPIRED", ref="spring-old")
        if name in ("状态冲突活动", "conflicting-offer"):
            return self.result("CONFLICT", ref="conflicting-offer")
        return self.result("NOT_FOUND")

    async def eligibility(self, member_id, offer_id):
        if offer_id != MEMBER_DAY.id:
            return self.result("NOT_FOUND", ref=offer_id)
        if member_id == "demo-c":
            return self.result("TEMPORARY_FAILURE", ref=offer_id)
        return self.result("OK", True, offer_id) if member_id == "demo-a" else self.result("INELIGIBLE", False, offer_id)


class UnconfiguredMerchant:
    mode = "unconfigured"
    source = "not_configured"
    available: tuple[Operation, ...] = ()

    async def unavailable(self, *args):
        return Reading("UNAVAILABLE", self.source, datetime.now(UTC))

    verify = catalog = inventory = orders = promotion = eligibility = unavailable


def merchant_for_connection(connection_id: str) -> Merchant:
    if (connection_id == "fixture-channel" and os.getenv("DEV_EVENT_TOKEN")
            and os.getenv("COMMERCE_MODE") == "fixture"):
        return FixtureMerchant()
    return UnconfiguredMerchant()


def official_service_url():
    return os.getenv("OFFICIAL_SERVICE_URL", "").strip()
