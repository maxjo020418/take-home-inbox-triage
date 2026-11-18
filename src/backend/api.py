"""Backend HTTP API for the TUI (and any future client).

Run: uvicorn src.backend.api:app --host 0.0.0.0 --port 8000

Every route except /health and /auth/login requires `Authorization: Bearer
<session token>`. The poller and classifier workers run as background tasks in
this process; the queue lives in Postgres, so they can move to their own
process later without code changes.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from src.backend import auth, domain_check, pipeline
from src.backend.config import Settings, load_settings
from src.backend.db import DB
from src.triage_skill import apply_edit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per poll is noise
log = logging.getLogger("triage.api")
VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = load_settings()
    db = DB(s.database_url, max_size=s.workers + 6)
    await db.open()
    if await db.count_users() == 0:
        if s.bootstrap_user and s.bootstrap_password:
            await db.create_user(s.bootstrap_user, auth.hash_password(s.bootstrap_password))
            log.info("bootstrapped first user %r", s.bootstrap_user)
        else:
            log.warning("no users exist: set BOOTSTRAP_ADMIN_USER/PASSWORD or run `python -m src.backend.users add`")
    await asyncio.to_thread(domain_check.warm_up)  # precompute dnstwist lookalikes (~0.5s)
    if n := await db.recover_stale():
        log.info("re-queued %d stale job(s)", n)
    tasks = [asyncio.create_task(pipeline.poller(db, s), name="poller")]
    tasks += [asyncio.create_task(pipeline.worker(i, db, s), name=f"worker-{i}") for i in range(s.workers)]
    app.state.db, app.state.settings = db, s
    log.info("polling %s every %gs with %d workers", s.api_base_url, s.poll_interval_s, s.workers)
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await db.close()


app = FastAPI(title="Inbox Triage Backend", version=VERSION, lifespan=lifespan)


def get_db(request: Request) -> DB:
    return request.app.state.db


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


DBDep = Annotated[DB, Depends(get_db)]


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    return authorization.removeprefix("Bearer ").strip()


async def current_user(db: DBDep, authorization: str | None = Header(default=None)) -> dict:
    user = await db.user_for_session(auth.token_hash(_bearer(authorization)))
    if user is None:
        raise HTTPException(401, "invalid or expired session", headers={"WWW-Authenticate": "Bearer"})
    return user


User = Annotated[dict, Depends(current_user)]


# -- public -------------------------------------------------------------------
@app.get("/health")
async def health(db: DBDep, response: Response) -> dict:
    try:
        db_ok = await db.ping()
    except Exception:
        db_ok = False
    if not db_ok:  # so orchestrators / load balancers see the failure, not just the body
        response.status_code = 503
    return {"status": "ok" if db_ok else "degraded", "db": db_ok, "version": VERSION}


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LoginOut(BaseModel):
    token: str
    username: str
    expires_at: datetime


@app.post("/auth/login")
async def login(body: LoginIn, db: DBDep, s: Annotated[Settings, Depends(get_settings)]) -> LoginOut:
    user = await db.get_user(body.username)
    # Always run scrypt so a missing user and a wrong password take the same time.
    ok = await asyncio.to_thread(auth.verify_password, body.password, user["password_hash"] if user else auth.DUMMY_HASH)
    if not (user and ok):
        raise HTTPException(401, "invalid username or password")
    token = auth.new_session_token()
    expires = await db.create_session(user["id"], auth.token_hash(token), s.session_ttl_h)
    return LoginOut(token=token, username=user["username"], expires_at=expires)


# -- authenticated ------------------------------------------------------------
@app.post("/auth/logout", status_code=204)
async def logout(db: DBDep, user: User, authorization: str | None = Header(default=None)) -> None:
    await db.delete_session(auth.token_hash(_bearer(authorization)))


@app.get("/auth/me")
async def me(user: User) -> dict:
    return {"username": user["username"], "expires_at": user["expires_at"]}


@app.get("/dashboard")
async def dashboard(db: DBDep, user: User, events_after: int = 0) -> dict[str, Any]:
    """Everything the TUI renders, in one round trip (it polls this)."""
    approvals, working, history, events = await asyncio.gather(
        db.view_approvals(), db.view_working(), db.view_history(), db.recent_events(events_after)
    )
    return {"approvals": approvals, "working": working, "history": history, "events": events[::-1]}


STALE_DETAIL = "draft changed since you viewed it; review it again"
REOPEN_DETAIL = "only failed actions whose write never reached the API can be reopened"


def _outcome_response(outcome: str, detail: Any) -> dict:
    if outcome == "not_found":
        raise HTTPException(404, "no such action")
    if outcome == "already_decided":
        raise HTTPException(409, "action was already decided")
    if outcome == "stale":
        raise HTTPException(409, STALE_DETAIL)
    if outcome == "failed":
        raise HTTPException(502, f"approved, but execution failed: {detail}")
    return {"outcome": outcome, "result": detail}


class ApproveIn(BaseModel):
    # sha256 of the payload the reviewer saw (src.triage_skill.payload_digest; also served
    # as `payload_digest` on approval rows). Omitted = no staleness check (legacy clients).
    payload_digest: str | None = None


@app.post("/actions/{action_id}/approve")
async def approve_action(action_id: int, db: DBDep, user: User, s: Annotated[Settings, Depends(get_settings)],
                         body: ApproveIn | None = None) -> dict:
    digest = body.payload_digest if body else None
    return _outcome_response(*await pipeline.approve(db, s, action_id, user["username"], expected_digest=digest))


@app.post("/actions/{action_id}/reject")
async def reject_action(action_id: int, db: DBDep, user: User) -> dict:
    return _outcome_response(*await pipeline.reject(db, action_id, user["username"]))


class EditIn(BaseModel):
    changes: dict[str, str]


@app.patch("/actions/{action_id}")
async def edit_action(action_id: int, body: EditIn, db: DBDep, user: User) -> dict:
    """Edit draft wording before approval. Destination fields are not editable —
    enforced here, not in the client."""
    action = await db.get_action(action_id)
    if action is None:
        raise HTTPException(404, "no such action")
    if action["status"] != "pending":
        raise HTTPException(409, "only pending actions can be edited")
    try:  # validation only: the field whitelist depends on kind, never on current contents
        apply_edit(action["kind"], action["payload"], body.changes)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Merge just the changed fields in SQL, so a concurrent edit of another field survives.
    payload = await db.update_payload(action_id, body.changes, user["username"])
    if payload is None:
        raise HTTPException(409, "action was decided while editing")
    return {"payload": payload}


@app.post("/actions/{action_id}/reopen")
async def reopen_action(action_id: int, db: DBDep, user: User) -> dict:
    """failed-but-never-sent -> pending, to be reviewed and approved again."""
    if not await db.reopen_action(action_id, user["username"]):
        if await db.get_action(action_id) is None:
            raise HTTPException(404, "no such action")
        raise HTTPException(409, REOPEN_DETAIL)
    return {"outcome": "reopened"}


@app.post("/emails/{email_id}/retry")
async def retry_email(email_id: str, db: DBDep, user: User) -> dict:
    if not await db.retry_email(email_id, user["username"]):
        raise HTTPException(409, "only failed emails can be retried")
    return {"outcome": "queued"}
