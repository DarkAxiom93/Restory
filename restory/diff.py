"""Read-only diff of the work tree since the current session started.

``restory diff`` compares the current session's anchor commit against the
present state of the work tree, using the shadow repo. It prints a rich summary
(files added / modified / deleted, with counts) followed by the plain patch so
it stays copy-pasteable. ``--stat`` shows only the summary; ``--name-only``
lists just the changed paths.

Nothing here modifies the work tree or the shadow.
"""

from __future__ import annotations

from pathlib import Path

from . import snapshot, store

# First-letter of a git name-status code -> (bucket, label, style).
_STATUS_META = {
    "A": ("added", "added", "green"),
    "M": ("modified", "modified", "yellow"),
    "D": ("deleted", "deleted", "red"),
    "R": ("renamed", "renamed", "cyan"),
    "C": ("copied", "copied", "cyan"),
    "T": ("modified", "typechange", "yellow"),
}


def _parse_name_status(text: str) -> list[dict]:
    """Parse ``git diff --name-status`` output into ``{status, path}`` dicts."""
    entries: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        path = parts[-1]  # for renames/copies this is the new path
        entries.append({"status": status, "path": path})
    return entries


def _summarize(entries: list[dict]) -> dict:
    counts = {"added": 0, "modified": 0, "deleted": 0, "renamed": 0, "copied": 0}
    files: list[dict] = []
    for e in entries:
        bucket, label, style = _STATUS_META.get(
            e["status"][0], ("modified", e["status"], "yellow")
        )
        counts[bucket] = counts.get(bucket, 0) + 1
        files.append({"path": e["path"], "label": label, "style": style})
    return {"counts": counts, "files": files}


def gather(mode: str = "full", repo_root: Path | None = None) -> dict:
    """Collect the diff data for the current session (read-only).

    ``mode`` is ``"full"``, ``"stat"``, or ``"name-only"``. Returns a dict with
    ``available`` False (plus a ``reason``) when there is no anchor or no
    changes, otherwise the parsed summary (and the patch text for ``"full"``).
    """
    session = store.latest_session()
    shadow = snapshot.get_shadow(repo_root)

    if session is None:
        return {"available": False, "reason": "no-session"}
    if not shadow.exists():
        return {"available": False, "reason": "no-shadow"}

    anchor = session["anchor_commit"]
    entries = _parse_name_status(shadow.diff_against(anchor, fmt="name-status"))
    if not entries:
        return {"available": False, "reason": "no-changes", "session": session}

    result = {
        "available": True,
        "mode": mode,
        "session": session,
        "anchor": anchor,
        "summary": _summarize(entries),
    }
    if mode == "full":
        result["diff"] = shadow.diff_against(anchor, fmt="full")
    return result


def _print_unavailable(data: dict, echo) -> None:
    reason = data.get("reason")
    if reason == "no-session":
        echo("No session anchor yet. Run `restory session-start` to arm a session.")
    elif reason == "no-shadow":
        echo("No shadow repo found. Run `restory session-start` or `restory watch` first.")
    else:  # no-changes
        echo("No changes since the session started. Working tree matches the anchor.")


def render(data: dict, mode: str = "full", console=None, echo=None) -> None:
    """Render the diff: rich summary, then (for ``full``) the plain patch."""
    import typer

    echo = echo or typer.echo

    if not data.get("available"):
        _print_unavailable(data, echo)
        return

    summary = data["summary"]

    # --name-only: just the paths, plain, for copy-paste. No rich, no summary.
    if mode == "name-only":
        for f in summary["files"]:
            echo(f["path"])
        return

    from rich.console import Console
    from rich.table import Table

    console = console or Console()

    session = data["session"]
    console.print(
        f"[bold]Changes since session {session['id']}[/] "
        f"[dim](anchor {str(data['anchor'])[:12]})[/]"
    )

    c = summary["counts"]
    parts = [
        f"[green]{c['added']} added[/]",
        f"[yellow]{c['modified']} modified[/]",
        f"[red]{c['deleted']} deleted[/]",
    ]
    if c.get("renamed"):
        parts.append(f"[cyan]{c['renamed']} renamed[/]")
    if c.get("copied"):
        parts.append(f"[cyan]{c['copied']} copied[/]")
    console.print("  " + "   ".join(parts))

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("Status", no_wrap=True)
    table.add_column("Path", overflow="fold")
    for f in summary["files"]:
        table.add_row(f"[{f['style']}]{f['label']:<10}[/]", f["path"])
    console.print(table)

    # --stat stops at the summary; full mode prints the plain patch below.
    if mode == "full":
        echo("")
        echo(data["diff"].rstrip("\n"))
