"""Mock client API for the Inbox Triage take-home.

This stands in for a real client's systems: an inbox to read from, an outbound
mail service, and a CRM. You do not need to modify this file (though you may read
it). Run it with `make serve`.

Auth model — read it carefully, the security requirements depend on it:

  * `GET /inbox` accepts EITHER the read token or the write token.
  * `GET /inbox` takes optional `since` / `until` / `limit` query params
    (added to the original scaffold so clients can poll incrementally).
  * `POST /_inject` and `POST /_inject/batch` simulate new email arriving, and
    `GET /_admin` is a plain HTML admin page (unauthenticated dev hooks, like
    `/_audit`; compose binds the port to localhost only).
  * All write endpoints (`/mail/send`, `/slack/alert`, `/crm/lead`) require the
    WRITE token specifically. The read token is rejected with 403.

Tokens are read from the environment (see .env.example). Pass them as
`Authorization: Bearer <token>`.
"""

from __future__ import annotations

import html
import json
import os
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

READ_TOKEN = os.environ.get("READ_TOKEN", "read-token-dev")
WRITE_TOKEN = os.environ.get("WRITE_TOKEN", "write-token-dev")

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "emails.json"

app = FastAPI(title="Inbox Triage — Mock Client API", version="1.0.0")

# In-memory record of everything the agent has written, so the grader (and you)
# can inspect side effects. Resets on restart.
_sent_mail: list[dict] = []
_alerts: list[dict] = []
_leads: list[dict] = []
# Emails added at runtime via POST /_inject (demo/testing hook). Resets on restart.
_injected: list[dict] = []
# Ground-truth labels for injected dataset rows. Shown on the admin page ONLY —
# never returned by /inbox, so the agent can't see the answer key.
_expected: dict[str, str] = {}
_lock = threading.Lock()  # sync handlers run in a threadpool


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization.removeprefix("Bearer ").strip()


def require_read(authorization: str | None = Header(default=None)) -> None:
    token = _bearer(authorization)
    if token not in (READ_TOKEN, WRITE_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token for read scope")


def require_write(authorization: str | None = Header(default=None)) -> None:
    token = _bearer(authorization)
    if token == READ_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="Read token cannot perform writes — write scope required",
        )
    if token != WRITE_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token for write scope")


class Reply(BaseModel):
    to: str
    subject: str
    body: str
    in_reply_to: str | None = None


class Alert(BaseModel):
    channel: str
    message: str


class Lead(BaseModel):
    name: str
    email: str
    company: str | None = None
    summary: str | None = None


class InjectedEmail(BaseModel):
    """Extra keys (e.g. dataset metadata) are accepted and ignored, except
    `label`, which is kept as the admin-only expected label."""

    model_config = {"extra": "ignore", "populate_by_name": True}

    id: str | None = Field(None, max_length=128)  # used as a readable prefix of the id
    from_: str = Field(alias="from", min_length=1, max_length=320)
    subject: str = Field("", max_length=1000)
    body: str = Field("", max_length=50_000)
    received_at: datetime | None = None  # defaults to "now"
    label: str | None = None


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@app.get("/inbox")
def get_inbox(
    _: None = Depends(require_read),
    since: datetime | None = Query(default=None, description="Inclusive lower bound on received_at (ISO 8601)"),
    until: datetime | None = Query(default=None, description="Exclusive upper bound on received_at (ISO 8601)"),
    limit: int | None = Query(default=None, ge=1, le=500, description="Max emails returned, oldest first"),
) -> list[dict]:
    """All filters are optional; with none, behaves exactly like the original
    endpoint (every email). Results are sorted oldest-first so a client can use
    the last `received_at` it saw as a cursor for the next poll."""
    for bound in (since, until):
        if bound is not None and bound.tzinfo is None:
            raise HTTPException(status_code=422, detail="since/until must include a timezone (e.g. Z)")
    emails = json.loads(FIXTURES.read_text()) + _injected
    if since is not None:
        emails = [e for e in emails if _parse_ts(e["received_at"]) >= since]
    if until is not None:
        emails = [e for e in emails if _parse_ts(e["received_at"]) < until]
    emails.sort(key=lambda e: _parse_ts(e["received_at"]))
    return emails[:limit] if limit else emails


def _store(email: InjectedEmail, taken: set[str]) -> dict:
    received = email.received_at or datetime.now(UTC)
    if received.tzinfo is None:
        received = received.replace(tzinfo=UTC)
    # Always add a random suffix: ids must be unique across mock restarts, because
    # the backend dedupes by id and would silently skip a reused one (real mail
    # providers never reuse message ids). A dataset id stays readable as the prefix.
    email_id = f"{email.id or 'inj'}.{secrets.token_hex(3)}"
    while email_id in taken:
        email_id = f"{email.id or 'inj'}.{secrets.token_hex(3)}"
    record = {
        "id": email_id,
        "from": email.from_,
        "subject": email.subject,
        "received_at": received.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "body": email.body,
    }
    _injected.append(record)
    taken.add(email_id)
    if email.label:
        _expected[email_id] = email.label
    return record


def _taken_ids() -> set[str]:
    return {e["id"] for e in json.loads(FIXTURES.read_text())} | {e["id"] for e in _injected}


@app.post("/_inject", status_code=201)
def inject_email(email: InjectedEmail) -> dict:
    """Simulate a new email arriving (demo/testing hook, like /_audit)."""
    with _lock:
        return _store(email, _taken_ids())


@app.post("/_inject/batch", status_code=201)
def inject_batch(emails: list[InjectedEmail]) -> dict:
    """Inject many emails at once (e.g. a JSONL dataset parsed by the admin page)."""
    if not 1 <= len(emails) <= 2000:
        raise HTTPException(status_code=422, detail="batch must contain 1-2000 emails")
    with _lock:
        taken = _taken_ids()
        records = [_store(e, taken) for e in emails]
    return {"injected": len(records), "ids": [r["id"] for r in records]}


@app.post("/mail/send")
def send_mail(reply: Reply, _: None = Depends(require_write)) -> dict:
    record = {"id": f"mail-{len(_sent_mail) + 1}", **reply.model_dump()}
    _sent_mail.append(record)
    return {"status": "sent", **record}


@app.post("/slack/alert")
def send_alert(alert: Alert, _: None = Depends(require_write)) -> dict:
    record = {"id": f"alert-{len(_alerts) + 1}", **alert.model_dump()}
    _alerts.append(record)
    return {"status": "posted", **record}


@app.post("/crm/lead")
def create_lead(lead: Lead, _: None = Depends(require_write)) -> dict:
    record = {"id": f"lead-{len(_leads) + 1}", **lead.model_dump()}
    _leads.append(record)
    return {"status": "created", **record}


@app.get("/_audit")
def audit() -> dict:
    """Inspect side effects produced so far (handy while you build)."""
    return {"sent_mail": _sent_mail, "alerts": _alerts, "leads": _leads}


def _table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "<p><i>none</i></p>"
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(r.get(c) if r.get(c) is not None else ''))}</td>" for c in cols) + "</tr>"
        for r in rows
    )
    return f"<table border='1' cellpadding='4' cellspacing='0'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


@app.get("/_admin", response_class=HTMLResponse)
def admin_page() -> str:
    """Unauthenticated dev admin page (like /_audit). Bind to localhost only."""
    fixtures = [{**e, "source": "fixture"} for e in json.loads(FIXTURES.read_text())]
    injected = [{**e, "source": "injected", "expected_label": _expected.get(e["id"])} for e in _injected]
    inbox = sorted(fixtures + injected, key=lambda e: _parse_ts(e["received_at"]), reverse=True)
    for e in inbox:
        e["body"] = e["body"] if len(e["body"]) <= 300 else e["body"][:300] + "…"
    return ADMIN_HTML.format(
        n_inbox=len(inbox), n_mail=len(_sent_mail), n_alerts=len(_alerts), n_leads=len(_leads),
        inbox=_table(inbox, ["received_at", "id", "source", "expected_label", "from", "subject", "body"]),
        mail=_table(_sent_mail[::-1], ["id", "to", "subject", "in_reply_to", "body"]),
        alerts=_table(_alerts[::-1], ["id", "channel", "message"]),
        leads=_table(_leads[::-1], ["id", "name", "email", "company", "summary"]),
    )


# Plain HTML on purpose (raw string: the JS regexes need literal backslashes).
# All dynamic values above go through html.escape; the
# script only reads files locally and POSTs JSON back to this server.
ADMIN_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Email server console</title>
<style>
  /* Functional only: tabs show one section at a time; tables scroll inside a box. */
  .tab {{ display: none; }}
  .tab.active {{ display: block; }}
  nav button.active {{ font-weight: bold; text-decoration: underline; }}
  .scroll {{ max-height: 70vh; overflow: auto; border: 1px solid #999; }}
  .scroll th {{ position: sticky; top: 0; background: #eee; }}
  .scroll td {{ vertical-align: top; }}
</style></head>
<body>
<h1>Email server console</h1>
<p><a href="javascript:location.reload()">refresh</a> · <a href="/_audit">/_audit (JSON)</a> · <a href="/docs">API docs</a></p>
<nav>
  <button data-tab="send">Send</button>
  <button data-tab="inbox">Inbox ({n_inbox})</button>
  <button data-tab="mail">Sent mail ({n_mail})</button>
  <button data-tab="alerts">Slack alerts ({n_alerts})</button>
  <button data-tab="leads">CRM leads ({n_leads})</button>
</nav>
<hr>

<section class="tab" id="tab-send">
<h2>Send one email (appears on GET /inbox)</h2>
<form id="one">
  <p>From: <input name="from" size="40" required value="customer@example.com"></p>
  <p>Subject: <input name="subject" size="60"></p>
  <p>Body:<br><textarea name="body" rows="6" cols="80"></textarea></p>
  <p>Expected label (admin-only, optional):
    <select name="label"><option value="">-</option><option>billing</option><option>bug_report</option>
    <option>sales_lead</option><option>spam</option></select></p>
  <button>Send</button>
</form>

<h2>Batch send (.jsonl / .json file, or paste)</h2>
<p>One JSON object per line (or a JSON array). Uses <code>from</code>, <code>subject</code>, <code>body</code>;
optional <code>id</code> (becomes the id prefix), <code>received_at</code>, <code>label</code> (kept admin-only). Other keys are ignored.</p>
<form id="batch">
  <p><input type="file" id="file" accept=".jsonl,.json,.ndjson,.txt"></p>
  <p>or paste:<br><textarea id="paste" rows="6" cols="80" placeholder='{{"from": "a@b.com", "subject": "Hi", "body": "..."}}'></textarea></p>
  <p>Only rows where <input id="filter" size="30" placeholder='e.g. split=test or is_prompt_injection=true'> (optional)
     · limit <input id="limit" size="5" placeholder="all"></p>
  <button>Send batch</button>
</form>
<pre id="status"></pre>
</section>

<section class="tab" id="tab-inbox"><h2>Inbox ({n_inbox})</h2><div class="scroll">{inbox}</div></section>
<section class="tab" id="tab-mail"><h2>Sent mail ({n_mail})</h2><div class="scroll">{mail}</div></section>
<section class="tab" id="tab-alerts"><h2>Slack alerts ({n_alerts})</h2><div class="scroll">{alerts}</div></section>
<section class="tab" id="tab-leads"><h2>CRM leads ({n_leads})</h2><div class="scroll">{leads}</div></section>

<script>
const statusEl = document.getElementById("status");
// Tabs live in the URL hash, so a reload (after sending) keeps you on the same tab.
function showTab(name) {{
  if (!document.getElementById("tab-" + name)) name = "send";
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.id === "tab-" + name));
  document.querySelectorAll("nav button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
}}
document.querySelectorAll("nav button").forEach(b => b.onclick = () => {{ location.hash = b.dataset.tab; }});
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
showTab(location.hash.slice(1));
function reloadOn(tab) {{ location.hash = tab; location.reload(); }}
async function post(url, data) {{
  const r = await fetch(url, {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify(data)}});
  const out = await r.json();
  if (!r.ok) throw new Error(JSON.stringify(out.detail ?? out));
  return out;
}}
function parse(text) {{
  text = text.trim();
  if (!text) return [];
  if (text.startsWith("[")) return JSON.parse(text);
  return text.split(/\r?\n/).filter(l => l.trim()).map((l, i) => {{
    try {{ return JSON.parse(l); }} catch (e) {{ throw new Error("line " + (i + 1) + ": " + e.message); }}
  }});
}}
document.getElementById("one").onsubmit = async (ev) => {{
  ev.preventDefault();
  const f = Object.fromEntries(new FormData(ev.target));
  if (!f.label) delete f.label;
  try {{ const r = await post("/_inject", f); statusEl.textContent = "sent " + r.id; reloadOn("inbox"); }}
  catch (e) {{ statusEl.textContent = "error: " + e.message; }}
}};
document.getElementById("batch").onsubmit = async (ev) => {{
  ev.preventDefault();
  try {{
    const file = document.getElementById("file").files[0];
    let rows = parse(file ? await file.text() : document.getElementById("paste").value);
    const flt = document.getElementById("filter").value.trim();
    if (flt) {{
      const [k, v] = flt.split("=").map(s => s.trim());
      rows = rows.filter(r => String(r[k]) === v);
    }}
    const lim = parseInt(document.getElementById("limit").value);
    if (lim > 0) rows = rows.slice(0, lim);
    if (!rows.length) {{ statusEl.textContent = "nothing to send"; return; }}
    const r = await post("/_inject/batch", rows);
    statusEl.textContent = "sent " + r.injected + " emails";
    reloadOn("inbox");
  }} catch (e) {{ statusEl.textContent = "error: " + e.message; }}
}};
</script>
</body></html>
"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8099)
