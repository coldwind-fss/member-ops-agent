import logging
from uuid import uuid4

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status, code, message, details=None):
        self.status, self.code, self.message, self.details = status, code, message, details


def install_http_handlers(app):
    def error_response(request, error):
        result = {"code": error.code, "message": error.message, "request_id": request.state.request_id}
        if error.details is not None:
            result["details"] = error.details
        return JSONResponse(result, status_code=error.status, headers={"X-Request-ID": request.state.request_id})

    @app.middleware("http")
    async def request_context(request, call_next):
        request.state.request_id = str(uuid4())
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("Unhandled request failure [%s]", request.state.request_id)
            return error_response(request, ApiError(500, "INTERNAL_ERROR", "服务暂时无法完成请求。"))
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def business_error(request, error):
        return error_response(request, error)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        fields = [".".join(map(str, item["loc"])) for item in error.errors()]
        return error_response(request, ApiError(422, "VALIDATION_ERROR", "请求字段不符合约定。", {"fields": fields}))

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        logger.warning("Database request failed [%s]: %s", request.state.request_id, type(error).__name__)
        return error_response(request, ApiError(503, "DEPENDENCY_UNAVAILABLE", "数据库暂不可用。"))

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        code = "NOT_FOUND" if error.status_code == 404 else "BAD_REQUEST"
        return error_response(request, ApiError(error.status_code, code, "请求无法处理。"))
