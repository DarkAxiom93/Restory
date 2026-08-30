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
* ``detect``          — is this agent installed on *this* machine? (for ``--all``)

Adding a new agent (Cursor, Windsurf, opencode, ...) is one more ``Adapter``
subclass plus a line in :data:`_ADAPTERS`; ``classify``/``store``/``snapshot``
stay untouched. Because ``restory init --all`` iterates the registry and asks
each adapter to ``detect`` itself, a newly registered adapter is automatically
included in ``--all`` with no other change.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import config

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
# Adapter interface
# --------------------------------------------------------------------------- #


class Adapter:
    """Translates one coding agent's hook contract to/from restory's core."""

    key: str = ""  # value accepted by ``restory init --agent``
    label: str = ""  # human-readable name for CLI output

    # Presence signals used by the default :meth:`detect` heuristic. An adapter
    # that ships a per-project/per-user config directory names it here (checked
    # in the user's home *and* the repo root), plus any CLI executables that,
    # when found on PATH, prove the agent is installed. An adapter with a wholly
    # different signal (a registry key, a plugin manifest, ...) overrides
    # :meth:`detect` instead.
    config_dirname: str = ""
    executables: tuple[str, ...] = ()

    @classmethod
    def matches(cls, payload: dict) -> bool:
        """True if ``payload`` was emitted by this agent (for auto-detection)."""
        raise NotImplementedError

    def detect(self) -> bool:
        """True if this agent appears to be installed on the current machine.

        Default heuristic: a directory named :attr:`config_dirname` exists in
        the user's home directory or in the current repo root, or one of
        :attr:`executables` resolves on ``PATH``. Subclasses with a different
        way of proving presence override this method; ``restory init --all``
        only ever calls ``detect`` — it never inspects these attributes — so a
        custom override slots in transparently.
        """
        if self.config_dirname:
            if (config.home_dir() / self.config_dirname).is_dir():
                return True
            if (config.find_repo_root() / self.config_dirname).is_dir():
                return True
        return any(shutil.which(exe) for exe in self.executables)

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

    config_dirname = ".claude"
    executables = ("claude",)

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

    config_dirname = ".gemini"
    executables = ("gemini",)

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
# Registry + lookup
# --------------------------------------------------------------------------- #

_ADAPTERS: dict[str, Adapter] = {
    a.key: a for a in (ClaudeAdapter(), GeminiAdapter())
}


def agent_keys() -> list[str]:
    """Return the accepted ``--agent`` values (default first)."""
    return list(_ADAPTERS)


def all_adapters() -> list[Adapter]:
    """Return every registered adapter (default first)."""
    return list(_ADAPTERS.values())


def detect_present_adapters() -> list[Adapter]:
    """Return the registered adapters that ``detect`` themselves as installed.

    Generic over the registry: each adapter decides its own presence, so a
    future adapter is picked up by ``restory init --all`` with no change here.
    """
    return [adapter for adapter in _ADAPTERS.values() if adapter.detect()]


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
