"""Integration: every dashboard query runs against the real schema.

Skipped when Postgres isn't reachable (DATABASE_URL, default localhost:5432). Exists
because a UNION column mismatch once shipped: unit tests never executed the SQL.
Read-only: only SELECTs and the idempotent schema DDL.
"""

import asyncio
import os

import psycopg
import pytest

from src.backend.db import DB

URL = os.environ.get("DATABASE_URL", "postgresql://triage:triage@127.0.0.1:5432/triage")


def _reachable() -> bool:
    try:
        psycopg.connect(URL, connect_timeout=2).close()
        return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="Postgres not reachable")


def test_dashboard_views_execute():
    async def run():
        db = DB(URL, max_size=2)
        await db.open()
        try:
            approvals = await db.view_approvals()
            working = await db.view_working()
            history = await db.view_history(limit=5)
            events = await db.recent_events(limit=5)
        finally:
            await db.close()
        for rows, needed in ((approvals, {"row_type", "email_id", "received_at", "domain_verdict", "injection_detail"}),
                             (working, {"email_id", "received_at", "injection_detected"}),
                             (history, {"email_id", "received_at", "updated_at", "actions", "injection_detail"})):
            for r in rows[:3]:
                assert needed <= set(r), needed - set(r)
        return len(approvals), len(working), len(history), len(events)

    asyncio.run(run())
