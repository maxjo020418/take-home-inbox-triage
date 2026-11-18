"""Postgres access: the emails table doubles as the job queue.

Workers claim jobs with `FOR UPDATE SKIP LOCKED`, so N workers (or N
processes) never grab the same email, and nobody blocks on a locked row.
Every state change writes an `events` row in the same transaction.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from src.triage_skill import ProposedAction, payload_digest

INTERRUPTED_ERROR = ("execution interrupted after approval; outcome unknown — "
                     "verify in the target system before re-sending")

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


async def _event(conn: AsyncConnection, actor: str, message: str, email_id: str | None = None, action_id: int | None = None) -> None:
    await conn.execute(
        "INSERT INTO events (actor, email_id, action_id, message) VALUES (%s, %s, %s, %s)",
        (actor, email_id, action_id, message),
    )


class StaleDraft(Exception):
    """The payload changed since the reviewer computed its digest; nothing was decided."""


class DB:
    def __init__(self, url: str, *, max_size: int = 10):
        self.pool = AsyncConnectionPool(url, min_size=1, max_size=max_size, open=False, kwargs={"row_factory": dict_row})

    async def open(self) -> None:
        await self.pool.open(wait=True, timeout=30)
        async with self.pool.connection() as conn:
            await conn.execute(SCHEMA)

    async def close(self) -> None:
        await self.pool.close()

    async def ping(self) -> bool:
        return (await self._one("SELECT 1 AS ok"))["ok"] == 1

    async def _all(self, sql: str, params: Any = None) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def _one(self, sql: str, params: Any = None) -> dict | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchone()

    # -- users & sessions -----------------------------------------------------
    async def count_users(self) -> int:
        return (await self._one("SELECT count(*) AS n FROM users"))["n"]

    async def create_user(self, username: str, password_hash: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)", (username, password_hash))
            await _event(conn, "system", f"user {username!r} created")

    async def get_user(self, username: str) -> dict | None:
        return await self._one("SELECT * FROM users WHERE username=%s", (username,))

    async def create_session(self, user_id: int, token_hash: str, ttl_h: float) -> datetime:
        row = await self._one(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, now() + %s) RETURNING expires_at",
            (token_hash, user_id, timedelta(hours=ttl_h)),
        )
        return row["expires_at"]

    async def user_for_session(self, token_hash: str) -> dict | None:
        return await self._one(
            """SELECT u.id, u.username, s.expires_at FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash=%s AND s.expires_at > now()""",
            (token_hash,),
        )

    async def delete_session(self, token_hash: str) -> None:
        await self._one("DELETE FROM sessions WHERE token_hash=%s RETURNING 1", (token_hash,))

    # -- ingest ---------------------------------------------------------------
    async def latest_received_at(self) -> datetime | None:
        row = await self._one("SELECT max(received_at) AS ts FROM emails")
        return row["ts"] if row else None

    async def insert_emails(self, emails: list[dict]) -> int:
        """Idempotent: re-polled emails are ignored by primary key."""
        new = 0
        async with self.pool.connection() as conn, conn.transaction():
            for e in emails:
                cur = await conn.execute(
                    """INSERT INTO emails (id, sender, subject, body, received_at)
                       VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                    (e["id"], e["from"], e.get("subject", ""), e.get("body", ""), e["received_at"]),
                )
                if cur.rowcount:
                    new += 1
                    await _event(conn, "poller", f"ingested from {e['from']}", e["id"])
        return new

    # -- queue ----------------------------------------------------------------
    async def recover_stale(self, older_than_s: float = 300) -> int:
        """Re-queue jobs whose worker died mid-classification (at-least-once)."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """UPDATE emails SET status='queued', locked_at=NULL, claim_id=NULL, updated_at=now()
                   WHERE status='classifying' AND locked_at < now() - make_interval(secs => %s)""",
                (older_than_s,),
            )
            return cur.rowcount

    async def reconcile_interrupted(self, older_than_s: float = 600) -> int:
        """Approved actions with no recorded execution after `older_than_s`: the process
        died between approval and record_execution. The write may or may not have gone
        out, so mark them failed (not retryable) for a human to verify. Never replays."""
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """UPDATE actions SET status='failed', retryable=false, error=%s, executed_at=now()
                   WHERE status='approved' AND executed_at IS NULL
                     AND decided_at < now() - make_interval(secs => %s)
                   RETURNING id, email_id, kind""",
                (INTERRUPTED_ERROR, older_than_s),
            )
            rows = await cur.fetchall()
            for r in rows:
                await _event(conn, "system", f"execution FAILED {r['kind']}: {INTERRUPTED_ERROR}", r["email_id"], r["id"])
                await self._close_email_if_settled(conn, r["email_id"])
        return len(rows)

    async def claim_next(self) -> dict | None:
        return await self._one(
            """UPDATE emails SET status='classifying', attempts=attempts+1, locked_at=now(),
                                 claim_id=gen_random_uuid(), updated_at=now()
               WHERE id = (SELECT id FROM emails WHERE status='queued'
                           ORDER BY received_at FOR UPDATE SKIP LOCKED LIMIT 1)
               RETURNING *"""
        )

    async def set_domain_check(self, email_id: str, verdict: str, detail: dict) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE emails SET domain_verdict=%s, domain_detail=%s WHERE id=%s",
                (verdict, Jsonb(detail), email_id),
            )
            if verdict != "ok":
                await _event(conn, "worker", f"sender domain {verdict}: {'; '.join(detail.get('reasons', []))[:200]}", email_id)

    async def save_triage(self, email_id: str, label: str, reason: str, actions: list[ProposedAction],
                          injection: dict | None = None, *, claim_id=None) -> bool:
        """`injection` = {"llm": bool, "patterns": [...]}; flagged if either source fired.

        Fenced to the claim that produced the result: if the job was re-queued (and maybe
        re-claimed) while this classification ran, nothing is written and False is returned."""
        injection = injection or {"llm": False, "patterns": []}
        flagged = bool(injection["llm"] or injection["patterns"])
        status = "needs_approval" if actions else "dropped"
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """UPDATE emails SET status=%s, label=%s, reason=%s, injection_detected=%s, injection_detail=%s,
                                     last_error=NULL, locked_at=NULL, claim_id=NULL, updated_at=now()
                   WHERE id=%s AND status='classifying' AND claim_id IS NOT DISTINCT FROM %s""",
                (status, label, reason, flagged, Jsonb(injection), email_id, claim_id),
            )
            if cur.rowcount == 0:
                return False  # superseded claim: discard, no actions inserted
            if flagged:
                src = ", ".join((["model"] if injection["llm"] else []) + injection["patterns"])
                await _event(conn, "worker", f"prompt-injection attempt detected ({src}); ignored, flagged for review", email_id)
            for a in actions:
                await conn.execute(
                    "INSERT INTO actions (email_id, kind, payload, rationale, extra) VALUES (%s, %s, %s, %s, %s)",
                    (email_id, a.kind, Jsonb(a.payload), a.rationale, a.extra),
                )
            kinds = ", ".join(a.kind + (" (+extra)" if a.extra else "") for a in actions) or "no action (dropped)"
            await _event(conn, "worker", f"classified {label}: {kinds}", email_id)
        return True

    async def mark_failed(self, email_id: str, error: str, max_attempts: int, *, claim_id=None) -> str:
        """Back to the queue for another try, or `failed` once attempts run out.
        Never falls back to a label: a failed email must not be silently dropped.
        Fenced like save_triage: a superseded claim changes nothing ("superseded")."""
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """UPDATE emails SET status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'queued' END,
                                     last_error=%s, locked_at=NULL, claim_id=NULL, updated_at=now()
                   WHERE id=%s AND status='classifying' AND claim_id IS NOT DISTINCT FROM %s
                   RETURNING status""",
                (max_attempts, error[:2000], email_id, claim_id),
            )
            row = await cur.fetchone()
            if row is None:
                return "superseded"
            status = row["status"]
            await _event(conn, "worker", f"classification error -> {status}: {error[:200]}", email_id)
        return status

    async def retry_email(self, email_id: str, actor: str) -> bool:
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "UPDATE emails SET status='queued', attempts=0, updated_at=now() WHERE id=%s AND status='failed'",
                (email_id,),
            )
            if cur.rowcount:
                await _event(conn, actor, "re-queued failed email", email_id)
            return bool(cur.rowcount)

    # -- approval gate ----------------------------------------------------------
    async def get_action(self, action_id: int) -> dict | None:
        return await self._one("SELECT * FROM actions WHERE id=%s", (action_id,))

    async def decide(self, action_id: int, *, approve: bool, actor: str,
                     expected_digest: str | None = None) -> dict | None:
        """Atomically move pending -> approved|rejected. Returns the row only if
        THIS call made the transition, so a double click can't execute twice.
        With `expected_digest`, raises StaleDraft (no transition) unless the stored
        payload is still the one the reviewer saw; the row lock keeps an edit from
        slipping in between the check and the transition."""
        verdict = "approved" if approve else "rejected"
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute("SELECT payload FROM actions WHERE id=%s AND status='pending' FOR UPDATE", (action_id,))
            locked = await cur.fetchone()
            if locked is None:
                return None
            if expected_digest is not None and expected_digest != payload_digest(locked["payload"]):
                raise StaleDraft(action_id)
            cur = await conn.execute(
                """UPDATE actions SET status=%s, decided_at=now(), decided_by=%s
                   WHERE id=%s AND status='pending' RETURNING *""",
                (verdict, actor, action_id),
            )
            row = await cur.fetchone()
            if row:
                await _event(conn, actor, f"{verdict} {row['kind']}", row["email_id"], action_id)
                if not approve:
                    await self._close_email_if_settled(conn, row["email_id"])
        return row

    async def record_execution(self, action_id: int, *, result: dict | None = None, error: str | None = None,
                               retryable: bool | None = None) -> None:
        """`retryable` only matters on failure: True iff the write never reached the API."""
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """UPDATE actions SET status=%s, result=%s, error=%s, retryable=%s, executed_at=now()
                   WHERE id=%s AND status='approved' RETURNING email_id, kind""",
                ("failed" if error else "executed", Jsonb(result) if result else None, error,
                 bool(retryable) if error else None, action_id),
            )
            row = await cur.fetchone()
            if row:
                msg = f"execution FAILED {row['kind']}: {error}" if error else f"executed {row['kind']}"
                await _event(conn, "system", msg, row["email_id"], action_id)
                await self._close_email_if_settled(conn, row["email_id"])

    async def update_payload(self, action_id: int, changes: dict, actor: str) -> dict | None:
        """Merge only the (already validated) changed fields, so two concurrent edits of
        different fields both survive. Returns the resulting payload, or None if the
        action is no longer pending."""
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """UPDATE actions SET payload = payload || %s::jsonb
                   WHERE id=%s AND status='pending' RETURNING email_id, payload""",
                (Jsonb(changes), action_id),
            )
            row = await cur.fetchone()
            if row:
                await _event(conn, actor, f"edited draft ({', '.join(sorted(changes))})", row["email_id"], action_id)
            return row["payload"] if row else None

    async def reopen_action(self, action_id: int, actor: str) -> bool:
        """failed (write never reached the API) -> pending: back to the approvals
        queue, to be reviewed and approved again. Uncertain failures stay failed."""
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """UPDATE actions SET status='pending', decided_at=NULL, decided_by=NULL, error=NULL,
                                      retryable=NULL, result=NULL, executed_at=NULL
                   WHERE id=%s AND status='failed' AND retryable RETURNING email_id, kind""",
                (action_id,),
            )
            row = await cur.fetchone()
            if row:
                # Same serialization as settlement: lock the email unconditionally first, so a
                # concurrent settle->done can't commit after we looked and leave done+pending.
                await conn.execute("SELECT 1 FROM emails WHERE id=%s FOR UPDATE", (row["email_id"],))
                await conn.execute(
                    "UPDATE emails SET status='needs_approval', updated_at=now() WHERE id=%s AND status='done'",
                    (row["email_id"],),
                )
                await _event(conn, actor, f"reopened failed {row['kind']} for re-approval (write never reached the API)",
                             row["email_id"], action_id)
            return row is not None

    @staticmethod
    async def _close_email_if_settled(conn: AsyncConnection, email_id: str) -> None:
        # Lock the email first: two actions settling concurrently each see the other's
        # (uncommitted) row as still open; serialized, the second sees the first's commit.
        await conn.execute("SELECT 1 FROM emails WHERE id=%s FOR UPDATE", (email_id,))
        await conn.execute(
            """UPDATE emails SET status='done', updated_at=now()
               WHERE id=%s AND status='needs_approval'
                 AND NOT EXISTS (SELECT 1 FROM actions WHERE email_id=%s AND status IN ('pending','approved'))""",
            (email_id, email_id),
        )

    # -- dashboard views ----------------------------------------------------------
    async def view_approvals(self) -> list[dict]:
        """row_type: 'action' (pending, approvable), 'exec_failed' (failed before the
        write reached the API; reopenable), 'failed' (email classification failed)."""
        rows = await self._all(
            """SELECT CASE WHEN a.status='pending' THEN 'action' ELSE 'exec_failed' END AS row_type,
                      a.id AS action_id, a.kind, a.payload, a.rationale, a.extra,
                      a.status AS action_status, a.error AS action_error, a.retryable,
                      e.id AS email_id, e.sender, e.subject, e.body, e.label, NULL AS last_error,
                      e.domain_verdict, e.domain_detail, e.received_at, e.injection_detected, e.injection_detail
               FROM actions a JOIN emails e ON e.id = a.email_id
               WHERE a.status='pending' OR (a.status='failed' AND a.retryable)
               UNION ALL
               SELECT 'failed', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                      id, sender, subject, body, label, last_error,
                      domain_verdict, domain_detail, received_at, injection_detected, injection_detail
               FROM emails WHERE status='failed'
               ORDER BY received_at DESC, email_id, action_id NULLS LAST"""
        )
        for r in rows:  # what the client echoes back on approve (see decide)
            r["payload_digest"] = payload_digest(r["payload"]) if r["payload"] is not None else None
        return rows

    async def view_working(self) -> list[dict]:
        return await self._all(
            """SELECT id AS email_id, sender, subject, body, status, attempts, last_error, received_at,
                      domain_verdict, domain_detail, injection_detected, injection_detail
               FROM emails WHERE status IN ('queued','classifying') ORDER BY received_at"""
        )

    async def view_history(self, limit: int = 200) -> list[dict]:
        return await self._all(
            """SELECT e.id AS email_id, e.sender, e.subject, e.body, e.label, e.status, e.reason, e.updated_at, e.received_at,
                      e.domain_verdict, e.domain_detail, e.injection_detected, e.injection_detail,
                      coalesce(json_agg(json_build_object('id', a.id, 'kind', a.kind, 'status', a.status,
                               'decided_by', a.decided_by, 'error', a.error, 'retryable', a.retryable, 'payload', a.payload, 'extra', a.extra) ORDER BY a.id)
                               FILTER (WHERE a.id IS NOT NULL), '[]') AS actions
               FROM emails e LEFT JOIN actions a ON a.email_id = e.id
               WHERE e.status IN ('done','dropped')
                  OR a.status IN ('rejected','executed','failed')
               GROUP BY e.id ORDER BY e.updated_at DESC LIMIT %s""",
            (limit,),
        )

    async def recent_events(self, after_id: int = 0, limit: int = 100) -> list[dict]:
        return await self._all(
            "SELECT * FROM events WHERE id > %s ORDER BY id DESC LIMIT %s",
            (after_id, limit),
        )
