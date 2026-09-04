"""Security tests for the local restory server (Issue 1).

Every ``/api/*`` request must prove a per-session token, an acceptable Origin
(when present), and a loopback Host. These tests drive the FastAPI app through
Starlette's ``TestClient``; the ``base_url`` determines the ``Host`` header, so a
foreign base_url simulates a DNS-rebinding request.

A real shadow repo with a pending undo is set up so the *success* case actually
reverts a file — and, conversely, the blocked cases can assert that **no**
filesystem change occurred.
"""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from restory import server, snapshot

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)

PORT = 8765
TOKEN = "unit-test-token-value-not-a-real-secret"
GOOD_BASE = f"http://127.0.0.1:{PORT}"
GOOD_ORIGIN = f"http://127.0.0.1:{PORT}"


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
