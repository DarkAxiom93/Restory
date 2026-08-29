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
    store.record_session(anchor)
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


def test_diff_no_shadow(monkeypatch, tmp_path):
    """A recorded session but no shadow repo reports the no-shadow reason."""
    repo = _isolate(monkeypatch, tmp_path)
    store.record_session("anchor", started_at="2026-08-28T00:00:00+00:00")
    data = diff.gather(repo_root=repo)
    assert data["available"] is False
    assert data["reason"] == "no-shadow"


# --------------------------------------------------------------------------- #
# _parse_name_status / render() — no git required
# --------------------------------------------------------------------------- #


def test_parse_name_status_skips_blank_lines():
    entries = diff._parse_name_status("A\tnew.txt\n\nM\tkeep.txt\nR100\told\tnew\n")
    assert entries == [
        {"status": "A", "path": "new.txt"},
        {"status": "M", "path": "keep.txt"},
        {"status": "R100", "path": "new"},  # rename -> new path is last field
    ]


def _render(data, mode):
    from rich.console import Console

    console = Console(record=True, width=120)
    lines: list[str] = []
    diff.render(data, mode=mode, console=console, echo=lines.append)
    return console.export_text() + "\n".join(lines)


def test_render_unavailable_messages():
    for reason, needle in (
        ("no-session", "session-start"),
        ("no-shadow", "No shadow repo"),
        ("no-changes", "No changes"),
    ):
        out = _render({"available": False, "reason": reason}, mode="full")
        assert needle in out


def test_render_full_prints_summary_and_patch():
    data = {
        "available": True,
        "mode": "full",
        "session": {"id": 3, "started_at": "t", "anchor_commit": "abc123def456"},
        "anchor": "abc123def456",
        "summary": {
            "counts": {"added": 1, "modified": 1, "deleted": 1,
                       "renamed": 1, "copied": 1},
            "files": [
                {"path": "new.txt", "label": "added", "style": "green"},
                {"path": "old.txt", "label": "deleted", "style": "red"},
            ],
        },
        "diff": "--- patch body ---\n",
    }
    out = _render(data, mode="full")
    assert "Changes since session 3" in out
    assert "1 added" in out and "1 renamed" in out and "1 copied" in out
    assert "new.txt" in out
    assert "patch body" in out


def test_render_stat_omits_patch():
    data = {
        "available": True,
        "mode": "stat",
        "session": {"id": 1, "started_at": "t", "anchor_commit": "abc"},
        "anchor": "abc",
        "summary": {
            "counts": {"added": 0, "modified": 1, "deleted": 0,
                       "renamed": 0, "copied": 0},
            "files": [{"path": "a.txt", "label": "modified", "style": "yellow"}],
        },
        "diff": "SHOULD NOT APPEAR",
    }
    out = _render(data, mode="stat")
    assert "a.txt" in out
    assert "SHOULD NOT APPEAR" not in out


def test_render_name_only_lists_paths_plainly():
    data = {
        "available": True,
        "mode": "name-only",
        "session": {"id": 1, "started_at": "t", "anchor_commit": "abc"},
        "anchor": "abc",
        "summary": {
            "counts": {"added": 1, "modified": 0, "deleted": 0,
                       "renamed": 0, "copied": 0},
            "files": [
                {"path": "a.txt", "label": "added", "style": "green"},
                {"path": "b.txt", "label": "added", "style": "green"},
            ],
        },
    }
    out = _render(data, mode="name-only")
    assert "a.txt" in out and "b.txt" in out
    # name-only prints only paths, never the "Changes since" summary header.
    assert "Changes since" not in out
