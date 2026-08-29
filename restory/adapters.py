"""Per-agent adapters mapping external hook contracts onto restory's core.

restory's core (``classify`` / ``store`` / ``snapshot``) speaks a single
*canonical* payload shape::

    {"tool_name": str, "tool_input": dict, "cwd": str | None, "hook_event_name": str}

with ``tool_name`` in ``{Bash, Write, Edit, MultiEdit}`` and ``hook_event_name``
in ``{PreToolUse, PostToolUse}`` — the shape Claude Code already emits. Every
other coding agent restory guards emits its *own* hook payload and expects its
*own* decision object. An :class:`Adapter` translates both directions so the
core never has to know which agent it is serving:

* ``normalize``       — agent payload  -> canonical payload (inbound)
* ``render_decision`` — restory verdict -> agent decision object (outbound)
* ``install``         — write the agent's hook-config file
* ``matches``         — recognise the agent from an incoming payload

Adding a new agent (Cursor, Windsurf, opencode, ...) is one more ``Adapter``
subclass plus a line in :data:`_ADAPTERS`; ``classify``/``store``/``snapshot``
stay untouched.
"""

from __future__ import annotations

import json
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

# Canonical tool names restory's classifier understands.
CANONICAL_MUTATING = ("Bash", "Write", "Edit", "MultiEdit")

DEFAULT_AGENT = "claude"


# --------------------------------------------------------------------------- #
# Shared settings.json writer (Claude + Gemini use the same nested shape)
# --------------------------------------------------------------------------- #


@dataclass
class InstallResult:
    settings_path: Path
    backup_path: Path | None
    rendered: str


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


def _load_settings(settings_path: Path) -> tuple[dict, Path | None]:
    """Read (and back up) an existing settings file.

    Returns ``(settings, backup_path)``; ``backup_path`` is ``None`` when no
    file existed yet. A malformed or non-object file is treated as empty so a
    fresh, valid config is written over it (after backing it up).
    """
    if not settings_path.exists():
        return {}, None
    backup = settings_path.with_suffix(settings_path.suffix + ".bak")
    shutil.copy2(settings_path, backup)
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            settings = {}
    except (json.JSONDecodeError, OSError):
        settings = {}
    return settings, backup


def _install_settings_json(
    settings_path: Path,
    *,
    tool_events: tuple[str, ...],
    matcher: str,
    hook_command: str,
    session_event: str,
    session_command: str,
) -> InstallResult:
    """Merge restory hook entries into ``settings_path`` without clobbering others.

    Idempotent: an entry is only appended if the same ``command`` is not already
    present for that event.
    """
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings, backup = _load_settings(settings_path)

    hooks = settings.setdefault("hooks", {})
    for event in tool_events:
        entries = hooks.setdefault(event, [])
        if not _entry_has_command(entries, hook_command):
            entries.append(_hook_block(hook_command, matcher=matcher))

    sess_entries = hooks.setdefault(session_event, [])
    if not _entry_has_command(sess_entries, session_command):
        sess_entries.append(_hook_block(session_command))

    rendered = json.dumps(settings, indent=2)
    settings_path.write_text(rendered + "\n", encoding="utf-8")
    return InstallResult(settings_path, backup, rendered)


# --------------------------------------------------------------------------- #
# Cursor hooks.json writer (Cursor's own, flatter shape)
# --------------------------------------------------------------------------- #


def _install_cursor_hooks(
    hooks_path: Path,
    *,
    hook_command: str,
    session_command: str,
    matcher: str,
) -> InstallResult:
    """Merge restory hook entries into a Cursor ``.cursor/hooks.json``.

    Cursor's schema is ``{"version": 1, "hooks": {<event>: [{"command", ...}]}}``
    — a flat list of ``{"command", "matcher", "failClosed", ...}`` entries per
    event, *not* the ``{"matcher", "hooks": [{"type", "command"}]}`` nesting that
    Claude/Gemini use, so this needs its own writer rather than
    :func:`_install_settings_json`.

    Idempotent: an entry is appended only if the same ``command`` is not already
    present for that event. ``preToolUse`` is the guard (``failClosed: true`` so a
    hook crash/timeout blocks rather than fails open — the safe default for a
    security tool); ``postToolUse`` drives snapshotting; ``sessionStart`` anchors
    the undo baseline.
    """
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    settings, backup = _load_settings(hooks_path)

    settings.setdefault("version", 1)
    hooks = settings.setdefault("hooks", {})

    def _ensure(event: str, command: str, *, matcher: str | None = None,
                fail_closed: bool = False) -> None:
        entries = hooks.setdefault(event, [])
        if any(isinstance(e, dict) and e.get("command") == command for e in entries):
            return
        entry: dict = {"command": command}
        if matcher is not None:
            entry["matcher"] = matcher
        if fail_closed:
            entry["failClosed"] = True
        entries.append(entry)

    _ensure("preToolUse", hook_command, matcher=matcher, fail_closed=True)
    _ensure("postToolUse", hook_command, matcher=matcher)
    _ensure("sessionStart", session_command)

    rendered = json.dumps(settings, indent=2)
    hooks_path.write_text(rendered + "\n", encoding="utf-8")
    return InstallResult(hooks_path, backup, rendered)


# --------------------------------------------------------------------------- #
# Adapter interface
# --------------------------------------------------------------------------- #


class Adapter:
    """Translates one coding agent's hook contract to/from restory's core."""

    key: str = ""  # value accepted by ``restory init --agent``
    label: str = ""  # human-readable name for CLI output

    @classmethod
    def matches(cls, payload: dict) -> bool:
        """True if ``payload`` was emitted by this agent (for auto-detection)."""
        raise NotImplementedError

    def normalize(self, payload: dict) -> dict:
        """Agent payload -> canonical ``{tool_name, tool_input, cwd, hook_event_name}``."""
        raise NotImplementedError

    def render_decision(self, danger: bool, reason: str) -> dict:
        """restory verdict -> the decision object this agent expects on stdout."""
        raise NotImplementedError

    def install(
        self, repo_root: Path, hook_command: str, session_command: str
    ) -> InstallResult:
        """Write this agent's hook-config file under ``repo_root``."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Claude Code (default — the shape restory was built around)
# --------------------------------------------------------------------------- #


class ClaudeAdapter(Adapter):
    key = "claude"
    label = "Claude Code"

    _MATCHER = "Bash|Write|Edit|MultiEdit"

    @classmethod
    def matches(cls, payload: dict) -> bool:
        return payload.get("hook_event_name") in ("PreToolUse", "PostToolUse")

    def normalize(self, payload: dict) -> dict:
        # Claude's payload already *is* the canonical shape; copy the fields the
        # core reads so downstream never sees agent-specific extras.
        return {
            "tool_name": payload.get("tool_name", ""),
            "tool_input": payload.get("tool_input") or {},
            "cwd": payload.get("cwd"),
            "hook_event_name": payload.get("hook_event_name", ""),
        }

    def render_decision(self, danger: bool, reason: str) -> dict:
        if danger:
            return {"decision": "block", "reason": reason}
        return {"decision": "approve"}

    def install(
        self, repo_root: Path, hook_command: str, session_command: str
    ) -> InstallResult:
        return _install_settings_json(
            repo_root / ".claude" / "settings.json",
            tool_events=("PreToolUse", "PostToolUse"),
            matcher=self._MATCHER,
            hook_command=hook_command,
            session_event="SessionStart",
            session_command=session_command,
        )


# --------------------------------------------------------------------------- #
# Gemini CLI
# --------------------------------------------------------------------------- #


class GeminiAdapter(Adapter):
    """Gemini CLI (``.gemini/settings.json`` — ``BeforeTool`` / ``AfterTool``).

    Gemini's hook payload carries ``tool_name`` / ``tool_input`` / ``cwd`` /
    ``hook_event_name`` just like Claude's, so normalization is only a rename of
    the tool-name and event vocabularies. On stdout Gemini honours
    ``{"decision": "block", "reason": ...}`` unchanged; for the allow case it
    treats an *omitted* decision as "proceed", which sidesteps version-to-version
    differences in how a non-block ``decision`` value is read.
    """

    key = "gemini"
    label = "Gemini CLI"

    _MATCHER = "run_shell_command|write_file|replace"

    # Gemini tool name -> canonical restory tool name.
    _TOOL_NAMES = {
        "run_shell_command": "Bash",
        "write_file": "Write",
        "replace": "Edit",
    }
    # Gemini hook event -> canonical restory event.
    _EVENTS = {
        "BeforeTool": "PreToolUse",
        "AfterTool": "PostToolUse",
    }

    @classmethod
    def matches(cls, payload: dict) -> bool:
        if payload.get("hook_event_name") in cls._EVENTS:
            return True
        return payload.get("tool_name") in cls._TOOL_NAMES

    def normalize(self, payload: dict) -> dict:
        raw_tool = payload.get("tool_name", "")
        raw_event = payload.get("hook_event_name", "")
        return {
            # Unknown tools pass through untranslated; classify() simply returns
            # "safe" for anything outside the canonical set.
            "tool_name": self._TOOL_NAMES.get(raw_tool, raw_tool),
            "tool_input": payload.get("tool_input") or {},
            "cwd": payload.get("cwd"),
            "hook_event_name": self._EVENTS.get(raw_event, raw_event),
        }

    def render_decision(self, danger: bool, reason: str) -> dict:
        if danger:
            return {"decision": "block", "reason": reason}
        # Omit the decision entirely -> Gemini proceeds (safe fallback).
        return {}

    def install(
        self, repo_root: Path, hook_command: str, session_command: str
    ) -> InstallResult:
        return _install_settings_json(
            repo_root / ".gemini" / "settings.json",
            tool_events=("BeforeTool", "AfterTool"),
            matcher=self._MATCHER,
            hook_command=hook_command,
            session_event="SessionStart",
            session_command=session_command,
        )


# --------------------------------------------------------------------------- #
# Cursor (experimental)
# --------------------------------------------------------------------------- #


class CursorAdapter(Adapter):
    """Cursor (``.cursor/hooks.json`` — ``preToolUse`` / ``postToolUse``).

    **Experimental.** The contract is confirmed against Cursor's hooks docs
    (cursor.com/docs/hooks), but Cursor notes ``preToolUse`` enforcement is still
    maturing (``ask`` is accepted-but-not-enforced today; ``deny`` blocks), so we
    ship it marked experimental like the Gemini adapter.

    Cursor's generic ``preToolUse`` hook fires for all tools and carries
    ``tool_name`` / ``tool_input`` (the shell command lives at
    ``tool_input.command``, unlike the ``beforeShellExecution`` event where it is
    top-level). Normalization renames Cursor's tool/event vocabularies onto
    restory's canonical set. The ``Delete`` tool has no shell command, so it is
    re-expressed as an ``rm <path>`` command: this routes a real file deletion
    through ``classify``'s existing delete detector — a delete of the repo root,
    home, the filesystem root, or a path outside the repo is flagged and blocked,
    while an ordinary in-repo file delete is allowed and captured by the snapshot
    /undo net (matching how restory already treats ``rm`` in a shell payload).

    On stdout Cursor wants ``{"permission": "deny", "agent_message": ...}`` to
    block (snake_case, confirmed) and ``{"permission": "allow"}`` to proceed.
    """

    key = "cursor"
    label = "Cursor (experimental)"

    # preToolUse tools restory guards. Read/Grep/Task are irrelevant (classify
    # treats them as safe) and are excluded from the installed matcher.
    _MATCHER = "Shell|Write|Delete"

    # Cursor preToolUse tool name -> canonical restory tool name. ``Delete`` maps
    # to Bash because normalize() rewrites it as an ``rm`` command.
    _TOOL_NAMES = {
        "Shell": "Bash",
        "Write": "Write",
        "Delete": "Bash",
    }
    # Cursor hook event -> canonical restory event.
    _EVENTS = {
        "preToolUse": "PreToolUse",
        "postToolUse": "PostToolUse",
        "postToolUseFailure": "PostToolUse",
    }
    _CURSOR_EVENTS = frozenset(_EVENTS)

    @classmethod
    def matches(cls, payload: dict) -> bool:
        if payload.get("hook_event_name") in cls._CURSOR_EVENTS:
            return True
        # Cursor's other tool names (Read/Write/Grep/Task) collide with Claude's,
        # so only "Shell" (Claude uses "Bash") is a safe event-free signal.
        return payload.get("tool_name") == "Shell"

    def normalize(self, payload: dict) -> dict:
        raw_tool = payload.get("tool_name", "")
        raw_event = payload.get("hook_event_name", "")
        tool_input = payload.get("tool_input") or {}

        if raw_tool == "Delete":
            # Re-express the deletion as an `rm <path>` command so classify()'s
            # delete detector runs. Non-recursive on purpose: dangerous targets
            # (repo root / home / fs root / outside repo) are flagged, ordinary
            # in-repo deletes stay allowed and are caught by the undo net.
            target = tool_input.get("file_path") or tool_input.get("path") or "."
            tool_input = {"command": f"rm {shlex.quote(str(target))}"}

        return {
            # Unknown tools pass through untranslated; classify() returns "safe"
            # for anything outside the canonical set.
            "tool_name": self._TOOL_NAMES.get(raw_tool, raw_tool),
            "tool_input": tool_input,
            "cwd": payload.get("cwd"),
            "hook_event_name": self._EVENTS.get(raw_event, raw_event),
        }

    def render_decision(self, danger: bool, reason: str) -> dict:
        # Confirmed snake_case contract (preToolUse output on cursor.com/docs/
        # hooks). hook.py exits 0; Cursor also treats exit code 2 as an
        # equivalent deny, our documented fallback if stdout is unavailable.
        if danger:
            return {"permission": "deny", "agent_message": reason}
        return {"permission": "allow"}

    def install(
        self, repo_root: Path, hook_command: str, session_command: str
    ) -> InstallResult:
        return _install_cursor_hooks(
            repo_root / ".cursor" / "hooks.json",
            hook_command=hook_command,
            session_command=session_command,
            matcher=self._MATCHER,
        )


# --------------------------------------------------------------------------- #
# Registry + lookup
# --------------------------------------------------------------------------- #

_ADAPTERS: dict[str, Adapter] = {
    a.key: a for a in (ClaudeAdapter(), GeminiAdapter(), CursorAdapter())
}


def agent_keys() -> list[str]:
    """Return the accepted ``--agent`` values (default first)."""
    return list(_ADAPTERS)


def get_adapter(key: str) -> Adapter:
    """Look up an adapter by ``--agent`` key, raising ``ValueError`` if unknown."""
    try:
        return _ADAPTERS[key]
    except KeyError:
        choices = ", ".join(_ADAPTERS)
        raise ValueError(f"Unknown agent {key!r}. Choose from: {choices}.") from None


def detect_adapter(payload: dict) -> Adapter:
    """Pick the adapter for an incoming hook payload.

    Non-default agents are probed first; anything unrecognised falls back to the
    default (Claude) adapter, preserving restory's original behaviour for empty
    or Claude-shaped payloads.
    """
    for adapter in _ADAPTERS.values():
        if adapter.key == DEFAULT_AGENT:
            continue
        if type(adapter).matches(payload):
            return adapter
    return _ADAPTERS[DEFAULT_AGENT]
