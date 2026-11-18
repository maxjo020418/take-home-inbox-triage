"""Inbox Triage skill worker — core, framework-free logic.

Everything here is pure or a thin HTTP wrapper, so it can be tested without the
TUI, the database, or a live model. The app (src/backend/) wires it together.

Security model:
  * The LLM chooses the *label* and drafts *text*. Code decides *which actions*
    exist (ROUTING) and *where they go* (recipient = original sender, Slack
    channel fixed). A prompt-injected email cannot redirect a write.
  * A TriageClient built without a write token cannot write — it refuses before
    any network call. Only the approval path ever builds one with the token.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

# The only four labels a triage may produce.
LABELS = ("billing", "bug_report", "sales_lead", "spam")

# Which actions each classification implies. `spam` implies none.
ROUTING: dict[str, list[str]] = {
    "billing": ["send_reply"],
    "bug_report": ["send_alert"],
    "sales_lead": ["send_reply", "create_lead"],
    "spam": [],
}

# Action kinds your plan may contain.
ACTION_KINDS = ("send_reply", "send_alert", "create_lead")

ENGINEERING_CHANNEL = "#engineering"


@dataclass
class ProposedAction:
    """An action the agent WANTS to take. Proposing is not doing — nothing here
    touches the outside world until it has been approved and executed."""

    kind: str
    payload: dict
    # Every external write requires the write scope. Reads/no-ops do not.
    requires_write: bool = True
    rationale: str = ""
    # True for an action the model suggested beyond the label's standard routing.
    extra: bool = False


@dataclass
class TriageResult:
    email_id: str
    label: str
    actions: list[ProposedAction] = field(default_factory=list)


@dataclass
class Drafts:
    """LLM-written text that fills action payloads. Every field is optional:
    plan_actions falls back to a safe template if the model omitted one."""

    reason: str = ""
    reply_subject: str | None = None
    reply_body: str | None = None
    alert_summary: str | None = None
    lead_name: str | None = None
    lead_company: str | None = None
    lead_summary: str | None = None
    extra_reason: str | None = None


class WriteScopeError(PermissionError):
    """Raised when a write is attempted on a client that holds no write token."""


class TriageClient:
    """Thin wrapper over the mock API.

    Construct with `write_token=None` (the default) for anything that only
    reads. Write methods on such a client raise WriteScopeError locally.
    """

    def __init__(
        self,
        base_url: str,
        read_token: str,
        write_token: str | None = None,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self._read_token = read_token
        self._write_token = write_token

    @property
    def can_write(self) -> bool:
        return self._write_token is not None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TriageClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- read scope ---------------------------------------------------------
    def get_inbox(self, *, since: str | None = None, limit: int | None = None) -> list[dict]:
        params = {k: v for k, v in {"since": since, "limit": limit}.items() if v is not None}
        r = self._http.get("/inbox", params=params, headers=self._auth(self._read_token))
        r.raise_for_status()
        return r.json()

    # -- write scope --------------------------------------------------------
    def send_reply(self, *, to: str, subject: str, body: str, in_reply_to: str | None = None) -> dict:
        return self._write("/mail/send", {"to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to})

    def send_alert(self, *, channel: str, message: str) -> dict:
        return self._write("/slack/alert", {"channel": channel, "message": message})

    def create_lead(self, *, name: str, email: str, company: str | None = None, summary: str | None = None) -> dict:
        return self._write("/crm/lead", {"name": name, "email": email, "company": company, "summary": summary})

    def _write(self, path: str, body: dict) -> dict:
        if self._write_token is None:
            raise WriteScopeError(f"client has no write scope; refusing {path}")
        r = self._http.post(path, json=body, headers=self._auth(self._write_token))
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _auth(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}


def classify_email(email: dict) -> str:
    """Return exactly one of LABELS (sync convenience wrapper over the agent)."""
    from src.backend.agent import triage_email_sync  # lazy: keeps this module LLM-free

    return triage_email_sync(email).label


def allowed_extras(label: str, requested: list[str] | tuple[str, ...], *, injection_flagged: bool) -> list[str]:
    """Which model-suggested extra actions may be proposed on top of ROUTING[label].

    Code policy, not model judgment: spam gets nothing; an email carrying a
    prompt-injection attempt gets no extras (an attacker's easiest lever is
    "also create a CRM lead"); duplicates of the standard routing are dropped.
    """
    if label == "spam" or injection_flagged:
        return []
    standard = set(ROUTING[label])
    out: list[str] = []
    for kind in requested:
        if kind in ACTION_KINDS and kind not in standard and kind not in out:
            out.append(kind)
    return out


def _fallback_name(sender: str) -> str:
    return f"Unknown contact ({sender})"


def plan_actions(label: str, email: dict, drafts: Drafts | None = None,
                 extras: list[str] | tuple[str, ...] = ()) -> list[ProposedAction]:
    """Turn a classification into the actions it implies: the routing table's
    standard actions, then any `extras` (already vetted by `allowed_extras`).

    Pure and deterministic — no network, no LLM, no side effects. `spam` plans
    nothing. Recipients and channels come from code, never from `drafts`.
    """
    if label not in ROUTING:
        raise ValueError(f"unknown label {label!r}")
    d = drafts or Drafts()
    sender = email["from"]
    subject = email.get("subject", "")
    why = d.reason
    plan = [(k, False) for k in ROUTING[label]]
    if label != "spam":
        plan += [(k, True) for k in extras if k in ACTION_KINDS and k not in ROUTING[label]]

    actions: list[ProposedAction] = []
    for kind, is_extra in plan:
        if kind == "send_reply":
            payload = {
                "to": sender,
                "subject": d.reply_subject or (subject if subject.lower().startswith("re:") else f"Re: {subject}"),
                "body": d.reply_body or "Thanks for reaching out — a member of our team will follow up shortly.",
                "in_reply_to": email["id"],
            }
        elif kind == "send_alert":
            summary = d.alert_summary or subject
            payload = {
                "channel": ENGINEERING_CHANNEL,
                "message": f"[{email['id']}] Bug report from {sender}: {summary}",
            }
        elif kind == "create_lead":
            payload = {
                "name": d.lead_name or _fallback_name(sender),
                "email": sender,
                "company": d.lead_company,
                "summary": d.lead_summary or subject,
            }
        else:  # pragma: no cover - ROUTING only contains ACTION_KINDS
            raise ValueError(kind)
        rationale = f"extra: {d.extra_reason}" if is_extra and d.extra_reason else why
        actions.append(ProposedAction(kind=kind, payload=payload, requires_write=True, rationale=rationale, extra=is_extra))
    return actions


_DISPATCH: dict[str, Callable[[TriageClient], Callable[..., dict]]] = {
    "send_reply": lambda c: c.send_reply,
    "send_alert": lambda c: c.send_alert,
    "create_lead": lambda c: c.create_lead,
}


def execute(action: ProposedAction, client: TriageClient, *, approved: bool) -> dict | None:
    """Execute a single proposed action — but only if a human approved it.

    This is the human-in-the-loop gate. If `approved` is False, NOTHING external
    happens: returns None without touching the client.
    """
    if approved is not True:  # identity check: truthy non-bools don't count
        return None
    if action.kind not in _DISPATCH:
        raise ValueError(f"unknown action kind {action.kind!r}")
    return _DISPATCH[action.kind](client)(**action.payload)


def triage_inbox(
    client: TriageClient,
    approver: Callable[[dict, ProposedAction], bool],
    classifier: Callable[[dict], str] = classify_email,
) -> list[TriageResult]:
    """Synchronous one-shot run (the README's suggested shape). The TUI uses the
    same pieces asynchronously via the DB-backed queue instead."""
    results = []
    for email in client.get_inbox():
        label = classifier(email)
        if label not in LABELS:
            raise ValueError(f"classifier returned invalid label {label!r}")
        actions = plan_actions(label, email)
        for action in actions:
            execute(action, client, approved=approver(email, action) is True)
        results.append(TriageResult(email_id=email["id"], label=label, actions=actions))
    return results


# Fields a human may edit before approving. Destination fields (to, channel,
# email, in_reply_to) are deliberately absent: reviewers change wording, not where
# the write goes.
EDITABLE_FIELDS: dict[str, frozenset[str]] = {
    "send_reply": frozenset({"subject", "body"}),
    "send_alert": frozenset({"message"}),
    "create_lead": frozenset({"name", "company", "summary"}),
}


def apply_edit(kind: str, payload: dict, changes: dict) -> dict:
    """Return `payload` with `changes` applied, refusing non-editable fields."""
    allowed = EDITABLE_FIELDS.get(kind)
    if allowed is None:
        raise ValueError(f"unknown action kind {kind!r}")
    if bad := set(changes) - allowed:
        raise ValueError(f"not editable for {kind}: {', '.join(sorted(bad))}")
    if not all(isinstance(v, str) for v in changes.values()):
        raise ValueError("edited values must be strings")
    return {**payload, **changes}


def payload_digest(payload: dict) -> str:
    """Stable fingerprint of an action payload. The reviewer's client sends the
    digest of the draft it displayed; approval is refused if the stored payload
    no longer matches (someone edited it in between)."""
    import hashlib
    import json

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()
