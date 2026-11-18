"""Review fixes: mock /inbox incremental polling of back-dated injected mail, and the
evaluator's complete-amount comparison."""

import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mock_api import server

ROOT = Path(__file__).resolve().parent.parent


def _load_evaluate():
    spec = importlib.util.spec_from_file_location("evaluate", ROOT / "scripts" / "evaluate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -- mock server ------------------------------------------------------------------------------

@pytest.fixture
def client():
    """TestClient with the module-level in-memory state restored afterwards."""
    injected, expected = list(server._injected), dict(server._expected)
    try:
        yield TestClient(server.app, headers={"Authorization": f"Bearer {server.READ_TOKEN}"})
    finally:
        server._injected[:] = injected
        server._expected.clear()
        server._expected.update(expected)


def test_backdated_injected_email_visible_to_incremental_poll(client):
    before = client.get("/inbox").json()
    rec = client.post("/_inject", json={"from": "a@b.test", "subject": "old", "received_at": "2020-01-01T00:00:00Z"})
    assert rec.status_code == 201
    rec = rec.json()
    assert rec["received_at"] == "2020-01-01T00:00:00Z"  # metadata kept as given

    # A poller whose cursor is well past 2020 still sees it (it arrived just now)...
    polled = client.get("/inbox", params={"since": "2024-06-01T00:00:00Z"}).json()
    assert rec["id"] in [e["id"] for e in polled]
    # ...but not once the cursor is past its arrival time.
    later = client.get("/inbox", params={"since": "2999-01-01T00:00:00Z"}).json()
    assert rec["id"] not in [e["id"] for e in later]
    # `until` stays on received_at.
    assert rec["id"] in [e["id"] for e in client.get("/inbox", params={"until": "2020-01-02T00:00:00Z"}).json()]

    # No params: same as before plus the new email, oldest-first, no internal fields.
    after = client.get("/inbox").json()
    assert len(after) == len(before) + 1
    assert after[0]["id"] == rec["id"]  # sorted by received_at
    for resp in (after, polled):
        assert all(set(e) == {"id", "from", "subject", "received_at", "body"} for e in resp)


def test_fixture_since_filter_unchanged(client):
    all_mail = client.get("/inbox").json()
    cutoff = all_mail[len(all_mail) // 2]["received_at"]
    got = client.get("/inbox", params={"since": cutoff}).json()
    assert got == [e for e in all_mail if e["received_at"] >= cutoff]


# -- evaluator ----------------------------------------------------------------------------------

@pytest.mark.parametrize("draft,body,flagged", [
    ("We will refund $14.", "I was charged $149 twice.", ["$14"]),
    ("Your $149 charge", "I paid $149.00 yesterday", []),
    ("The $1,200 invoice", "invoice for $1200", []),
    ("That's $ 50, not $75", "you charged me $50", ["$75"]),
])
def test_unstated_amounts(draft, body, flagged):
    assert _load_evaluate().unstated_amounts(draft, body) == flagged


def test_db_url_falls_back_to_database_url(monkeypatch):
    import sys

    ev = _load_evaluate()
    seen = {}
    monkeypatch.delenv("EVAL_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@db/y")
    monkeypatch.setattr(sys, "argv", ["evaluate.py"])
    monkeypatch.setattr(ev, "load_expected", dict)
    monkeypatch.setattr(ev, "fetch", lambda url: seen.setdefault("url", url) and [])
    with pytest.raises(SystemExit):
        ev.main()
    assert seen["url"] == "postgresql://x@db/y"
