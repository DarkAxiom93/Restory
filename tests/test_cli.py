"""Tests for the restory CLI command dispatch (typer CliRunner).

These exercise command wiring, option handling, and exit codes — not the
underlying logic, which is covered in the classify/store/snapshot/diff tests.
Each test runs in an isolated temp home (``USERPROFILE``) and an isolated
working directory so nothing touches real data.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from restory import snapshot, store
from restory.cli import app

runner = CliRunner()

needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


def _git_init(repo):
    """Make ``repo`` a real git repo so the CLI's ``find_repo_root()`` pins here.

    The commands resolve the shadow via ``find_repo_root()``, which walks up
    from the cwd to the nearest ``.git``. Without a ``.git`` in the temp repo
    the walk can escape to a stray ancestor repo (e.g. above the pytest temp
    dir), so the CLI looks for the shadow under a different path than the test
    set it up under. A real ``.git`` here pins the root deterministically and
    matches how restory is actually run (always inside a git repo).
    """
    if shutil.which("git") is None:
        return
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, capture_output=True)


def _isolate(monkeypatch, tmp_path):
    """Isolate the data dir (USERPROFILE) and cwd (repo root) for a CLI run."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.chdir(repo)
    return repo


def _armed_session(repo):
    """Init a shadow, take a baseline, and record it as the current session."""
    shadow, _ = snapshot.ensure_shadow(repo)
    anchor = shadow.session_baseline()
    store.record_session(anchor)
    return shadow, anchor


# --------------------------------------------------------------------------- #
# version / help
# --------------------------------------------------------------------------- #


def test_version_option_prints_version_and_exits():
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0
    assert res.stdout.strip()  # some version string (or "unknown")


def test_help_lists_commands():
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    for cmd in ("init", "status", "report", "diff", "export", "undo",
                "session-start", "hook", "monitor", "watch", "open"):
        assert cmd in res.stdout


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


def test_init_default_agent_writes_claude_settings(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    res = runner.invoke(app, ["init"])
    assert res.exit_code == 0
    assert "Claude Code" in res.stdout
    settings = repo / ".claude" / "settings.json"
    assert settings.exists()
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert "PreToolUse" in data["hooks"]
    assert "SessionStart" in data["hooks"]


def test_init_gemini_agent_writes_gemini_settings(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    res = runner.invoke(app, ["init", "--agent", "gemini"])
    assert res.exit_code == 0
    assert "Gemini CLI" in res.stdout
    assert (repo / ".gemini" / "settings.json").exists()


def test_init_unknown_agent_exits_2(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    res = runner.invoke(app, ["init", "--agent", "bogus"])
    assert res.exit_code == 2
    assert "Unknown agent" in res.stdout


def test_init_twice_is_idempotent_and_backs_up(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    runner.invoke(app, ["init"])
    res = runner.invoke(app, ["init"])
    assert res.exit_code == 0
    assert "Backed up existing settings" in res.stdout
    assert (repo / ".claude" / "settings.json.bak").exists()


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def test_status_not_armed(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0
    assert "not armed" in res.stdout


def test_status_armed_reports_counts(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    store.record_session("deadbeef", started_at="2026-08-28T00:00:00+00:00")
    store.append_event("Bash", ["net-egress"], True, "block",
                       {"tool_input": {"command": "curl x"}},
                       timestamp="2026-08-28T00:01:00+00:00")
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0
    assert "armed" in res.stdout


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def test_report_table(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    store.record_session("anchor", started_at="2026-08-28T00:00:00+00:00")
    store.append_event("Bash", ["net-egress"], True, "blocked curl",
                       {"tool_input": {"command": "curl evil"}},
                       timestamp="2026-08-28T00:02:00+00:00")
    res = runner.invoke(app, ["report"])
    assert res.exit_code == 0
    assert "blocked" in res.stdout.lower()


def test_report_json(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    store.record_session("anchor", started_at="2026-08-28T00:00:00+00:00")
    store.append_event("Bash", [], False, "ok",
                       {"tool_input": {"command": "ls"}},
                       timestamp="2026-08-28T00:01:00+00:00")
    res = runner.invoke(app, ["report", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.stdout)
    assert payload["total_events"] == 1
    assert payload["blocked"] == 0


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #


def test_export_to_stdout_default_md(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    store.record_session("anchor", started_at="2026-08-28T00:00:00+00:00")
    res = runner.invoke(app, ["export"])
    assert res.exit_code == 0
    assert res.stdout.strip()  # some markdown emitted


def test_export_to_json_file_infers_format(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    store.record_session("anchor", started_at="2026-08-28T00:00:00+00:00")
    out = tmp_path / "report.json"
    res = runner.invoke(app, ["export", str(out)])
    assert res.exit_code == 0
    assert out.exists()
    json.loads(out.read_text(encoding="utf-8"))  # valid JSON
    assert "Wrote json report" in res.stdout


def test_export_unknown_format_exits_2(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    res = runner.invoke(app, ["export", "--format", "xml"])
    assert res.exit_code == 2
    assert "Unknown format" in res.stdout


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #


def test_diff_conflicting_flags_exit_2(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    res = runner.invoke(app, ["diff", "--stat", "--name-only"])
    assert res.exit_code == 2
    assert "only one of" in res.stdout.lower()


def test_diff_no_session_message(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    res = runner.invoke(app, ["diff"])
    assert res.exit_code == 0
    assert "No session anchor" in res.stdout


@needs_git
def test_diff_shows_changes(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    (repo / "keep.txt").write_text("original\n", encoding="utf-8")
    _armed_session(repo)
    (repo / "keep.txt").write_text("changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("brand new\n", encoding="utf-8")

    res = runner.invoke(app, ["diff"])
    assert res.exit_code == 0
    assert "new.txt" in res.stdout
    assert "brand new" in res.stdout


@needs_git
def test_diff_name_only(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _armed_session(repo)
    (repo / "a.txt").write_text("a2\n", encoding="utf-8")

    res = runner.invoke(app, ["diff", "--name-only"])
    assert res.exit_code == 0
    assert "a.txt" in res.stdout


# --------------------------------------------------------------------------- #
# session-start / watch
# --------------------------------------------------------------------------- #


@needs_git
def test_session_start_anchors_session(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    res = runner.invoke(app, ["session-start"])
    assert res.exit_code == 0
    assert "anchored" in res.stdout
    assert store.latest_session() is not None
    assert snapshot.get_shadow(repo).exists()


@needs_git
def test_watch_initializes_then_reports_existing(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    res1 = runner.invoke(app, ["watch"])
    assert res1.exit_code == 0
    assert "Initialized shadow repo" in res1.stdout
    res2 = runner.invoke(app, ["watch"])
    assert res2.exit_code == 0
    assert "already present" in res2.stdout


# --------------------------------------------------------------------------- #
# undo
# --------------------------------------------------------------------------- #


def test_undo_without_shadow_exits_1(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    res = runner.invoke(app, ["undo"])
    assert res.exit_code == 1
    assert "No shadow repo found" in res.stdout


@needs_git
def test_undo_session_without_anchor_exits_1(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    snapshot.ensure_shadow(repo)  # shadow exists but no recorded session
    res = runner.invoke(app, ["undo", "--session"])
    assert res.exit_code == 1
    assert "No session anchor recorded" in res.stdout


@needs_git
def test_undo_reverts_latest_snapshot(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    (repo / "f.txt").write_text("v1\n", encoding="utf-8")
    shadow, _ = _armed_session(repo)
    (repo / "f.txt").write_text("v2\n", encoding="utf-8")
    shadow.snapshot("e1")

    res = runner.invoke(app, ["undo"])
    assert res.exit_code == 0
    assert "Reverted" in res.stdout
    assert (repo / "f.txt").read_text(encoding="utf-8") == "v1\n"


@needs_git
def test_undo_session_resets_to_baseline(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    (repo / "f.txt").write_text("v1\n", encoding="utf-8")
    shadow, _ = _armed_session(repo)
    (repo / "f.txt").write_text("v2\n", encoding="utf-8")
    shadow.snapshot("e1")

    res = runner.invoke(app, ["undo", "--session"])
    assert res.exit_code == 0
    assert (repo / "f.txt").read_text(encoding="utf-8") == "v1\n"


# --------------------------------------------------------------------------- #
# hook
# --------------------------------------------------------------------------- #


def test_hook_safe_command_approves(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "hook_event_name": "PreToolUse",
    }
    res = runner.invoke(app, ["hook"], input=json.dumps(payload))
    assert res.exit_code == 0
    assert json.loads(res.stdout.strip()) == {"decision": "approve"}
    assert store.count_events() == 1


def test_hook_dangerous_command_blocks(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /"},
        "hook_event_name": "PreToolUse",
    }
    res = runner.invoke(app, ["hook"], input=json.dumps(payload))
    assert res.exit_code == 0
    decision = json.loads(res.stdout.strip())
    assert decision["decision"] == "block"
    assert decision["reason"]


# --------------------------------------------------------------------------- #
# monitor / open (dispatch only — the servers/TUI are stubbed out)
# --------------------------------------------------------------------------- #


def test_monitor_dispatches_to_tui_run(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    called = {}
    from restory import tui
    monkeypatch.setattr(tui, "run", lambda: called.setdefault("ran", True))
    res = runner.invoke(app, ["monitor"])
    assert res.exit_code == 0
    assert called.get("ran") is True


def test_open_no_browser_runs_uvicorn(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    captured = {}
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app_, **kw: captured.update(kw))
    res = runner.invoke(app, ["open", "--no-browser", "--port", "9999"])
    assert res.exit_code == 0
    assert captured["port"] == 9999
    assert captured["host"] == "127.0.0.1"


def test_open_default_schedules_browser(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    import threading
    import uvicorn

    class _FakeTimer:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass

    monkeypatch.setattr(threading, "Timer", _FakeTimer)
    monkeypatch.setattr(uvicorn, "run", lambda app_, **kw: None)
    res = runner.invoke(app, ["open"])
    assert res.exit_code == 0
    assert "restory UI at" in res.stdout
