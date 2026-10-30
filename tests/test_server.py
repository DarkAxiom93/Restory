"""Tests for the local restory server.

Two concerns live in this one file:

* **Security (Issue 1).** Every ``/api/*`` request must prove a per-session
  token, an acceptable ``Origin`` (when present), and a loopback ``Host``. These
  drive the FastAPI app through Starlette's ``TestClient``; the ``base_url``
  determines the ``Host`` header, so a foreign ``base_url`` simulates a
  DNS-rebinding request. A real shadow repo with a pending undo is set up so the
  *success* case actually reverts a file — and the blocked cases can assert that
  **no** filesystem change occurred.

* **Functional coverage.** GET ``/api/events`` shaping/limits, POST ``/api/undo``
  409/revert paths, static UI serving, and ``/api``-over-static precedence.

Each test isolates the data dir (``USERPROFILE``) and, where the shadow repo is
involved, the working directory, so nothing touches real data. The functional
``_client`` helper builds the app with a known token and sends it (plus a
loopback ``Host``/``Origin``) by default, so those tests exercise the *secured*
server rather than slipping past the guard.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from restory import server, snapshot, store

# Every server test here needs a real git repo (shadow snapshots). Skip the
# whole module when git is unavailable.
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)
# Kept as an explicit per-test marker on the undo tests that build a real repo;
# it documents intent and is harmless alongside the module-level skip above.
needs_git = pytestmark

PORT = 8765
TOKEN = "unit-test-token-value-not-a-real-secret"
GOOD_BASE = f"http://127.0.0.1:{PORT}"
GOOD_ORIGIN = f"http://127.0.0.1:{PORT}"


# =========================================================================== #
# Security: token + Origin + Host guard on every /api/* route
# =========================================================================== #


def _setup(monkeypatch, tmp_path):
    """Isolate the data dir and build a repo whose last snapshot can be undone.

    After setup, ``f.txt`` holds "v2" and the shadow's most recent commit is that
    change, so ``POST /api/undo`` (if allowed through) would restore it to "v1".
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.txt").write_text("v1\n", encoding="utf-8")
    shadow, _ = snapshot.ensure_shadow(repo)  # initial snapshot: f.txt == v1
    (repo / "f.txt").write_text("v2\n", encoding="utf-8")
    shadow.snapshot("evt-1")  # commit v2; undo() would restore v1

    app = server.create_app(token=TOKEN, port=PORT, repo_root=repo)
    return app, repo


def test_undo_without_token_is_forbidden_and_no_fs_change(monkeypatch, tmp_path):
    app, repo = _setup(monkeypatch, tmp_path)
    client = TestClient(app, base_url=GOOD_BASE)

    before = (repo / "f.txt").read_text(encoding="utf-8")
    assert before == "v2\n"  # a real pending undo exists

    resp = client.post("/api/undo")

    assert resp.status_code == 403
    # The undo must NOT have run: the work tree is byte-for-byte unchanged.
    assert (repo / "f.txt").read_text(encoding="utf-8") == "v2\n"
    # The token is never echoed back in an error response.
    assert TOKEN not in resp.text


def test_undo_with_wrong_token_is_forbidden(monkeypatch, tmp_path):
    app, repo = _setup(monkeypatch, tmp_path)
    client = TestClient(app, base_url=GOOD_BASE)

    resp = client.post("/api/undo", headers={"X-Restory-Token": "not-the-token"})

    assert resp.status_code == 403
    assert (repo / "f.txt").read_text(encoding="utf-8") == "v2\n"


def test_undo_with_valid_token_but_foreign_origin_is_forbidden(monkeypatch, tmp_path):
    app, repo = _setup(monkeypatch, tmp_path)
    client = TestClient(app, base_url=GOOD_BASE)

    resp = client.post(
        "/api/undo",
        headers={"X-Restory-Token": TOKEN, "Origin": "http://evil.example.com"},
    )

    assert resp.status_code == 403
    assert (repo / "f.txt").read_text(encoding="utf-8") == "v2\n"


def test_undo_with_valid_token_but_mismatched_host_is_forbidden(monkeypatch, tmp_path):
    app, repo = _setup(monkeypatch, tmp_path)
    # A DNS-rebinding request carries the attacker's hostname in Host.
    client = TestClient(app, base_url="http://evil.example.com")

    resp = client.post("/api/undo", headers={"X-Restory-Token": TOKEN})

    assert resp.status_code == 403
    assert (repo / "f.txt").read_text(encoding="utf-8") == "v2\n"


def test_undo_with_token_origin_and_host_succeeds(monkeypatch, tmp_path):
    app, repo = _setup(monkeypatch, tmp_path)
    client = TestClient(app, base_url=GOOD_BASE)

    resp = client.post(
        "/api/undo",
        headers={"X-Restory-Token": TOKEN, "Origin": GOOD_ORIGIN},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # The undo ran: f.txt is restored to the previous snapshot.
    assert (repo / "f.txt").read_text(encoding="utf-8") == "v1\n"


def test_events_without_token_is_forbidden(monkeypatch, tmp_path):
    app, _ = _setup(monkeypatch, tmp_path)
    client = TestClient(app, base_url=GOOD_BASE)

    resp = client.get("/api/events")

    assert resp.status_code == 403
    assert TOKEN not in resp.text


def test_events_with_valid_token_succeeds(monkeypatch, tmp_path):
    app, _ = _setup(monkeypatch, tmp_path)
    client = TestClient(app, base_url=GOOD_BASE)

    resp = client.get("/api/events", headers={"X-Restory-Token": TOKEN})

    assert resp.status_code == 200
    assert "events" in resp.json()


def test_localhost_host_and_origin_are_accepted(monkeypatch, tmp_path):
    app, _ = _setup(monkeypatch, tmp_path)
    client = TestClient(app, base_url=f"http://localhost:{PORT}")

    resp = client.get(
        "/api/events",
        headers={"X-Restory-Token": TOKEN, "Origin": f"http://localhost:{PORT}"},
    )

    assert resp.status_code == 200


def test_index_injects_token_bootstrap_without_leaking_token(monkeypatch, tmp_path):
    app, _ = _setup(monkeypatch, tmp_path)
    client = TestClient(app, base_url=GOOD_BASE)

    # The app shell is served unguarded (it carries no session data) but must
    # carry the fetch-patching bootstrap so the UI can authenticate its calls.
    resp = client.get("/")
    assert resp.status_code == 200
    assert "X-Restory-Token" in resp.text
    # The server-side token is NEVER embedded in the served HTML — the UI gets it
    # from the URL fragment, which the browser never sends to the server.
    assert TOKEN not in resp.text


def test_bootstrap_runs_before_bundle_and_persists_token(monkeypatch, tmp_path):
    """Guards the header-timing/persistence contract that a real browser needs.

    Regression: the token used to live only in the URL fragment for a single
    load, so any plain reload (which lands on ``/`` with no fragment) sent
    unauthenticated polls and got a perpetual 403. The served page must therefore
    (a) patch ``fetch`` in an inline script that runs *before* the app's bundle
    scripts, and (b) persist the token to ``sessionStorage`` and read it back, so
    a reload keeps authenticating. These are structural checks on the served HTML
    — the isolated middleware tests can't see this timing bug.
    """
    app, _ = _setup(monkeypatch, tmp_path)
    client = TestClient(app, base_url=GOOD_BASE)
    html = client.get("/").text

    # (a) The inline bootstrap must appear before the first bundle <script src=…>.
    boot = html.find("__RESTORY_TOKEN__")
    first_bundle = html.find("<script src=")
    assert boot != -1, "token bootstrap missing from served HTML"
    assert first_bundle != -1, "expected the app's bundle <script src> tags"
    assert boot < first_bundle, "bootstrap must run before the bundle scripts"

    # (b) The token must be persisted and re-read, not captured only from the
    # fragment for one load (otherwise a reload 403s forever). Per-session
    # sessionStorage matches the token's lifetime and avoids a disk-backed store.
    assert "sessionStorage" in html
    assert "localStorage" not in html
    assert "X-Restory-Token" in html
    # Header is attached from a call-time global (survives reloads), not a
    # one-shot closure over the fragment value.
    assert "window.__RESTORY_TOKEN__" in html


def test_every_api_route_is_guarded_without_a_token(monkeypatch, tmp_path):
    """Enumerate the app's real routes and prove EVERY ``/api/*`` one fails closed.

    This is deliberately introspective rather than a hand-listed set: any future
    ``/api/*`` route (a stats endpoint, etc.) that is added without going through
    the token+Origin+Host guard will make this test fail. A request with a
    loopback Host/Origin but NO token must get a 403 on each API route, for every
    method it accepts (except HEAD/OPTIONS, which are transport-level).
    """
    app, _ = _setup(monkeypatch, tmp_path)
    client = TestClient(app, base_url=GOOD_BASE)

    api_calls = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/"):
            continue
        for method in sorted(getattr(route, "methods", set()) or set()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            api_calls.append((method, path))

    # There is at least one API route, and we actually exercised it.
    assert api_calls, "no /api/* routes discovered on the app"

    for method, path in api_calls:
        resp = client.request(method, path)  # correct Host/Origin, but no token
        assert resp.status_code == 403, f"{method} {path} was not guarded: {resp.status_code}"
        # An error response must never echo the session token back.
        assert TOKEN not in resp.text, f"{method} {path} leaked the token"


# =========================================================================== #
# Functional coverage: events shaping, undo paths, static serving
#
# These exercise the *secured* server: ``_client`` builds the app with a known
# token and the returned TestClient sends that token plus a loopback Host/Origin
# by default, so the guard is satisfied and the handlers' own behavior is what's
# under test.
# =========================================================================== #


def _git_init(repo):
    """Make ``repo`` a real git repo so the server's ``find_repo_root()`` pins here.

    ``POST /api/undo`` resolves the shadow via ``find_repo_root()`` (nearest
    ``.git`` above the cwd). A real ``.git`` in the temp repo keeps that from
    escaping to a stray ancestor repo above the pytest temp dir.
    """
    if shutil.which("git") is None:
        return
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, capture_output=True)


def _isolate(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.chdir(repo)
    return repo


def _client(monkeypatch, ui_dir=None):
    """Build a fresh app (optionally pointing UI_DIR at ``ui_dir``).

    The app is built with a known token; the returned client sends that token and
    a loopback Host/Origin on every request so the guard passes and the handler
    behavior is what's exercised. Static-shell routes (``/``) are unguarded, so
    the extra header is simply ignored there.
    """
    if ui_dir is not None:
        monkeypatch.setattr(server, "UI_DIR", ui_dir)
    app = server.create_app(token=TOKEN, port=PORT)
    return TestClient(
        app,
        base_url=GOOD_BASE,
        headers={"X-Restory-Token": TOKEN, "Origin": GOOD_ORIGIN},
    )


# --------------------------------------------------------------------------- #
# GET /api/events
# --------------------------------------------------------------------------- #


def test_get_events_empty(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    client = _client(monkeypatch, ui_dir=tmp_path / "no-ui")
    res = client.get("/api/events")
    assert res.status_code == 200
    assert res.json() == {"events": []}


def test_get_events_shapes_records(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    store.append_event("Bash", ["net-egress"], True, "blocked curl",
                       {"tool_input": {"command": "curl evil"},
                        "hook_event_name": "PreToolUse"},
                       timestamp="2026-08-28T00:02:00+00:00")
    store.append_event("Write", [], False, "ok",
                       {"tool_input": {"file_path": "a.py"},
                        "hook_event_name": "PostToolUse"},
                       timestamp="2026-08-28T00:03:00+00:00")

    client = _client(monkeypatch, ui_dir=tmp_path / "no-ui")
    res = client.get("/api/events")
    assert res.status_code == 200
    events = res.json()["events"]
    assert len(events) == 2
    # Newest-first.
    newest = events[0]
    assert newest["tool_name"] == "Write"
    assert newest["command"] == "a.py"
    assert newest["decision"] == "approve"
    assert newest["event"] == "PostToolUse"
    # The blocked one is shaped with decision "block".
    blocked = events[1]
    assert blocked["danger"] is True
    assert blocked["decision"] == "block"
    assert blocked["command"] == "curl evil"


def test_get_events_respects_limit(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    for i in range(5):
        store.append_event("Bash", [], False, "ok",
                           {"tool_input": {"command": f"echo {i}"}},
                           timestamp=f"2026-08-28T00:0{i}:00+00:00")
    client = _client(monkeypatch, ui_dir=tmp_path / "no-ui")
    res = client.get("/api/events", params={"limit": 2})
    assert res.status_code == 200
    assert len(res.json()["events"]) == 2


# --------------------------------------------------------------------------- #
# POST /api/undo
# --------------------------------------------------------------------------- #


def test_undo_without_shadow_returns_409(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    client = _client(monkeypatch, ui_dir=tmp_path / "no-ui")
    res = client.post("/api/undo")
    assert res.status_code == 409
    body = res.json()
    assert body["ok"] is False
    assert "No shadow repo" in body["message"]


@needs_git
def test_undo_with_nothing_to_revert_returns_409(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    snapshot.ensure_shadow(repo)  # only the initial snapshot exists
    client = _client(monkeypatch, ui_dir=tmp_path / "no-ui")
    res = client.post("/api/undo")
    assert res.status_code == 409
    body = res.json()
    assert body["ok"] is False
    assert "nothing to undo" in body["message"]


@needs_git
def test_undo_reverts_latest_snapshot(monkeypatch, tmp_path):
    repo = _isolate(monkeypatch, tmp_path)
    (repo / "f.txt").write_text("v1\n", encoding="utf-8")
    shadow, _ = snapshot.ensure_shadow(repo)
    shadow.session_baseline()
    (repo / "f.txt").write_text("v2\n", encoding="utf-8")
    shadow.snapshot("e1")

    client = _client(monkeypatch, ui_dir=tmp_path / "no-ui")
    res = client.post("/api/undo")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "Reverted 1 change(s)" in body["message"]
    assert body["reverted"][0]["path"] == "f.txt"
    assert (repo / "f.txt").read_text(encoding="utf-8") == "v1\n"


# --------------------------------------------------------------------------- #
# Static UI serving + /api precedence
# --------------------------------------------------------------------------- #


def test_static_fallback_when_ui_not_built(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    client = _client(monkeypatch, ui_dir=tmp_path / "missing-ui")
    res = client.get("/")
    assert res.status_code == 200
    assert "UI not built" in res.text


def test_static_ui_served_when_built(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    ui = tmp_path / "ui-out"
    ui.mkdir()
    (ui / "index.html").write_text("<h1>hello timeline</h1>", encoding="utf-8")
    client = _client(monkeypatch, ui_dir=ui)
    res = client.get("/")
    assert res.status_code == 200
    assert "hello timeline" in res.text


def test_api_takes_precedence_over_static_mount(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    ui = tmp_path / "ui-out"
    ui.mkdir()
    # A file named the same as the API route must NOT shadow /api/events.
    (ui / "index.html").write_text("<h1>static</h1>", encoding="utf-8")
    store.append_event("Bash", [], False, "ok",
                       {"tool_input": {"command": "ls"}},
                       timestamp="2026-08-28T00:01:00+00:00")
    client = _client(monkeypatch, ui_dir=ui)

    api = client.get("/api/events")
    assert api.status_code == 200
    assert api.headers["content-type"].startswith("application/json")
    assert len(api.json()["events"]) == 1

    root = client.get("/")
    assert "static" in root.text
