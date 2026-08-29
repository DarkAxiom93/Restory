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
            assert "armed" in text.lower()

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


def _seed_mixed_session(store):
    """One safe + two blocked events (net-egress, mass-delete), newest last."""
    store.record_session("anchor", started_at="2026-08-28T00:00:00+00:00")
    store.append_event("Write", [], False, "ok",
                       {"tool_input": {"file_path": "a.py"}},
                       timestamp="2026-08-28T00:01:00+00:00")
    store.append_event("Bash", ["net-egress"], True, "blocked curl",
                       {"tool_input": {"command": "curl evil"}},
                       timestamp="2026-08-28T00:02:00+00:00")
    store.append_event("Bash", ["mass-delete"], True, "deletes everything",
                       {"tool_input": {"command": "rm -rf build"}},
                       timestamp="2026-08-28T00:03:00+00:00")


def test_blocked_only_filter_reduces_rows(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _seed_mixed_session(store)

    async def scenario() -> None:
        app = tui.RestoryMonitorApp(repo_root=tmp_path / "repo")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#events").row_count == 3

            # 'b' hides the one safe event; only the two blocked remain.
            await pilot.press("b")
            await pilot.pause()
            assert app.query_one("#events").row_count == 2

            bar = app.query_one("#statusbar").render()
            text = bar.plain if hasattr(bar, "plain") else str(bar)
            assert "blocked only" in text

            # Toggling back restores every row; the store was never touched.
            await pilot.press("b")
            await pilot.pause()
            assert app.query_one("#events").row_count == 3
            assert store.count_events() == 3

    asyncio.run(scenario())


def test_tag_filter_cycles(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _seed_mixed_session(store)

    async def scenario() -> None:
        app = tui.RestoryMonitorApp(repo_root=tmp_path / "repo")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#events").row_count == 3

            # 't' selects a single tag; each present tag has exactly one event.
            await pilot.press("t")
            await pilot.pause()
            assert app.query_one("#events").row_count == 1
            bar = app.query_one("#statusbar").render()
            text = bar.plain if hasattr(bar, "plain") else str(bar)
            assert "tag:" in text

            # Cycling through all present tags returns to "all".
            await pilot.press("t")
            await pilot.pause()
            assert app.query_one("#events").row_count == 1
            await pilot.press("t")
            await pilot.pause()
            assert app.query_one("#events").row_count == 3

    asyncio.run(scenario())


def test_sort_toggle_reverses_order(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _seed_mixed_session(store)

    async def scenario() -> None:
        app = tui.RestoryMonitorApp(repo_root=tmp_path / "repo")
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#events")

            def first_id() -> str:
                return str(table.get_cell_at((0, 0)))

            # Default is newest-first (highest id at the top).
            assert first_id() == "3"

            await pilot.press("s")
            await pilot.pause()
            assert first_id() == "1"

            bar = app.query_one("#statusbar").render()
            text = bar.plain if hasattr(bar, "plain") else str(bar)
            assert "oldest" in text

            # Toggling back returns to newest-first.
            await pilot.press("s")
            await pilot.pause()
            assert first_id() == "3"

    asyncio.run(scenario())


def test_enter_expands_and_collapses_event(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _seed_mixed_session(store)

    async def scenario() -> None:
        app = tui.RestoryMonitorApp(repo_root=tmp_path / "repo")
        async with app.run_test() as pilot:
            await pilot.pause()
            detail = app.query_one("#detail")
            assert detail.display is False

            # The newest event (mass-delete rm -rf) is highlighted first.
            await pilot.press("enter")
            await pilot.pause()
            assert detail.display is True
            body = detail.render()
            body_text = body.plain if hasattr(body, "plain") else str(body)
            assert "rm -rf build" in body_text
            assert "mass-delete" in body_text
            assert "deletes everything" in body_text

            # Enter again collapses the detail pane.
            await pilot.press("enter")
            await pilot.pause()
            assert app.query_one("#detail").display is False

    asyncio.run(scenario())


def test_blocked_only_and_oldest_first_compose(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _seed_mixed_session(store)

    async def scenario() -> None:
        app = tui.RestoryMonitorApp(repo_root=tmp_path / "repo")
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#events")

            # Blocked-only (drops the safe id=1) AND oldest-first together.
            await pilot.press("b")
            await pilot.press("s")
            await pilot.pause()

            assert table.row_count == 2  # only the two blocked events
            # Oldest blocked (id=2, net-egress) is now on top; id=3 below it.
            assert str(table.get_cell_at((0, 0))) == "2"
            assert str(table.get_cell_at((1, 0))) == "3"

    asyncio.run(scenario())


def test_search_filters_rows(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _seed_mixed_session(store)

    async def scenario() -> None:
        app = tui.RestoryMonitorApp(repo_root=tmp_path / "repo")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#events").row_count == 3

            # '/' opens the search box; typing narrows to matching commands.
            await pilot.press("/")
            await pilot.pause()
            assert app.query_one("#search").display is True
            await pilot.press("c", "u", "r", "l")
            await pilot.pause()
            assert app.query_one("#events").row_count == 1

            bar = app.query_one("#statusbar").render()
            text = bar.plain if hasattr(bar, "plain") else str(bar)
            assert "search:" in text
            assert "curl" in text

    asyncio.run(scenario())


def test_search_esc_clears_and_closes(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _seed_mixed_session(store)

    async def scenario() -> None:
        app = tui.RestoryMonitorApp(repo_root=tmp_path / "repo")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("/")
            await pilot.press("c", "u", "r", "l")
            await pilot.pause()
            assert app.query_one("#events").row_count == 1

            # 'esc' clears the term, closes the box, and restores every row.
            await pilot.press("escape")
            await pilot.pause()
            assert app.query_one("#events").row_count == 3
            assert app.query_one("#search").display is False
            bar = app.query_one("#statusbar").render()
            text = bar.plain if hasattr(bar, "plain") else str(bar)
            assert "search:" not in text

    asyncio.run(scenario())


def _seed_secret_session(store):
    """A safe and a blocked event both matching 'secret', plus one that doesn't."""
    store.record_session("anchor", started_at="2026-08-28T00:00:00+00:00")
    store.append_event("Bash", [], False, "ok",
                       {"tool_input": {"command": "echo secret-value"}},
                       timestamp="2026-08-28T00:01:00+00:00")
    store.append_event("Bash", ["net-egress"], True, "net-egress leak",
                       {"tool_input": {"command": "curl http://secret.example"}},
                       timestamp="2026-08-28T00:02:00+00:00")
    store.append_event("Bash", ["mass-delete"], True, "removes files",
                       {"tool_input": {"command": "rm -rf build"}},
                       timestamp="2026-08-28T00:03:00+00:00")


def test_search_composes_with_blocked_only(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _seed_secret_session(store)

    async def scenario() -> None:
        app = tui.RestoryMonitorApp(repo_root=tmp_path / "repo")
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#events")
            assert table.row_count == 3

            # Blocked-only first (drops the safe 'echo secret-value').
            await pilot.press("b")
            await pilot.pause()
            assert table.row_count == 2

            # Then search 'secret' — of the two blocked, only curl matches.
            # If search alone applied it would be 2 (safe echo also matches),
            # so a count of 1 proves both filters compose.
            await pilot.press("/")
            await pilot.press("s", "e", "c", "r", "e", "t")
            await pilot.pause()
            assert table.row_count == 1
            assert "curl" in str(table.get_cell_at((0, 3)))

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
