"""Tests for the restory FastAPI server (GET /api/events, POST /api/undo,
static UI serving, and /api-over-static precedence).

Each test isolates the data dir (USERPROFILE) and, where the shadow repo is
involved, the working directory, so nothing touches real data.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from starlette.testclient import TestClient

from restory import server, snapshot, store

needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


def _git_init(repo):
    """Make ``repo`` a real git repo so the server's ``find_repo_root()`` pins here.

    ``POST /api/undo`` resolves the shadow via ``find_repo_root()`` (nearest
    ``.git`` above the cwd). A real ``.git`` in the temp repo keeps that from
    escaping to a stray ancestor repo above the pytest temp dir.
    """
    if shutil.which("git") is None:
        return
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, capture_output=True)


def _isolate(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.chdir(repo)
    return repo


def _client(monkeypatch, ui_dir=None):
    """Build a fresh app (optionally pointing UI_DIR at ``ui_dir``)."""
    if ui_dir is not None:
        monkeypatch.setattr(server, "UI_DIR", ui_dir)
    return TestClient(server.create_app())


# --------------------------------------------------------------------------- #
# GET /api/events
# --------------------------------------------------------------------------- #


def test_get_events_empty(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    client = _client(monkeypatch, ui_dir=tmp_path / "no-ui")
    res = client.get("/api/events")
    assert res.status_code == 200
    assert res.json() == {"events": []}


def test_get_events_shapes_records(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    store.append_event("Bash", ["net-egress"], True, "blocked curl",
                       {"tool_input": {"command": "curl evil"},
                        "hook_event_name": "PreToolUse"},
                       timestamp="2026-08-28T00:02:00+00:00")
    store.append_event("Write", [], False, "ok",
                       {"tool_input": {"file_path": "a.py"},
                        "hook_event_name": "PostToolUse"},
                       timestamp="2026-08-28T00:03:00+00:00")

    client = _client(monkeypatch, ui_dir=tmp_path / "no-ui")
    res = client.get("/api/events")
    assert res.status_code == 200
    events = res.json()["events"]
    assert len(events) == 2
    # Newest-first.
    newest = events[0]
    assert newest["tool_name"] == "Write"
    assert newest["command"] == "a.py"
    assert newest["decision"] == "approve"
    assert newest["event"] == "PostToolUse"
    # The blocked one is shaped with decision "block".
    blocked = events[1]
    assert blocked["danger"] is True
    assert blocked["decision"] == "block"
    assert blocked["command"] == "curl evil"


def test_get_events_respects_limit(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    for i in range(5):
        store.append_event("Bash", [], False, "ok",
                           {"tool_input": {"command": f"echo {i}"}},
                           timestamp=f"2026-08-28T00:0{i}:00+00:00")
    client = _client(monkeypatch, ui_dir=tmp_path / "no-ui")
    res = client.get("/api/events", params={"limit": 2})
    assert res.status_code == 200
    assert len(res.json()["events"]) == 2


# --------------------------------------------------------------------------- #
# POST /api/undo
# --------------------------------------------------------------------------- #


def test_undo_without_shadow_returns_409(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    client = _client(monkeypatch, ui_dir=tmp_path / "no-ui")
    res = client.post("/api/undo")
    assert res.status_code == 409
    body = res.json()
    assert body["ok"] is False
    assert "No shadow repo" in body["message"]


@needs_git
def test_undo_with_nothing_to_revert_returns_409(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    snapshot.ensure_shadow(repo)  # only the initial snapshot exists
    client = _client(monkeypatch, ui_dir=tmp_path / "no-ui")
    res = client.post("/api/undo")
    assert res.status_code == 409
    body = res.json()
    assert body["ok"] is False
    assert "nothing to undo" in body["message"]


@needs_git
def test_undo_reverts_latest_snapshot(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    (repo / "f.txt").write_text("v1\n", encoding="utf-8")
    shadow, _ = snapshot.ensure_shadow(repo)
    shadow.session_baseline()
    (repo / "f.txt").write_text("v2\n", encoding="utf-8")
    shadow.snapshot("e1")

    client = _client(monkeypatch, ui_dir=tmp_path / "no-ui")
    res = client.post("/api/undo")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "Reverted 1 change(s)" in body["message"]
    assert body["reverted"][0]["path"] == "f.txt"
    assert (repo / "f.txt").read_text(encoding="utf-8") == "v1\n"


# --------------------------------------------------------------------------- #
# Static UI serving + /api precedence
# --------------------------------------------------------------------------- #


def test_static_fallback_when_ui_not_built(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    client = _client(monkeypatch, ui_dir=tmp_path / "missing-ui")
    res = client.get("/")
    assert res.status_code == 200
    assert "UI not built" in res.text


def test_static_ui_served_when_built(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    ui = tmp_path / "ui-out"
    ui.mkdir()
    (ui / "index.html").write_text("<h1>hello timeline</h1>", encoding="utf-8")
    client = _client(monkeypatch, ui_dir=ui)
    res = client.get("/")
    assert res.status_code == 200
    assert "hello timeline" in res.text


def test_api_takes_precedence_over_static_mount(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    ui = tmp_path / "ui-out"
    ui.mkdir()
    # A file named the same as the API route must NOT shadow /api/events.
    (ui / "index.html").write_text("<h1>static</h1>", encoding="utf-8")
    store.append_event("Bash", [], False, "ok",
                       {"tool_input": {"command": "ls"}},
                       timestamp="2026-08-28T00:01:00+00:00")
    client = _client(monkeypatch, ui_dir=ui)

    api = client.get("/api/events")
    assert api.status_code == 200
    assert api.headers["content-type"].startswith("application/json")
    assert len(api.json()["events"]) == 1

    root = client.get("/")
    assert "static" in root.text
