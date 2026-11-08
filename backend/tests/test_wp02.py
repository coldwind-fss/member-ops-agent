"""WP02 acceptance through the API, Worker, and an isolated PostgreSQL database."""
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import psycopg
import pytest
from sqlalchemy.engine import make_url

from test_wp01 import BASE, DEV_TOKEN, ORIGIN, PASSWORD, eventually, prepare, process


def test_wp02_commerce_query(tmp_path):
    database = os.getenv("WP02_TEST_DATABASE_URL")
    if not database:
        pytest.skip("Set WP02_TEST_DATABASE_URL to a dedicated PostgreSQL test database")
    url = make_url(database)
    assert url.database and url.database.endswith("_test")
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    # Observe what the fixture actually receives at the outbound boundary without retaining bodies in the app.
    (tmp_path / "observe_delivery.py").write_text(
        "from app import delivery, worker\n"
        "import json\n"
        "send = delivery.deliver\n"
        "async def observe(item):\n"
        "    if item['private_body']:\n"
        "        print('PRIVATE ' + json.dumps([str(item['matter_id']), item['private_body']]), flush=True)\n"
        "    return await send(item)\n"
        "delivery.deliver = observe\n"
        "raise SystemExit(worker.main())\n", encoding="utf-8",
    )
    env = {**os.environ, "DATABASE_URL": database, "DEV_EVENT_TOKEN": DEV_TOKEN, "AGENT_MODEL_MODE": "fixture",
           "COMMERCE_MODE": "fixture", "NOTIFICATION_MODE": "fixture", "FIXTURE_REPLY_RESULT": "sent",
           "OPERATOR_BOOTSTRAP_PASSWORD": PASSWORD, "OPERATOR_ORIGIN": ORIGIN, "PYTHONUTF8": "1",
           "PYTHONPATH": str(tmp_path)}
    prepare(env)
    scope = "wp02-" + uuid4().hex
    a, b, c = (f"{scope}-{member}" for member in ("a", "b", "c"))
    private_a, private_b = f"{a}-private", f"{b}-private"
    headers = {"X-Dev-Token": DEV_TOKEN}
    worker_log = tmp_path / "worker.log"
    queued = {}

    with process("app.server", env, tmp_path / "api.log"), httpx.Client(base_url=BASE, timeout=6) as client:
        eventually(lambda: client.get("/health/ready").status_code == 200)
        assert client.get("/commerce/bindings").status_code == 401
        response = client.post("/auth/login", headers={"Origin": ORIGIN},
                               json={"username": "wp01_fixture", "password": PASSWORD})
        assert response.status_code == 200, response.text

        def change(member, proof=None, private=None):
            data = {"connection_id": "fixture-channel", "sender_external_id": member,
                    "action": "bind" if proof else "unbind"}
            if proof:
                data.update(proof=proof, private_external_id=private)
            return client.post("/dev/commerce/bindings", headers=headers, json=data)

        def post(member, text, *, private=None, quote=None):
            uid = uuid4().hex
            response = client.post("/dev/inbound-events", headers=headers, json={
                "connection_id": "fixture-channel", "external_event_id": uid, "event_type": "message.received",
                "occurred_at": datetime.now(UTC).isoformat(), "sender_external_id": member,
                "conversation": {"kind": "private" if private else "group", "external_id": private or scope},
                "message": {"external_id": uid, "text": text, "mentions_bot": not private,
                            "reply_to_external_id": quote},
            })
            assert response.status_code == 202, response.text
            return response.json()["event_id"]

        def reply_for(event_id):
            with psycopg.connect(dsn) as conn:
                return conn.execute("""
                    SELECT m.matter_id::text,m.preview,m.external_id,d.id,d.operation_key
                    FROM messages m JOIN deliveries d ON d.message_id=m.id
                    JOIN messages input ON input.external_id=m.in_reply_to
                      AND input.connection_id=m.connection_id
                      AND input.conversation_external_id=m.conversation_external_id
                    WHERE input.id=%s AND d.state='sent'
                """, (event_id,)).fetchone()

        def actual(row):
            matter_id, preview = row[:2]
            if "（私密内容）" not in preview:
                return preview
            return [body for line in worker_log.read_text(encoding="utf-8").splitlines()
                    if line.startswith("PRIVATE ")
                    for mid, body in [json.loads(line.removeprefix("PRIVATE "))] if mid == matter_id][-1]

        def ask(member, text, **kwargs):
            event_id = post(member, text, **kwargs)
            return eventually(lambda: reply_for(event_id))

        def evidence(matter_id):
            response = client.get(f"/matters/{matter_id}/commerce-checks")
            assert response.status_code == 200, response.text
            return response.json()["items"]

        assert change(a, "invalid", private_a).status_code == 403
        for member, proof, private in ((a, "wp02-proof-a", private_a), (b, "wp02-proof-b", private_b)):
            binding = change(member, proof, private).json()
            assert binding["status"] == "verified" and binding["private_available"]
            assert "member_id" not in binding and "private_external_id" not in binding
        assert client.get("/commerce/capabilities").json()["mode"] == "fixture"

        with process("observe_delivery", env, worker_log):
            assert "阴干" in actual(ask(c, "风行跑步上衣怎么洗？"))
            assert "先完成会员身份核验" in actual(ask(c, "我的订单呢？"))
            group = actual(ask(a, "查订单ORD-DEMO-B201"))
            assert "私密会话" in group and "B201" not in group
            assert "私密会话" in actual(ask(a, "查订单", private=f"{a}-wrong"))

            choices = ask(a, "我的订单物流", private=private_a)
            assert "ORD-DEMO-A101" in actual(choices) and "ORD-DEMO-A102" in actual(choices)
            selected = ask(a, "第二笔", private=private_a, quote=choices[2])
            assert selected[0] == choices[0] and "ORD-DEMO-A102" in actual(selected) and "已签收" in actual(selected)
            forbidden = ask(a, "订单 ORD-DEMO-B201", private=private_a)
            forbidden_body = actual(forbidden)
            absent = ask(a, "订单 ORD-DEMO-MISSING", private=private_a)
            assert actual(absent) == forbidden_body and "B201" not in forbidden_body
            assert any(item["result_code"] == "FORBIDDEN" for item in evidence(forbidden[0]))
            detail = client.get("/matters/" + choices[0]).text
            assert "ORD-DEMO" not in detail and "私密内容" in detail

            inventory_a = post(a, "风行跑步上衣黑色 M 有货吗？")
            inventory_b = post(b, "风行跑步上衣黑色 L 有货吗？")
            assert "黑色 M 有货" in actual(eventually(lambda: reply_for(inventory_a)))
            assert "黑色 L 暂时缺货" in actual(eventually(lambda: reply_for(inventory_b)))
            assert "已经结束" in actual(ask(c, "春季旧活动还有吗？"))
            assert "存在冲突" in actual(ask(c, "状态冲突活动怎么参加？"))
            assert "不在适用商品范围" in actual(ask(c, "轻量跑步腰包能用会员日优惠吗？"))
            assert "不符合" in actual(ask(b, "会员日活动我能参加吗？", private=private_b))
            assert "你符合" in actual(ask(a, "会员日活动我能参加吗？", private=private_a))
            private_c = f"{c}-private"
            assert change(c, "wp02-proof-c", private_c).status_code == 200
            assert "暂时不可用" in actual(ask(c, "会员日活动我能参加吗？", private=private_c))
            assert change(c, "wp02-proof-c").status_code == 200
            fallback = ask(c, "我的订单怎么查？")
            assert client.get("/matters/" + fallback[0]).json()["handoff_id"]
            unavailable = ask(c, "轻量跑步腰包有货吗？")
            assert "接口暂时不可用" in actual(unavailable)
            assert any(item["result_code"] == "TEMPORARY_FAILURE" for item in evidence(unavailable[0]))

            first = ask(c, "帮我推荐一下，预算 150 元")
            assert "主要用来" in actual(first)
            second = ask(c, "跑步", quote=first[2])
            second_body = actual(second)
            assert second[0] == first[0] and "轻量跑步腰包" in second_body and "风行跑步上衣" not in second_body
            third = ask(c, "预算改成 320 元", quote=second[2])
            assert third[0] == first[0] and "风行跑步上衣" in actual(third)
            other = ask(c, "给朋友买，日常穿，预算 120 元", quote=third[2])
            assert other[0] == first[0] and "没有找到" in actual(other)
            mismatch = ask(b, "如果订单 ORD-DEMO-B201 寄错了就找人工", private=private_b)
            handoff = client.get("/matters/" + mismatch[0]).json()
            assert handoff["handoff_id"] and handoff["runs"][-1]["error_code"] == "CONFIRMED_ORDER_MISMATCH"

            for member, private, order in ((a, private_a, "A101"), (b, private_b, "B201")):
                row = ask(member, f"订单 ORD-DEMO-{order}", private=private)
                queued[member] = (row, actual(row))

        # Recreate a persisted reply queued before the binding or matter version changed.
        with psycopg.connect(dsn) as conn:
            for (matter, _, _, delivery_id, key), body in queued.values():
                conn.execute("UPDATE deliveries SET state='pending',private_body=%s WHERE id=%s", (body, delivery_id))
                conn.execute("UPDATE jobs SET state='pending',run_after=now() WHERE operation_key=%s", (key,))
            conn.execute("UPDATE matters SET status='human_handling',version=version+1 WHERE id=%s",
                         (queued[b][0][0],))
        assert change(a).json()["status"] == "unbound"

        def both_cancelled():
            with psycopg.connect(dsn) as conn:
                return all(conn.execute("SELECT state,private_body FROM deliveries WHERE id=%s", (row[3],))
                           .fetchone() == ("cancelled", None) for row, _ in queued.values())

        with process("app.worker", env, tmp_path / "cancellation.log"):
            eventually(both_cancelled)
