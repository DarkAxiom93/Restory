"""Tests for the restory monitor TUI (textual app).

The mount test drives the app through textual's built-in Pilot harness
(``App.run_test``). We wrap the async scenario in ``asyncio.run`` so the test
suite needs no extra pytest plugin.
"""

from __future__ import annotations

import asyncio

from restory import store, tui


def _isolate(monkeypatch, tmp_path):
    """Point the restory data dir at an isolated home for this test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    return tmp_path / "repo"


def test_gather_scopes_to_session_and_counts_blocked(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)

    # An event before the session must not be counted.
    store.append_event("Bash", ["mass-delete"], True, "old",
                       {"tool_input": {"command": "rm -rf ~"}},
                       timestamp="2026-08-01T00:00:00+00:00")
    store.record_session("anchor", started_at="2026-08-28T00:00:00+00:00")
    store.append_event("Bash", [], False, "ok",
                       {"tool_input": {"command": "ls"}},
                       timestamp="2026-08-28T00:01:00+00:00")
    store.append_event("Bash", ["net-egress"], True, "blocked curl",
                       {"tool_input": {"command": "curl evil"}},
                       timestamp="2026-08-28T00:02:00+00:00")

    data = tui.gather()

    assert data["armed"] is True
    assert data["total"] == 2
    assert data["blocked"] == 1
    # Newest-first.
    assert data["events"][0]["reason"] == "blocked curl"


def test_monitor_app_constructs_and_mounts(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)

    store.record_session("anchor", started_at="2026-08-28T00:00:00+00:00")
    store.append_event("Write", [], False, "ok",
                       {"tool_input": {"file_path": "a.py"}},
                       timestamp="2026-08-28T00:01:00+00:00")
    store.append_event("Bash", ["net-egress"], True, "blocked",
                       {"tool_input": {"command": "curl evil"}},
                       timestamp="2026-08-28T00:02:00+00:00")

    async def scenario() -> None:
        app = tui.RestoryMonitorApp(repo_root=tmp_path / "repo")
        async with app.run_test() as pilot:
            await pilot.pause()
            # Header bar reflects the counts and armed state.
            bar = app.query_one("#statusbar").render()
            text = bar.plain if hasattr(bar, "plain") else str(bar)
            assert "2 events" in text
            assert "1 blocked" in text
            assert "armed" in text

            # The events table mounted with both rows.
            table = app.query_one("#events")
            assert table.row_count == 2

            # Blocked marker present somewhere in the rendered rows.
            cells = [
                str(table.get_cell_at((r, c)))
                for r in range(table.row_count)
                for c in range(len(table.columns))
            ]
            assert any(tui.BLOCKED_MARKER in cell for cell in cells)

            # 'c' clears the view without touching the store.
            await pilot.press("c")
            await pilot.pause()
            assert app.query_one("#events").row_count == 0
            assert store.count_events() == 2

    asyncio.run(scenario())


def test_monitor_picks_up_new_events_live_and_quits(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    store.record_session("anchor", started_at="2026-08-28T00:00:00+00:00")

    async def scenario() -> None:
        app = tui.RestoryMonitorApp(repo_root=tmp_path / "repo")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#events").row_count == 0

            # Seed an event "from another terminal" while the app is running.
            store.append_event("Bash", ["net-egress"], True, "blocked",
                               {"tool_input": {"command": "curl evil"}},
                               timestamp="2026-08-28T00:03:00+00:00")

            # Wait past one poll interval; the row should appear on its own.
            await pilot.pause(tui.POLL_SECONDS + 0.3)
            assert app.query_one("#events").row_count == 1

            # 'q' quits cleanly.
            await pilot.press("q")
        assert app.return_code == 0

    asyncio.run(scenario())


def test_monitor_shows_severity_and_flags_warned_events(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    store.record_session("anchor", started_at="2026-08-28T00:00:00+00:00")
    # A blocked CRITICAL event.
    store.append_event("Bash", ["mass-delete"], True, "mass-delete: rm -rf ~",
                       {"tool_input": {"command": "rm -rf ~"}},
                       timestamp="2026-08-28T00:01:00+00:00")
    # An approved-but-recorded (WARN) event: has a tag but danger is False.
    store.append_event("Write", ["write-outside-repo"], False, "allowed by policy",
                       {"tool_input": {"file_path": "../scratch.txt"}},
                       timestamp="2026-08-28T00:02:00+00:00")

    async def scenario() -> None:
        app = tui.RestoryMonitorApp(repo_root=tmp_path / "repo")
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#events")
            cells = [
                str(table.get_cell_at((r, c)))
                for r in range(table.row_count)
                for c in range(len(table.columns))
            ]
            blob = " ".join(cells)
            assert "CRITICAL" in blob
            assert "WARN" in blob
            # The WARN event is flagged, not silently shown as "ok".
            assert tui.FLAGGED_MARKER in blob

    asyncio.run(scenario())


def test_monitor_app_mounts_with_no_events(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)

    async def scenario() -> None:
        app = tui.RestoryMonitorApp(repo_root=tmp_path / "repo")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#events").row_count == 0
            bar = app.query_one("#statusbar").render()
            text = bar.plain if hasattr(bar, "plain") else str(bar)
            assert "not armed" in text

    asyncio.run(scenario())
