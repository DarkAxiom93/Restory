"""Read-only session report for restory.

Summarizes the recorded events of the current (latest) session: how many
events were seen, how many were blocked, a breakdown by blast-radius tag, and
the list of blocked commands with their reasons.

This module never modifies the store or the work tree. ``build_report`` is a
pure aggregation over a list of event dicts so it can be unit-tested without a
database; ``gather`` reads from the store, and ``render``/``as_json`` present
the result.
"""

from __future__ import annotations

import json
from collections import Counter

from . import store
from .classify import WARN, severity_for_tags, severity_rank


def _command_of(raw: dict) -> str:
    """Best-effort command text for an event, from its raw hook payload."""
    tool_input = (raw or {}).get("tool_input") or {}
    return (
        tool_input.get("command")
        or tool_input.get("file_path")
        or tool_input.get("path")
        or ""
    )


def _effective_severity(ev: dict) -> str | None:
    """Severity of the decision that was actually made for ``ev``.

    A blocked event's severity is the max across its tags (BLOCK/CRITICAL); an
    approved-but-recorded (WARN) event has tags yet ``danger`` False. Untagged
    events have no severity.
    """
    tags = ev.get("tags") or []
    if not tags:
        return None
    if ev.get("danger"):
        return severity_for_tags(tags)
    return WARN


def _command_entry(ev: dict, severity: str | None) -> dict:
    return {
        "id": ev.get("id"),
        "timestamp": ev.get("timestamp"),
        "tool_name": ev.get("tool_name", ""),
        "command": _command_of(ev.get("raw") or {}),
        "tags": ev.get("tags", []),
        "reason": ev.get("reason", ""),
        "severity": severity,
    }


def build_report(events: list[dict], session: dict | None) -> dict:
    """Aggregate ``events`` into a report structure (pure, no I/O)."""
    tag_counts: Counter[str] = Counter()
    severity_counts: Counter[str] = Counter()
    blocked_commands: list[dict] = []
    warned_commands: list[dict] = []

    for ev in events:
        for tag in ev.get("tags", []):
            tag_counts[tag] += 1
        severity = _effective_severity(ev)
        if severity is not None:
            severity_counts[severity] += 1
        if ev.get("danger"):
            blocked_commands.append(_command_entry(ev, severity))
        elif ev.get("tags"):
            # Approved-but-recorded (WARN): surfaced, not blocked.
            warned_commands.append(_command_entry(ev, severity))

    # Blocked commands sort most-severe first, then oldest-first within a level
    # so each severity band still reads as a timeline.
    blocked_commands.sort(
        key=lambda c: (-severity_rank(c["severity"]), c["id"] is None, c["id"])
    )
    warned_commands.sort(key=lambda c: (c["id"] is None, c["id"]))

    # Severity counts ordered most-severe first for stable, legible output.
    ordered_counts = {
        sev: severity_counts[sev]
        for sev in sorted(severity_counts, key=severity_rank, reverse=True)
    }

    return {
        "session": session,
        "total_events": len(events),
        "blocked": sum(1 for ev in events if ev.get("danger")),
        "tags": dict(tag_counts.most_common()),
        "severity_counts": ordered_counts,
        "blocked_commands": blocked_commands,
        "warned_commands": warned_commands,
    }


def gather() -> dict:
    """Read the store and build the report for the current/latest session."""
    session = store.latest_session()
    if session is not None:
        events = store.fetch_events_since(session["started_at"])
    else:
        # No session anchor recorded yet; summarize everything we have.
        events = store.fetch_events(limit=5000)
    return build_report(events, session)


def as_json(data: dict) -> str:
    """Serialize the report as pretty JSON."""
    return json.dumps(data, indent=2)


# Terminal color for each severity (rich style names).
_SEVERITY_STYLE = {
    "CRITICAL": "bold red",
    "BLOCK": "red",
    "WARN": "yellow",
}


def _severity_text(severity: str | None):
    """A rich ``Text`` for a severity label, colored by level."""
    from rich.text import Text

    label = severity or "-"
    return Text(label, style=_SEVERITY_STYLE.get(severity, ""))


def render(data: dict, console=None) -> None:
    """Render the report to the terminal with rich tables and color."""
    from rich.console import Console
    from rich.table import Table

    console = console or Console()

    session = data.get("session")
    if session is not None:
        header = (
            f"Session [bold]{session['id']}[/] "
            f"started [cyan]{session['started_at']}[/] "
            f"@ [dim]{str(session['anchor_commit'])[:12]}[/]"
        )
    else:
        header = "[yellow]No session anchor recorded - summarizing all events[/]"
    console.print(header)

    total = data["total_events"]
    blocked = data["blocked"]
    blocked_style = "bold red" if blocked else "bold green"
    console.print(
        f"[bold]{total}[/] event(s)  |  "
        f"[{blocked_style}]{blocked} blocked[/]  |  "
        f"[green]{total - blocked} approved[/]"
    )

    severity_counts = data.get("severity_counts") or {}
    if severity_counts:
        from rich.text import Text

        line = Text("by severity: ")
        for i, (sev, count) in enumerate(severity_counts.items()):
            if i:
                line.append("  ")
            line.append(f"{sev} {count}", style=_SEVERITY_STYLE.get(sev, ""))
        console.print(line)

    if not total:
        console.print("[dim]No events recorded for this session yet.[/]")
        return

    tags = data["tags"]
    if tags:
        tag_table = Table(title="Blast-radius tags", title_style="bold", header_style="bold")
        # No color style: the tag name must render in the terminal's default
        # foreground so it stays readable on every console. A colored style
        # (e.g. magenta) can collapse into the background on some Windows
        # palettes, hiding the tag name while the cyan count stays visible.
        tag_table.add_column("Tag")
        tag_table.add_column("Count", justify="right", style="cyan")
        for tag, count in tags.items():
            tag_table.add_row(tag, str(count))
        console.print(tag_table)

    blocked_commands = data["blocked_commands"]
    if blocked_commands:
        cmd_table = Table(
            title="Blocked commands", title_style="bold red", header_style="bold"
        )
        cmd_table.add_column("#", justify="right", style="dim")
        cmd_table.add_column("Severity", no_wrap=True)
        cmd_table.add_column("Tool", style="yellow", no_wrap=True)
        cmd_table.add_column("Command", style="white", overflow="fold")
        cmd_table.add_column("Reason", style="red", overflow="fold")
        for cmd in blocked_commands:
            cmd_table.add_row(
                str(cmd["id"]) if cmd["id"] is not None else "-",
                _severity_text(cmd.get("severity")),
                cmd["tool_name"] or "-",
                cmd["command"] or "-",
                cmd["reason"] or "-",
            )
        console.print(cmd_table)
    else:
        console.print("[green]No commands were blocked in this session.[/]")

    warned_commands = data.get("warned_commands") or []
    if warned_commands:
        warn_table = Table(
            title="Flagged (allowed)", title_style="bold yellow", header_style="bold"
        )
        warn_table.add_column("#", justify="right", style="dim")
        warn_table.add_column("Severity", no_wrap=True)
        warn_table.add_column("Tool", style="yellow", no_wrap=True)
        warn_table.add_column("Command", style="white", overflow="fold")
        warn_table.add_column("Reason", overflow="fold")
        for cmd in warned_commands:
            warn_table.add_row(
                str(cmd["id"]) if cmd["id"] is not None else "-",
                _severity_text(cmd.get("severity")),
                cmd["tool_name"] or "-",
                cmd["command"] or "-",
                cmd["reason"] or "-",
            )
        console.print(warn_table)
