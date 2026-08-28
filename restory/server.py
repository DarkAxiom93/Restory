"""Local FastAPI server for the restory timeline UI.

Binds to 127.0.0.1 only. Exposes:

    GET  /api/events   -> recorded events, newest first
    POST /api/undo     -> revert the most recent shadow snapshot

and serves the statically-exported Next.js app from ``restory/ui/out`` at ``/``.
No auth, no external calls.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import snapshot, store

UI_DIR = Path(__file__).resolve().parent / "ui" / "out"


def _command_of(raw: dict) -> str:
    tool_input = raw.get("tool_input") or {}
    return (
        tool_input.get("command")
        or tool_input.get("file_path")
        or tool_input.get("path")
        or ""
    )


def _shape_event(ev: dict) -> dict:
    return {
        "id": ev["id"],
        "timestamp": ev["timestamp"],
        "tool_name": ev["tool_name"],
        "tags": ev["tags"],
        "danger": ev["danger"],
        "reason": ev["reason"],
        "command": _command_of(ev.get("raw") or {}),
        "event": (ev.get("raw") or {}).get("hook_event_name", ""),
        "decision": "block" if ev["danger"] else "approve",
    }


def create_app() -> FastAPI:
    app = FastAPI(title="restory", docs_url=None, redoc_url=None)

    @app.get("/api/events")
    def get_events(limit: int = 500) -> JSONResponse:
        events = [_shape_event(e) for e in store.fetch_events(limit=limit)]
        return JSONResponse({"events": events})

    @app.post("/api/undo")
    def post_undo() -> JSONResponse:
        shadow = snapshot.get_shadow()
        if not shadow.exists():
            return JSONResponse(
                {"ok": False, "message": "No shadow repo. Run `restory watch` first."},
                status_code=409,
            )
        try:
            changes = shadow.undo()
        except snapshot.SnapshotError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
        return JSONResponse(
            {
                "ok": True,
                "message": f"Reverted {len(changes)} change(s).",
                "reverted": [{"status": c.status, "path": c.path} for c in changes],
            }
        )

    # Static UI last so /api/* wins. html=True serves index.html at /.
    if UI_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
    else:  # pragma: no cover - only when UI not built

        @app.get("/", response_class=HTMLResponse)
        def _no_ui() -> str:
            return (
                "<h1>restory</h1><p>UI not built. Run the Next.js export into "
                "<code>restory/ui/out</code>.</p>"
            )

    return app


app = create_app()
