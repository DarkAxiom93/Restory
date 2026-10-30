"""Tests for the optional BLOCK desktop-notification feature.

Covers three guarantees:

1. A blocked decision *attempts* a notification when the feature is enabled.
2. Approves never notify (would be spam), and notifications stay off by default.
3. A notifier failure never affects the hook's stdout decision.

The real OS call (``plyer``) is always mocked — these tests never pop a toast.
"""

from __future__ import annotations

import io
import json

from restory import config, hook, notify
from restory.classify import ClassifyResult


def _isolate_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))


def _run_hook(monkeypatch, capsys, payload: dict) -> dict:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = hook.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out)


DANGEROUS = {
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf ~"},
    "hook_event_name": "PreToolUse",
}
SAFE = {
    "tool_name": "Bash",
    "tool_input": {"command": "ls -la"},
    "hook_event_name": "PreToolUse",
}


# --------------------------------------------------------------------------- #
# Enablement gating
# --------------------------------------------------------------------------- #


def test_notifications_off_by_default(monkeypatch):
    monkeypatch.delenv(config.NOTIFY_ENV, raising=False)
    monkeypatch.setattr(config, "NOTIFY_FLAG", None)
    assert config.notifications_enabled() is False


def test_env_var_opts_in(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_FLAG", None)
    for val in ("1", "true", "YES", "on"):
        monkeypatch.setenv(config.NOTIFY_ENV, val)
        assert config.notifications_enabled() is True
    monkeypatch.setenv(config.NOTIFY_ENV, "0")
    assert config.notifications_enabled() is False


def test_config_flag_overrides_env(monkeypatch):
    monkeypatch.setenv(config.NOTIFY_ENV, "1")
    monkeypatch.setattr(config, "NOTIFY_FLAG", False)
    assert config.notifications_enabled() is False


# --------------------------------------------------------------------------- #
# notify_block dispatch behaviour
# --------------------------------------------------------------------------- #


def test_notify_block_sends_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "notifications_enabled", lambda: True)
    calls = []
    monkeypatch.setattr(notify, "_send", lambda title, body: calls.append((title, body)))

    result = ClassifyResult(tags=["mass-delete"], danger=True, reason="mass-delete: rm")
    notify.notify_block(result, {"tool_name": "Bash", "tool_input": {"command": "rm -rf ~"}})

    assert len(calls) == 1
    title, body = calls[0]
    assert title == "restory blocked a command"
    assert "mass-delete" in body
    assert "rm -rf ~" in body


def test_notify_block_silent_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "notifications_enabled", lambda: False)
    calls = []
    monkeypatch.setattr(notify, "_send", lambda *a: calls.append(a))

    notify.notify_block(ClassifyResult(tags=["x"], danger=True), {})
    assert calls == []


def test_notify_block_swallows_send_failure(monkeypatch):
    monkeypatch.setattr(config, "notifications_enabled", lambda: True)

    def boom(*_a):
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(notify, "_send", boom)
    # Must not raise.
    notify.notify_block(ClassifyResult(tags=["x"], danger=True), {})


def test_missing_backend_is_silent(monkeypatch):
    # Simulate the optional backend not installed: the import inside the real OS
    # call fails on every platform. _deliver must swallow it.
    import builtins

    real_import = builtins.__import__
    blocked = ("plyer", "windows_toasts")

    def fake_import(name, *args, **kwargs):
        if name.startswith(blocked):
            raise ImportError(f"No module named '{name}'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    notify._deliver("t", "b")  # must not raise even with no backend available


def test_windows_falls_back_to_plyer(monkeypatch):
    # On Windows, if the WinRT toast backend raises, we fall back to plyer.
    monkeypatch.setattr(notify.sys, "platform", "win32")
    monkeypatch.setattr(
        notify, "_deliver_windows", lambda *a: (_ for _ in ()).throw(RuntimeError("no toast"))
    )
    called = []
    monkeypatch.setattr(notify, "_deliver_plyer", lambda t, b: called.append((t, b)))

    notify._deliver("t", "b")
    assert called == [("t", "b")]


def test_send_invokes_deliver(monkeypatch):
    # _send joins its worker thread briefly, so delivery is deterministic here.
    got = []
    monkeypatch.setattr(notify, "_deliver", lambda t, b: got.append((t, b)))
    notify._send("title", "body")
    assert got == [("title", "body")]


def test_body_truncates_long_command(monkeypatch):
    long_cmd = "echo " + "a" * 500
    _title, body = notify.build_message(
        ClassifyResult(tags=["uninspectable"], danger=True),
        {"tool_name": "Bash", "tool_input": {"command": long_cmd}},
    )
    assert len(body) < 130  # tag + truncated command, not the full 500 chars
    assert body.endswith("…")


# --------------------------------------------------------------------------- #
# End-to-end through the hook: the stdout contract is sacred
# --------------------------------------------------------------------------- #


def test_hook_blocked_decision_attempts_notification(monkeypatch, tmp_path, capsys):
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setenv(config.NOTIFY_ENV, "1")
    monkeypatch.setattr(config, "NOTIFY_FLAG", None)

    sent = []
    monkeypatch.setattr(notify, "_send", lambda title, body: sent.append((title, body)))

    payload = dict(DANGEROUS, cwd=str(tmp_path))
    decision = _run_hook(monkeypatch, capsys, payload)

    assert decision["decision"] == "block"
    assert decision["reason"]
    assert len(sent) == 1  # a notification was attempted on the block


def test_hook_approve_does_not_notify(monkeypatch, tmp_path, capsys):
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setenv(config.NOTIFY_ENV, "1")
    monkeypatch.setattr(config, "NOTIFY_FLAG", None)

    sent = []
    monkeypatch.setattr(notify, "_send", lambda *a: sent.append(a))

    payload = dict(SAFE, cwd=str(tmp_path))
    decision = _run_hook(monkeypatch, capsys, payload)

    assert decision == {"decision": "approve"}
    assert sent == []  # approves are silent


def test_hook_notifier_failure_does_not_affect_decision(monkeypatch, tmp_path, capsys):
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setenv(config.NOTIFY_ENV, "1")
    monkeypatch.setattr(config, "NOTIFY_FLAG", None)

    def boom(*_a):
        raise RuntimeError("notifier down")

    monkeypatch.setattr(notify, "_send", boom)

    payload = dict(DANGEROUS, cwd=str(tmp_path))
    decision = _run_hook(monkeypatch, capsys, payload)

    # The block decision is still emitted, uncorrupted, despite the failure.
    assert decision["decision"] == "block"
    assert decision["reason"]


def test_hook_disabled_by_default_does_not_notify(monkeypatch, tmp_path, capsys):
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.delenv(config.NOTIFY_ENV, raising=False)
    monkeypatch.setattr(config, "NOTIFY_FLAG", None)

    sent = []
    monkeypatch.setattr(notify, "_send", lambda *a: sent.append(a))

    payload = dict(DANGEROUS, cwd=str(tmp_path))
    decision = _run_hook(monkeypatch, capsys, payload)

    assert decision["decision"] == "block"
    assert sent == []  # off by default even on a block
