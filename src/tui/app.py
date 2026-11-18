"""Textual thin client: connect/login screen, then the 3-tab dashboard.

    uv run python -m src.tui [--backend URL] [--user NAME]

Keys: / filter · space mark · ctrl+a mark all · x unmark · a/r approve/reject (marked or current) ·
e edit · t retry failed (reopen a failed send) · [ ] panel width · { } log height (or drag the splitters) ·
d toggle details panel · Enter scroll details (Esc back) ·
l log out · q quit
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime

from rich.console import Group
from rich.markup import escape
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.worker import get_current_worker
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import (
    Button, DataTable, Footer, Header, Input, Label, RichLog, Static, TabbedContent, TabPane, TextArea,
)

from src.tui.filtering import compile_query
from src.tui.safety import LEVELS, assess

# Workaround for a Textual 8.2.8 crash: rapid right-clicks on a toast that is being
# dismissed start a text selection whose container is already detached
# (AttributeError: 'NoneType' object has no attribute 'region' in Screen._forward_event).
# Toasts have nothing worth selecting, so opting them out skips that code path.
try:
    from textual.widgets._toast import Toast as _Toast

    _Toast.ALLOW_SELECT = False
except ImportError:  # private module moved in a future Textual; the workaround is optional
    pass
from src.triage_skill import payload_digest
from src.tui.client import BackendClient, BackendError, Disconnected, Unauthorized, check_backend_url
from src.tui.profile import Profile, config_path

EDITABLE = {  # mirrors triage_skill.EDITABLE_FIELDS; the backend is the enforcer
    "send_reply": ("subject", "body"),
    "send_alert": ("message",),
    "create_lead": ("name", "company", "summary"),
}
MULTILINE = {"body", "message", "summary"}


def safety_cell(r: dict) -> Text:
    """Combined safety rating for the table: worst check wins (see src/tui/safety.py)."""
    sf = assess(r)
    return Text(sf.cell(), style=sf.style)


def _short(text: str | None, n: int = 60) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def cell(value: object, default: str = "") -> Text:
    """DataTable cell for untrusted text: a plain Text, never parsed as markup
    (a str cell containing e.g. "[/]" raises MarkupError and kills the table)."""
    return value if isinstance(value, Text) else Text(default if value in (None, "") else str(value))


DRAFT_CHANGED = "draft changed since you viewed it"


# -- connect / login ------------------------------------------------------------
class ConnectScreen(Screen):
    CSS = """
    ConnectScreen { align: center middle; }
    #box { width: 64; height: auto; border: round $accent; padding: 1 2; }
    #box Input { margin-bottom: 1; }
    #status { height: auto; margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        p: Profile = self.app.profile
        with Vertical(id="box"):
            yield Label("[b]Inbox Triage[/b] · connect to backend")
            yield Input(p.backend_url, placeholder="http://127.0.0.1:8000", id="url")
            yield Input(p.username, placeholder="username", id="user")
            yield Input(placeholder="password", password=True, id="password")
            yield Button("Connect", variant="primary", id="connect")
            yield Static(f"profile: {config_path()}", id="status")

    def on_mount(self) -> None:
        self.query_one("#password" if self.app.profile.username else "#user", Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#connect")
    def _submit(self) -> None:
        self.connect()

    @work(exclusive=True)
    async def connect(self) -> None:
        url = self.query_one("#url", Input).value.strip().rstrip("/")
        user = self.query_one("#user", Input).value.strip()
        pw = self.query_one("#password", Input).value
        status = self.query_one("#status", Static)
        if not (url and user and pw):
            status.update("[yellow]URL, username and password are required.[/]")
            return
        if problem := check_backend_url(url):  # before anything (credentials) is sent
            status.update(Text(problem, style="red"))
            return
        status.update(Text(f"checking {url}/health …"))
        client = BackendClient(url)
        try:
            health = await client.health()
            if not isinstance(health, dict) or health.get("status") != "ok":
                status.update(Text(f"backend reachable but degraded: {health}", style="yellow"))
                await client.aclose()
                return
            await client.login(user, pw)
        except Unauthorized:
            await client.aclose()
            status.update("[red]Invalid username or password.[/]")
            self.query_one("#password", Input).value = ""
            return
        except BackendError as exc:
            await client.aclose()
            if exc.status == 503:  # /health answers 503 when e.g. the database is down
                status.update(Text(f"backend reachable but degraded: {exc}", style="yellow"))
            else:
                status.update(Text(str(exc), style="red"))
            return
        self.app.profile.backend_url, self.app.profile.username = url, user
        try:
            self.app.profile.save()
        except OSError as exc:  # read-only/full config dir: still log in, just don't remember
            self.notify(escape(f"could not save profile: {exc}"), severity="warning")
        await self.app.set_client(client)
        self.app.switch_screen(DashboardScreen())


# -- edit modal ---------------------------------------------------------------------
class EditScreen(ModalScreen[dict | None]):
    CSS = """
    EditScreen { align: center middle; }
    #edit { width: 90%; height: 90%; background: $panel; padding: 1 2; border: round $accent; }
    #edit TextArea { height: 1fr; }
    #edit-buttons { height: auto; margin-top: 1; }
    #edit-buttons Button { margin-right: 1; }
    """

    def __init__(self, kind: str, payload: dict):
        super().__init__()
        self.kind, self.payload = kind, payload

    def compose(self) -> ComposeResult:
        with Vertical(id="edit"):
            dest = payload_dest(self.kind, self.payload)
            verb = "creates" if self.kind == "create_lead" else "goes to"
            yield Label(f"[b]Edit {escape(self.kind)}[/b]: {verb} {escape(dest)} (fixed, not editable)")
            for f in EDITABLE[self.kind]:
                yield Label(f)
                value = self.payload.get(f) or ""
                yield TextArea(value, id=f"f-{f}") if f in MULTILINE else Input(value, id=f"f-{f}")
            with Horizontal(id="edit-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed)
    def _done(self, event: Button.Pressed) -> None:
        if event.button.id != "save":
            self.dismiss(None)
            return
        changes = {}
        for f in EDITABLE[self.kind]:
            w = self.query_one(f"#f-{f}")
            value = w.text if isinstance(w, TextArea) else w.value
            if value != (self.payload.get(f) or ""):
                changes[f] = value
        self.dismiss(changes or None)


def payload_dest(kind: str | None, payload: dict | None) -> str:
    payload = payload or {}
    return {"send_reply": payload.get("to"), "send_alert": payload.get("channel"),
            "create_lead": f"new CRM record (contact: {payload.get('email')})"}.get(kind, "-")


# -- detail rendering -------------------------------------------------------------
# Untrusted strings (email content, LLM output) only ever go into rich Text(...),
# which never interprets markup, so an email can't restyle or fake the panel.
LABEL_STYLE = {"billing": "bold yellow", "bug_report": "bold red", "sales_lead": "bold green", "spam": "bold magenta"}
STATUS_STYLE = {"executed": "green", "rejected": "yellow", "failed": "bold red", "pending": "cyan", "approved": "cyan"}
DOMAIN_BADGE = {"ok": ("✓", "green"), "check_needed": ("?", "bold yellow"),
                "impersonation_suspected": ("⚠", "bold red reverse")}
KIND_NAME = {"send_reply": "Reply", "send_alert": "Slack alert", "create_lead": "CRM lead"}


def _section(title: str) -> Rule:
    return Rule(Text(f" {title.upper()} ", style="bold reverse"), align="left", style="bright_black")


def _fields(*pairs: tuple[str, object]) -> Table:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="bold bright_black", no_wrap=True)
    grid.add_column(overflow="fold")
    for name, value in pairs:
        if value not in (None, ""):
            grid.add_row(name, value if isinstance(value, Text) else Text(str(value)))
    return grid


def _payload_parts(kind: str, payload: dict, *lead: tuple[str, object]) -> list:
    """Short fields as one aligned grid, long text fields as their own titled block."""
    verb = "Creates" if kind == "create_lead" else "To"
    short = [*lead, (verb, payload_dest(kind, payload))]
    short += [(f.capitalize(), payload.get(f)) for f in EDITABLE.get(kind, ()) if f not in MULTILINE]
    parts: list = [_fields(*short)]
    for f in EDITABLE.get(kind, ()):
        if f in MULTILINE and payload.get(f):
            parts += [Text(""), Text(f.capitalize(), style="bold bright_black"), Text(payload[f])]
    return parts


def render_detail(row: dict) -> Group:
    sf = assess(row)
    parts: list = [_section("Safety"), Text(f"{sf.icon} {sf.word.upper()}", style=sf.style)]
    for c in sf.checks:
        icon, style = LEVELS[c.rank][1], LEVELS[c.rank][2]
        parts.append(_fields((Text(f"{icon} {c.name}", style=style), Text(c.detail))))
    if sf.rank >= 2:
        parts.append(Text("Flagged even if the draft looks fine: read the original email before approving.",
                          style="italic " + sf.style))
    parts += [Text(""), _section("Email")]
    parts.append(_fields(("ID", row.get("email_id")), ("From", row.get("sender")),
                         ("Subject", row.get("subject")), ("Received", fmt_ts(row.get("received_at")) if row.get("received_at") else None),
                         ("Status", row.get("status"))))

    if d := row.get("domain_detail"):
        verdict = row.get("domain_verdict") or d.get("verdict")
        icon, style = DOMAIN_BADGE.get(verdict, ("·", ""))
        posture = [("SPF", "published" if d.get("spf") else ("missing" if d.get("has_mx") is not None else "not checked")),
                   ("DMARC", d.get("dmarc_policy") and f"p={d['dmarc_policy']}" or ("missing" if d.get("has_mx") is not None else "not checked"))]
        parts += [Text(""), _section("Sender domain"),
                  _fields(("Domain", d.get("domain")), ("Verdict", Text(f"{icon} {verdict}", style=style)),
                          ("Lookalike", d.get("lookalike_of") and f"{d['lookalike_of']} ({d.get('technique')})"), *posture)]
        for reason in d.get("reasons", []):
            parts.append(Text(f"  • {reason}", style="bright_black"))

    if row.get("label") or row.get("rationale") or row.get("reason"):
        label = row.get("label")
        parts += [Text(""), _section("Classification"),
                  _fields(("Label", Text(label or "?", style=LABEL_STYLE.get(label, "bold"))),
                          ("Why", row.get("rationale") or row.get("reason")))]


    if row.get("payload"):  # a pending action in "Needs approval"
        title = f"{'Failed' if row.get('row_type') == 'exec_failed' else 'Proposed'} {KIND_NAME.get(row['kind'], row['kind'])}" + (" (+extra)" if row.get("extra") else "")
        parts += [Text(""), _section(title)]
        if row.get("extra"):
            parts.append(Text("Suggested beyond the standard routing for this label: "
                              + (row.get("rationale") or "").removeprefix("extra: "), style="cyan"))
        parts += _payload_parts(row["kind"], row["payload"])
        if row.get("row_type") == "exec_failed":
            parts += [Text(""), _section("Execution failed"),
                      _fields(("Status", Text(str(row.get("action_status") or "failed"), style="bold red")),
                              ("Error", Text(str(row.get("action_error") or "unknown error"), style="red"))),
                      Text("The write never reached the API, so it is safe to retry: t reopens it as a "
                           "pending draft to review and approve again.", style="cyan"),
                      Text(""), Text("t reopen", style="italic bright_black")]
        else:
            parts += [Text(""), Text("a approve · r reject · e edit", style="italic bright_black")]
    for act in row.get("actions") or []:  # history: what happened to each action
        status = Text(act["status"], style=STATUS_STYLE.get(act["status"], ""))
        if act.get("decided_by"):
            status.append(f" by {act['decided_by']}", style="")
        parts += [Text(""), _section(f"{KIND_NAME.get(act['kind'], act['kind'])}{' (+extra)' if act.get('extra') else ''} · {act['status']}")]
        parts += _payload_parts(act["kind"], act.get("payload") or {}, ("Status", status))
        if act.get("error"):
            parts.append(_fields(("Error", Text(act["error"], style="red"))))

    if row.get("last_error"):
        parts += [Text(""), _section("Error"), Text(row["last_error"], style="red")]
    if row.get("body"):
        parts += [Text(""), _section("Original email"), Text(row["body"])]
    return Group(*parts)


# -- dashboard ------------------------------------------------------------------------
LABEL_COLOR = {"billing": "yellow", "bug_report": "red", "sales_lead": "green", "spam": "magenta"}


def fmt_ts(value: str | None) -> str:
    """ISO timestamp from the API -> local 'MM-DD HH:MM'."""
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return value[:16]


# Row builders: every value from the backend goes through `cell` (plain Text, no markup).
def approval_row(r: dict, marked: set[str] = frozenset()) -> tuple[str, tuple]:
    ts = cell(fmt_ts(r.get("received_at")))
    if r["row_type"] == "failed":
        return f"f:{r['email_id']}", (safety_cell(r), cell(""), ts, cell(r["email_id"]), cell(r["sender"]), cell("?"),
                                       Text("FAILED (t=retry)", style="bold red"), cell("-"), cell(_short(r["last_error"])))
    p = r.get("payload") or {}
    preview = p.get("body") or p.get("message") or p.get("summary")
    kind = cell(r["kind"]) + (Text(" +extra", style="bold cyan") if r.get("extra") else Text(""))
    if r["row_type"] == "exec_failed":
        return f"x:{r['action_id']}", (safety_cell(r), cell(""), ts, cell(r["email_id"]), cell(r["sender"]),
                                        cell(r.get("label"), "-"), Text("SEND FAILED (t=reopen)", style="bold red"),
                                        cell(payload_dest(r["kind"], p)), cell(_short(r.get("action_error"), 50)))
    key = f"a:{r['action_id']}"
    mark = Text("☑", style="bold green") if key in marked else Text("☐", style="dim")
    return key, (safety_cell(r), mark, ts, cell(r["email_id"]), cell(r["sender"]), cell(r["label"]), kind,
                 cell(payload_dest(r["kind"], p)), cell(_short(preview, 50)))


def working_row(r: dict) -> tuple[str, tuple]:
    return r["email_id"], (safety_cell(r), cell(fmt_ts(r.get("received_at"))), cell(r["email_id"]), cell(r["sender"]),
                           cell(_short(r["subject"], 40)), cell(r["status"]), cell(r["attempts"]),
                           cell(_short(r["last_error"], 40)))


def history_row(r: dict) -> tuple[str, tuple]:
    actions = ", ".join(f"{a['kind']}{'+' if a.get('extra') else ''}:{a['status']}" for a in r["actions"])
    return r["email_id"], (safety_cell(r), cell(fmt_ts(r.get("received_at"))), cell(fmt_ts(r.get("updated_at"))),
                           cell(r["email_id"]), cell(r["sender"]), cell(r["label"], "-"), cell(r["status"]),
                           cell(actions, "-"), cell(_short(r["subject"], 40)))


class Splitter(Widget):
    """Drag to resize: `vertical` sets the width of the widget to its right,
    `horizontal` sets the height of the widget below it."""

    DEFAULT_CSS = """
    Splitter { background: $panel-lighten-1; }
    Splitter:hover, Splitter.-dragging { background: $accent; }
    Splitter.-vertical { width: 1; height: 1fr; }
    Splitter.-horizontal { height: 1; width: 1fr; }
    """

    class Resized(Message):
        pass

    def __init__(self, target: str, orientation: str, **kw):
        super().__init__(classes=f"-{orientation}", **kw)
        self.target, self.orientation = target, orientation
        self._dragging = False

    def render(self) -> Text:
        # A bare Widget renders its own repr ("Splitter#id.-classes"); draw a centered grip instead.
        w, h = self.size
        if self.orientation == "vertical":
            mid = max(0, h // 2 - 1)
            return Text("\n" * mid + "┃\n┃\n┃", style="bold", no_wrap=True)
        return Text("━━━━━".center(w), style="bold", no_wrap=True)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._dragging = True
        self.add_class("-dragging")
        self.capture_mouse()
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._dragging:
            self._dragging = False
            self.remove_class("-dragging")
            self.release_mouse()
            self.post_message(self.Resized())

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        target = self.screen.query_one(self.target)
        w, h = self.screen.size
        if self.orientation == "vertical":
            target.styles.width = max(30, min(w - 40, w - event.screen_x - 1))
        else:  # rows between the splitter and the footer
            target.styles.height = max(3, min(h - 10, h - event.screen_y - 2))


class DetailPanel(VerticalScroll):
    def on_focus(self) -> None:
        self.screen.style_detail_panel()

    def on_blur(self) -> None:
        self.screen.style_detail_panel()


class ConfirmScreen(ModalScreen[bool]):
    CSS = """
    ConfirmScreen { align: center middle; }
    #confirm { width: 70; height: auto; background: $panel; border: thick $warning; padding: 1 2; }
    #confirm-buttons { height: auto; margin-top: 1; }
    #confirm-buttons Button { margin-right: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("y", "ok", "Yes")]

    def __init__(self, title: str, body: Text):
        super().__init__()
        self._title, self._body = title, body

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Label(Text(self._title, style="bold"))
            yield Static(self._body)
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes (y)", variant="warning", id="yes")
                yield Button("Cancel (Esc)", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()  # safe default

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_ok(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class DashboardScreen(Screen):
    CSS = """
    #main { height: 1fr; }
    #left { width: 1fr; }
    #filter { height: 3; margin: 0 0 0 0; }
    #detail-panel { width: 45%; min-width: 30; padding: 0 1; border-title-align: left; border-subtitle-align: right; }
    TabbedContent { height: 1fr; }
    DataTable { height: 1fr; }
    #log { height: 7; border: round $panel; }
    """
    BINDINGS = [
        Binding("slash", "focus_filter", "Filter"),
        Binding("space", "toggle_mark", "Mark"),
        Binding("ctrl+a", "mark_all", "Mark all"),
        Binding("x", "clear_marks", "Unmark", show=False),
        Binding("a", "approve", "Approve"),
        Binding("r", "reject", "Reject"),
        Binding("e", "edit", "Edit"),
        Binding("t", "retry", "Retry", show=False),
        Binding("d", "toggle_detail", "Details"),
        Binding("enter", "focus_detail", "Scroll details", show=False),
        Binding("left_square_bracket", "resize_detail(4)", "Panel+", show=False),
        Binding("right_square_bracket", "resize_detail(-4)", "Panel-", show=False),
        Binding("left_curly_bracket", "resize_log(1)", "Log+", show=False),
        Binding("right_curly_bracket", "resize_log(-1)", "Log-", show=False),
        Binding("l", "logout", "Log out", show=False),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self._rows: dict[str, dict[str, dict]] = {"approvals": {}, "working": {}, "history": {}}
        self._totals = {"approvals": 0, "working": 0, "history": 0}
        self._data: dict | None = None
        self._match = compile_query("")
        self._marked: set[str] = set()
        self._last_event = 0
        self._seen_first_batch = False
        self._detail_key: tuple[str, str | None] | None = None
        self._detail_text = ""
        self._refreshing: object | None = None  # the refresh worker currently in flight

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Input(placeholder="/ filter: text · label:billing · safety:warn · safety:injection · action:send_reply · -spam",
                            id="filter")
                with TabbedContent(initial="approvals"):
                    with TabPane("Needs approval", id="approvals"):
                        yield DataTable(id="t-approvals", cursor_type="row")
                    with TabPane("Queue / working", id="working"):
                        yield DataTable(id="t-working", cursor_type="row")
                    with TabPane("History", id="history"):
                        yield DataTable(id="t-history", cursor_type="row")
            yield Splitter("#detail-panel", "vertical", id="split-detail")
            with DetailPanel(id="detail-panel"):
                yield Static("Select a row to see details.", id="detail")
        yield Splitter("#log", "horizontal", id="split-log")
        yield RichLog(id="log", max_lines=500, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#t-approvals", DataTable).add_columns(
            "safety", "✓", "received", "email", "from", "label", "action", "to", "preview")
        self.query_one("#t-working", DataTable).add_columns(
            "safety", "received", "email", "from", "subject", "status", "tries", "last error")
        self.query_one("#t-history", DataTable).add_columns(
            "safety", "received", "updated", "email", "from", "label", "status", "actions", "subject")
        p = self.app.profile
        if p.detail_width:
            self.query_one("#detail-panel").styles.width = p.detail_width
        self.query_one("#log").styles.height = p.log_height
        c = self.app.client
        self.app.title = f"Inbox Triage · {c.username}@{c.base_url}"
        self.style_detail_panel()
        self.set_interval(p.refresh_s, self._tick)
        self.refresh_data()
        self._focus_table()

    @property
    def client(self) -> BackendClient:
        return self.app.client

    # -- data ---------------------------------------------------------------
    def _tick(self) -> None:
        # A slow request (> refresh interval) must finish rather than be cancelled by
        # the next tick (exclusive=True would restart it forever and leave the table empty).
        if self._refreshing is None:
            self.refresh_data()

    @work(exclusive=True, group="refresh")
    async def refresh_data(self) -> None:
        # An explicit refresh (after approve etc.) may still replace an older one;
        # only the worker that is current clears the flag.
        self._refreshing = me = get_current_worker()
        try:
            await self._refresh_once()
        finally:
            if self._refreshing is me:
                self._refreshing = None

    async def _refresh_once(self) -> None:
        try:
            data = await self.client.dashboard(self._last_event)
        except Unauthorized:
            self.notify("Session expired. Please log in again.", severity="warning")
            await self.app.drop_client()
            self.app.switch_screen(ConnectScreen())
            return
        except BackendError as exc:
            self.sub_title = f"⚠ {exc} (retrying)"
            return
        if self.app.client is None:  # logged out while the request was in flight
            return
        log = self.query_one("#log", RichLog)
        new_mail = []
        for ev in data["events"]:
            self._last_event = max(self._last_event, ev["id"])
            log.write(f"{fmt_ts(ev['at'])[6:]} {ev['actor']:<8} {ev.get('email_id') or '':<10} {ev['message']}")
            if ev["actor"] == "poller" and ev["message"].startswith("ingested"):
                new_mail.append(ev)
            elif self._seen_first_batch and ev["actor"] == "worker" and (
                    ev["message"].startswith("prompt-injection attempt")
                    or ev["message"].startswith("sender domain impersonation_suspected")):
                kind = "Prompt-injection attempt" if ev["message"].startswith("prompt") else "Impersonation suspected"
                self.notify(escape(f"{ev.get('email_id')}: {ev['message']}"), title=f"⚠ {kind}",
                            severity="warning", timeout=10)
        # The first fetch replays recent history; only toast mail that arrives while we watch.
        if new_mail and self._seen_first_batch:
            senders = ", ".join(escape(ev["message"].removeprefix("ingested from ")) for ev in new_mail[:3])
            more = f" +{len(new_mail) - 3} more" if len(new_mail) > 3 else ""
            self.notify(f"{senders}{more}", title=f"{len(new_mail)} new email(s)", severity="information", timeout=6)
        self._seen_first_batch = True
        self._data = data
        self._render_dashboard()

    def _render_dashboard(self) -> None:
        if self._data is None:
            return
        data = self._data
        live = {f"a:{r['action_id']}" for r in data["approvals"] if r["row_type"] == "action"}
        self._marked &= live  # decided elsewhere (or by us) -> drop the mark

        self._fill("approvals", data["approvals"], lambda r: approval_row(r, self._marked))
        self._fill("working", data["working"], working_row)
        self._fill("history", data["history"], history_row)

        tabs = self.query_one(TabbedContent)
        names = {"approvals": "Needs approval", "working": "Queue / working", "history": "History"}
        for tab, name in names.items():
            shown, total = len(self._rows[tab]), self._totals[tab]
            tabs.get_tab(tab).label = f"{name} ({shown}/{total})" if shown != total else f"{name} ({total})"
        marked = f" · {len(self._marked)} marked" if self._marked else ""
        self.sub_title = (f"{len(data['approvals'])} awaiting · {len(data['working'])} in queue · "
                          f"{len(data['history'])} done{marked}")
        self._show_detail()

    def _fill(self, name: str, rows: list[dict], to_row) -> None:
        # Cells are Text (see `cell`); change detection compares their plain text.
        table = self.query_one(f"#t-{name}", DataTable)
        self._totals[name] = len(rows)
        pairs = [(to_row(r), r) for r in rows if self._match(r)]
        keyed = {k: values for (k, values), _ in pairs}
        self._rows[name] = {k: r for (k, _), r in pairs}
        if list(keyed) == [k.value for k in table.rows] and all(
            tuple(map(str, table.get_row(k))) == tuple(map(str, v)) for k, v in keyed.items()
        ):
            return  # unchanged; avoid flicker and cursor jumps
        current = self._current_key(table)
        table.clear()
        for key, values in keyed.items():
            table.add_row(*values, key=key)
        if current in keyed:
            table.move_cursor(row=table.get_row_index(current))

    @staticmethod
    def _current_key(table: DataTable) -> str | None:
        if table.row_count == 0:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def _selected(self) -> tuple[str, str | None, dict | None]:
        tab = self.query_one(TabbedContent).active
        key = self._current_key(self.query_one(f"#t-{tab}", DataTable))
        return tab, key, self._rows[tab].get(key) if key else None

    # -- filter -----------------------------------------------------------------------
    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self._match = compile_query(event.value)
        self._render_dashboard()

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self) -> None:
        self._focus_table()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    # -- detail panel ---------------------------------------------------------------
    @on(DataTable.RowHighlighted)
    @on(TabbedContent.TabActivated)
    def _show_detail(self, _=None) -> None:
        tab, key, row = self._selected()
        detail = self.query_one("#detail", Static)
        panel = self.query_one("#detail-panel", DetailPanel)
        # Called on every refresh: only reset the scroll when the selection changes,
        # otherwise a reviewer reading a long email would be yanked back to the top.
        if (tab, key) != self._detail_key:
            self._detail_key = (tab, key)
            panel.scroll_home(animate=False)
        self.style_detail_panel(row)
        if not row:
            self._detail_text = ""
            detail.update("Select a row to see details.")
            return
        signature = json.dumps(row, sort_keys=True, default=str)
        if signature != self._detail_text:  # skip identical re-renders on refresh
            self._detail_text = signature
            detail.update(render_detail(row))

    def style_detail_panel(self, row: dict | None = None) -> None:
        """Border color follows the email's label; double border while focused."""
        if row is None:
            row = self._selected()[2]
        panel = self.query_one("#detail-panel", DetailPanel)
        label = (row or {}).get("label")
        color = LABEL_COLOR.get(label) or self.app.get_css_variables().get("accent", "blue")
        panel.styles.border = ("double" if panel.has_focus else "round", color)
        panel.border_title = Text(f" {label} ", style=f"bold {LABEL_COLOR[label]}") if label in LABEL_COLOR else "details"
        panel.border_subtitle = "↑↓ PgUp/PgDn · Esc" if panel.has_focus else "Enter to scroll"

    def action_toggle_detail(self) -> None:
        panel = self.query_one("#detail-panel")
        panel.display = not panel.display
        self.query_one("#split-detail").display = panel.display
        if not panel.display:
            self._focus_table()

    def action_focus_detail(self) -> None:
        panel = self.query_one("#detail-panel")
        panel.display = self.query_one("#split-detail").display = True
        panel.focus()

    def _focus_table(self) -> None:
        self.query_one(f"#t-{self.query_one(TabbedContent).active}", DataTable).focus()

    @on(DataTable.RowSelected)
    def _row_selected(self, _: DataTable.RowSelected) -> None:
        self.action_focus_detail()  # Enter on a row: jump into the panel to scroll it

    def key_escape(self) -> None:
        if self.query_one("#detail-panel").has_focus or self.query_one("#filter").has_focus:
            self._focus_table()

    # -- resizing ---------------------------------------------------------------------
    def action_resize_detail(self, delta: int) -> None:
        panel = self.query_one("#detail-panel")
        w = self.size.width
        panel.styles.width = max(30, min(w - 40, panel.outer_size.width + delta))
        self._save_sizes()

    def action_resize_log(self, delta: int) -> None:
        log = self.query_one("#log")
        log.styles.height = max(3, min(self.size.height - 10, log.outer_size.height + delta))
        self._save_sizes()

    @on(Splitter.Resized)
    def _save_sizes(self, _=None) -> None:
        p = self.app.profile
        p.detail_width = int(self.query_one("#detail-panel").styles.width.value) \
            if self.query_one("#detail-panel").styles.width.unit.name == "CELLS" else p.detail_width
        p.log_height = int(self.query_one("#log").outer_size.height) or p.log_height
        try:
            p.save()
        except OSError:
            pass

    # -- marks (bulk selection) --------------------------------------------------------
    def _in_approvals(self) -> bool:
        if self.query_one(TabbedContent).active == "approvals":
            return True
        self.notify("Marking works in 'Needs approval'.", severity="warning")
        return False

    def action_toggle_mark(self) -> None:
        if not self._in_approvals():
            return
        _, key, _ = self._selected()
        if not key or not key.startswith("a:"):
            return
        self._marked ^= {key}
        table = self.query_one("#t-approvals", DataTable)
        self._render_dashboard()
        if table.cursor_row < table.row_count - 1:  # move down, like a mail client
            table.move_cursor(row=table.cursor_row + 1)

    def action_mark_all(self) -> None:
        if not self._in_approvals():
            return
        visible = {k for k in self._rows["approvals"] if k.startswith("a:")}
        # Toggle: if everything visible is already marked, unmark it.
        self._marked = self._marked - visible if visible <= self._marked else self._marked | visible
        self._render_dashboard()

    def action_clear_marks(self) -> None:
        self._marked.clear()
        self._render_dashboard()

    # -- actions ------------------------------------------------------------------
    def _selected_action(self) -> dict | None:
        tab, key, row = self._selected()
        if tab == "approvals" and key and key.startswith("a:"):
            return row
        if tab == "approvals" and key and key.startswith("x:"):
            self.notify("This send failed. Press t to reopen it as a pending draft, then review and approve it again.",
                        severity="warning")
            return None
        self.notify("Select a proposed action in 'Needs approval'.", severity="warning")
        return None

    def _marked_rows(self) -> list[dict]:
        rows = {f"a:{r['action_id']}": r for r in (self._data or {}).get("approvals", []) if r["row_type"] == "action"}
        return [rows[k] for k in sorted(self._marked, key=lambda k: int(k[2:])) if k in rows]

    async def _call(self, label: str, coro) -> bool:
        try:
            await coro
            self.notify(escape(label))
            return True
        except Unauthorized:
            self.refresh_data()  # handles the redirect to login
        except Disconnected as exc:
            self.notify(escape(str(exc)), severity="error")
        except BackendError as exc:
            if _draft_changed(exc):
                self.notify(escape(f"{label} NOT done: the draft was changed since you viewed it. "
                                   "Review the updated draft and approve again."),
                            title="Draft changed", severity="warning", timeout=10)
            else:
                self.notify(escape(f"{label} failed: {exc}"), severity="error", timeout=8)
        return False

    async def _bulk(self, verb: str, rows: list[dict]) -> None:
        kinds = ", ".join(f"{n}× {k}" for k, n in sorted(Counter(r["kind"] for r in rows).items()))
        risky = [(r, assess(r)) for r in rows if assess(r).needs_confirmation]
        body = Text(f"{len(rows)} action(s): {kinds}\n")
        body.append(", ".join(sorted({r["email_id"] for r in rows}))[:400] + "\n", style="bright_black")
        if risky and verb == "approve":
            body.append(f"\n⚠ {len(risky)} of these have safety warnings:\n", style="bold red")
            for r, sf in risky[:12]:
                body.append(f"  {sf.icon} {r['email_id']} ({r['kind']}): {sf.worst.name}\n", style=sf.style)
        if not await self.app.push_screen_wait(ConfirmScreen(f"{verb.capitalize()} {len(rows)} marked action(s)?", body)):
            return
        done = failed = changed = 0
        for r in rows:  # sequential: each approval is its own audited, atomic transition
            try:
                if verb == "approve":  # bound to the draft shown when the reviewer marked/confirmed it
                    await self.client.approve(r["action_id"], payload_digest(r["payload"]))
                else:
                    await self.client.reject(r["action_id"])
                done += 1
            except Unauthorized:
                self.refresh_data()
                return
            except BackendError as exc:
                if _draft_changed(exc):
                    changed += 1
                    self.query_one("#log", RichLog).write(
                        f"bulk {verb} {r['email_id']} {r['kind']} skipped: draft changed since you viewed it")
                else:
                    failed += 1
                    self.query_one("#log", RichLog).write(f"bulk {verb} {r['email_id']} {r['kind']} failed: {exc}")
            self._marked.discard(f"a:{r['action_id']}")
        past = "approved" if verb == "approve" else "rejected"
        msg = f"{past} {done}"
        if changed:
            msg += f", {changed} not approved because the draft changed (review again)"
        if failed:
            msg += f", {failed} failed (see log)"
        self.notify(msg, severity="error" if failed else "warning" if changed else "information",
                    timeout=10 if changed or failed else 5)
        self.refresh_data()

    @work(exclusive=True, group="act")
    async def action_approve(self) -> None:
        if marked := self._marked_rows():
            await self._bulk("approve", marked)
        elif row := self._selected_action():
            sf = assess(row)
            if sf.needs_confirmation:
                body = Text(f"{row['kind']} for {row['email_id']} ({row['sender']})\n\n")
                for c in sf.checks:
                    if c.rank >= 2:
                        body.append(f"{LEVELS[c.rank][1]} {c.name}: {c.detail}\n", style=LEVELS[c.rank][2])
                body.append("\nRead the draft and the original email before approving.", style="italic")
                if not await self.app.push_screen_wait(ConfirmScreen(f"{sf.icon} Approve despite safety warning?", body)):
                    return
            # Digest of the payload this row displayed: the backend refuses (409) if it was edited since.
            await self._call(f"approved {row['kind']} for {row['email_id']}",
                             self.client.approve(row["action_id"], payload_digest(row["payload"])))
            self.refresh_data()

    @work(exclusive=True, group="act")
    async def action_reject(self) -> None:
        if marked := self._marked_rows():
            await self._bulk("reject", marked)
        elif row := self._selected_action():
            await self._call(f"rejected {row['kind']} for {row['email_id']}", self.client.reject(row["action_id"]))
            self.refresh_data()

    @work(exclusive=True, group="act")
    async def action_edit(self) -> None:
        if not (row := self._selected_action()):
            return
        changes = await self.app.push_screen_wait(EditScreen(row["kind"], row["payload"]))
        if changes:
            await self._call(f"edited {', '.join(changes)}", self.client.edit(row["action_id"], changes))
            self.refresh_data()

    @work(exclusive=True, group="act")
    async def action_retry(self) -> None:
        tab, key, row = self._selected()
        if tab == "approvals" and key and key.startswith("f:"):
            await self._call(f"re-queued {key[2:]}", self.client.retry(key[2:]))
            self.refresh_data()
        elif tab == "approvals" and key and key.startswith("x:") and row:
            await self._call(f"reopened {row['kind']} for {row['email_id']}: review and approve it again",
                             self.client.reopen(row["action_id"]))
            self.refresh_data()
        else:
            self.notify("Select a FAILED row to retry.", severity="warning")

    @work(exclusive=True, group="act")
    async def action_logout(self) -> None:
        await self.app.drop_client(logout=True)
        self.app.switch_screen(ConnectScreen())


def _draft_changed(exc: BackendError) -> bool:
    return exc.status == 409 and DRAFT_CHANGED in str(exc)


# -- app -----------------------------------------------------------------------------
class TriageTUI(App):
    TITLE = "Inbox Triage"

    def __init__(self, profile: Profile):
        super().__init__()
        self.profile = profile
        self.client: BackendClient | None = None

    def on_mount(self) -> None:
        self.push_screen(ConnectScreen())

    async def set_client(self, client: BackendClient) -> None:
        await self.drop_client()
        self.client = client

    async def drop_client(self, *, logout: bool = False) -> None:
        if self.client:
            if logout:
                try:
                    await self.client.logout()
                except BackendError:
                    pass
            await self.client.aclose()
            self.client = None

    async def on_unmount(self) -> None:
        await self.drop_client(logout=True)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m src.tui", description="Inbox Triage TUI client")
    ap.add_argument("--backend", help="backend URL (overrides saved profile / TRIAGE_BACKEND_URL)")
    ap.add_argument("--user", help="username to prefill")
    a = ap.parse_args(argv)
    profile = Profile.load()
    if url := a.backend or os.environ.get("TRIAGE_BACKEND_URL"):
        profile.backend_url = url
    if a.user:
        profile.username = a.user
    TriageTUI(profile).run()
