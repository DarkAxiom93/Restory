"""Tests for per-adapter detection and ``restory init --all``.

Detection is always mocked here (via ``monkeypatch``) so these tests never
depend on which coding agents happen to be installed on the machine running
them.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from restory import adapters
from restory.cli import app

runner = CliRunner()


def _repo(tmp_path, monkeypatch):
    """A tmp dir that resolves as the repo root, made the current directory."""
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _force_detection(monkeypatch, present_keys: set[str]) -> None:
    """Make each adapter report presence according to ``present_keys``."""
    monkeypatch.setattr(
        adapters.Adapter,
        "detect",
        lambda self: self.key in present_keys,
    )


# --------------------------------------------------------------------------- #
# Per-adapter detection heuristic (config dir / executable on PATH)
# --------------------------------------------------------------------------- #


def test_detect_finds_config_dir_in_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    # Isolate the repo-root probe to an empty dir. Just chdir'ing to an empty
    # dir is NOT enough: find_repo_root() walks *up* the real filesystem, so if
    # the pytest tmp tree sits under a real git repo (e.g. --basetemp=.pytest-tmp
    # inside this checkout) it would climb into it and see the real .claude/
    # .gemini dirs. Pin the seam directly.
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(adapters.config, "find_repo_root", lambda: workdir)
    monkeypatch.setattr(adapters.shutil, "which", lambda _exe: None)

    assert adapters.get_adapter("claude").detect() is True
    assert adapters.get_adapter("gemini").detect() is False


def test_detect_finds_config_dir_in_repo_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    repo = _repo(tmp_path, monkeypatch)
    (repo / ".gemini").mkdir()
    # Pin the repo-root seam explicitly so the probe targets this repo and can't
    # climb into a real checkout above the tmp tree.
    monkeypatch.setattr(adapters.config, "find_repo_root", lambda: repo)
    monkeypatch.setattr(adapters.shutil, "which", lambda _exe: None)

    assert adapters.get_adapter("gemini").detect() is True
    assert adapters.get_adapter("claude").detect() is False


def test_detect_finds_executable_on_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    # Isolate the repo-root probe (see note in test_detect_finds_config_dir_in_home):
    # PATH must be the *only* signal here, so pin find_repo_root() to an empty dir
    # rather than trusting an empty cwd, which find_repo_root() would climb out of.
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(adapters.config, "find_repo_root", lambda: workdir)
    monkeypatch.setattr(
        adapters.shutil,
        "which",
        lambda exe: "/usr/bin/gemini" if exe == "gemini" else None,
    )

    assert adapters.get_adapter("gemini").detect() is True
    assert adapters.get_adapter("claude").detect() is False


def test_detect_present_adapters_filters_by_detection(monkeypatch):
    _force_detection(monkeypatch, {"gemini"})
    present = adapters.detect_present_adapters()
    assert [a.key for a in present] == ["gemini"]


# --------------------------------------------------------------------------- #
# init --all
# --------------------------------------------------------------------------- #


def test_init_all_installs_every_detected_agent(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _force_detection(monkeypatch, {"claude", "gemini"})

    result = runner.invoke(app, ["init", "--all"])
    assert result.exit_code == 0, result.output

    claude_settings = repo / ".claude" / "settings.json"
    gemini_settings = repo / ".gemini" / "settings.json"
    assert claude_settings.exists()
    assert gemini_settings.exists()

    # Both agents' hooks are wired up correctly in their own config shapes.
    claude_cfg = json.loads(claude_settings.read_text(encoding="utf-8"))
    assert "PreToolUse" in claude_cfg["hooks"]
    gemini_cfg = json.loads(gemini_settings.read_text(encoding="utf-8"))
    assert "BeforeTool" in gemini_cfg["hooks"]

    assert "Claude Code" in result.output
    assert "Gemini CLI" in result.output


def test_init_all_installs_only_detected_and_reports_skips(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _force_detection(monkeypatch, {"claude"})

    result = runner.invoke(app, ["init", "--all"])
    assert result.exit_code == 0, result.output

    assert (repo / ".claude" / "settings.json").exists()
    assert not (repo / ".gemini").exists()
    assert "Installed Claude Code" in result.output
    assert "Skipped Gemini CLI" in result.output


def test_init_all_with_no_agents_prints_helpful_message(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _force_detection(monkeypatch, set())

    result = runner.invoke(app, ["init", "--all"])
    # Non-zero exit signals nothing was installed, but the message is the point.
    assert result.exit_code == 1
    assert "No coding agents detected" in result.output
    assert "restory init --agent" in result.output

    # Nothing was written.
    assert not (repo / ".claude").exists()
    assert not (repo / ".gemini").exists()


# --------------------------------------------------------------------------- #
# Single-agent init still works exactly as before (--all is additive)
# --------------------------------------------------------------------------- #


def test_init_single_agent_still_works(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    # Detection is irrelevant for an explicit --agent; make it all-false to
    # prove --agent does not consult it.
    _force_detection(monkeypatch, set())

    result = runner.invoke(app, ["init", "--agent", "gemini"])
    assert result.exit_code == 0, result.output

    assert (repo / ".gemini" / "settings.json").exists()
    assert not (repo / ".claude").exists()


def test_init_default_agent_is_claude(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _force_detection(monkeypatch, set())

    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    assert (repo / ".claude" / "settings.json").exists()
    assert not (repo / ".gemini").exists()
