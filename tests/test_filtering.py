"""Dashboard filter query syntax."""

from src.tui.filtering import compile_query

APPROVAL = {"row_type": "action", "email_id": "e-003", "sender": "priya@northwind.com", "subject": "Pilot for ops",
            "label": "sales_lead", "kind": "create_lead", "domain_verdict": "ok",
            "payload": {"email": "priya@northwind.com", "summary": "12-seat pilot"}}
SPAM = {"email_id": "imp-001", "sender": "x@micr0soft.com", "subject": "Password expires", "label": "spam",
        "status": "dropped", "domain_verdict": "impersonation_suspected", "actions": []}
HIST = {"email_id": "e-001", "sender": "dana@meridian.com", "subject": "Charged twice", "label": "billing",
        "status": "done", "domain_verdict": "ok", "actions": [{"kind": "send_reply", "status": "executed"}]}
ROWS = [APPROVAL, SPAM, HIST]


def ids(q):
    m = compile_query(q)
    return [r["email_id"] for r in ROWS if m(r)]


def test_empty_matches_all():
    assert ids("") == ids("   ") == ["e-003", "imp-001", "e-001"]


def test_free_text_is_case_insensitive_and_searches_preview():
    assert ids("PILOT") == ["e-003"]
    assert ids("12-seat") == ["e-003"]  # payload summary


def test_field_terms():
    assert ids("label:spam") == ["imp-001"]
    assert ids("domain:imp") == ["imp-001"]
    assert ids("action:send_reply") == ["e-001"]
    assert ids("status:executed") == ["e-001"]
    assert ids("to:priya") == ["e-003"]


def test_negation_and_conjunction():
    assert ids("-label:spam") == ["e-003", "e-001"]
    assert ids("-spam") == ["e-003", "e-001"]
    assert ids("domain:ok -action:create_lead") == ["e-001"]


def test_quotes_and_unknown_field_prefix():
    assert ids('"charged twice"') == ["e-001"]
    assert ids("foo:bar") == []  # unknown field -> literal text "foo:bar"
    assert ids('"unterminated') == []  # mid-typing quote doesn't crash
