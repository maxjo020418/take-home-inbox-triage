"""Regression tests for the TUI review fixes (markup safety, digest-bound approval,
exec_failed rows, refresh serialization, URL policy, malformed responses, safety, filtering)."""

import asyncio
import json

import httpx
import pytest
from rich.console import Console
from textual.app import App
from textual.widgets import DataTable

from src.triage_skill import payload_digest
from src.tui import app as tui
from src.tui.client import BackendClient, BackendError, check_backend_url
from src.tui.filtering import compile_query
from src.tui.profile import Profile
from src.tui.safety import assess

NASTY = {"subject": "Invoice [/] question", "sender": "[bold]x@y.com", "label": "billing[/]",
         "email_id": "e-[/b]1", "last_error": "boom [/] [red]", "status": "[/]done"}
CHECKS = {"domain_verdict": "ok", "injection_detected": False}


def approval(**kw):
    return {"row_type": "action", "action_id": 7, "kind": "send_reply", "received_at": "2026-11-18T10:00:00Z",
            "payload": {"to": "[/]a@b.com", "subject": "Re: [/]", "body": "hi [/] there"}, **CHECKS, **NASTY, **kw}


def dashboard_data():
    return {
        "approvals": [approval(), approval(row_type="failed", action_id=None),
                      approval(row_type="exec_failed", action_id=8, action_status="failed",
                               action_error="HTTP 503 [/]", retryable=True)],
        "working": [{**NASTY, **CHECKS, "attempts": 2}],
        "history": [{**NASTY, **CHECKS, "actions": [{"kind": "send_[/]reply", "status": "[/]executed",
                                                      "payload": {"to": "z@q.com", "body": "sent [/]"}}]}],
        "events": [],
    }


# -- 1. untrusted strings never parsed as markup --------------------------------------
def test_row_builders_emit_plain_text_cells():
    data = dashboard_data()
    rows = [tui.approval_row(r) for r in data["approvals"]] + [tui.working_row(data["working"][0]),
                                                               tui.history_row(data["history"][0])]
    for _, cells in rows:
        assert all(not isinstance(c, str) for c in cells)
    assert [k for k, _ in rows[:3]] == ["a:7", "f:e-[/b]1", "x:8"]
    assert "SEND FAILED (t=reopen)" in [str(c) for c in rows[2][1]]


class TableApp(App):
    def compose(self):
        yield DataTable()


def test_datatable_renders_markup_like_values():
    async def run():
        app = TableApp()
        async with app.run_test(size=(200, 20)) as pilot:
            table = app.query_one(DataTable)
            table.add_columns(*[str(i) for i in range(9)])
            for r in dashboard_data()["approvals"]:
                key, cells = tui.approval_row(r)
                table.add_row(*cells, key=key)
            await pilot.pause()
            assert "[bold]x@y.com" in str(table.get_row("a:7")[4])
    asyncio.run(run())


class FakeClient:
    username, base_url = "rev", "http://127.0.0.1:8000"

    def __init__(self, data, delay=0.0):
        self.data, self.delay, self.calls = data, delay, []

    async def dashboard(self, events_after=0):
        self.calls.append(events_after)
        await asyncio.sleep(self.delay)
        return self.data

    async def logout(self):
        pass

    async def aclose(self):
        pass


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # resizing etc. must not touch the real profile


def _app(client, tmp_path):
    app = tui.TriageTUI(Profile())
    app.client = client
    return app


def test_dashboard_with_markup_in_subject_and_sender(tmp_path):
    async def run():
        app = _app(FakeClient(dashboard_data()), tmp_path)
        async with app.run_test(size=(200, 50)) as pilot:
            await app.push_screen(tui.DashboardScreen())  # skip login: fake client already set
            for _ in range(20):
                await pilot.pause(0.05)
                if app.screen.query_one("#t-approvals", DataTable).row_count:
                    break
            scr = app.screen
            assert scr.query_one("#t-approvals", DataTable).row_count == 3
            assert scr.query_one("#t-history", DataTable).row_count == 1
            # the detail panel for every approvals row renders too
            for r in dashboard_data()["approvals"]:
                Console(file=open("/dev/null", "w"), width=100).print(tui.render_detail(r))
            # refresh again with identical data: change detection on Text cells still short-circuits
            scr._render_dashboard()
    asyncio.run(run())


def test_exec_failed_detail_shows_error():
    r = dashboard_data()["approvals"][2]
    c = Console(width=120, record=True, file=open("/dev/null", "w"))
    c.print(tui.render_detail(r))
    out = c.export_text()
    assert "HTTP 503 [/]" in out and "t reopen" in out and "a approve" not in out


# -- 4. refresh serialization ---------------------------------------------------------
def test_slow_refresh_is_not_cancelled_by_ticks(tmp_path):
    async def run():
        client = FakeClient(dashboard_data(), delay=0.4)
        app = _app(client, tmp_path)
        app.profile.refresh_s = 0.1
        async with app.run_test(size=(200, 50)) as pilot:
            await app.push_screen(tui.DashboardScreen())  # skip login: fake client already set
            await pilot.pause(1.2)
            assert app.screen.query_one("#t-approvals", DataTable).row_count == 3
            # ~12 ticks elapsed, but only serialized requests (not one per tick)
            assert len(client.calls) <= 4
    asyncio.run(run())


# -- 2. digest-bound approval / 3. reopen / 7. malformed responses --------------------
def _client(handler):
    return BackendClient("http://127.0.0.1:8000", transport=httpx.MockTransport(handler))


def test_approve_sends_digest_and_reopen_path():
    seen = []

    def handler(req):
        seen.append((req.url.path, req.content))
        return httpx.Response(200, json={"ok": True})

    async def run():
        c = _client(handler)
        p = {"to": "a@b.com", "body": "hi"}
        await c.approve(1, payload_digest(p))
        await c.approve(2)
        await c.reopen(3)
        await c.aclose()
        return p
    p = asyncio.run(run())
    assert seen[0][0] == "/actions/1/approve"
    assert json.loads(seen[0][1]) == {"payload_digest": payload_digest(p)}
    assert seen[1] == ("/actions/2/approve", b"")
    assert seen[2][0] == "/actions/3/reopen"


def test_draft_changed_409_detected():
    async def run():
        c = _client(lambda req: httpx.Response(409, json={"detail": "draft changed since you viewed it; review it again"}))
        with pytest.raises(BackendError) as ei:
            await c.approve(1, "abc")
        await c.aclose()
        return ei.value
    assert tui._draft_changed(asyncio.run(run()))
    assert not tui._draft_changed(BackendError("action is not pending", 409))


@pytest.mark.parametrize("resp", [
    httpx.Response(200, content=b"not json"),
    httpx.Response(500, json=[]),
    httpx.Response(400, json="oops"),
    httpx.Response(502, content=b"<html>bad gateway</html>"),
])
def test_malformed_responses_raise_backend_error(resp):
    async def run():
        c = _client(lambda req: resp)
        try:
            await c.dashboard()
        finally:
            await c.aclose()
    with pytest.raises(BackendError):
        asyncio.run(run())


def test_client_does_not_follow_redirects():
    def handler(req):
        if req.url.host == "evil.example":
            raise AssertionError("followed redirect")
        return httpx.Response(307, headers={"location": "https://evil.example/steal"})

    async def run():
        c = _client(handler)
        try:
            await c.health()
        finally:
            await c.aclose()
    with pytest.raises(BackendError):
        asyncio.run(run())


# -- 6. URL policy -----------------------------------------------------------------
@pytest.mark.parametrize("url", ["https://triage.example.com", "https://10.0.0.5:8443", "http://localhost:8000",
                                 "http://127.0.0.1:8000", "http://127.9.8.7", "http://[::1]:8000"])
def test_allowed_backend_urls(url):
    assert check_backend_url(url) is None


@pytest.mark.parametrize("url", ["http://triage.example.com", "http://10.0.0.5:8000", "ftp://localhost",
                                 "localhost:8000", "http://", "http://localhost.evil.com", "http://localhost:abc"])
def test_rejected_backend_urls(url):
    assert check_backend_url(url)


# -- 9. safety only when checks completed ----------------------------------------------
def test_classified_with_missing_domain_check_is_not_safe():
    sf = assess({"label": "billing", "injection_detected": False})
    assert sf.word == "check" and any(c.detail == "domain check did not run" for c in sf.checks)


def test_classified_with_missing_injection_check_is_not_safe():
    sf = assess({"label": "billing", "domain_verdict": "ok"})
    assert sf.word == "check" and any(c.name == "injection" and c.detail == "injection check did not run"
                                      for c in sf.checks)


def test_unclassified_partial_checks_are_pending():
    assert assess({"domain_verdict": "ok"}).word == "pending"
    assert assess({"domain_verdict": "impersonation_suspected"}).word == "danger"  # higher wins
    assert assess({"label": "billing", **CHECKS}).word == "safe"


# -- 10. filtering covers history action payloads -------------------------------------
def test_history_action_payloads_are_searchable():
    hist = {"email_id": "e-9", "status": "done", "label": "billing", **CHECKS,
            "actions": [{"kind": "send_reply", "status": "executed",
                         "payload": {"to": "dana@meridian.com", "body": "refund issued"}},
                        {"kind": "send_alert", "status": "executed",
                         "payload": {"channel": "#billing-ops", "message": "double charge"}}]}
    assert compile_query("to:dana")(hist)
    assert compile_query("to:billing-ops")(hist)
    assert compile_query("refund")(hist)
    assert compile_query('"double charge"')(hist)
    assert not compile_query("to:nobody")(hist)
