from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from .auth import Operator
from .http import ApiError
from .identity import BINDING_FIELDS, change_binding
from .ingress import Input, require_fixture
from .merchant import merchant_for_connection, official_service_url
from .operations import Limit, page
from .store import one

router = APIRouter()
ExternalID = Annotated[str, Field(min_length=1, max_length=255)]


class FixtureBinding(Input):
    action: Literal["bind", "unbind"]
    connection_id: Literal["fixture-channel"]
    sender_external_id: ExternalID
    proof: ExternalID | None = None
    private_external_id: ExternalID | None = None


@router.get("/commerce/capabilities")
async def commerce_capabilities(operator: Operator):
    merchant = merchant_for_connection("fixture-channel")
    return {"mode": merchant.mode, "source": merchant.source, "available": merchant.available,
            "official_service_configured": bool(official_service_url())}


@router.get("/commerce/bindings")
async def commerce_bindings(request: Request, operator: Operator, limit: Limit = 20, cursor: str | None = None):
    return await page(request.app.state.database, "member_bindings", BINDING_FIELDS, {}, limit, cursor)


@router.get("/matters/{matter_id}/commerce-checks")
async def commerce_checks(matter_id: UUID, request: Request, operator: Operator,
                          limit: Limit = 20, cursor: str | None = None):
    engine = request.app.state.database
    async with engine.connect() as conn:
        if not await one(conn, "SELECT id FROM matters WHERE id=:id", id=matter_id):
            raise ApiError(404, "NOT_FOUND", "会话事项不存在。")
    return await page(engine, "commerce_checks", "id,matter_id,operation,result_code,source,public_reference,checked_at",
                      {"matter_id": str(matter_id)}, limit, cursor, order_at="checked_at")


@router.post("/dev/commerce/bindings", dependencies=[Depends(require_fixture)])
async def fixture_binding(data: FixtureBinding, request: Request):
    merchant = merchant_for_connection(data.connection_id)
    if merchant.mode != "fixture":
        raise ApiError(404, "NOT_FOUND", "测试商家核验入口未启用。")
    if data.action == "unbind":
        if data.proof is not None or data.private_external_id is not None:
            raise ApiError(422, "VALIDATION_ERROR", "解绑只需要渠道及发送者标识。")
        member_id = None
    else:
        if data.proof is None:
            raise ApiError(422, "VALIDATION_ERROR", "绑定需要核验凭据。")
        verified = await merchant.verify(data.proof)
        if verified.code != "OK":
            raise ApiError(403, "FORBIDDEN", "未能验证该会员身份。")
        member_id = verified.value
    return await change_binding(request.app.state.database, data.connection_id, data.sender_external_id,
                                member_id, data.private_external_id)
