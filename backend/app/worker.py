import asyncio
import logging
from time import monotonic
from uuid import uuid4

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from sqlalchemy.exc import SQLAlchemyError

from .agent import build_answer_graph, build_handoff_graph, build_intent_graph, model_for_worker
from .business_events import process_business_event
from .commerce import build_commerce_graph
from .conversation import process_inbound, reply
from .database import create_database_engine, database_is_ready, expected_migration_heads, run_with_database_loop
from .delivery import process_delivery
from .insights import expire_sources, publish_daily_reports
from .manage import checkpoint_dsn
from .outreach import process_outreach
from .queue import claim, fence, finish
from .store import execute, one

logger = logging.getLogger(__name__)


def continuation_config(data):
    return {"configurable": {"thread_id": f"handoff:{data['cycle']}"}}


async def prepare_handoff(engine, graph, job):
    data = job["payload"]
    config = continuation_config(data)
    snapshot = await graph.aget_state(config)
    if not snapshot.values:
        await graph.ainvoke({"matter_id": data["matter_id"]}, config)
    async with engine.begin() as conn:
        if await fence(conn, job):
            await finish(conn, job)


async def resume(engine, graph, job):
    data = job["payload"]
    async with engine.connect() as conn:
        candidate = await one(conn, "SELECT version,status FROM matters WHERE id=:id", id=data["matter_id"])
    answer = None
    if candidate and candidate["version"] == data["expected_version"] and candidate["status"] == "open":
        config = continuation_config(data)
        snapshot = await graph.aget_state(config)
        if not snapshot.values:
            await graph.ainvoke({"matter_id": data["matter_id"]}, config)
        answer = snapshot.values.get("answer")
        if not answer:
            result = await graph.ainvoke(Command(resume={"resolution": data["resolution"]}), config)
            answer = result["answer"]
    async with engine.begin() as conn:
        if not await fence(conn, job):
            return
        matter = await one(conn, "SELECT * FROM matters WHERE id=:id FOR UPDATE", id=data["matter_id"])
        if answer and matter and matter["version"] == data["expected_version"] and matter["status"] == "open":
            original = await one(conn, """
                SELECT m.external_id FROM matter_inputs i JOIN messages m ON m.id=i.message_id
                WHERE i.matter_id=:matter ORDER BY m.occurred_at DESC,m.id DESC LIMIT 1
            """, matter=matter["id"])
            await execute(conn, """
                INSERT INTO agent_runs (id,operation_key,matter_id,intents,outcome)
                VALUES (:id,:key,:matter,ARRAY['human_handoff'],'answered')
            """, id=uuid4(), key=job["operation_key"], matter=matter["id"])
            await reply(conn, matter, answer, job["operation_key"],
                        original["external_id"] if original else None)
        await finish(conn, job)


async def work(engine, intent_graph, answer_graph, handoff_graph, commerce_graph, saver):
    next_cleanup = 0
    next_report = 0
    while True:
        try:
            if monotonic() >= next_cleanup:
                next_cleanup = monotonic() + 3600
                await expire_sources(engine, saver)
            if monotonic() >= next_report:
                next_report = monotonic() + 60
                await publish_daily_reports(engine)
            job = await claim(engine)
        except SQLAlchemyError as error:
            logger.warning("Worker 暂时无法领取任务：%s", type(error).__name__)
            await asyncio.sleep(2)
            continue
        if not job:
            await asyncio.sleep(0.4)
            continue
        try:
            if job["kind"] == "inbound":
                await process_inbound(engine, intent_graph, answer_graph, commerce_graph, job)
            elif job["kind"] == "deliver":
                await process_delivery(engine, job)
            elif job["kind"] == "prepare_handoff":
                await prepare_handoff(engine, handoff_graph, job)
            elif job["kind"] == "resume":
                await resume(engine, handoff_graph, job)
            elif job["kind"] == "business_event":
                await process_business_event(engine, job)
            elif job["kind"] == "outreach":
                await process_outreach(engine, job)
            else:
                raise ValueError("Unsupported job kind")
        except Exception as error:
            logger.error("任务失败 [%s]：%s", job["id"], type(error).__name__)
            try:
                async with engine.begin() as conn:
                    if await fence(conn, job):
                        # 发送任务再次领取时先读取发送状态，结果不明时不会直接重发。
                        retry = job["generation"] < 2
                        await finish(conn, job, "pending" if retry else "failed", "JOB_FAILED")
            except SQLAlchemyError:
                logger.warning("无法记录任务失败 [%s]，等待租约恢复。", job["id"])


async def run() -> int:
    engine = create_database_engine()
    try:
        if not await database_is_ready(engine, expected_migration_heads()):
            logger.error("数据库迁移未就绪，请先执行 Alembic 迁移。")
            return 1
        async with engine.connect() as conn:
            checkpoints = await one(conn, "SELECT to_regclass('checkpoints') AS name")
        if not checkpoints["name"]:
            logger.error("图快照表未就绪，请执行 python -m app.manage checkpoints。")
            return 1
        model = model_for_worker()
        async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn(engine)) as saver:
            logger.info("Worker 已连接数据库，等待任务。")
            await work(engine, build_intent_graph(model), build_answer_graph(engine, model, saver),
                       build_handoff_graph(saver), build_commerce_graph(), saver)
        return 0
    except SQLAlchemyError as error:
        logger.error("Worker 无法连接项目数据库：%s", type(error).__name__)
        return 1
    finally:
        await engine.dispose()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        return run_with_database_loop(run())
    except RuntimeError as error:
        logger.error("%s", error)
        return 1
    except KeyboardInterrupt:
        logger.info("Worker 已停止。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
