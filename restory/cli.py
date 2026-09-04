"""Command-line interface for restory."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import typer

from . import adapters
from .config import find_repo_root

app = typer.Typer(help="restory command-line interface.")


def _version_callback(value: bool) -> None:
    if value:
        from importlib.metadata import PackageNotFoundError, version

        try:
            typer.echo(version("restory"))
        except PackageNotFoundError:
            typer.echo("unknown")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the installed restory version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """restory command-line interface."""


def _restory_command(subcommand: str) -> str:
    """Return a Windows-safe command string that invokes ``restory <subcommand>``.

    Prefer the ``restory`` console script if it is on PATH; otherwise fall back
    to the absolute path of the current interpreter with ``-m restory``.
    """
    if shutil.which("restory"):
        return f"restory {subcommand}"
    return f'"{sys.executable}" -m restory {subcommand}'


@app.command()
def init(
    agent: str = typer.Option(
        adapters.DEFAULT_AGENT,
        "--agent",
        help=(
            "Coding agent to install hooks for. One of: "
            f"{', '.join(adapters.agent_keys())} (default: {adapters.DEFAULT_AGENT})."
        ),
    ),
) -> None:
    """Install restory SessionStart + pre/post tool-use hooks for a coding agent.

    Defaults to Claude Code (``.claude/settings.json``); ``--agent gemini``
    writes ``.gemini/settings.json`` instead. The same ``restory hook``
    entrypoint serves every agent — it detects the payload shape at run time.
    """
    try:
        adapter = adapters.get_adapter(agent)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(2)

    repo_root = find_repo_root()
    hook_command = _restory_command("hook")
    session_command = _restory_command("session-start")

    result = adapter.install(repo_root, hook_command, session_command)

    if result.backup_path is not None:
        typer.echo(f"Backed up existing settings to {result.backup_path}")
    typer.echo(f"Wrote {result.settings_path} ({adapter.label}):")
    typer.echo(result.rendered)


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
    session_id = store.record_session(anchor, repo_root=shadow.repo_root)
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
        sess = store.latest_session(repo_root=shadow.repo_root)
        if sess is None:
            typer.echo("No session anchor recorded for this repository. Run `restory session-start` first.")
            raise typer.Exit(1)
        # Defense in depth: never reset this work tree to an anchor recorded for
        # a different repository. ``latest_session`` is already scoped to this
        # repo, so this can only fail on a corrupted/hand-edited row — in which
        # case we hard-fail rather than guess or fall back to another session.
        # The same guard runs in the monitor TUI's undo path via this helper.
        mismatch = store.session_repo_mismatch(sess, repo_root=shadow.repo_root)
        if mismatch is not None:
            typer.echo(mismatch)
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
def monitor() -> None:
    """Open a live, full-screen terminal dashboard of session events.

    The terminal-native counterpart to ``restory open``: polls the SQLite store
    ~once a second and shows events newest-first, blocked ones highlighted in
    red. Keys: ``q`` quit, ``u`` undo the session (with confirmation), ``c``
    clear the view. Read-only except for the explicit undo action.
    """
    from . import tui

    tui.run()


@app.command()
def open(
    port: int = typer.Option(8765, help="Port to bind on 127.0.0.1."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser."),
) -> None:
    """Start the local restory server and open the timeline UI in a browser."""
    import secrets
    import threading
    import webbrowser

    import uvicorn

    from .config import find_repo_root
    from .server import create_app

    # Per-session token. It authenticates the UI's API calls and is handed to
    # the browser in the URL *fragment* (never sent to the server, never logged).
    token = secrets.token_urlsafe(32)
    repo_root = find_repo_root()
    fastapi_app = create_app(token=token, port=port, repo_root=repo_root)

    # The token rides in the fragment (after '#'); the browser keeps it client
    # side and the UI strips it from the address bar on load.
    launch_url = f"http://127.0.0.1:{port}/#{token}"
    display_url = f"http://127.0.0.1:{port}/"  # token-free: safe to print/log
    typer.echo(f"restory UI at {display_url} (Ctrl+C to stop)")
    if not no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(launch_url)).start()
    # Bind strictly to loopback (never 0.0.0.0) so the server is unreachable
    # from other hosts on the network.
    uvicorn.run(fastapi_app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    app()
