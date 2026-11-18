"""Review fixes: stale-draft approval, atomic edits, failure classification + reopen,
interrupted-approval reconciliation, poll cursor cap, settlement locking, /health 503.

Pure tests always run. DB-backed tests create their own `test-rf-*` rows against the
live Postgres and delete them afterwards; skipped when Postgres isn't reachable.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
from psycopg.types.json import Jsonb
import pytest
from fastapi.testclient import TestClient

from src.backend import api, pipeline
from src.backend.db import DB, INTERRUPTED_ERROR, StaleDraft
from src.triage_skill import WriteScopeError, payload_digest

URL = os.environ.get("DATABASE_URL", "postgresql://triage:triage@127.0.0.1:5432/triage")


def _reachable() -> bool:
    try:
        psycopg.connect(URL, connect_timeout=2).close()
        return True
    except psycopg.OperationalError:
        return False


needs_db = pytest.mark.skipif(not _reachable(), reason="Postgres not reachable")


# -- pure ---------------------------------------------------------------------
@pytest.mark.parametrize("exc", [
    WriteScopeError("no write scope"),
    RuntimeError("missing required env var WRITE_TOKEN (see env.example)"),
    httpx.ConnectError("refused"),
    httpx.ConnectTimeout("connect timed out"),
])
def test_failures_before_send_are_retryable(exc):
    retryable, err = pipeline.classify_failure(exc)
    assert retryable and "never reached the API" in err


@pytest.mark.parametrize("exc", [
    httpx.ReadTimeout("read timed out"),
    httpx.HTTPStatusError("500", request=httpx.Request("POST", "http://x"), response=httpx.Response(500)),
    RuntimeError("something else"),
    ValueError("boom"),
])
def test_failures_after_send_are_uncertain(exc):
    retryable, err = pipeline.classify_failure(exc)
    assert not retryable and "uncertain" in err and "verify manually" in err


def test_poll_cursor_capped_at_now():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert pipeline.poll_since(None, now, 120) is None
    assert pipeline.poll_since(now + timedelta(days=30), now, 120) == "2026-01-01T11:58:00Z"
    assert pipeline.poll_since(now - timedelta(hours=1), now, 120) == "2026-01-01T10:58:00Z"


class _FakeDB:
    def __init__(self, ping_ok=True, reopen_ok=False, action=None):
        self.ping_ok, self.reopen_ok, self.action = ping_ok, reopen_ok, action

    async def ping(self):
        if not self.ping_ok:
            raise OSError("db down")
        return True

    async def reopen_action(self, action_id, actor):
        return self.reopen_ok

    async def get_action(self, action_id):
        return self.action


@pytest.fixture
def client_with():
    def make(fake):
        api.app.dependency_overrides[api.get_db] = lambda: fake
        api.app.dependency_overrides[api.current_user] = lambda: {"username": "tester"}
        api.app.dependency_overrides[api.get_settings] = lambda: None
        return TestClient(api.app)  # no `with`: lifespan (poller/workers) never starts
    yield make
    api.app.dependency_overrides.clear()


def test_health_503_when_db_down(client_with):
    r = client_with(_FakeDB(ping_ok=False)).get("/health")
    assert r.status_code == 503 and r.json()["status"] == "degraded" and r.json()["db"] is False
    assert client_with(_FakeDB()).get("/health").status_code == 200


def test_reopen_refused_and_ok(client_with):
    r = client_with(_FakeDB(reopen_ok=False, action={"id": 1})).post("/actions/1/reopen")
    assert r.status_code == 409 and r.json()["detail"] == api.REOPEN_DETAIL
    assert client_with(_FakeDB(action=None)).post("/actions/1/reopen").status_code == 404
    assert client_with(_FakeDB(reopen_ok=True)).post("/actions/1/reopen").json() == {"outcome": "reopened"}


def test_approve_stale_and_body_optional(client_with, monkeypatch):
    seen = []

    async def fake_approve(db, s, action_id, actor, expected_digest=None):
        seen.append(expected_digest)
        return ("stale", None) if expected_digest == "old" else ("executed", {"ok": True})

    monkeypatch.setattr(pipeline, "approve", fake_approve)
    c = client_with(_FakeDB())
    r = c.post("/actions/1/approve", json={"payload_digest": "old"})
    assert r.status_code == 409 and r.json()["detail"] == "draft changed since you viewed it; review it again"
    assert c.post("/actions/1/approve").status_code == 200  # no body: legacy behaviour
    assert seen == ["old", None]


# -- DB-backed (own rows only) ----------------------------------------------------
def _run(coro_fn):
    async def go():
        db = DB(URL, max_size=4)
        await db.open()
        eid = f"test-rf-{uuid.uuid4().hex[:10]}"
        try:
            async with db.pool.connection() as conn:
                await conn.execute(
                    """INSERT INTO emails (id, sender, subject, body, received_at, status)
                       VALUES (%s, 'a@example.com', 's', 'b', now(), 'needs_approval')""", (eid,))
            await coro_fn(db, eid)
        finally:
            async with db.pool.connection() as conn:
                await conn.execute("DELETE FROM events WHERE email_id=%s", (eid,))
                await conn.execute("DELETE FROM emails WHERE id=%s", (eid,))  # cascades to actions
            await db.close()
    asyncio.run(go())


async def _add_action(db, eid, payload=None, kind="send_reply") -> int:
    payload = payload or {"to": "a@example.com", "subject": "Re: s", "body": "hello", "in_reply_to": eid}
    row = await db._one("INSERT INTO actions (email_id, kind, payload) VALUES (%s, %s, %s) RETURNING id",
                        (eid, kind, Jsonb(payload)))
    return row["id"]


@needs_db
def test_decide_refuses_stale_digest():
    async def body(db, eid):
        aid = await _add_action(db, eid)
        seen = payload_digest((await db.get_action(aid))["payload"])
        await db.update_payload(aid, {"body": "changed by someone else"}, "other")
        with pytest.raises(StaleDraft):
            await db.decide(aid, approve=True, actor="me", expected_digest=seen)
        assert (await db.get_action(aid))["status"] == "pending"
        fresh = payload_digest((await db.get_action(aid))["payload"])
        assert (await db.decide(aid, approve=True, actor="me", expected_digest=fresh))["status"] == "approved"
    _run(body)


@needs_db
def test_concurrent_edits_of_different_fields_both_survive():
    async def body(db, eid):
        aid = await _add_action(db, eid)
        await asyncio.gather(db.update_payload(aid, {"subject": "S2"}, "u1"),
                             db.update_payload(aid, {"body": "B2"}, "u2"))
        p = (await db.get_action(aid))["payload"]
        assert p["subject"] == "S2" and p["body"] == "B2" and p["to"] == "a@example.com"
    _run(body)


@needs_db
def test_retryable_failure_shows_in_approvals_and_reopens():
    async def body(db, eid):
        aid = await _add_action(db, eid)
        await db.decide(aid, approve=True, actor="me")
        await db.record_execution(aid, error="WriteScopeError: x", retryable=True)
        a = await db.get_action(aid)
        assert a["status"] == "failed" and a["retryable"] is True
        assert (await db._one("SELECT status FROM emails WHERE id=%s", (eid,)))["status"] == "done"
        rows = [r for r in await db.view_approvals() if r["email_id"] == eid]
        assert [r["row_type"] for r in rows] == ["exec_failed"]
        assert rows[0]["action_status"] == "failed" and rows[0]["retryable"] is True and rows[0]["action_error"]
        assert await db.reopen_action(aid, "me")
        a = await db.get_action(aid)
        assert a["status"] == "pending" and a["decided_by"] is None and a["error"] is None and a["payload"]["body"] == "hello"
        assert (await db._one("SELECT status FROM emails WHERE id=%s", (eid,)))["status"] == "needs_approval"
        rows = [r for r in await db.view_approvals() if r["email_id"] == eid]
        assert rows[0]["row_type"] == "action" and rows[0]["payload_digest"] == payload_digest(a["payload"])
        assert not await db.reopen_action(aid, "me")  # pending now: nothing to reopen
    _run(body)


@needs_db
def test_uncertain_failure_cannot_be_reopened():
    async def body(db, eid):
        aid = await _add_action(db, eid)
        await db.decide(aid, approve=True, actor="me")
        await db.record_execution(aid, error="ReadTimeout: x (outcome uncertain)", retryable=False)
        assert not await db.reopen_action(aid, "me")
        assert not [r for r in await db.view_approvals() if r["email_id"] == eid]
    _run(body)


@needs_db
def test_interrupted_approval_is_failed_not_replayed():
    async def body(db, eid):
        aid = await _add_action(db, eid)
        await db.decide(aid, approve=True, actor="me")
        # Backdate far enough that only this row matches the (huge) threshold below.
        await db._one("UPDATE actions SET decided_at = now() - interval '100 years' WHERE id=%s RETURNING 1", (aid,))
        assert await db.reconcile_interrupted(older_than_s=50 * 365 * 86400) == 1
        a = await db.get_action(aid)
        assert a["status"] == "failed" and a["retryable"] is False and a["error"] == INTERRUPTED_ERROR
        assert (await db._one("SELECT status FROM emails WHERE id=%s", (eid,)))["status"] == "done"
    _run(body)


@needs_db
def test_concurrent_settlements_close_email():
    async def body(db, eid):
        a1, a2 = await _add_action(db, eid), await _add_action(db, eid, kind="send_alert",
                                                               payload={"channel": "#x", "message": "m"})
        await db.decide(a1, approve=True, actor="me")
        await db.decide(a2, approve=True, actor="me")
        await asyncio.gather(db.record_execution(a1, result={"ok": 1}), db.record_execution(a2, result={"ok": 2}))
        assert (await db._one("SELECT status FROM emails WHERE id=%s", (eid,)))["status"] == "done"
    _run(body)
