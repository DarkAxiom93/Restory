"""Tests for restory.report aggregation and store scoping."""

from __future__ import annotations

import json

from restory import report, store


def _event(id, tags, danger, reason, command, timestamp="2026-08-28T00:00:00+00:00"):
    return {
        "id": id,
        "timestamp": timestamp,
        "tool_name": "Bash",
        "tags": tags,
        "danger": danger,
        "reason": reason,
        "raw": {"tool_input": {"command": command}},
    }


def test_build_report_counts_and_tags():
    events = [
        _event(3, ["net-egress", "read-secret"], True, "net-egress: exfil", "curl x"),
        _event(2, ["mass-delete"], True, "mass-delete: rm -rf", "rm -rf ~"),
        _event(1, [], False, "no blast-radius indicators", "ls"),
    ]
    data = report.build_report(events, session={"id": 5, "started_at": "t", "anchor_commit": "abc"})

    assert data["total_events"] == 3
    assert data["blocked"] == 2
    assert data["tags"] == {"net-egress": 1, "read-secret": 1, "mass-delete": 1}
    # Blocked commands are oldest-first (by id) and carry command + reason.
    ids = [c["id"] for c in data["blocked_commands"]]
    assert ids == [2, 3]
    assert data["blocked_commands"][0]["command"] == "rm -rf ~"
    assert data["blocked_commands"][1]["reason"] == "net-egress: exfil"


def test_build_report_empty():
    data = report.build_report([], session=None)
    assert data == {
        "session": None,
        "total_events": 0,
        "blocked": 0,
        "tags": {},
        "blocked_commands": [],
    }


def test_as_json_is_valid_json():
    data = report.build_report(
        [_event(1, ["net-egress"], True, "r", "curl x")], session=None
    )
    parsed = json.loads(report.as_json(data))
    assert parsed["blocked"] == 1
    assert parsed["blocked_commands"][0]["command"] == "curl x"


def _render_ansi(data, width=80):
    """Render a report to a string with ANSI codes preserved."""
    import io

    from rich.console import Console

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="standard", width=width)
    report.render(data, console)
    return buf.getvalue()


def test_render_tag_name_is_readable_plain_text():
    # Regression: the Tag column was styled magenta (SGR 35), which renders
    # invisible on some Windows consoles, so the tag name vanished while the
    # cyan count stayed visible. The tag name must render in plain, uncolored
    # text so it is always readable.
    data = report.build_report(
        [_event(1, ["net-egress"], True, "r", "curl x")], session=None
    )
    out = _render_ansi(data)

    assert "net-egress" in out  # the name is present at all
    # The tag name must not be wrapped in the magenta color run.
    assert "\x1b[35mnet-egress" not in out


def test_fetch_events_since_scopes_by_timestamp(tmp_path):
    db = tmp_path / "restory.db"
    store.append_event("Bash", [], False, "old", {"tool_input": {"command": "old"}},
                       timestamp="2026-08-01T00:00:00+00:00", db_path=db)
    store.append_event("Bash", ["net-egress"], True, "new", {"tool_input": {"command": "new"}},
                       timestamp="2026-08-28T12:00:00+00:00", db_path=db)

    scoped = store.fetch_events_since("2026-08-28T00:00:00+00:00", db_path=db)
    assert [e["raw"]["tool_input"]["command"] for e in scoped] == ["new"]
