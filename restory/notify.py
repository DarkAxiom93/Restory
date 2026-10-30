"""Optional native desktop notifications for BLOCK decisions.

When the hook classifies a command as dangerous and returns a *block* decision,
this module can pop a native OS notification so the user notices even when they
are not watching the timeline. It is:

* **opt-in** — OFF unless ``RESTORY_NOTIFY`` (or the ``config.NOTIFY_FLAG``
  config flag) is enabled; see :func:`restory.config.notifications_enabled`.
* **optional** — the ``plyer`` dependency is an extra (``pip install
  "restory[notify]"``); if it is missing, nothing happens.
* **fail-safe** — any failure (missing library, backend error, timeout) is
  swallowed. A notification must **never** delay, corrupt, or crash the hook's
  stdout decision.

Backends are platform-specific because no single dependency does both jobs
well:

* **Windows** uses ``windows-toasts`` — real WinRT toast notifications. plyer's
  Windows backend only emits a legacy ``Shell_NotifyIcon`` tray balloon from a
  throwaway window/icon, which Windows 10/11 renders unreliably (and not at all
  from a short-lived hook process). ``show_toast()`` hands the toast to the OS
  notification platform synchronously, so it survives our process exiting.
* **macOS / Linux** use ``plyer`` — a pure-Python facade over ``osascript`` /
  ``notify-send`` with zero required transitive deps and a synchronous API (no
  asyncio loop to spin up inside a latency-sensitive hook).

The OS call is dispatched on a daemon thread and joined only briefly, so it can
never hold up the hook — the block decision has already been flushed to stdout
by the time we get here.
"""

from __future__ import annotations

import sys
import threading
from typing import TYPE_CHECKING

from . import config

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .classify import ClassifyResult

# Keep the notification body short: title + a truncated command is enough to
# recognise what was blocked without dumping a huge one-liner into a toast.
_TITLE = "restory blocked a command"
_CMD_MAX = 100
_APP_NAME = "restory"
_TIMEOUT_SECS = 5
# Upper bound on how long we wait for the OS handoff to complete. The WinRT /
# osascript / notify-send call normally returns in tens of ms; this only caps a
# misbehaving backend so it can never wedge the hook. The decision is already on
# stdout, so this delays (at most) process exit, never the block decision.
_HANDOFF_TIMEOUT = 3.0


def _describe_target(payload: dict) -> str:
    """Best-effort short description of what the blocked call touched."""
    tool_input = payload.get("tool_input") or {}
    target = (
        tool_input.get("command")
        or tool_input.get("file_path")
        or tool_input.get("path")
        or payload.get("tool_name")
        or ""
    )
    return str(target)


def _truncate(text: str, limit: int = _CMD_MAX) -> str:
    text = " ".join(text.split())  # collapse newlines/runs of whitespace
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"  # ellipsis


def build_message(result: "ClassifyResult", payload: dict) -> tuple[str, str]:
    """Return ``(title, body)`` for a blocked command's notification."""
    tag = result.tags[0] if result.tags else "danger"
    body = f"[{tag}] {_truncate(_describe_target(payload))}".strip()
    return _TITLE, body


def _deliver_windows(title: str, body: str) -> None:
    """Real WinRT toast via ``windows-toasts`` (optional dependency)."""
    from windows_toasts import Toast, WindowsToaster  # optional dependency

    toaster = WindowsToaster(_APP_NAME)
    toast = Toast()
    toast.text_fields = [title, body]
    # Synchronous handoff to the OS notification platform; the toast persists
    # after this process exits.
    toaster.show_toast(toast)


def _deliver_plyer(title: str, body: str) -> None:
    """Native notification via ``plyer`` (macOS/Linux; optional dependency)."""
    from plyer import notification  # optional dependency

    notification.notify(
        title=title,
        message=body,
        app_name=_APP_NAME,
        timeout=_TIMEOUT_SECS,
    )


def _deliver(title: str, body: str) -> None:
    """Do the real OS call for the current platform. Swallows everything.

    Runs on a daemon thread; a notifier failure (missing library, backend
    error) must never escape. On Windows, if ``windows-toasts`` is unavailable
    we fall back to plyer as a best effort.
    """
    try:
        if sys.platform == "win32":
            try:
                _deliver_windows(title, body)
            except Exception:  # noqa: BLE001 - fall back to plyer if toasts fail
                _deliver_plyer(title, body)
        else:
            _deliver_plyer(title, body)
    except Exception:  # noqa: BLE001 - never let a notifier failure escape
        pass


def _send(title: str, body: str) -> None:
    """Dispatch the OS notification without blocking the block decision.

    Fires on a daemon thread and joins only briefly (:data:`_HANDOFF_TIMEOUT`)
    so a slow/hung backend can never wedge the hook, while giving a healthy
    backend time to complete its (fast) handoff to the OS. Mocked in tests.
    """
    thread = threading.Thread(target=_deliver, args=(title, body), daemon=True)
    thread.start()
    thread.join(_HANDOFF_TIMEOUT)


def notify_block(result: "ClassifyResult", payload: dict) -> None:
    """Fire a best-effort desktop notification for a BLOCK. Never raises.

    No-op unless notifications are enabled. Safe to call unconditionally; the
    caller must still only invoke this on a block decision so approves stay
    silent.
    """
    try:
        if not config.notifications_enabled():
            return
        title, body = build_message(result, payload)
        _send(title, body)
    except Exception:  # noqa: BLE001 - the hook contract must never be affected
        pass
