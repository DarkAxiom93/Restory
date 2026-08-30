"""Tests for the restory hook entrypoint's severity-aware decisions."""

from __future__ import annotations

import io
import json

from restory import classify as classify_mod
from restory import hook as hook_mod
from restory import store


def _isolate(monkeypatch, tmp_path):
    """Redirect the restory data dir to an isolated home for this test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))


def _run_hook(monkeypatch, payload: dict) -> dict:
    """Drive hook.main() with ``payload`` on stdin, return the decision dict."""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    hook_mod.main()
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    return json.loads(lines[-1])


def test_critical_block_reason_names_the_severity(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf ~"},
        "cwd": str(tmp_path),
        "hook_event_name": "PreToolUse",
    }
    decision = _run_hook(monkeypatch, payload)
    assert decision["decision"] == "block"
    # Severity is surfaced in the block reason the agent shows the user.
    assert decision["reason"].startswith("[CRITICAL]")
    assert "mass-delete" in decision["reason"]


def test_block_level_reason_names_the_severity(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path.parent / "evil.dll")},
        "cwd": str(tmp_path),
        "hook_event_name": "PreToolUse",
    }
    decision = _run_hook(monkeypatch, payload)
    assert decision["decision"] == "block"
    assert decision["reason"].startswith("[BLOCK]")


def test_safe_call_approves_with_a_clean_reason(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "npm test"},
        "cwd": str(tmp_path),
        "hook_event_name": "PreToolUse",
    }
    decision = _run_hook(monkeypatch, payload)
    assert decision["decision"] == "approve"
    assert "[" not in decision.get("reason", "")


def test_warn_downgrade_approves_but_records(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setitem(classify_mod._TAG_SEVERITY, "write-outside-repo", classify_mod.WARN)
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path.parent / "note.txt")},
        "cwd": str(tmp_path),
        "hook_event_name": "PreToolUse",
    }
    decision = _run_hook(monkeypatch, payload)
    # WARN allows the call through...
    assert decision["decision"] == "approve"
    # ...but the event is still recorded, carrying its tag, so the timeline and
    # report surface it.
    events = store.fetch_events(limit=10)
    assert events
    latest = events[0]
    assert latest["danger"] is False
    assert "write-outside-repo" in latest["tags"]
