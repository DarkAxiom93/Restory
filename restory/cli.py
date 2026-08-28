"""Command-line interface for restory."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import typer

from .config import find_repo_root

app = typer.Typer(help="restory command-line interface.")

# Tools whose calls restory guards.
_MATCHER = "Bash|Write|Edit|MultiEdit"
_HOOK_EVENTS = ("PreToolUse", "PostToolUse")


def _restory_command(subcommand: str) -> str:
    """Return a Windows-safe command string that invokes ``restory <subcommand>``.

    Prefer the ``restory`` console script if it is on PATH; otherwise fall back
    to the absolute path of the current interpreter with ``-m restory``.
    """
    if shutil.which("restory"):
        return f"restory {subcommand}"
    return f'"{sys.executable}" -m restory {subcommand}'


def _hook_block(command: str, *, matcher: str | None = None) -> dict:
    block: dict = {"hooks": [{"type": "command", "command": command}]}
    if matcher is not None:
        block["matcher"] = matcher
    return block


def _entry_has_command(entries: list, command: str) -> bool:
    return any(
        isinstance(entry, dict)
        and any(
            isinstance(h, dict) and command == h.get("command")
            for h in entry.get("hooks", [])
        )
        for entry in entries
    )


def _merge_hooks(settings: dict, command: str) -> dict:
    """Merge restory PreToolUse/PostToolUse hook entries without clobbering others."""
    hooks = settings.setdefault("hooks", {})
    for event in _HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        if not _entry_has_command(entries, command):
            entries.append(_hook_block(command, matcher=_MATCHER))
    return settings


def _merge_session_start(settings: dict, command: str) -> dict:
    """Merge a restory SessionStart hook entry without clobbering others."""
    hooks = settings.setdefault("hooks", {})
    entries = hooks.setdefault("SessionStart", [])
    if not _entry_has_command(entries, command):
        entries.append(_hook_block(command))
    return settings


@app.command()
def init() -> None:
    """Install restory SessionStart + PreToolUse/PostToolUse hooks into .claude/settings.json."""
    repo_root = find_repo_root()
    claude_dir = repo_root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"

    if settings_path.exists():
        backup = settings_path.with_suffix(".json.bak")
        shutil.copy2(settings_path, backup)
        typer.echo(f"Backed up existing settings to {backup}")
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                settings = {}
        except (json.JSONDecodeError, OSError):
            settings = {}
    else:
        settings = {}

    hook_command = _restory_command("hook")
    session_command = _restory_command("session-start")
    settings = _merge_hooks(settings, hook_command)
    settings = _merge_session_start(settings, session_command)

    rendered = json.dumps(settings, indent=2)
    settings_path.write_text(rendered + "\n", encoding="utf-8")

    typer.echo(f"Wrote {settings_path}:")
    typer.echo(rendered)


@app.command()
def hook() -> None:
    """Run the PreToolUse/PostToolUse hook (reads JSON on stdin)."""
    from . import hook as hook_mod

    raise typer.Exit(hook_mod.main())


@app.command()
def watch() -> None:
    """Start watching: ensure the shadow repo exists with an initial snapshot."""
    from . import snapshot

    shadow, created = snapshot.ensure_shadow()
    if created:
        typer.echo(f"Initialized shadow repo at {shadow.git_dir}")
        typer.echo(f"Took initial snapshot of {shadow.repo_root}")
    else:
        typer.echo(f"Shadow repo already present at {shadow.git_dir}")
    typer.echo("Watching. Mutations are snapshotted on each PostToolUse hook.")


@app.command(name="session-start")
def session_start() -> None:
    """Anchor a new session: ensure the shadow repo and record a baseline commit.

    Installed as a Claude Code SessionStart hook by ``restory init`` so undo is
    armed automatically at the start of every session (no need to run
    ``restory watch`` first).
    """
    from . import snapshot, store

    shadow, created = snapshot.ensure_shadow()
    if created:
        typer.echo(f"Initialized shadow repo at {shadow.git_dir}")
    anchor = shadow.session_baseline()
    session_id = store.record_session(anchor)
    typer.echo(f"Session {session_id} anchored at {anchor[:12]} ({shadow.repo_root})")


@app.command()
def undo(
    session: bool = typer.Option(
        False,
        "--session",
        help="Undo the entire latest session, resetting to its baseline anchor.",
    ),
) -> None:
    """Revert the most recent snapshot (or the whole session with --session)."""
    from . import snapshot, store

    shadow = snapshot.get_shadow()
    if not shadow.exists():
        typer.echo("No shadow repo found. Start a session (`restory session-start`) or run `restory watch` first.")
        raise typer.Exit(1)

    if session:
        sess = store.latest_session()
        if sess is None:
            typer.echo("No session anchor recorded. Run `restory session-start` first.")
            raise typer.Exit(1)
        try:
            changes = shadow.undo_to(sess["anchor_commit"])
        except snapshot.SnapshotError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1)
        if not changes:
            typer.echo(
                f"Session {sess['id']} undo complete: work tree already at the baseline."
            )
            return
        typer.echo(f"Reverted session {sess['id']} — {len(changes)} change(s):")
        for line in snapshot.describe_changes(changes):
            typer.echo(line)
        return

    try:
        changes = shadow.undo()
    except snapshot.SnapshotError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1)
    if not changes:
        typer.echo("Undo complete: no file changes in the reverted snapshot.")
        return
    typer.echo(f"Reverted {len(changes)} change(s):")
    for line in snapshot.describe_changes(changes):
        typer.echo(line)


@app.command()
def report(
    json_output: bool = typer.Option(
        False, "--json", help="Emit the report as JSON instead of a formatted table."
    ),
) -> None:
    """Summarize the current session: events, blocks, tags, and blocked commands.

    Read-only: reads the recorded events from the SQLite store and never
    modifies the store or the work tree.
    """
    from . import report as report_mod

    data = report_mod.gather()
    if json_output:
        typer.echo(report_mod.as_json(data))
    else:
        report_mod.render(data)


@app.command()
def export(
    output: Path = typer.Argument(
        None, help="File to write the report to. Prints to stdout if omitted."
    ),
    format: str = typer.Option(
        None,
        "--format",
        "-f",
        help="Output format: md (default), json, or html. Inferred from the "
        "output extension when not given.",
    ),
) -> None:
    """Export the current session as a shareable Markdown/JSON/HTML artifact.

    Read-only: reads the recorded events and never modifies the store or the
    work tree.
    """
    from . import export as export_mod

    _ext_fmt = {".md": "md", ".markdown": "md", ".json": "json", ".html": "html", ".htm": "html"}
    if format is None:
        format = _ext_fmt.get(output.suffix.lower(), "md") if output is not None else "md"
    format = format.lower()
    if format not in export_mod.FORMATS:
        typer.echo(f"Unknown format {format!r}. Choose from: {', '.join(export_mod.FORMATS)}.")
        raise typer.Exit(2)

    data = export_mod.gather()
    rendered = export_mod.render(data, fmt=format)

    if output is None:
        # Write UTF-8 bytes straight to stdout so emoji/box glyphs survive a
        # legacy Windows console (cp1252), which typer.echo would choke on.
        try:
            sys.stdout.buffer.write(rendered.encode("utf-8"))
            sys.stdout.buffer.write(b"\n")
            sys.stdout.buffer.flush()
        except (AttributeError, ValueError):  # no binary buffer (e.g. captured)
            typer.echo(rendered)
    else:
        output.write_text(rendered, encoding="utf-8")
        typer.echo(f"Wrote {format} report to {output}")


@app.command()
def diff(
    stat: bool = typer.Option(
        False, "--stat", help="Show only the summary (files + counts), not the patch."
    ),
    name_only: bool = typer.Option(
        False, "--name-only", help="List only the changed file paths."
    ),
) -> None:
    """Show what changed in the work tree since the current session started.

    Diffs the session anchor commit against the present work tree via the
    shadow repo. Read-only: never modifies the work tree or the shadow.
    """
    from . import diff as diff_mod

    if stat and name_only:
        typer.echo("Use only one of --stat or --name-only.")
        raise typer.Exit(2)

    mode = "stat" if stat else "name-only" if name_only else "full"
    data = diff_mod.gather(mode=mode)
    diff_mod.render(data, mode=mode)


@app.command()
def status() -> None:
    """Show a quick, read-only health-check snapshot of the current state.

    Reports whether the session is armed, its id and start time, this session's
    event/block counts, whether the shadow repo exists (and where), and whether
    the UI is built. Read-only: never modifies the store or the work tree.
    """
    from . import status as status_mod

    data = status_mod.build_status()
    status_mod.render(data)


@app.command()
def open(
    port: int = typer.Option(8765, help="Port to bind on 127.0.0.1."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser."),
) -> None:
    """Start the local restory server and open the timeline UI in a browser."""
    import threading
    import webbrowser

    import uvicorn

    from .server import app as fastapi_app

    url = f"http://127.0.0.1:{port}/"
    typer.echo(f"restory UI at {url} (Ctrl+C to stop)")
    if not no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(fastapi_app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    app()
