"""Claude Code PreToolUse/PostToolUse hook entry point.

Reads a single JSON hook payload from stdin, classifies it, records the
event in the session store, and writes a decision object to stdout matching
Claude Code's hook contract:

    danger  -> {"decision": "block",   "reason": <reason>}
    safe    -> {"decision": "approve"}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import store
from .classify import classify
from .config import find_repo_root


def _read_payload(stream) -> dict:
    data = stream.read()
    if not data:
        return {}
    # Windows pipes (e.g. PowerShell) prepend a UTF-8 BOM; strip it and any
    # surrounding whitespace before parsing.
    data = data.lstrip("﻿").strip()
    if not data:
        return {}
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    payload = _read_payload(sys.stdin)

    cwd = payload.get("cwd")
    repo_root = find_repo_root(Path(cwd)) if cwd else None

    result = classify(payload, repo_root=repo_root)

    # Always record the event, even for safe calls. Never let a store failure
    # break the hook contract on stdout.
    try:
        event_id = store.append_event(
            tool_name=payload.get("tool_name", ""),
            tags=result.tags,
            danger=result.danger,
            reason=result.reason,
            raw=payload,
        )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"leash: failed to record event: {exc}", file=sys.stderr)
        event_id = None

    # On PostToolUse mutations, snapshot the work tree into the shadow repo
    # (only if watching has already created the shadow). Never break stdout.
    if payload.get("hook_event_name") == "PostToolUse" and payload.get("tool_name") in (
        "Bash",
        "Write",
        "Edit",
        "MultiEdit",
    ):
        try:
            from . import snapshot

            shadow = snapshot.get_shadow(Path(cwd) if cwd else None)
            if shadow.exists():
                shadow.snapshot(event_id if event_id is not None else "unknown")
        except Exception as exc:  # pragma: no cover - defensive
            print(f"leash: snapshot failed: {exc}", file=sys.stderr)

    if result.danger:
        decision = {"decision": "block", "reason": result.reason}
    else:
        decision = {"decision": "approve"}

    sys.stdout.write(json.dumps(decision))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
