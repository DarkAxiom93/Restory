"""Tests for restory.status health-check snapshot."""

from __future__ import annotations

from restory import status, store


def _isolate(monkeypatch, tmp_path):
    """Point the restory data dir at an isolated home for this test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    return tmp_path / "repo"


def test_status_not_armed_before_session(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    repo.mkdir()

    data = status.build_status(repo_root=repo)

    assert data["armed"] is False
    assert data["session"] is None
    assert data["total_events"] == 0
    assert data["blocked"] == 0
    assert data["shadow_exists"] is False
    assert data["shadow_path"]  # a path is always reported


def test_status_armed_after_session_with_events(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    repo.mkdir()

    sid = store.record_session("deadbeef", started_at="2026-08-28T00:00:00+00:00")
    store.append_event("Bash", [], False, "ok", {"tool_input": {"command": "ls"}},
                       timestamp="2026-08-28T00:01:00+00:00")
    store.append_event("Bash", ["net-egress"], True, "block",
                       {"tool_input": {"command": "curl x"}},
                       timestamp="2026-08-28T00:02:00+00:00")

    data = status.build_status(repo_root=repo)

    assert data["armed"] is True
    assert data["session"]["id"] == sid
    assert data["session"]["started_at"] == "2026-08-28T00:00:00+00:00"
    assert data["total_events"] == 2
    assert data["blocked"] == 1


def test_status_events_scoped_to_current_session(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    repo.mkdir()

    # An event from before the (later) session must not be counted.
    store.append_event("Bash", ["mass-delete"], True, "old",
                       {"tool_input": {"command": "rm -rf ~"}},
                       timestamp="2026-08-01T00:00:00+00:00")
    store.record_session("anchor", started_at="2026-08-28T00:00:00+00:00")
    store.append_event("Bash", [], False, "new", {"tool_input": {"command": "ls"}},
                       timestamp="2026-08-28T00:05:00+00:00")

    data = status.build_status(repo_root=repo)

    assert data["armed"] is True
    assert data["total_events"] == 1
    assert data["blocked"] == 0
