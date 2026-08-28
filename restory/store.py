"""SQLite-backed session store for restory events.

The database lives in the restory data directory (``%USERPROFILE%/.restory`` on
Windows) as ``restory.db``. There is a single ``events`` table.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import get_data_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT    NOT NULL,
    tool_name  TEXT    NOT NULL,
    tags       TEXT    NOT NULL,
    danger     INTEGER NOT NULL,
    reason     TEXT    NOT NULL,
    raw        TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT    NOT NULL,
    anchor_commit TEXT    NOT NULL
);
"""


def get_db_path() -> Path:
    """Return the path to the restory SQLite database."""
    return get_data_dir() / "restory.db"


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a connection with the schema ensured."""
    path = db_path or get_db_path()
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
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
    db_path: Path | None = None,
) -> int:
    """Append one event row. Returns the new row id."""
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO events (timestamp, tool_name, tags, danger, reason, raw) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                ts,
                tool_name,
                json.dumps(tags),
                1 if danger else 0,
                reason,
                json.dumps(raw),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def fetch_events(limit: int = 500, db_path: Path | None = None) -> list[dict]:
    """Return events newest-first, with ``tags`` and ``raw`` parsed from JSON."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, timestamp, tool_name, tags, danger, reason, raw "
            "FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
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
    started_at: str, limit: int = 5000, db_path: Path | None = None
) -> list[dict]:
    """Return events with ``timestamp >= started_at``, newest-first.

    Timestamps are ISO-8601 UTC strings, so lexicographic comparison in SQL
    matches chronological order. Used to scope a report to a single session.
    """
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, timestamp, tool_name, tags, danger, reason, raw "
            "FROM events WHERE timestamp >= ? ORDER BY id DESC LIMIT ?",
            (started_at, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_event(row) for row in rows]


def count_events(db_path: Path | None = None) -> int:
    """Return the number of stored events (convenience for tests/reports)."""
    conn = connect(db_path)
    try:
        (n,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(n)
    finally:
        conn.close()


def record_session(
    anchor_commit: str,
    *,
    started_at: str | None = None,
    db_path: Path | None = None,
) -> int:
    """Record a new session anchored at ``anchor_commit``. Returns the row id."""
    ts = started_at or datetime.now(timezone.utc).isoformat()
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO sessions (started_at, anchor_commit) VALUES (?, ?)",
            (ts, anchor_commit),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def latest_session(db_path: Path | None = None) -> dict | None:
    """Return the most recently recorded session, or None if there are none."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, started_at, anchor_commit FROM sessions "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    sid, started_at, anchor_commit = row
    return {"id": sid, "started_at": started_at, "anchor_commit": anchor_commit}
