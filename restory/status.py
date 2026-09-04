"""Read-only "health check at a glance" for restory.

``restory status`` prints a short, scannable snapshot of the current state:
whether the session is armed (a session anchor exists), the session id and
start time, how many events were recorded in this session and how many were
blocked, whether the shadow git repo exists (and where), and whether the UI is
built.

This module never modifies the store or the work tree. ``build_status``
gathers the data; ``render`` presents it with rich.
"""

from __future__ import annotations

from pathlib import Path

from . import snapshot, store


def _ui_index_path() -> Path:
    """Path to the built UI entry point (``restory/ui/out/index.html``)."""
    return Path(__file__).resolve().parent / "ui" / "out" / "index.html"


def build_status(repo_root: Path | None = None, db_path: Path | None = None) -> dict:
    """Gather the current-state snapshot (read-only)."""
    session = store.latest_session(repo_root=repo_root, db_path=db_path)
    if session is not None:
        events = store.fetch_events_since(
            session["started_at"], repo_root=repo_root, db_path=db_path
        )
    else:
        # Not armed: no current session, so there is nothing to count.
        events = []

    shadow = snapshot.get_shadow(repo_root)
    ui_index = _ui_index_path()

    return {
        "armed": session is not None,
        "session": session,
        "total_events": len(events),
        "blocked": sum(1 for ev in events if ev.get("danger")),
        "shadow_exists": shadow.exists(),
        "shadow_path": str(shadow.git_dir),
        "ui_built": ui_index.exists(),
        "ui_path": str(ui_index),
    }


def render(data: dict, console=None) -> None:
    """Render the status snapshot as a compact, scannable table."""
    from rich.console import Console
    from rich.table import Table

    console = console or Console()

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("Field", style="bold", no_wrap=True)
    table.add_column("Value", overflow="fold")

    if data["armed"]:
        session = data["session"]
        armed = "[bold green]armed[/]"
        session_line = (
            f"session [bold]{session['id']}[/], "
            f"started [cyan]{session['started_at']}[/]"
        )
    else:
        armed = "[bold red]not armed[/] [dim](run `restory session-start`)[/]"
        session_line = "[dim]-[/]"

    blocked = data["blocked"]
    blocked_style = "red" if blocked else "green"
    events_line = (
        f"[bold]{data['total_events']}[/] recorded, "
        f"[{blocked_style}]{blocked} blocked[/]"
    )

    if data["shadow_exists"]:
        shadow_line = f"[green]present[/]  [dim]{data['shadow_path']}[/]"
    else:
        shadow_line = f"[yellow]missing[/]  [dim]{data['shadow_path']}[/]"

    if data["ui_built"]:
        ui_line = "[green]built[/]"
    else:
        ui_line = "[yellow]not built[/]"

    table.add_row("Session", armed)
    if data["armed"]:
        table.add_row("", session_line)
    table.add_row("Events", events_line)
    table.add_row("Shadow repo", shadow_line)
    table.add_row("UI build", ui_line)

    console.print(table)
