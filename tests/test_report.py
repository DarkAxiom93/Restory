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
        "severity_counts": {},
        "blocked_commands": [],
        "warned_commands": [],
    }


def test_blocked_commands_carry_severity():
    events = [
        _event(3, ["net-egress"], True, "net-egress: exfil", "curl x"),
        _event(2, ["write-outside-repo"], True, "write-outside-repo: ...", "echo > /x"),
    ]
    data = report.build_report(events, session=None)
    by_id = {c["id"]: c for c in data["blocked_commands"]}
    assert by_id[3]["severity"] == "CRITICAL"
    assert by_id[2]["severity"] == "BLOCK"


def test_blocked_commands_sorted_most_severe_first():
    events = [
        _event(1, ["write-outside-repo"], True, "block", "echo > /x"),  # BLOCK
        _event(2, ["mass-delete"], True, "crit", "rm -rf ~"),           # CRITICAL
    ]
    data = report.build_report(events, session=None)
    severities = [c["severity"] for c in data["blocked_commands"]]
    assert severities == ["CRITICAL", "BLOCK"]


def test_severity_counts_group_flagged_events():
    events = [
        _event(4, ["mass-delete"], True, "crit", "rm -rf ~"),
        _event(3, ["net-egress"], True, "crit", "curl x"),
        _event(2, ["read-secret"], True, "block", "cat .env"),
        _event(1, [], False, "clean", "ls"),
    ]
    data = report.build_report(events, session=None)
    assert data["severity_counts"] == {"CRITICAL": 2, "BLOCK": 1}


def test_warned_events_are_recorded_but_not_blocked():
    # A WARN-severity event is approved (danger False) but still tagged; the
    # report surfaces it separately from the blocked commands.
    events = [
        _event(2, ["write-outside-repo"], False, "warn: allowed", "echo > note"),
        _event(1, ["mass-delete"], True, "crit", "rm -rf ~"),
    ]
    data = report.build_report(events, session=None)
    assert data["blocked"] == 1
    assert [c["id"] for c in data["warned_commands"]] == [2]
    assert data["warned_commands"][0]["command"] == "echo > note"
    # An approved-but-recorded event's effective severity is WARN, reflecting
    # the decision that was actually made regardless of the tag's default.
    assert data["warned_commands"][0]["severity"] == "WARN"
    assert data["severity_counts"] == {"CRITICAL": 1, "WARN": 1}


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


def test_render_shows_severity_column_and_value():
    data = report.build_report(
        [_event(1, ["mass-delete"], True, "mass-delete: rm -rf ~", "rm -rf ~")],
        session=None,
    )
    out = _render_ansi(data)
    assert "Severity" in out
    assert "CRITICAL" in out


def test_render_shows_warned_events_section():
    events = [
        _event(2, ["write-outside-repo"], False, "warn allowed", "echo > note"),
        _event(1, ["mass-delete"], True, "crit", "rm -rf ~"),
    ]
    data = report.build_report(events, session=None)
    out = _render_ansi(data, width=120)
    # The approved-but-recorded command surfaces with a WARN label.
    assert "WARN" in out
    assert "echo > note" in out


def test_fetch_events_since_scopes_by_timestamp(tmp_path):
    db = tmp_path / "restory.db"
    store.append_event("Bash", [], False, "old", {"tool_input": {"command": "old"}},
                       timestamp="2026-08-01T00:00:00+00:00", db_path=db)
    store.append_event("Bash", ["net-egress"], True, "new", {"tool_input": {"command": "new"}},
                       timestamp="2026-08-28T12:00:00+00:00", db_path=db)

    scoped = store.fetch_events_since("2026-08-28T00:00:00+00:00", db_path=db)
    assert [e["raw"]["tool_input"]["command"] for e in scoped] == ["new"]
