"""CodeRabbit review round 2: claim fencing, reopen locking, client response validation."""

import asyncio
import os
import uuid

import httpx
import psycopg
import pytest

from src.tui.client import BackendClient, BackendError

URL = os.environ.get("DATABASE_URL", "postgresql://triage:triage@127.0.0.1:5432/triage")


def _db_up() -> bool:
    try:
        psycopg.connect(URL, connect_timeout=2).close()
        return True
    except psycopg.OperationalError:
        return False


# -- client: non-2xx and wrongly-shaped 2xx bodies follow the BackendError path ----------
def _client(status: int, body, *, content_type="application/json") -> BackendClient:
    def handler(req: httpx.Request) -> httpx.Response:
        if isinstance(body, (bytes, str)):
            return httpx.Response(status, content=body, headers={"content-type": content_type})
        return httpx.Response(status, json=body)
    return BackendClient("http://127.0.0.1:8000", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("body", [[], None, {}, {"token": 1, "username": "a"}, {"token": "t"}])
def test_login_rejects_wrong_shape(body):
    with pytest.raises(BackendError):
        asyncio.run(_client(200, body if body is not None else b"null").login("a", "b"))


@pytest.mark.parametrize("body", [[], {}, {"approvals": [], "working": [], "history": []}, {"approvals": {}, "working": [], "history": [], "events": []}])
def test_dashboard_rejects_wrong_shape(body):
    with pytest.raises(BackendError):
        asyncio.run(_client(200, body).dashboard())


def test_dashboard_accepts_valid_shape():
    ok = {"approvals": [], "working": [], "history": [], "events": []}
    assert asyncio.run(_client(200, ok).dashboard()) == ok


def test_non_success_without_location_is_error():
    with pytest.raises(BackendError):
        asyncio.run(_client(304, b"").health())


# -- DB: claim fencing and reopen locking (own rows, cleaned up) ------------------------
needs_db = pytest.mark.skipif(not _db_up(), reason="Postgres not reachable")


@needs_db
def test_superseded_claim_cannot_write():
    from src.backend.db import DB

    async def run():
        db = DB(URL, max_size=3)
        await db.open()
        eid = f"test-r2-{uuid.uuid4().hex[:8]}"
        try:
            async with db.pool.connection() as c:
                # far-past received_at so live workers would pick it... only if queued; we insert it already classifying
                await c.execute("""INSERT INTO emails (id, sender, subject, body, received_at, status, attempts, locked_at, claim_id)
                                   VALUES (%s,'a@b.io','s','b', now(), 'classifying', 1, now(), gen_random_uuid())""", (eid,))
                old = (await (await c.execute("SELECT claim_id FROM emails WHERE id=%s", (eid,))).fetchone())["claim_id"]
                # stale sweep re-queues it, then a new claim takes it
                await c.execute("UPDATE emails SET status='classifying', claim_id=gen_random_uuid() WHERE id=%s", (eid,))
            # the superseded run finishes: nothing may be written
            assert await db.save_triage(eid, "billing", "late", [], claim_id=old) is False
            assert await db.mark_failed(eid, "late error", 3, claim_id=old) == "superseded"
            async with db.pool.connection() as c:
                row = await (await c.execute("SELECT status, label, last_error FROM emails WHERE id=%s", (eid,))).fetchone()
                assert (row["status"], row["label"], row["last_error"]) == ("classifying", None, None)
                current = (await (await c.execute("SELECT claim_id FROM emails WHERE id=%s", (eid,))).fetchone())["claim_id"]
            # the current claim writes normally
            assert await db.save_triage(eid, "spam", "ok", [], claim_id=current) is True
        finally:
            async with db.pool.connection() as c:
                await c.execute("DELETE FROM events WHERE email_id=%s", (eid,))
                await c.execute("DELETE FROM emails WHERE id=%s", (eid,))
            await db.close()

    asyncio.run(run())
