"""Pre/Post tool-use hook entry point for every supported coding agent.

Reads a single JSON hook payload from stdin, detects which agent emitted it,
normalizes it to restory's canonical shape, classifies it, records the event in
the session store, and writes back the decision object that *that* agent expects
on stdout. For Claude Code this is::

    danger  -> {"decision": "block",   "reason": <reason>}
    safe    -> {"decision": "approve"}

Other agents differ only in the decision object (e.g. Gemini omits the decision
for the safe case); the mapping lives entirely in :mod:`restory.adapters`, so
this entrypoint stays agent-agnostic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import allowlist, store
from .adapters import CANONICAL_MUTATING, detect_adapter
from .classify import classify
from .config import find_repo_root

ALLOWLIST_TAG = "allowlisted-override"


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


def _command_string(payload: dict) -> str:
    """The exact command text the allowlist matches against.

    The allowlist is command-string based, so it only applies to Bash tool
    calls; file operations (Write/Edit) have no command to allowlist and always
    fall through to normal classification.
    """
    if payload.get("tool_name") != "Bash":
        return ""
    tool_input = payload.get("tool_input") or {}
    return str(tool_input.get("command", ""))


def main() -> int:
    raw_payload = _read_payload(sys.stdin)

    # Detect the emitting agent and translate its payload to the canonical shape
    # the core (classify/store/snapshot) understands.
    adapter = detect_adapter(raw_payload)
    payload = adapter.normalize(raw_payload)

    cwd = payload.get("cwd")
    repo_root = find_repo_root(Path(cwd)) if cwd else None

    result = classify(payload, repo_root=repo_root)

    # User allowlist carve-out: AFTER classification, BEFORE blocking, check
    # whether the user has explicitly, exactly allowlisted this command. If so,
    # approve it but keep a full audit trail (danger stays False since it ran,
    # the original tags are preserved, and an ``allowlisted-override`` tag is
    # added so the timeline/report shows an allowlisted command was let through).
    # The allowlist is read only from the user's own dir — never the repo — and
    # only an exact-command match flips the decision; everything else is
    # untouched and blocks exactly as before. Classification itself is never
    # weakened: we override the *decision*, not the *analysis*.
    tags = result.tags
    danger = result.danger
    reason = result.reason
    if danger:
        command = _command_string(payload)
        if command and allowlist.is_allowlisted(command):
            danger = False
            tags = [*result.tags, ALLOWLIST_TAG]
            reason = f"{ALLOWLIST_TAG}: user-approved, was [{result.reason}]"

    # Always record the event, even for safe calls. Never let a store failure
    # break the hook contract on stdout. The raw agent payload is kept verbatim
    # for provenance; the recorded tool_name is the canonical one.
    try:
        event_id = store.append_event(
            tool_name=payload.get("tool_name", ""),
            tags=tags,
            danger=danger,
            reason=reason,
            raw=raw_payload,
        )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"restory: failed to record event: {exc}", file=sys.stderr)
        event_id = None

    # On PostToolUse mutations, snapshot the work tree into the shadow repo
    # (only if watching has already created the shadow). Never break stdout.
    if payload.get("hook_event_name") == "PostToolUse" and payload.get(
        "tool_name"
    ) in CANONICAL_MUTATING:
        try:
            from . import snapshot

            shadow = snapshot.get_shadow(Path(cwd) if cwd else None)
            if shadow.exists():
                shadow.snapshot(event_id if event_id is not None else "unknown")
        except Exception as exc:  # pragma: no cover - defensive
            print(f"restory: snapshot failed: {exc}", file=sys.stderr)

    decision = adapter.render_decision(danger, reason)

    sys.stdout.write(json.dumps(decision))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
