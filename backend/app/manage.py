import argparse
import getpass
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

from .auth import hasher
from .database import create_database_engine, run_with_database_loop
from .store import execute, one


def checkpoint_dsn(engine):
    return engine.url.set(drivername="postgresql").render_as_string(hide_password=False)


async def manage(args):
    engine = create_database_engine()
    try:
        if args.command == "checkpoints":
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn(engine)) as saver:
                await saver.setup()
            print("LangGraph 快照表已初始化。")
        elif args.command == "operator":
            password = os.getenv("OPERATOR_BOOTSTRAP_PASSWORD") or getpass.getpass("设置运营密码：")
            if len(password) < 12:
                raise SystemExit("请使用至少 12 个字符的密码。")
            async with engine.begin() as conn:
                current = await one(conn, "SELECT id,username FROM operators WHERE singleton FOR UPDATE")
                if current and current["username"] != args.username:
                    raise SystemExit("已存在运营账号。请使用现有账号名更新。")
                operator_id = current["id"] if current else uuid4()
                await execute(conn, """
                    INSERT INTO operators (id,username,display_name,password_hash) VALUES (:id,:username,:name,:password)
                    ON CONFLICT (id) DO UPDATE SET display_name=EXCLUDED.display_name,password_hash=EXCLUDED.password_hash
                """, id=operator_id, username=args.username, name=args.display_name, password=hasher.hash(password))
                await execute(conn, "DELETE FROM operator_sessions WHERE operator_id=:id", id=operator_id)
            print("运营账号已初始化，已有会话已撤销。")
        elif args.command == "seed":
            samples = [
                (1, 1, "风行上衣旧版洗护", "旧版已失效。", "风行跑步上衣", "care",
                 ["风行上衣", "风行跑步衫"], ["洗", "烘干", "保养", "缩水"], datetime(2020, 1, 1, tzinfo=UTC), datetime(2021, 1, 1, tzinfo=UTC)),
                (1, 2, "风行上衣洗护说明", "请用 30℃ 以下冷水轻柔洗涤，洗后阴干，不建议使用烘干机。",
                 "风行跑步上衣", "care", ["风行上衣", "风行跑步衫"], ["洗", "烘干", "保养", "缩水"], datetime(2021, 1, 1, tzinfo=UTC), None),
                (2, 1, "越野背心洗护说明", "建议将背心翻面后用冷水手洗，避免漂白，平铺阴干。",
                 "越野训练背心", "care", ["越野背心"], ["洗", "烘干", "保养"], datetime(2021, 1, 1, tzinfo=UTC), None),
                (3, 1, "通用退换规则", "请在原订单入口提交售后申请。特殊情况由运营核实。",
                 "", "after_sale", [], ["退换", "退货", "换货"], datetime(2021, 1, 1, tzinfo=UTC), None),
                (4, 1, "拾风运动品牌介绍", "拾风运动是项目示例使用的虚构品牌，主营跑步和户外训练用品。",
                 "", "brand", [], ["品牌", "拾风"], datetime(2021, 1, 1, tzinfo=UTC), None),
            ]
            async with engine.begin() as conn:
                for number, version, title, body, product, topic, aliases, keywords, start, end in samples:
                    await execute(conn, """
                        INSERT INTO knowledge (id,version,title,body,product,topic,aliases,keywords,valid_from,valid_until)
                        VALUES (:id,:version,:title,:body,:product,:topic,:aliases,:keywords,:start,:end)
                        ON CONFLICT (id,version) DO NOTHING
                    """, id=UUID(int=number), version=version, title=title, body=body, product=product,
                         topic=topic, aliases=aliases, keywords=keywords, start=start, end=end)
            print("虚构知识样例已加载。")
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="WP01 本机管理命令")
    sub = parser.add_subparsers(dest="command", required=True)
    operator = sub.add_parser("operator")
    operator.add_argument("username")
    operator.add_argument("--display-name", default="品牌运营")
    sub.add_parser("checkpoints")
    sub.add_parser("seed")
    run_with_database_loop(manage(parser.parse_args()))


if __name__ == "__main__":
    main()
