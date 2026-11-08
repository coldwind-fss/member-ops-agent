import asyncio
import os
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def run_with_database_loop(coroutine):
    # psycopg 的异步连接在 Windows 上需要 Selector 事件循环。
    loop_factory = asyncio.SelectorEventLoop if os.name == "nt" else None
    return asyncio.run(coroutine, loop_factory=loop_factory)


def create_database_engine() -> AsyncEngine:
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("请设置 DATABASE_URL，指向项目专用 PostgreSQL 数据库。")
    try:
        url = make_url(value)
    except ArgumentError:
        raise RuntimeError("DATABASE_URL 格式无效。") from None
    if url.drivername != "postgresql+psycopg" or not url.database:
        raise RuntimeError("DATABASE_URL 必须使用 postgresql+psycopg 并指定数据库。")
    return create_async_engine(
        url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 3, "options": "-c statement_timeout=3000"},
    )


def expected_migration_heads() -> set[str]:
    config_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    return set(ScriptDirectory.from_config(Config(str(config_path))).get_heads())


async def database_is_ready(engine: AsyncEngine, expected_heads: set[str]) -> bool:
    def current_heads(connection):
        return set(MigrationContext.configure(connection).get_current_heads())

    async with engine.connect() as connection:
        return await connection.run_sync(current_heads) == expected_heads
