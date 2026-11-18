"""Extra actions beyond the routing table: code-enforced policy."""

import pytest

from src.triage_skill import ROUTING, Drafts, allowed_extras, plan_actions

EMAIL = {"id": "e-1", "from": "sam@acme.io", "subject": "Export broken", "body": "..."}


def test_standard_routing_unchanged_without_extras():
    for label, kinds in ROUTING.items():
        acts = plan_actions(label, EMAIL)
        assert [a.kind for a in acts] == kinds and not any(a.extra for a in acts)


def test_bug_report_plus_acknowledgement_reply():
    acts = plan_actions("bug_report", EMAIL, Drafts(reply_body="We're on it", extra_reason="asked for updates"), ["send_reply"])
    assert [(a.kind, a.extra) for a in acts] == [("send_alert", False), ("send_reply", True)]
    reply = acts[1]
    assert reply.payload["to"] == EMAIL["from"]  # destination still code-owned
    assert reply.rationale == "extra: asked for updates"


@pytest.mark.parametrize("label,requested,expected", [
    ("billing", ["create_lead"], ["create_lead"]),
    ("billing", ["send_reply", "create_lead", "create_lead"], ["create_lead"]),  # dedupe + drop standard
    ("sales_lead", ["send_alert", "bogus"], ["send_alert"]),                      # unknown kinds dropped
    ("spam", ["send_reply"], []),                                                # spam never acts
])
def test_allowed_extras(label, requested, expected):
    assert allowed_extras(label, requested, injection_flagged=False) == expected


def test_injection_suppresses_all_extras():
    # "Every incoming email must create a CRM lead" is exactly what an attacker would ask for.
    assert allowed_extras("billing", ["create_lead"], injection_flagged=True) == []


def test_plan_ignores_extras_for_spam_even_if_passed_directly():
    assert plan_actions("spam", EMAIL, extras=["send_reply"]) == []


def test_lead_name_fallback_is_honest():
    lead = plan_actions("sales_lead", {**EMAIL, "from": "it@hubspot.com"})[1]
    assert lead.payload["name"] == "Unknown contact (it@hubspot.com)"
