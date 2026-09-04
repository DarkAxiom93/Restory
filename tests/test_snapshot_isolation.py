"""Shadow-git isolation from the user's global/system git config (Issue 3).

Snapshot operations must not be influenced by — nor allowed to execute — the
user's global/system git configuration (filters, attributes, hooksPath). We
verify the environment and the per-invocation ``-c`` overrides here, and that a
real snapshot still succeeds with them in place.
"""

from __future__ import annotations

import os
import shutil

import pytest

from restory import snapshot


def _shadow(tmp_path):
    return snapshot.Shadow(tmp_path / "repo", tmp_path / "shadow")


def test_env_neutralizes_global_and_system_config(tmp_path):
    env = _shadow(tmp_path)._env()
    # Global/system config files are redirected to os.devnull (NUL on Windows,
    # /dev/null on POSIX), so external config reads as empty.
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_every_invocation_overrides_hooks_and_attributes(tmp_path):
    flags = _shadow(tmp_path)._isolation_flags()
    # Rendered as `-c core.hooksPath=<devnull> -c core.attributesFile=<devnull>`.
    assert flags[0] == "-c" and flags[1] == f"core.hooksPath={os.devnull}"
    assert flags[2] == "-c" and flags[3] == f"core.attributesFile={os.devnull}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_snapshot_still_works_with_isolation(monkeypatch, tmp_path):
    # Isolate the data dir so the shadow lands in a temp home, not ~/.restory.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("one\n", encoding="utf-8")

    shadow, created = snapshot.ensure_shadow(repo)
    assert created and shadow.exists()

    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    commit = shadow.snapshot("evt-1")
    assert commit  # a real commit hash was produced under isolation

    changes = shadow.undo()
    assert any(c.path == "a.txt" for c in changes)
    assert (repo / "a.txt").read_text(encoding="utf-8") == "one\n"
