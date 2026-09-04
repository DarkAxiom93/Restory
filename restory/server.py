"""Local FastAPI server for the restory timeline UI.

Binds to 127.0.0.1 only and is protected against cross-origin abuse and DNS
rebinding. Exposes:

    GET  /api/events   -> recorded events for this repo, newest first
    POST /api/undo     -> revert the most recent shadow snapshot

and serves the statically-exported Next.js app from ``restory/ui/out`` at ``/``.

Security model
--------------
While ``restory open`` is running, the loopback server would otherwise be
reachable by *any* web page the user visits: a page can fire a simple
cross-origin ``POST`` at ``127.0.0.1:<port>`` (a ``text/plain`` or form body
triggers no preflight), and although it cannot read the response, the *undo*
would already have destroyed the user's uncommitted session work.

Every ``/api/*`` request is therefore required to prove three things:

1. **Token** — a per-session secret (``secrets.token_urlsafe(32)``) generated at
   startup and handed to the UI out-of-band (in the URL fragment, never sent to
   the server or written to a log). The UI echoes it back in the custom
   ``X-Restory-Token`` header on every request. A custom header cannot be sent
   cross-origin without a CORS preflight, so it is itself a defense. Tokens are
   compared with :func:`hmac.compare_digest`.
2. **Origin** — when an ``Origin`` header is present it must be exactly this
   server's loopback origin; a foreign origin is rejected.
3. **Host** — the ``Host`` header must be loopback (``127.0.0.1``/``localhost``
   with the right port), which blocks DNS-rebinding (a rebinding attacker's page
   carries the attacker's hostname in ``Host``).

Any failure returns ``403`` with a short message and nothing else — the token is
never echoed into a response, a log line, or a URL.
"""

from __future__ import annotations

import hmac
import re
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import snapshot, store

UI_DIR = Path(__file__).resolve().parent / "ui" / "out"

# Injected into the served index.html. It runs synchronously in <head> *before*
# any of the app's bundle scripts, so window.fetch is patched before a single
# line of app code evaluates. It resolves the per-session token in this order:
#
#   1. the URL fragment (how ``restory open`` delivers it — never sent to the
#      server, never logged), which is then persisted to sessionStorage and
#      stripped from the address bar; then
#   2. sessionStorage, so a plain reload or a re-opened same-tab load (which land
#      on ``/`` with no fragment) still authenticate — the previous bug was that
#      the token lived only in the fragment for one load, so every poll after a
#      reload got a 403.
#
# sessionStorage (not localStorage) matches the token's lifetime exactly: the
# token is minted fresh on each ``restory open`` and sessionStorage is cleared
# when the tab closes, so no stale token lingers in disk-backed storage. It is
# also same-origin only, so a DNS-rebinding page on a different origin can't read
# it.
#
# The wrapper reads the token from a global at call time (not a one-shot closure
# capture) and only attaches the header to *same-origin* requests, so the token
# can never be sent to a third-party endpoint.
_TOKEN_BOOTSTRAP = """<script>
(function () {
  var KEY = "__restory_token__";
  function stored() {
    try { return window.sessionStorage.getItem(KEY) || ""; } catch (e) { return ""; }
  }
  function persist(t) {
    try { window.sessionStorage.setItem(KEY, t); } catch (e) {}
  }
  try {
    var raw = window.location.hash || "";
    var fromHash = raw.charAt(0) === "#" ? raw.slice(1) : raw;
    if (fromHash) {
      persist(fromHash);
      try {
        history.replaceState(null, "", window.location.pathname + window.location.search);
      } catch (e) {}
    }
    window.__RESTORY_TOKEN__ = fromHash || stored();
    var origFetch = window.fetch ? window.fetch.bind(window) : null;
    if (origFetch) {
      window.fetch = function (input, init) {
        var token = window.__RESTORY_TOKEN__;
        var url = null;
        try {
          url = new URL(
            typeof input === "string" ? input : (input && input.url) || "",
            window.location.href
          );
        } catch (e) {}
        var sameOrigin = !!url && url.origin === window.location.origin;
        if (token && sameOrigin) {
          init = init ? Object.assign({}, init) : {};
          var headers = new Headers(
            (init && init.headers) ||
              (typeof input !== "string" && input && input.headers) ||
              {}
          );
          headers.set("X-Restory-Token", token);
          init.headers = headers;
        }
        return origFetch(input, init);
      };
    }
  } catch (e) {}
})();
</script>"""


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


def _forbidden(message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "message": message}, status_code=403)


def _inject_token_bootstrap(html: str) -> str:
    """Insert the token-bootstrap script so it runs before the app's scripts.

    Placed immediately after the opening ``<head>`` so it parses and patches
    ``fetch`` before any of the app's (async) script tags are even encountered.
    Falls back to before ``</head>`` and then to the top of the document.
    """
    m = re.search(r"<head[^>]*>", html, flags=re.IGNORECASE)
    if m:
        idx = m.end()
        return html[:idx] + _TOKEN_BOOTSTRAP + html[idx:]
    if "</head>" in html:
        return html.replace("</head>", _TOKEN_BOOTSTRAP + "</head>", 1)
    return _TOKEN_BOOTSTRAP + html


def create_app(
    token: str | None = None,
    port: int = 8765,
    repo_root: Path | None = None,
) -> FastAPI:
    """Build the restory server app.

    ``token`` is the per-session secret the UI must present; when omitted a
    fresh one is generated (leaving the app effectively locked, since no client
    knows it — callers that need a usable server, i.e. ``restory open``, pass the
    token they also hand to the browser). ``port`` and ``repo_root`` scope the
    Host/Origin checks and the event/undo data to this loopback server's repo.
    """
    session_token = token or secrets.token_urlsafe(32)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    app = FastAPI(title="restory", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        # Only the data/action API is guarded. The static shell (HTML/JS/CSS)
        # carries no session data and cannot send a custom header from a <script>
        # or <link> tag, so requiring the token there would just break loading.
        if request.url.path.startswith("/api/"):
            # Host first: blocks DNS rebinding regardless of any token.
            host = request.headers.get("host", "")
            if host not in allowed_hosts:
                return _forbidden("Forbidden: invalid Host header.")
            # Origin (only when present — same-origin GETs may omit it).
            origin = request.headers.get("origin")
            if origin is not None and origin not in allowed_origins:
                return _forbidden("Forbidden: cross-origin request refused.")
            # Token on every API request (data disclosure and state change alike).
            supplied = request.headers.get("x-restory-token", "")
            if not supplied or not hmac.compare_digest(supplied, session_token):
                return _forbidden("Forbidden: missing or invalid restory token.")
        return await call_next(request)

    @app.get("/api/events")
    def get_events(limit: int = 500) -> JSONResponse:
        events = [
            _shape_event(e)
            for e in store.fetch_events(limit=limit, repo_root=repo_root)
        ]
        return JSONResponse({"events": events})

    @app.post("/api/undo")
    def post_undo() -> JSONResponse:
        shadow = snapshot.get_shadow(repo_root)
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

    # Serve the app shell with the token bootstrap injected. Defined before the
    # static mount so these exact paths win over StaticFiles.
    if UI_DIR.is_dir():
        _index = UI_DIR / "index.html"

        @app.get("/", response_class=HTMLResponse)
        def _serve_index() -> HTMLResponse:
            return HTMLResponse(
                _inject_token_bootstrap(_index.read_text(encoding="utf-8"))
            )

        @app.get("/index.html", response_class=HTMLResponse)
        def _serve_index_html() -> HTMLResponse:
            return HTMLResponse(
                _inject_token_bootstrap(_index.read_text(encoding="utf-8"))
            )

        # Everything else (/_next/*, assets) is static and unguarded.
        app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
    else:  # pragma: no cover - only when UI not built

        @app.get("/", response_class=HTMLResponse)
        def _no_ui() -> str:
            return (
                "<h1>restory</h1><p>UI not built. Run the Next.js export into "
                "<code>restory/ui/out</code>.</p>"
            )

    return app


# Module-level app for import safety. It is generated with a random, unshared
# token, so it is effectively locked; ``restory open`` builds its own app via
# ``create_app`` with the token it also passes to the browser.
app = create_app()
