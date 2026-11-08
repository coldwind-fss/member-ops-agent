"""Exercise WP03 through the API, persisted jobs, and a real PostgreSQL test database."""
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest
from sqlalchemy.engine import make_url

from test_wp01 import BASE, DEV_TOKEN, ORIGIN, PASSWORD, eventually, prepare, process


def test_wp03_member_activity_and_editorial_flow(tmp_path):
    database = os.getenv("WP03_TEST_DATABASE_URL")
    if not database:
        pytest.skip("Set WP03_TEST_DATABASE_URL to a dedicated PostgreSQL test database")
    url = make_url(database)
    assert url.database and url.database.endswith("_test")
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    env = {**os.environ, "DATABASE_URL": database, "DEV_EVENT_TOKEN": DEV_TOKEN,
           "AGENT_MODEL_MODE": "fixture", "COMMERCE_MODE": "fixture", "NOTIFICATION_MODE": "fixture",
           "FIXTURE_REPLY_RESULT": "sent", "OPERATOR_BOOTSTRAP_PASSWORD": PASSWORD,
           "OPERATOR_ORIGIN": ORIGIN, "PYTHONUTF8": "1", "PYTHONPATH": str(tmp_path)}
    (tmp_path / "wp03_observe.py").write_text(
        "from app import delivery, worker\n"
        "import json\n"
        "send = delivery.deliver\n"
        "async def observe(item):\n"
        "    if item['private_body']:\n"
        "        print('PRIVATE ' + json.dumps([item['in_reply_to'], item['private_body']]), flush=True)\n"
        "    return await send(item)\n"
        "delivery.deliver = observe\n"
        "raise SystemExit(worker.main())\n", encoding="utf-8",
    )
    prepare(env)
    suffix = uuid4().hex[:9]
    group, other = f"wp03-group-{suffix}", f"wp03-off-{suffix}"
    a, b, c = (f"wp03-{suffix}-{member}" for member in "abc")
    headers = {"X-Dev-Token": DEV_TOKEN}
    log = tmp_path / "worker.log"

    with process("app.server", env, tmp_path / "api.log"), httpx.Client(base_url=BASE, timeout=8) as client:
        eventually(lambda: client.get("/health/ready").status_code == 200)
        assert client.get("/members").status_code == 401
        login = client.post("/auth/login", headers={"Origin": ORIGIN}, json={"username": "wp01_fixture", "password": PASSWORD})
        assert login.status_code == 200, login.text
        csrf = login.json()["csrf_token"]

        def fetch(path, **params):
            response = client.get(path, params=params)
            assert response.status_code == 200, response.text
            return response.json()

        def post(path, data, *, dev=False):
            supplied = headers if dev else {"Origin": ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex}
            response = client.post(path, json=data, headers=supplied)
            assert response.is_success, response.text
            return response.json()

        def bind(member, proof=None):
            return post("/dev/commerce/bindings", {
                "connection_id": "fixture-channel", "sender_external_id": member,
                **({"action": "bind", "proof": proof, "private_external_id": f"{member}-private"} if proof else {"action": "unbind"}),
            }, dev=True)

        def send(member, text, *, private=False, mention=True, target=group):
            key = uuid4().hex
            data = {"connection_id": "fixture-channel", "external_event_id": key, "event_type": "message.received",
                    "occurred_at": datetime.now(UTC).isoformat(), "sender_external_id": member,
                    "conversation": {"kind": "private" if private else "group", "external_id": f"{member}-private" if private else target},
                    "message": {"external_id": key, "text": text, "mentions_bot": mention and not private, "reply_to_external_id": None}}
            saved = post("/dev/inbound-events", data, dev=True)
            return data, saved["event_id"]

        def processed(event_id):
            with psycopg.connect(dsn) as conn:
                row = conn.execute("SELECT state FROM jobs WHERE operation_key=%s", ("inbound:" + event_id,)).fetchone()
                return row and row[0] == "done"

        def answer(event):
            data, _ = event
            key = data["message"]["external_id"]
            with psycopg.connect(dsn) as conn:
                row = conn.execute("""
                    SELECT matter_id::text,preview FROM messages WHERE in_reply_to=%s AND delivery_state='sent'
                """, (key,)).fetchone()
            if row and "（私密内容）" in row[1]:
                bodies = dict(json.loads(line.removeprefix("PRIVATE ")) for line in log.read_text(encoding="utf-8").splitlines()
                              if line.startswith("PRIVATE "))
                return row[0], bodies[key]
            return row

        def ask(member, text, **kwargs):
            event = send(member, text, **kwargs)
            return eventually(lambda: answer(event))

        def profiles():
            return {channel: row for row in fetch("/members")["items"] for channel in row["identities"]}

        def review(profile, item, action, value=None):
            return post(f"/members/{profile}/memories/{item['id']}", {"action": action,
                        "expected_version": item["version"], "value": value})

        for member, proof in ((a, "wp02-proof-a"), (b, "wp02-proof-b"), (c, "wp02-proof-c")):
            bind(member, proof)
        profile = profiles()[a]["id"]
        post("/insights/groups", {"connection_id": "fixture-channel", "group_id": group, "enabled": True})
        with process("wp03_observe", env, log):
            preference = send(a, "我平时穿 M 码")
            assert "已记录" in eventually(lambda: answer(preference))[1]
            assert "M 有货" in ask(a, "推荐风行跑步上衣", private=True)[1]
            assert "L 暂时缺货" in ask(a, "给朋友买风行跑步上衣黑色 L", private=True)[1]
            assert "M 有货" in ask(b, "推荐风行跑步上衣", private=True)[1]
            assert "L 暂时缺货" in ask(b, "推荐风行跑步上衣", private=True)[1]
            detail = fetch("/members/" + profile)
            size = next(item for item in detail["memories"] if item["topic"] == "size")
            assert size["value"] == "M" and size["evidence"] == "我平时穿 M 码"
            corrected = review(profile, size, "correct", "L")
            corrected_answer = ask(a, "推荐风行跑步上衣", private=True)[1]
            assert "L 暂时缺货" in corrected_answer and "M 有货" not in corrected_answer
            review(profile, corrected, "delete")
            assert post("/dev/inbound-events", preference[0], dev=True)["duplicate"]
            assert "M 有货" in ask(a, "推荐风行跑步上衣", private=True)[1]
            assert next(item for item in fetch("/members/" + profile)["memories"] if item["topic"] == "size")["status"] == "deleted"

            ask(a, "风行跑步上衣有货吗？")
            memory = next(item for item in fetch("/members/" + profile)["memories"] if item["kind"] == "interest")
            assert memory["status"] == "candidate"
            assert fetch("/audiences/preview", group_id=group, tag=memory["value"])["matched_count"] == 0
            assert fetch("/audiences/preview", group_id=group, interest=memory["value"])["matched_count"] == 1
            review(profile, memory, "confirm")
            preview = fetch("/audiences/preview", group_id=group, tag=memory["value"])
            assert preview["matched_count"] == 1 and any(not item["matched"] and item["reasons"] for item in preview["items"])
            saved = post("/audiences", {"name": suffix, "group_id": group, "tag": memory["value"], "confirmed_member_ids": [profile]})
            assert saved["confirmed_member_ids"] == [profile]
            post("/dev/group-memberships", {"connection_id": "fixture-channel", "group_id": group,
                                           "sender_external_id": a, "active": False}, dev=True)
            assert fetch("/audiences/preview", group_id=group, tag=memory["value"])["matched_count"] == 0
            post("/dev/group-memberships", {"connection_id": "fixture-channel", "group_id": group,
                                           "sender_external_id": a, "active": True}, dev=True)

            now = datetime.now(UTC)
            title = "晨跑挑战" + suffix
            config = {"title": title, "group_id": group, "rule": "每天一次打卡", "checkin": True, "question": "跑了多远？",
                      "starts_at": (now - timedelta(minutes=5)).isoformat(), "ends_at": (now + timedelta(hours=1)).isoformat()}
            activity = post("/activities", config)
            path = "/activities/" + activity["id"]
            def change(item, action):
                return post(path, {"action": action, "expected_version": item["version"]})

            activity = change(activity, "publish")
            assert activity["effective_status"] == "open"
            assert "每天一次打卡" in ask(a, title + "怎么报名？")[1]
            assert "已登记" in ask(a, "帮我报名" + title)[1]
            assert "已登记" in ask(a, "帮我报名" + title)[1]
            assert "已登记" in ask(a, title + "打卡")[1]
            assert "已登记" in ask(a, title + "打卡")[1]
            assert "私密会话" in ask(a, title + "问卷：跑了 5 公里")[1]
            assert "已登记" in ask(a, title + "问卷：跑了 5 公里", private=True)[1]
            paused = change(activity, "pause")
            assert "已暂停" in ask(b, "帮我报名" + title)[1]
            assert change(paused, "resume")["effective_status"] == "open"
            progress = fetch(path)
            assert progress["progress"] == {"register": 1, "checkin": 1, "survey": 1}
            assert all(row["activity_version"] == 2 for row in progress["participation"])
            with psycopg.connect(dsn) as conn:
                conn.execute("UPDATE activities SET starts_at=now()-interval '2 hours',ends_at=now()-interval '1 second' WHERE id=%s",
                             (activity["id"],))
            assert "已结束" in ask(b, "帮我报名" + title)[1]

            day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            quiet = send(b, "风行跑步上衣怎么洗？", mention=False)
            ignored = send(b, "越野训练背心怎么洗？", mention=False, target=other)
            complaint = send(b, "这件衣服刚收到就破损了", mention=False)
            risk = send(b, "这件衣服会不会开胶？", mention=False)
            for _, event_id in (quiet, ignored, complaint, risk):
                eventually(lambda: processed(event_id))
            original = ask(a, "风行跑步上衣怎么洗？")
            report = fetch("/insights", connection_id="fixture-channel", group_id=group, day=day)
            bucket = next(bucket for bucket in report["items"] if bucket["topic"] == "商品洗护")
            assert bucket["count"] >= 2 and any(signal["preview"] for signal in bucket["signals"])
            assert not fetch("/insights", connection_id="fixture-channel", group_id=other, day=day)["items"]
            complaints = next(bucket for bucket in report["items"] if bucket["topic"] == "质量与售后反馈")
            assert complaints["count"] == 1
            signal = complaints["signals"][0]
            assert fetch("/matters/" + signal["matter_id"])["handoff_id"]
            assert answer(complaint) is None

            old_evidence = fetch("/matters/" + original[0])["evidence"][0]
            draft = post("/knowledge/drafts", {"knowledge_id": old_evidence["knowledge_id"], "matter_id": original[0],
                         "category": "outdated", "title": "更新洗护 " + suffix, "body": "请用冷水手洗，洗后平铺晾干。",
                         "product": "风行跑步上衣", "topic": "care", "aliases": ["风行上衣"], "keywords": ["洗", "保养", "烘干"]})
            published = post(f"/knowledge/drafts/{draft['id']}/publish", {"expected_version": draft["expected_version"]})
            assert published["version"] == old_evidence["version"] + 1
            new = ask(b, "风行跑步上衣怎么洗？")
            assert "平铺晾干" in new[1] and fetch("/matters/" + original[0])["evidence"][0] == old_evidence
            cases = post("/knowledge/verify", {"draft_id": draft["id"], "questions": ["风行跑步上衣怎么洗？", "风行上衣如何保养？"],
                                               "expected_text": "平铺晾干"})
            assert all(case["passed"] for case in cases["cases"])
            bind(a)
            assert fetch("/audiences/preview", group_id=group, tag=memory["value"])["matched_count"] == 0
