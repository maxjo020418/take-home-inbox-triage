"""Deterministic tests for routing, the approval gate and least privilege.
No LLM, no DB, no running server: HTTP goes to an in-memory transport."""

import httpx
import pytest

from src.triage_skill import (
    LABELS, ROUTING, Drafts, ProposedAction, TriageClient, WriteScopeError, execute, plan_actions, triage_inbox,
)

EMAIL = {"id": "e-1", "from": "dana@meridian.com", "subject": "Charged twice", "body": "..."}


class Recorder:
    def __init__(self, inbox=None):
        self.calls: list[tuple[str, str, str | None]] = []
        self.inbox = inbox or [EMAIL]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path, req.headers.get("authorization")))
        if req.url.path == "/inbox":
            return httpx.Response(200, json=self.inbox)
        return httpx.Response(200, json={"status": "ok"})

    @property
    def writes(self):
        return [c for c in self.calls if c[0] == "POST"]


def client(rec, write=None):
    return TriageClient("http://mock", "read-tok", write, transport=httpx.MockTransport(rec))


def test_routing_table_matches_readme():
    assert ROUTING == {
        "billing": ["send_reply"],
        "bug_report": ["send_alert"],
        "sales_lead": ["send_reply", "create_lead"],
        "spam": [],
    }


@pytest.mark.parametrize("label", LABELS)
def test_plan_kinds_follow_routing(label):
    assert [a.kind for a in plan_actions(label, EMAIL)] == ROUTING[label]


def test_unknown_label_rejected():
    with pytest.raises(ValueError):
        plan_actions("urgent", EMAIL)


def test_llm_drafts_cannot_choose_destination():
    # Even if the model's text is hostile, recipient/channel come from code.
    d = Drafts(reply_body="ok", alert_summary="x", lead_name="Eve")
    reply = plan_actions("billing", EMAIL, d)[0].payload
    alert = plan_actions("bug_report", EMAIL, d)[0].payload
    lead = plan_actions("sales_lead", EMAIL, d)[1].payload
    assert reply["to"] == EMAIL["from"] and reply["in_reply_to"] == "e-1"
    assert alert["channel"] == "#engineering"
    assert lead["email"] == EMAIL["from"]


@pytest.mark.parametrize("approved", [False, None, 1, "yes"])
def test_unapproved_never_calls_client(approved):
    rec = Recorder()
    action = plan_actions("billing", EMAIL)[0]
    assert execute(action, client(rec, "write-tok"), approved=approved) is None
    assert rec.calls == []


def test_approved_executes_with_write_token():
    rec = Recorder()
    execute(plan_actions("bug_report", EMAIL)[0], client(rec, "write-tok"), approved=True)
    assert rec.writes == [("POST", "/slack/alert", "Bearer write-tok")]


def test_read_only_client_refuses_writes_locally():
    rec = Recorder()
    with pytest.raises(WriteScopeError):
        execute(ProposedAction("send_reply", {"to": "a", "subject": "b", "body": "c"}), client(rec), approved=True)
    assert rec.calls == []


def test_inbox_read_uses_read_token_even_on_write_client():
    rec = Recorder()
    client(rec, "write-tok").get_inbox(since="2026-01-01T00:00:00Z")
    assert rec.calls[0][2] == "Bearer read-tok"


def test_triage_inbox_rejecting_approver_makes_no_writes():
    rec = Recorder(inbox=[EMAIL, {**EMAIL, "id": "e-2"}])
    results = triage_inbox(client(rec, "write-tok"), approver=lambda e, a: False, classifier=lambda e: "sales_lead")
    assert [r.label for r in results] == ["sales_lead", "sales_lead"]
    assert rec.writes == []


def test_triage_inbox_spam_plans_nothing():
    rec = Recorder()
    results = triage_inbox(client(rec, "write-tok"), approver=lambda e, a: True, classifier=lambda e: "spam")
    assert results[0].actions == [] and rec.writes == []


def test_invalid_classifier_output_raises():
    with pytest.raises(ValueError):
        triage_inbox(client(Recorder()), approver=lambda e, a: True, classifier=lambda e: "SPAM!!")
