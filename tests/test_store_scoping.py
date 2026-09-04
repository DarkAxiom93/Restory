"""Per-repository scoping and safe migration for the store (Issue 2).

Sessions and events share one SQLite database across every repo the user works
in, so each row is tagged with its repository root. Reads filter by the current
repo's key, and a legacy database (created before the column existed) must open
and migrate rather than crash.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3

import pytest

from restory import snapshot, store
from restory.cli import app as cli_app


def test_sessions_and_events_are_scoped_by_repo(tmp_path):
    db = tmp_path / "shared.db"
    repo_a = tmp_path / "repoA"
    repo_b = tmp_path / "repoB"

    store.record_session("anchorA", repo_root=repo_a, db_path=db)
    store.append_event(
        "Bash", ["net-egress"], True, "blocked",
        {"tool_input": {"command": "curl evil"}},
        repo_root=repo_a, db_path=db,
    )

    # From repo B, repo A's session and events are invisible.
    assert store.latest_session(repo_root=repo_b, db_path=db) is None
    assert store.fetch_events(repo_root=repo_b, db_path=db) == []
    assert store.count_events(repo_root=repo_b, db_path=db) == 0

    # From repo A they are visible, tagged with A's normalized key.
    sess = store.latest_session(repo_root=repo_a, db_path=db)
    assert sess is not None
    assert sess["anchor_commit"] == "anchorA"
    assert sess["repo_root"] == store.repo_key(repo_a)
    assert store.count_events(repo_root=repo_a, db_path=db) == 1


def test_repo_key_is_absolute_and_case_folded_on_windows(tmp_path):
    p = tmp_path / "MixedCase"
    key = store.repo_key(p)
    assert os.path.isabs(key)
    if os.name == "nt":
        # Windows paths are case-insensitive: differently-cased spellings of the
        # same path must produce the same key, or scoping would be defeated.
        assert store.repo_key(str(p).upper()) == store.repo_key(str(p).lower())


def test_old_database_without_repo_root_migrates_without_crashing(tmp_path):
    db = tmp_path / "legacy.db"

    # Build a pre-scoping database by hand: no repo_root column anywhere.
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, tool_name TEXT NOT NULL, tags TEXT NOT NULL,
            danger INTEGER NOT NULL, reason TEXT NOT NULL, raw TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL, anchor_commit TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO events (timestamp, tool_name, tags, danger, reason, raw) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-01-01T00:00:00+00:00", "Bash", "[]", 0, "ok",
         json.dumps({"tool_input": {"command": "ls"}})),
    )
    conn.execute(
        "INSERT INTO sessions (started_at, anchor_commit) VALUES (?, ?)",
        ("2026-01-01T00:00:00+00:00", "deadbeef"),
    )
    conn.commit()
    conn.close()

    # Opening through the store must migrate the schema, not raise.
    migrated = store.connect(db)
    try:
        event_cols = {r[1] for r in migrated.execute("PRAGMA table_info(events)")}
        session_cols = {r[1] for r in migrated.execute("PRAGMA table_info(sessions)")}
    finally:
        migrated.close()
    assert "repo_root" in event_cols
    assert "repo_root" in session_cols

    # Legacy rows have NULL repo_root, so scoped reads simply exclude them
    # (never crash, never mis-attribute them to the current repo).
    assert store.count_events(repo_root=tmp_path / "whatever", db_path=db) == 0
    assert store.latest_session(repo_root=tmp_path / "whatever", db_path=db) is None

    # New, properly-scoped writes work against the migrated database.
    store.record_session("newanchor", repo_root=tmp_path / "repoX", db_path=db)
    store.append_event(
        "Bash", [], False, "ok", {"tool_input": {"command": "pwd"}},
        repo_root=tmp_path / "repoX", db_path=db,
    )
    sess = store.latest_session(repo_root=tmp_path / "repoX", db_path=db)
    assert sess is not None and sess["anchor_commit"] == "newanchor"
    assert store.count_events(repo_root=tmp_path / "repoX", db_path=db) == 1


# --------------------------------------------------------------------------- #
# End-to-end: undo --session must never cross repository boundaries.
# --------------------------------------------------------------------------- #


def _git_init(path):
    env = os.environ.copy()
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    subprocess_run = __import__("subprocess").run
    subprocess_run(["git", "init", "-q"], cwd=str(path), env=env)
    subprocess_run(["git", "config", "user.email", "t@localhost"], cwd=str(path), env=env)
    subprocess_run(["git", "config", "user.name", "t"], cwd=str(path), env=env)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_undo_session_refuses_to_use_another_repos_anchor(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))

    # Repo A: a real session with a snapshotted change.
    repo_a = tmp_path / "a"
    repo_a.mkdir()
    _git_init(repo_a)
    (repo_a / "x.txt").write_text("A1\n", encoding="utf-8")
    shadow_a, _ = snapshot.ensure_shadow(repo_a)
    anchor_a = shadow_a.session_baseline()
    store.record_session(anchor_a, repo_root=repo_a)
    (repo_a / "x.txt").write_text("A2\n", encoding="utf-8")
    shadow_a.snapshot("evt-a")

    # Repo B: a shadow exists, but B has NO session of its own.
    repo_b = tmp_path / "b"
    repo_b.mkdir()
    _git_init(repo_b)
    (repo_b / "y.txt").write_text("B1\n", encoding="utf-8")
    shadow_b, _ = snapshot.ensure_shadow(repo_b)
    (repo_b / "y.txt").write_text("B2\n", encoding="utf-8")
    shadow_b.snapshot("evt-b")

    from typer.testing import CliRunner

    monkeypatch.chdir(repo_b)
    result = CliRunner().invoke(cli_app, ["undo", "--session"])

    # It must hard-fail (never fall back to repo A's "most recent session").
    assert result.exit_code == 1
    assert "this repository" in result.stdout.lower()
    # Repo B's work tree is untouched: A's anchor was never applied to B.
    assert (repo_b / "y.txt").read_text(encoding="utf-8") == "B2\n"
