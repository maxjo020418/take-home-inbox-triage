"""Background loops (poller, classifier workers) and the approval executor.

Privilege boundaries:
  poller   -> read-only TriageClient (no write token in scope)
  workers  -> no API client at all; DB + LLM only
  approve  -> builds a write-capable client per approved action, then drops it
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, timedelta
from typing import Literal

from src.backend.agent import TriageOutput, triage_email
from src.backend.config import Settings, write_token
from src.backend.db import DB
from src.backend import injection
from src.backend.domain_check import check_sender
from src.triage_skill import ROUTING, ProposedAction, TriageClient, allowed_extras, execute, plan_actions

log = logging.getLogger("triage.pipeline")
Classifier = Callable[..., Awaitable[TriageOutput]]  # (email, *, sender_check=str|None)


async def poll_once(db: DB, s: Settings) -> int:
    """Fetch emails newer than our cursor (minus an overlap window for late
    arrivals) and insert the new ones. Dedupe is by email id in the DB."""
    latest = await db.latest_received_at()
    since = None
    if latest is not None:
        since = (latest - timedelta(seconds=s.poll_overlap_s)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    with TriageClient(s.api_base_url, s.read_token) as client:  # read scope only
        emails = await asyncio.to_thread(client.get_inbox, since=since)
    return await db.insert_emails(emails)


async def poller(db: DB, s: Settings) -> None:
    while True:
        try:
            if new := await poll_once(db, s):
                log.info("ingested %d new email(s)", new)
        except Exception:  # keep polling through transient API errors
            log.exception("poll failed")
        await asyncio.sleep(s.poll_interval_s)


def _email_dict(row: dict) -> dict:
    return {"id": row["id"], "from": row["sender"], "subject": row["subject"], "body": row["body"]}


async def process_one(db: DB, s: Settings, classify: Classifier = triage_email) -> bool:
    """Claim and classify one queued email. Returns False if the queue was empty."""
    job = await db.claim_next()
    if job is None:
        return False
    email = _email_dict(job)
    sender_check = None
    try:  # deterministic signal first; a DNS problem must not block classification
        dc = await asyncio.to_thread(check_sender, email["from"])
        await db.set_domain_check(job["id"], dc.verdict, dc.to_dict())
        sender_check = dc.summary()
    except Exception:
        log.exception("%s domain check failed", job["id"])
    try:
        out = await classify(email, sender_check=sender_check)
        inj = {"llm": out.injection_detected, "patterns": injection.detect(email["subject"], email["body"])}
        flagged = bool(inj["llm"] or inj["patterns"])
        extras = allowed_extras(out.label, out.extra_actions, injection_flagged=flagged)
        if suppressed := [k for k in out.extra_actions if k not in extras and k not in ROUTING[out.label]]:
            inj["suppressed_extras"] = suppressed  # recorded for the reviewer / audit
        actions = plan_actions(out.label, email, out.drafts(), extras)
        await db.save_triage(job["id"], out.label, out.reason, actions, inj)
        log.info("%s -> %s (%d action(s), extras=%s)", job["id"], out.label, len(actions), extras or "-")
    except Exception as exc:
        status = await db.mark_failed(job["id"], f"{type(exc).__name__}: {exc}", s.max_attempts)
        log.warning("%s classification error (%s): %r", job["id"], status, exc)
    return True


async def worker(n: int, db: DB, s: Settings) -> None:
    while True:
        try:
            if not await process_one(db, s):
                await asyncio.sleep(0.5)  # idle; LISTEN/NOTIFY would remove this poll
        except Exception:  # DB hiccup: back off, don't kill the worker
            log.exception("worker %d error", n)
            await asyncio.sleep(2)


Outcome = Literal["executed", "rejected", "already_decided", "failed", "not_found"]


async def approve(db: DB, s: Settings, action_id: int, actor: str) -> tuple[Outcome, dict | str | None]:
    """Human said yes: transition, then execute with write scope."""
    row = await db.decide(action_id, approve=True, actor=actor)
    if row is None:
        return ("already_decided" if await db.get_action(action_id) else "not_found"), None
    action = ProposedAction(kind=row["kind"], payload=row["payload"], rationale=row["rationale"])
    try:
        with TriageClient(s.api_base_url, s.read_token, write_token()) as client:
            result = await asyncio.to_thread(execute, action, client, approved=True)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        await db.record_execution(action_id, error=err)
        return "failed", err
    await db.record_execution(action_id, result=result)
    return "executed", result


async def reject(db: DB, action_id: int, actor: str) -> tuple[Outcome, None]:
    if await db.decide(action_id, approve=False, actor=actor):
        return "rejected", None
    return ("already_decided" if await db.get_action(action_id) else "not_found"), None
