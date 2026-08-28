"""Command-line interface for leash."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import typer

from .config import find_repo_root

app = typer.Typer(help="leash command-line interface.")

# Tools whose calls leash guards.
_MATCHER = "Bash|Write|Edit|MultiEdit"
_HOOK_EVENTS = ("PreToolUse", "PostToolUse")


def _leash_command(subcommand: str) -> str:
    """Return a Windows-safe command string that invokes ``leash <subcommand>``.

    Prefer the ``leash`` console script if it is on PATH; otherwise fall back
    to the absolute path of the current interpreter with ``-m leash``.
    """
    if shutil.which("leash"):
        return f"leash {subcommand}"
    return f'"{sys.executable}" -m leash {subcommand}'


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
    """Merge leash PreToolUse/PostToolUse hook entries without clobbering others."""
    hooks = settings.setdefault("hooks", {})
    for event in _HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        if not _entry_has_command(entries, command):
            entries.append(_hook_block(command, matcher=_MATCHER))
    return settings


def _merge_session_start(settings: dict, command: str) -> dict:
    """Merge a leash SessionStart hook entry without clobbering others."""
    hooks = settings.setdefault("hooks", {})
    entries = hooks.setdefault("SessionStart", [])
    if not _entry_has_command(entries, command):
        entries.append(_hook_block(command))
    return settings


@app.command()
def init() -> None:
    """Install leash SessionStart + PreToolUse/PostToolUse hooks into .claude/settings.json."""
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

    hook_command = _leash_command("hook")
    session_command = _leash_command("session-start")
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

    Installed as a Claude Code SessionStart hook by ``leash init`` so undo is
    armed automatically at the start of every session (no need to run
    ``leash watch`` first).
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
        typer.echo("No shadow repo found. Start a session (`leash session-start`) or run `leash watch` first.")
        raise typer.Exit(1)

    if session:
        sess = store.latest_session()
        if sess is None:
            typer.echo("No session anchor recorded. Run `leash session-start` first.")
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
def report() -> None:
    """Generate a report."""
    typer.echo("TODO: report")


@app.command()
def open(
    port: int = typer.Option(8765, help="Port to bind on 127.0.0.1."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser."),
) -> None:
    """Start the local leash server and open the timeline UI in a browser."""
    import threading
    import webbrowser

    import uvicorn

    from .server import app as fastapi_app

    url = f"http://127.0.0.1:{port}/"
    typer.echo(f"leash UI at {url} (Ctrl+C to stop)")
    if not no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(fastapi_app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    app()
