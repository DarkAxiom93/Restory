"""Full-screen terminal dashboard for restory (``restory monitor``).

The terminal-native counterpart to ``restory open``: a live, full-screen
Textual app that polls the SQLite store roughly once a second and shows session
events newest-first. Blocked events are rendered prominently in red with a
``⛔`` marker and their reason; safe events are dimmed.

``gather`` is a pure read over the store (mirroring ``report.gather`` /
``status.build_status``) so the data shaping can be unit-tested without a
terminal. ``RestoryMonitorApp`` is the Textual app; it never writes to the
store except through the explicit ``u`` (undo session) action, which prompts
for confirmation first.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Static

from . import store

# ⛔ marker shown on blocked rows. Textual renders through its own UTF-8 buffer,
# so this stays legible even on a legacy cp1252 Windows console.
BLOCKED_MARKER = "⛔"

# How often (seconds) to poll the store for new events.
POLL_SECONDS = 1.0


def gather(db_path: Path | None = None) -> dict:
    """Read the store and shape the current session's events (pure, read-only).

    When a session anchor exists, events are scoped to that session; otherwise
    the most recent events are shown so ``monitor`` is still useful before a
    session has been anchored. Events come back newest-first from the store.
    """
    session = store.latest_session(db_path=db_path)
    if session is not None:
        events = store.fetch_events_since(session["started_at"], db_path=db_path)
    else:
        events = store.fetch_events(limit=500, db_path=db_path)

    total = len(events)
    blocked = sum(1 for ev in events if ev.get("danger"))
    return {
        "session": session,
        "armed": session is not None,
        "events": events,
        "total": total,
        "blocked": blocked,
    }


def _fmt_time(iso: str) -> str:
    """Best-effort ``HH:MM:SS`` (local) from an ISO-8601 timestamp string."""
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return (iso or "")[:8]
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%H:%M:%S")


def _detail_of(ev: dict) -> str:
    """Short one-line detail for an event, from its raw hook payload."""
    tool_input = (ev.get("raw") or {}).get("tool_input") or {}
    return (
        tool_input.get("command")
        or tool_input.get("file_path")
        or tool_input.get("path")
        or ""
    )


def apply_view(
    events: list[dict],
    *,
    blocked_only: bool = False,
    tag: str | None = None,
    oldest_first: bool = False,
) -> list[dict]:
    """Filter and sort events for display (pure, read-only).

    ``events`` arrive newest-first. ``blocked_only`` and ``tag`` compose (both
    must match), and ``oldest_first`` reverses the final order so filtering and
    sorting combine cleanly. Never mutates ``events`` or its members.
    """
    out = list(events)
    if blocked_only:
        out = [ev for ev in out if ev.get("danger")]
    if tag is not None:
        out = [ev for ev in out if tag in (ev.get("tags") or [])]
    if oldest_first:
        out = list(reversed(out))
    return out


class ConfirmScreen(ModalScreen[bool]):
    """Modal yes/no confirmation. Dismisses with ``True`` when confirmed."""

    BINDINGS = [
        Binding("y", "confirm", "Yes", show=False),
        Binding("n,escape", "cancel", "No", show=False),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self._message, id="confirm-msg")
            with Horizontal(id="confirm-buttons"):
                yield Button("Undo session (y)", variant="error", id="yes")
                yield Button("Cancel (n)", variant="primary", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class RestoryMonitorApp(App[None]):
    """Live full-screen dashboard of restory session events."""

    TITLE = "restory monitor"

    CSS = """
    #statusbar {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text;
    }
    DataTable {
        height: 1fr;
    }
    #detail {
        dock: bottom;
        height: auto;
        max-height: 50%;
        padding: 0 1;
        border-top: heavy $accent;
        background: $panel;
    }
    #detail.-hidden {
        display: none;
    }
    #confirm-box {
        width: 60;
        height: auto;
        padding: 1 2;
        border: thick $error;
        background: $surface;
    }
    #confirm-msg {
        height: auto;
        margin-bottom: 1;
    }
    #confirm-buttons {
        height: auto;
        align-horizontal: center;
    }
    #confirm-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("u", "undo", "Undo session"),
        Binding("c", "clear", "Clear view"),
        Binding("b", "toggle_blocked", "Blocked only"),
        Binding("t", "cycle_tag", "Tag filter"),
        Binding("s", "toggle_sort", "Sort"),
        Binding("enter", "toggle_detail", "Details"),
        Binding("j", "row_down", "Down", show=False),
        Binding("k", "row_up", "Up", show=False),
    ]

    def __init__(
        self,
        db_path: Path | None = None,
        repo_root: Path | None = None,
    ) -> None:
        super().__init__()
        self._db_path = db_path
        self._repo_root = repo_root
        # Events with id <= this threshold are hidden by "clear view". The DB is
        # never touched; clearing is purely visual and new events still appear.
        self._clear_before_id = 0
        # Read-only view state. None of these ever touch the DB.
        self._blocked_only = False
        self._tag_filter: str | None = None
        self._sort_oldest_first = False
        # id of the event whose full details are expanded, or None.
        self._expanded_id: int | None = None
        # Clear-filtered events from the last gather, in newest-first order.
        # Used by the tag-cycle action to know which tags are present.
        self._events: list[dict] = []
        # Events in current display order, so a highlighted row maps to an event.
        self._row_events: list[dict] = []
        # Signature of the last render, so we only rebuild the table on change.
        self._last_signature: tuple | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="statusbar")
        table = DataTable(id="events", zebra_stripes=True, cursor_type="row")
        table.add_columns("#", "Time", "Tool", "Detail", "Status")
        yield table
        yield Static("", id="detail", classes="-hidden")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#events", DataTable).focus()
        self._refresh()
        self.set_interval(POLL_SECONDS, self._refresh)

    # -- data / rendering ----------------------------------------------------

    def _visible_events(self, events: list[dict]) -> list[dict]:
        return [ev for ev in events if (ev.get("id") or 0) > self._clear_before_id]

    def _refresh(self) -> None:
        try:
            data = gather(db_path=self._db_path)
        except Exception as exc:  # never let a transient store error kill the UI
            self.query_one("#statusbar", Static).update(
                Text(f"store error: {exc}", style="bold red")
            )
            return

        self._events = self._visible_events(data["events"])
        # If the expanded event has scrolled out of the current view, forget it.
        if self._expanded_id is not None and not any(
            ev.get("id") == self._expanded_id for ev in self._events
        ):
            self._expanded_id = None

        view = apply_view(
            self._events,
            blocked_only=self._blocked_only,
            tag=self._tag_filter,
            oldest_first=self._sort_oldest_first,
        )
        signature = (
            data["armed"],
            data["total"],
            data["blocked"],
            self._clear_before_id,
            self._blocked_only,
            self._tag_filter,
            self._sort_oldest_first,
            self._expanded_id,
            tuple(ev.get("id") for ev in view),
        )
        if signature == self._last_signature:
            return
        self._last_signature = signature

        self._render_statusbar(data)
        self._render_table(view)
        self._render_detail()

    def _render_statusbar(self, data: dict) -> None:
        bar = Text()
        bar.append(f"{data['total']} events", style="bold")
        bar.append(" · ")
        blocked = data["blocked"]
        bar.append(f"{blocked} blocked", style="bold red" if blocked else "dim")
        bar.append("     ")
        if data["armed"]:
            session = data["session"]
            bar.append("armed", style="bold green")
            bar.append(f"  session {session['id']}", style="dim")
        else:
            bar.append("not armed", style="bold yellow")

        # Active view filters/sort (read-only; none of these touch the DB).
        if self._blocked_only:
            bar.append(" · ")
            bar.append("blocked only", style="bold yellow")
        if self._tag_filter is not None:
            bar.append(" · ")
            bar.append(f"tag:{self._tag_filter}", style="bold cyan")
        bar.append(" · ")
        bar.append(
            "oldest first" if self._sort_oldest_first else "newest first",
            style="dim",
        )
        self.query_one("#statusbar", Static).update(bar)

    def _render_table(self, events: list[dict]) -> None:
        table = self.query_one("#events", DataTable)
        table.clear()
        self._row_events = list(events)
        for ev in events:
            blocked = bool(ev.get("danger"))
            rid = str(ev.get("id") if ev.get("id") is not None else "-")
            time_s = _fmt_time(ev.get("timestamp", ""))
            tool = ev.get("tool_name", "") or "-"
            detail = _detail_of(ev) or "-"
            tags = ev.get("tags") or []

            if blocked:
                reason = ev.get("reason", "") or "blocked"
                tag_suffix = f" [{', '.join(tags)}]" if tags else ""
                status = Text(f"{BLOCKED_MARKER} {reason}{tag_suffix}", style="bold red")
                row = (
                    Text(rid, style="red"),
                    Text(time_s, style="red"),
                    Text(tool, style="bold red"),
                    Text(detail, style="red"),
                    status,
                )
            else:
                row = (
                    Text(rid, style="dim"),
                    Text(time_s, style="dim"),
                    Text(tool, style="dim"),
                    Text(detail, style="dim"),
                    Text("ok", style="dim green"),
                )
            table.add_row(*row)

    def _render_detail(self) -> None:
        """Show the expanded event's full details, or hide the pane."""
        panel = self.query_one("#detail", Static)
        ev = None
        if self._expanded_id is not None:
            ev = next(
                (e for e in self._events if e.get("id") == self._expanded_id),
                None,
            )
        if ev is None:
            panel.add_class("-hidden")
            panel.update("")
            return

        blocked = bool(ev.get("danger"))
        tags = ev.get("tags") or []
        body = Text()
        head = f"Event {ev.get('id')} — {ev.get('tool_name') or '-'}"
        body.append(head + "\n", style="bold red" if blocked else "bold")
        body.append("command: ", style="dim")
        body.append((_detail_of(ev) or "-") + "\n")
        body.append("tags:    ", style="dim")
        body.append((", ".join(tags) if tags else "-") + "\n")
        body.append("reason:  ", style="dim")
        body.append((ev.get("reason") or "-") + "\n", style="red" if blocked else "")
        body.append("time:    ", style="dim")
        body.append((ev.get("timestamp") or "-") + "\n")
        body.append("status:  ", style="dim")
        body.append("blocked" if blocked else "ok",
                    style="bold red" if blocked else "green")
        body.append("   (enter to collapse)", style="dim")
        panel.remove_class("-hidden")
        panel.update(body)

    # -- actions -------------------------------------------------------------

    def action_toggle_blocked(self) -> None:
        """Toggle showing only blocked events (view-only)."""
        self._blocked_only = not self._blocked_only
        self._last_signature = None
        self._refresh()

    def action_cycle_tag(self) -> None:
        """Cycle the tag filter: all → each present tag → all (view-only)."""
        present = sorted({t for ev in self._events for t in (ev.get("tags") or [])})
        cycle: list[str | None] = [None, *present]
        try:
            idx = cycle.index(self._tag_filter)
        except ValueError:
            idx = 0
        self._tag_filter = cycle[(idx + 1) % len(cycle)]
        self._last_signature = None
        self._refresh()

    def action_toggle_sort(self) -> None:
        """Toggle newest-first (default) vs oldest-first ordering (view-only)."""
        self._sort_oldest_first = not self._sort_oldest_first
        self._last_signature = None
        self._refresh()

    def action_row_down(self) -> None:
        self.query_one("#events", DataTable).action_cursor_down()

    def action_row_up(self) -> None:
        self.query_one("#events", DataTable).action_cursor_up()

    def action_toggle_detail(self) -> None:
        """Expand/collapse full details for the highlighted event."""
        table = self.query_one("#events", DataTable)
        self._toggle_detail_at(table.cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._toggle_detail_at(event.cursor_row)

    def _toggle_detail_at(self, row_index: int | None) -> None:
        if row_index is None or not (0 <= row_index < len(self._row_events)):
            return
        eid = self._row_events[row_index].get("id")
        self._expanded_id = None if self._expanded_id == eid else eid
        self._last_signature = None
        self._refresh()

    def action_clear(self) -> None:
        """Hide currently-shown events from the view (does not touch the DB)."""
        data = gather(db_path=self._db_path)
        ids = [ev.get("id") or 0 for ev in data["events"]]
        self._clear_before_id = max(ids) if ids else 0
        self._last_signature = None  # force a rebuild
        self._refresh()

    def action_undo(self) -> None:
        """Prompt, then undo the entire latest session (resets to its baseline)."""

        def handle(confirmed: bool | None) -> None:
            if confirmed:
                self._perform_undo_session()

        self.push_screen(
            ConfirmScreen(
                "Undo the entire session?\n"
                "This resets the work tree to the session baseline."
            ),
            handle,
        )

    def _perform_undo_session(self) -> None:
        from . import snapshot

        shadow = snapshot.get_shadow(self._repo_root)
        if not shadow.exists():
            self.notify("No shadow repo found.", severity="error", title="undo")
            return
        sess = store.latest_session(db_path=self._db_path)
        if sess is None:
            self.notify("No session anchor recorded.", severity="error", title="undo")
            return
        try:
            changes = shadow.undo_to(sess["anchor_commit"])
        except snapshot.SnapshotError as exc:
            self.notify(str(exc), severity="error", title="undo")
            return
        if not changes:
            self.notify(
                f"Session {sess['id']}: already at baseline.", title="undo"
            )
        else:
            self.notify(
                f"Reverted session {sess['id']} — {len(changes)} change(s).",
                title="undo",
            )
        self._last_signature = None
        self._refresh()


def run(db_path: Path | None = None, repo_root: Path | None = None) -> None:
    """Construct and run the monitor app (blocking until the user quits)."""
    RestoryMonitorApp(db_path=db_path, repo_root=repo_root).run()
