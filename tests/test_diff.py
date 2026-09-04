"""Tests for restory.diff (read-only work-tree diff since session start)."""

from __future__ import annotations

import shutil

import pytest

from restory import diff, snapshot, store

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


def _isolate(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _armed_session(repo):
    """Init a shadow, take a baseline, and record it as the current session."""
    shadow, _ = snapshot.ensure_shadow(repo)
    anchor = shadow.session_baseline()
    store.record_session(anchor, repo_root=repo)
    return anchor


def test_diff_no_session(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    data = diff.gather(repo_root=repo)
    assert data["available"] is False
    assert data["reason"] == "no-session"


def test_diff_no_changes(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    (repo / "keep.txt").write_text("original\n", encoding="utf-8")
    _armed_session(repo)

    data = diff.gather(repo_root=repo)
    assert data["available"] is False
    assert data["reason"] == "no-changes"


def test_diff_reports_add_modify_delete(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    (repo / "keep.txt").write_text("original\n", encoding="utf-8")
    (repo / "gone.txt").write_text("delete me\n", encoding="utf-8")
    _armed_session(repo)

    # Agent changes since the anchor.
    (repo / "keep.txt").write_text("changed\n", encoding="utf-8")
    (repo / "gone.txt").unlink()
    (repo / "new.txt").write_text("brand new\n", encoding="utf-8")

    data = diff.gather(mode="full", repo_root=repo)
    assert data["available"] is True

    counts = data["summary"]["counts"]
    assert counts["added"] == 1
    assert counts["modified"] == 1
    assert counts["deleted"] == 1

    paths = {f["path"] for f in data["summary"]["files"]}
    assert paths == {"keep.txt", "gone.txt", "new.txt"}

    # Full mode includes the patch text with the new content.
    assert "brand new" in data["diff"]
    assert "new.txt" in data["diff"]


def test_diff_name_only_mode_omits_patch(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _armed_session(repo)
    (repo / "a.txt").write_text("a2\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")

    data = diff.gather(mode="name-only", repo_root=repo)
    assert data["available"] is True
    assert {f["path"] for f in data["summary"]["files"]} == {"a.txt", "b.txt"}
    # name-only never fetches the patch body.
    assert "diff" not in data


def test_diff_is_read_only(monkeypatch, tmp_path):
    """Running a diff must not commit anything or alter the work tree."""
    repo = _isolate(monkeypatch, tmp_path)
    (repo / "keep.txt").write_text("original\n", encoding="utf-8")
    _armed_session(repo)
    (repo / "keep.txt").write_text("changed\n", encoding="utf-8")

    shadow = snapshot.get_shadow(repo)
    head_before = shadow._git(["rev-parse", "HEAD"]).stdout.strip()

    diff.gather(mode="full", repo_root=repo)

    head_after = shadow._git(["rev-parse", "HEAD"]).stdout.strip()
    assert head_before == head_after
    # Work tree still holds the agent's edit (diff didn't revert anything).
    assert (repo / "keep.txt").read_text(encoding="utf-8") == "changed\n"
