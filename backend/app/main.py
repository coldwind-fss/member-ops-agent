import logging
import os
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from .auth import router as auth_router
from .activity_api import router as activity_router
from .business_events import router as business_event_router
from .commerce_api import router as commerce_router
from .database import create_database_engine, database_is_ready, expected_migration_heads
from .http import install_http_handlers
from .ingress import router as ingress_router
from .insight_api import router as insight_router
from .knowledge_api import router as knowledge_router
from .member_api import router as member_router
from .operations import router as operations_router
from .outreach_api import router as outreach_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.migration_heads = expected_migration_heads()
    engine = create_database_engine()
    app.state.database = engine
    app.state.login_attempts = deque()
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(title="私域运营助手 API", lifespan=lifespan)
install_http_handlers(app)
app.include_router(auth_router, prefix="/api/v1")
app.include_router(ingress_router, prefix="/api/v1")
app.include_router(operations_router, prefix="/api/v1")
app.include_router(commerce_router, prefix="/api/v1")
app.include_router(member_router, prefix="/api/v1")
app.include_router(activity_router, prefix="/api/v1")
app.include_router(knowledge_router, prefix="/api/v1")
app.include_router(insight_router, prefix="/api/v1")
app.include_router(outreach_router, prefix="/api/v1")
app.include_router(business_event_router, prefix="/api/v1")


@app.get("/api/v1/health/live")
async def liveness():
    return {"status": "ok"}


@app.get("/api/v1/health/ready")
async def readiness(request: Request):
    request_id = request.state.request_id
    try:
        ready = await database_is_ready(request.app.state.database, request.app.state.migration_heads)
    except SQLAlchemyError as error:
        logger.warning("Database readiness failed [%s]: %s", request_id, type(error).__name__)
        ready = False
    if ready:
        return {"status": "ok"}
    return JSONResponse(
        {
            "code": "DEPENDENCY_UNAVAILABLE",
            "message": "数据库未就绪，请检查连接和迁移状态。",
            "request_id": request_id,
        },
        status_code=503,
    )


if frontend := os.getenv("PRIVATE_OPS_FRONTEND_DIR"):
    build = Path(frontend)
    app.mount("/assets", StaticFiles(directory=build / "assets"), name="frontend-assets")

    @app.get("/", include_in_schema=False)
    async def workspace():
        return FileResponse(build / "index.html", headers={"Cache-Control": "no-cache"})
