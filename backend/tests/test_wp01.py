"""One opt-in WP01 acceptance scenario against an isolated PostgreSQL database."""
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from sqlalchemy.engine import make_url

PORT = int(os.getenv("PRIVATE_OPS_PORT", "8000"))
BASE = f"http://127.0.0.1:{PORT}/api/v1"
ORIGIN = "http://127.0.0.1:5173"
BACKEND = Path(__file__).resolve().parents[1]
DEV_TOKEN = "wp01-isolated-fixture"
PASSWORD = "fixture-operator-only-2026"


def eventually(check, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except httpx.ConnectError:
            pass
        time.sleep(0.2)
    pytest.fail("WP01 timed out waiting for an observable result")


@contextmanager
def process(module, env, output):
    with output.open("w", encoding="utf-8") as log:
        child = subprocess.Popen([sys.executable, "-m", module], cwd=BACKEND,
                                 env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            yield child
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)


def prepare(env):
    for command in (
        ["alembic", "upgrade", "head"],
        ["app.manage", "checkpoints"],
        ["app.manage", "seed"],
        ["app.manage", "operator", "wp01_fixture"],
    ):
        subprocess.run([sys.executable, "-m", *command], cwd=BACKEND, env=env, check=True, timeout=30)


def test_wp01_conversation_to_operator(tmp_path):
    database = os.getenv("WP01_TEST_DATABASE_URL")
    if not database:
        pytest.skip("Set WP01_TEST_DATABASE_URL to a dedicated PostgreSQL test database")
    url = make_url(database)
    assert url.database and url.database.endswith("_test"), "This scenario requires a dedicated *_test database"
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    env = {**os.environ, "DATABASE_URL": database, "DEV_EVENT_TOKEN": DEV_TOKEN,
           "AGENT_MODEL_MODE": "fixture", "NOTIFICATION_MODE": "fixture", "COMMERCE_MODE": "unconfigured",
           "FIXTURE_NOTIFICATION_RESULT": "failed", "FIXTURE_REPLY_RESULT": "sent",
           "OPERATOR_BOOTSTRAP_PASSWORD": PASSWORD, "OPERATOR_ORIGIN": ORIGIN, "PYTHONUTF8": "1"}
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", PORT))
    prepare(env)
    scope = "wp01-" + uuid4().hex
    event_headers = {"X-Dev-Token": DEV_TOKEN}
    now = datetime.now(UTC).isoformat()

    def event(index, member, text, *, mention=True, quote=None):
        identifier = f"{scope}-{index}"
        return {
            "connection_id": "fixture-channel", "external_event_id": identifier, "event_type": "message.received",
            "occurred_at": now, "conversation": {"kind": "group", "external_id": scope}, "sender_external_id": member,
            "message": {"external_id": identifier, "text": text, "mentions_bot": mention, "reply_to_external_id": quote},
        }

    with process("app.server", env, tmp_path / "api.log"), httpx.Client(base_url=BASE, timeout=6) as client:
        eventually(lambda: client.get("/health/ready").status_code == 200)
        assert client.get("/matters").status_code == 401
        login = client.post("/auth/login", headers={"Origin": ORIGIN},
                            json={"username": "wp01_fixture", "password": PASSWORD})
        assert login.status_code == 200, login.text
        assert "HttpOnly" in login.headers["set-cookie"] and login.json()["csrf_token"]
        csrf = login.json()["csrf_token"]
        assert client.get("/auth/session").status_code == 200
        assert client.post("/auth/logout", headers={"X-CSRF-Token": "invalid"}).status_code == 403

        def post(payload):
            result = client.post("/dev/inbound-events", json=payload, headers=event_headers)
            assert result.status_code == 202, result.text
            return result.json()

        first = event(1, "a", "风行跑步上衣怎么洗？")
        saved = post(first)
        assert post(first) == {"event_id": saved["event_id"], "duplicate": True}
        changed = {**first, "message": {**first["message"], "text": "其他内容"}}
        assert client.post("/dev/inbound-events", headers=event_headers, json=changed).status_code == 409
        post(event(2, "b", "越野训练背心怎么洗？"))
        ignored = post(event(3, "b", "嗯嗯", mention=False))
        post(event(4, "c", "如果寄错了帮我找人工"))
        post(event(5, "d", "不用找人工，风行上衣能烘干吗？"))
        post(event(6, "e", "[fixture:model-failure]"))
        post(event(7, "f", "这款怎么洗？"))
        post(event(8, "g", "鞋收到就开胶了，找人工"))
        post(event(9, "g", "我再补充：鞋底也是开胶的。"))

        def details():
            rows = client.get("/matters", params={"conversation_external_id": scope}).json()["items"]
            return {item["participant_label"]: client.get("/matters/" + item["id"]).json() for item in rows}

        def ready_members():
            result = details()
            if len(result) == 7 and len(result["g"]["runs"]) == 2 and all(row["runs"] and all(msg["delivery_state"] != "pending" for msg in row["messages"])
                                        for row in result.values()):
                return result
            return None

        with process("app.worker", env, tmp_path / "worker.log"):
            result = eventually(ready_members)
            assert result["a"]["evidence"][0]["version"] == 2
            assert "阴干" in result["a"]["messages"][-1]["preview"]
            assert "手洗" in result["b"]["messages"][-1]["preview"]
            assert result["c"]["runs"][0]["error_code"] == "UNVERIFIED_CONDITION" and not result["c"]["handoff_id"]
            assert result["d"]["runs"][0]["outcome"] == "answered"
            assert result["e"]["runs"][0]["error_code"] == "MODEL_OUTPUT_UNAVAILABLE"
            assert result["f"]["runs"][0]["outcome"] == "clarification"
            assert [run["outcome"] for run in result["g"]["runs"]] == ["handoff", "handoff"]
            assert len([m for m in result["g"]["messages"] if m["kind"] == "assistant"]) == 1
            handoff_id = result["g"]["handoff_id"]
            with psycopg.connect(dsn) as conn:
                assert conn.execute("SELECT last_error FROM jobs WHERE operation_key=%s",
                                    ("inbound:" + ignored["event_id"],)).fetchone()[0] == "IGNORED_NOT_ADDRESSED"
                # Advance this fixture's pending notice to the next working slot without changing app rules.
                conn.execute("""UPDATE jobs j SET run_after=now() FROM deliveries d
                              WHERE j.operation_key=d.operation_key AND d.handoff_id=%s""", (handoff_id,))
                answer_id = conn.execute("SELECT external_id FROM messages WHERE matter_id=%s AND kind='assistant'",
                                         (result["a"]["matter"]["id"],)).fetchone()[0]
            post(event(10, "quoted", "这款也能烘干吗？", mention=False, quote=answer_id))
            quoted = eventually(lambda: (item := details().get("quoted")) and item["evidence"] and item)
            assert quoted["matter"]["id"] != result["a"]["matter"]["id"]
            assert quoted["evidence"][0]["version"] == 2
            assert not any("风行跑步上衣怎么洗" in message["preview"] for message in quoted["messages"])

            conflicting_id = uuid4()
            with psycopg.connect(dsn) as conn:
                conn.execute("""INSERT INTO knowledge (id,version,title,body,product,topic,aliases,keywords,valid_from)
                              VALUES (%s,1,'冲突样例','允许高温烘干。','越野训练背心','care',ARRAY[]::text[],ARRAY['烘干'],now())""",
                             (conflicting_id,))
            try:
                post(event(11, "conflict", "越野训练背心能烘干吗？"))
                conflicting = eventually(lambda: (item := details().get("conflict")) and item["runs"] and item)
                assert conflicting["runs"][0]["error_code"] == "KNOWLEDGE_CONFLICT"
                assert not conflicting["evidence"]
            finally:
                with psycopg.connect(dsn) as conn:
                    conn.execute("DELETE FROM knowledge WHERE id=%s", (conflicting_id,))
            def get_handoff():
                return next(row for row in client.get("/handoffs").json()["items"] if row["id"] == handoff_id)
            eventually(lambda: get_handoff()["notification_state"] == "failed")
            item = get_handoff()
            url = "/handoffs/" + item["id"] + "/resolve"
            bad = client.post(url, headers={"X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex},
                              json={"expected_version": 1, "resolution": "过期版本"})
            assert bad.status_code == 409 and bad.json()["code"] == "VERSION_CONFLICT"
            headers = {"X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex}
            body = {"expected_version": item["version"], "resolution": "请从原订单入口申请售后", "resume_agent": True}
            resolved = client.post(url, headers=headers, json=body)
            assert resolved.status_code == 200, resolved.text
            assert client.post(url, headers=headers, json=body).json() == resolved.json()
            assert client.post(url, headers=headers, json={**body, "resolution": "内容不同"}).status_code == 409
            eventually(lambda: any(m["preview"].startswith("运营确认") and m["delivery_state"] == "sent"
                                   for m in details()["g"]["messages"]))

        # Recreate the persisted boundary of a process lost after dispatch started.
        with psycopg.connect(dsn) as conn:
            delivery_id, operation_key = conn.execute("""
                UPDATE deliveries SET state='sending',provider_ref=NULL
                WHERE message_id=(SELECT id FROM messages WHERE matter_id=%s AND kind='assistant' LIMIT 1)
                RETURNING id,operation_key
            """, (result["a"]["matter"]["id"],)).fetchone()
            conn.execute("""UPDATE jobs SET state='running',lease_until=now()-interval '1 second'
                            WHERE operation_key=%s""", (operation_key,))

        with process("app.worker", env, tmp_path / "worker-restart.log"):
            def unknown_recovered():
                with psycopg.connect(dsn) as conn:
                    return conn.execute("SELECT state,provider_ref FROM deliveries WHERE id=%s", (delivery_id,)).fetchone() == ("unknown", None)
            eventually(unknown_recovered)
            assert client.post("/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
            assert client.get("/auth/session").status_code == 401
