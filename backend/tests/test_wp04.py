"""Verify scheduled work and delivery outcomes using the API and a real Worker."""
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import psycopg
import pytest
from sqlalchemy.engine import make_url

from test_wp01 import BASE, DEV_TOKEN, ORIGIN, PASSWORD, eventually, prepare, process


def test_wp04_operations_and_delivery(tmp_path):
    database = os.getenv("WP04_TEST_DATABASE_URL")
    if not database:
        pytest.skip("Set WP04_TEST_DATABASE_URL to a dedicated PostgreSQL test database")
    url = make_url(database)
    assert url.database and url.database.endswith("_test")
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    env = {**os.environ, "DATABASE_URL": database, "DEV_EVENT_TOKEN": DEV_TOKEN,
           "AGENT_MODEL_MODE": "fixture", "COMMERCE_MODE": "fixture", "NOTIFICATION_MODE": "fixture",
           "FIXTURE_REPLY_RESULT": "sent", "OPERATOR_BOOTSTRAP_PASSWORD": PASSWORD,
           "OPERATOR_ORIGIN": ORIGIN, "PYTHONUTF8": "1", "PYTHONPATH": str(tmp_path)}
    (tmp_path / "wp04_worker.py").write_text(
        "from app import delivery, worker\n"
        "send = delivery.FixtureReplyChannel.send\n"
        "attempts = set()\n"
        "async def fixture(self, **data):\n"
        "    if data['text'] == '等待回执': return 'accepted', 'fixture-accept-1'\n"
        "    if data['text'] == '结果不明': return 'unknown', None\n"
        "    if data['text'] == '暂时失败后恢复' and data['operation_key'] not in attempts:\n"
        "        attempts.add(data['operation_key'])\n"
        "        return 'retryable', None\n"
        "    return await send(self, **data)\n"
        "delivery.FixtureReplyChannel.send = fixture\n"
        "raise SystemExit(worker.main())\n", encoding="utf-8",
    )
    prepare(env)
    scope = "wp04-" + uuid4().hex[:8]
    a, b, c, newcomer = (f"{scope}-{label}" for label in ("a", "b", "c", "new"))
    headers = {"X-Dev-Token": DEV_TOKEN}

    with process("app.server", env, tmp_path / "api.log"), httpx.Client(base_url=BASE, timeout=8) as client:
        eventually(lambda: client.get("/health/ready").status_code == 200)
        assert client.get("/outreach/plans").status_code == 401
        login = client.post("/auth/login", headers={"Origin": ORIGIN}, json={"username": "wp01_fixture", "password": PASSWORD})
        assert login.status_code == 200, login.text
        csrf = login.json()["csrf_token"]

        def get(path, **params):
            response = client.get(path, params=params)
            assert response.status_code == 200, response.text
            return response.json()

        def post(path, data, *, dev=False):
            request_headers = headers if dev else {"Origin": ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex}
            response = client.post(path, json=data, headers=request_headers)
            assert response.is_success, response.text
            return response.json()

        def configure(*, replies=True, outreach=True):
            return post("/outreach/groups", {"connection_id": "fixture-channel", "group_id": scope, "reply_enabled": replies,
                                             "analysis_enabled": True, "outreach_enabled": outreach,
                                             "welcome_text": "欢迎加入运动品牌会员群", "care_text": "收到商品后可查看保养提示。"})

        def join_fixture(member):
            post("/dev/group-memberships", {"connection_id": "fixture-channel", "group_id": scope,
                                           "sender_external_id": member, "active": True}, dev=True)

        def emit(kind, **payload):
            event = {"connection_id": "fixture-channel", "external_event_id": uuid4().hex, "event_type": kind,
                     "occurred_at": datetime.now(UTC).isoformat(), **payload}
            row = post("/dev/business-events", event, dev=True)
            return event, row["event_id"]

        def inbound(member, text, *, image=False):
            uid = uuid4().hex
            return post("/dev/inbound-events", {
                "connection_id": "fixture-channel", "external_event_id": uid, "event_type": "message.received",
                "occurred_at": datetime.now(UTC).isoformat(), "sender_external_id": member,
                "conversation": {"kind": "group", "external_id": scope},
                "message": {"external_id": uid, "text": text, "mentions_bot": True, "reply_to_external_id": None,
                            "image_present": image},
            }, dev=True)["event_id"]

        def done(event_id, prefix):
            with psycopg.connect(dsn) as conn:
                row = conn.execute("SELECT state,last_error FROM jobs WHERE operation_key=%s", (prefix + event_id,)).fetchone()
                return row if row and row[0] == "done" else None

        def reply(event_id):
            with psycopg.connect(dsn) as conn:
                row = conn.execute("""
                    SELECT msg.preview FROM messages msg JOIN messages inbound ON inbound.external_id=msg.in_reply_to
                    WHERE inbound.id=%s AND msg.delivery_state='sent'
                """, (event_id,)).fetchone()
                return row[0] if row else None

        def ask(member, text):
            event = inbound(member, text)
            return eventually(lambda: reply(event))

        def rows(**match):
            return [row for row in get("/outreach/records", limit=100)["items"]
                    if row["group_id"] == scope and all(row[key] == value for key, value in match.items())]

        def record(**match):
            return eventually(lambda: next(iter(rows(**match)), None))

        def force_due(record_key):
            with psycopg.connect(dsn) as conn:
                conn.execute("UPDATE jobs SET run_after=now() WHERE operation_key=%s", ("send:" + record_key,))

        def previous_day(record_id):
            with psycopg.connect(dsn) as conn:
                conn.execute("UPDATE outreach_records SET updated_at=now()-interval '1 day',dispatch_at=now()-interval '1 day' WHERE id=%s",
                             (record_id,))

        def plan(body, *, mode="group", audience=None, delay=0, expires=180, members=()):
            now = datetime.now(UTC)
            item = post("/outreach/plans", {"title": scope + " " + body, "body": body, "connection_id": "fixture-channel",
                       "group_id": scope, "mode": mode, "audience_id": audience,
                       "scheduled_at": (now + timedelta(seconds=delay)).isoformat(),
                       "expires_at": (now + timedelta(seconds=delay + expires)).isoformat()})
            path = "/outreach/plans/" + item["id"]
            preview = get(path + "/preview")
            assert preview["mode"] == mode
            return post(path, {"action": "approve", "expected_version": item["version"], "member_ids": list(members)})

        for member, proof, private in ((a, "wp02-proof-a", True), (b, "wp02-proof-b", True), (c, "wp02-proof-c", False)):
            post("/dev/commerce/bindings", {"action": "bind", "connection_id": "fixture-channel",
                 "sender_external_id": member, "proof": proof, "private_external_id": f"{member}-private" if private else None},
                 dev=True)
            join_fixture(member)
        profiles = {identity: item["id"] for item in get("/members")["items"] for identity in item["identities"]}
        configure()

        with process("wp04_worker", env, tmp_path / "worker.log"):
            assert "暂时缺货" in ask(a, "风行跑步上衣黑色 XL 有货吗？")
            assert not get("/outreach/subscriptions")["items"]
            assert "已登记" in ask(b, "风行跑步上衣黑色 XL 到货提醒我")
            sub = get("/outreach/subscriptions")["items"][0]
            assert sub["profile_id"] == profiles[b] and sub["size"] == "XL"
            configure(outreach=False)
            _, wrong = emit("stock.changed", product_id="wind-top", color="黑色", size="L", quantity=9)
            eventually(lambda: done(wrong, "business:"))
            assert not rows(kind="restock")
            stock, change = emit("stock.changed", product_id="wind-top", color="黑色", size="XL", quantity=4)
            assert post("/dev/business-events", stock, dev=True)["duplicate"]
            pending = record(kind="restock", subscription_id=sub["id"], state="pending")
            assert "已取消" in ask(b, "取消风行跑步上衣黑色 XL 到货提醒")
            configure()
            force_due(pending["operation_key"])
            cancelled = record(kind="restock", subscription_id=sub["id"], state="cancelled")
            assert cancelled["attempts"] == 0 and len(rows(subscription_id=sub["id"])) == 1
            assert "已登记" in ask(b, "风行跑步上衣黑色 XL 到货提醒我")
            next_sub = get("/outreach/subscriptions")["items"][0]
            _, zero = emit("stock.changed", product_id="wind-top", color="黑色", size="XL", quantity=0)
            eventually(lambda: done(zero, "business:"))
            _, change = emit("stock.changed", product_id="wind-top", color="黑色", size="XL", quantity=3)
            assert record(subscription_id=next_sub["id"], state="sent")["attempts"] == 1
            assert get("/outreach/subscriptions")["items"][0]["status"] == "fulfilled"

            selected = [profiles[a], profiles[c]]
            audience = post("/audiences", {"name": scope, "group_id": scope, "confirmed_member_ids": selected})
            approved = plan("个人计划", mode="personal", audience=audience["id"], delay=40, members=selected)
            _, leave = emit("member.left", group_id=scope, sender_external_id=a)
            eventually(lambda: done(leave, "business:"))
            for row in rows(plan_id=approved["id"]):
                force_due(row["operation_key"])
            assert record(plan_id=approved["id"], sender_external_id=a, state="cancelled")["reason"] == "MEMBER_LEFT"
            manual = record(plan_id=approved["id"], sender_external_id=c, state="manual")
            assert len(rows(plan_id=approved["id"])) == 2
            completed = post(f"/outreach/records/{manual['id']}/resolve", {"expected_version": manual["version"],
                             "state": "sent", "evidence": "运营已通过核实后的官方服务渠道告知本人"})
            assert completed["state"] == "sent"

            accepted_plan = plan("等待回执")
            accepted = record(plan_id=accepted_plan["id"], state="accepted")
            assert accepted["attempts"] == 1
            _, proof = emit("delivery.receipt", operation_key=accepted["operation_key"], state="delivered",
                            provider_ref=accepted["provider_ref"])
            delivered = record(plan_id=accepted_plan["id"], state="delivered")
            assert delivered["evidence"].startswith("receipt:")
            assert record(plan_id=plan("超出每日频率")["id"], state="cancelled")["reason"] == "DAILY_FREQUENCY"
            previous_day(delivered["id"])
            retried = record(plan_id=plan("暂时失败后恢复")["id"], state="sent")
            assert retried["attempts"] == 2
            previous_day(retried["id"])
            unclear = record(plan_id=plan("结果不明")["id"], state="unknown")
            assert unclear["attempts"] == 1
            verify = post(f"/outreach/records/{unclear['id']}/resolve", {"expected_version": unclear["version"],
                          "state": "delivered", "provider_ref": "manual-receipt-1", "evidence": "供应商后台回执确认已送达"})
            assert verify["state"] == "delivered"

            history = [
                emit("member.joined", group_id=scope, sender_external_id=newcomer, historical=True),
                emit("order.signed", group_id=scope, sender_external_id=a, order_id="ORD-DEMO-A102", historical=True),
            ]
            for _, event_id in history:
                eventually(lambda: done(event_id, "business:"))
            assert not rows(kind="welcome") and not rows(kind="care")
            _, arrival = emit("member.joined", group_id=scope, sender_external_id=newcomer)
            assert record(kind="welcome", state="sent", event_id=arrival)["attempts"] == 1
            _, repeat = emit("member.joined", group_id=scope, sender_external_id=newcomer)
            assert record(kind="welcome", event_id=repeat, state="cancelled")["reason"] == "WELCOME_FREQUENCY"
            join_fixture(a)
            _, signed = emit("order.signed", group_id=scope, sender_external_id=a, order_id="ORD-DEMO-A102")
            care = record(kind="care", event_id=signed)
            force_due(care["operation_key"])
            assert record(kind="care", event_id=signed, state="sent")["attempts"] == 1
            suggestion = next(row for row in get("/outreach/suggestions")["items"] if row["profile_id"] == profiles[a])
            closed = post(f"/outreach/suggestions/{suggestion['id']}", {"action": "close", "expected_version": suggestion["version"],
                          "reason": "purchased"})
            assert closed["result"] == "purchased" and not rows(kind="followup")

            configure(replies=False, outreach=False)
            paused = inbound(a, "风行跑步上衣怎么洗？")
            assert eventually(lambda: done(paused, "inbound:"))[1] == "REPLY_PAUSED"
            assert reply(paused) is None
            expiring = plan("暂停期间过期", expires=2)
            expired = record(plan_id=expiring["id"], state="expired")
            configure()
            assert expired["attempts"] == 0
            assert "阴干" in ask(a, "风行跑步上衣怎么洗？")
            image = inbound(a, "", image=True)
            assert "型号" in eventually(lambda: reply(image))

            stale = plan("进程中断", delay=45)
            state = record(plan_id=stale["id"], state="pending")
            with psycopg.connect(dsn) as conn:
                conn.execute("UPDATE outreach_records SET state='sending',attempts=1 WHERE id=%s", (state["id"],))
                conn.execute("UPDATE jobs SET state='running',lease_until=now()-interval '1 second' WHERE operation_key=%s",
                             ("send:" + state["operation_key"],))
            assert record(plan_id=stale["id"], state="unknown")["attempts"] == 1
