"""SQLite-backed session store for restory events.

The database lives in the restory data directory (``%USERPROFILE%/.restory`` on
Windows) as ``restory.db``. There is an ``events`` table and a ``sessions``
table.

Every session and event row is scoped to the **repository root** it belongs to
(absolute, resolved, and case-folded on Windows). All reads filter by that key
so that, with more than one repo sharing the single database, a session or
anchor lookup can never return a row belonging to a *different* repository — the
worst case being a whole-session undo that resets the wrong work tree.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import find_repo_root, get_data_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT    NOT NULL,
    tool_name  TEXT    NOT NULL,
    tags       TEXT    NOT NULL,
    danger     INTEGER NOT NULL,
    reason     TEXT    NOT NULL,
    raw        TEXT    NOT NULL,
    repo_root  TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT    NOT NULL,
    anchor_commit TEXT    NOT NULL,
    repo_root     TEXT
);
"""


def repo_key(repo_root: Path | str | None = None) -> str:
    """Return the canonical scoping key for a repository root.

    Absolute, resolved, and case-folded on Windows (``os.path.normcase``), so
    two spellings of the same path — differing only in case or separators on
    Windows — map to the same key. ``None`` means "the current repo" (resolved
    from the working directory).
    """
    root = Path(repo_root) if repo_root is not None else find_repo_root()
    return os.path.normcase(str(root.resolve()))


def get_db_path() -> Path:
    """Return the path to the restory SQLite database."""
    return get_data_dir() / "restory.db"


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older database up to the current schema.

    Adds the ``repo_root`` column to ``events`` and ``sessions`` when a database
    created before per-repo scoping is opened. Existing rows keep ``repo_root``
    NULL — their originating repo is unknowable, and scoped reads simply exclude
    them, which is the safe choice (guessing a repo could revert the wrong tree).
    """
    for table in ("events", "sessions"):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not cols:
            # Table does not exist yet; the schema script will create it.
            continue
        if "repo_root" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN repo_root TEXT")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a connection with the schema ensured and migrations applied."""
    path = db_path or get_db_path()
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def append_event(
    tool_name: str,
    tags: list[str],
    danger: bool,
    reason: str,
    raw: Any,
    *,
    timestamp: str | None = None,
    repo_root: Path | str | None = None,
    db_path: Path | None = None,
) -> int:
    """Append one event row scoped to ``repo_root``. Returns the new row id."""
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    key = repo_key(repo_root)
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO events (timestamp, tool_name, tags, danger, reason, raw, repo_root) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                tool_name,
                json.dumps(tags),
                1 if danger else 0,
                reason,
                json.dumps(raw),
                key,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def fetch_events(
    limit: int = 500,
    repo_root: Path | str | None = None,
    db_path: Path | None = None,
) -> list[dict]:
    """Return this repo's events newest-first, ``tags``/``raw`` parsed from JSON."""
    key = repo_key(repo_root)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, timestamp, tool_name, tags, danger, reason, raw "
            "FROM events WHERE repo_root = ? ORDER BY id DESC LIMIT ?",
            (key, limit),
        ).fetchall()
    finally:
        conn.close()

    return [_row_to_event(row) for row in rows]


def _row_to_event(row) -> dict:
    rid, ts, tool_name, tags_json, danger, reason, raw_json = row
    try:
        tags = json.loads(tags_json)
    except (json.JSONDecodeError, TypeError):
        tags = []
    try:
        raw = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        raw = {}
    return {
        "id": rid,
        "timestamp": ts,
        "tool_name": tool_name,
        "tags": tags,
        "danger": bool(danger),
        "reason": reason,
        "raw": raw,
    }


def fetch_events_since(
    started_at: str,
    limit: int = 5000,
    repo_root: Path | str | None = None,
    db_path: Path | None = None,
) -> list[dict]:
    """Return this repo's events with ``timestamp >= started_at``, newest-first.

    Timestamps are ISO-8601 UTC strings, so lexicographic comparison in SQL
    matches chronological order. Used to scope a report to a single session.
    """
    key = repo_key(repo_root)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, timestamp, tool_name, tags, danger, reason, raw "
            "FROM events WHERE repo_root = ? AND timestamp >= ? ORDER BY id DESC LIMIT ?",
            (key, started_at, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_event(row) for row in rows]


def count_events(
    repo_root: Path | str | None = None, db_path: Path | None = None
) -> int:
    """Return the number of stored events for this repo (tests/reports helper)."""
    key = repo_key(repo_root)
    conn = connect(db_path)
    try:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM events WHERE repo_root = ?", (key,)
        ).fetchone()
        return int(n)
    finally:
        conn.close()


def record_session(
    anchor_commit: str,
    *,
    started_at: str | None = None,
    repo_root: Path | str | None = None,
    db_path: Path | None = None,
) -> int:
    """Record a new session for ``repo_root`` anchored at ``anchor_commit``.

    Returns the new row id.
    """
    ts = started_at or datetime.now(timezone.utc).isoformat()
    key = repo_key(repo_root)
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO sessions (started_at, anchor_commit, repo_root) VALUES (?, ?, ?)",
            (ts, anchor_commit, key),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def latest_session(
    repo_root: Path | str | None = None, db_path: Path | None = None
) -> dict | None:
    """Return this repo's most recently recorded session, or None if there are none.

    Only sessions whose stored ``repo_root`` matches the current repo are
    considered, so a session anchored in another repository can never be
    returned here (which would risk a cross-repo undo).
    """
    key = repo_key(repo_root)
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, started_at, anchor_commit, repo_root FROM sessions "
            "WHERE repo_root = ? ORDER BY id DESC LIMIT 1",
            (key,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    sid, started_at, anchor_commit, stored_root = row
    return {
        "id": sid,
        "started_at": started_at,
        "anchor_commit": anchor_commit,
        "repo_root": stored_root,
    }
