"""
Coverage for issue #314 — security review of the export endpoints.

GET /api/export (self-service) and GET /admin/export-all (admin, loops every
user) both return real personal library data and, before this change, had zero
in-app throttle: an authenticated session (the account's own, or a stolen/
scripted one) could call either endpoint as fast as it liked. Cloudflare
Access sits in front of this instance (see CLAUDE.local.md) but that's a
login/identity gate, not a request-rate control — it doesn't limit repeated
calls from an already-authenticated session, so it doesn't cover this gap on
its own.

Fix: an in-memory, per-user, trailing-window rate limiter
(`_check_export_rate_limit` in app/main.py) applied to both routes — 10
requests/60s for /api/export, 3 requests/300s for the heavier /admin/export-all
(it loops the same query over every user in a single call). Same
single-process assumption as the existing `_totp_setup_state` in-memory dict
(no --workers flag, see Dockerfile CMD).

Needs a reachable Postgres via DATABASE_URL (same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions, same as tests/test_admin_invites.py)
— skipped entirely if one isn't available.
"""

import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()


@pytest.fixture()
def app_client(pg_conn, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    import app.main as m
    from fastapi.testclient import TestClient

    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE library_entries, personal_notes, anime, sessions, users RESTART IDENTITY CASCADE")

    # Global in-memory rate-limit state must not leak between tests.
    m._export_rate_limit_state.clear()

    return TestClient(m.app), m


def _make_user(m, email="user@example.com", password="password123", is_admin=False):
    m.db.execute(
        "INSERT INTO users (auth_provider, auth_provider_id, email, password_hash, is_admin, is_active) "
        "VALUES ('local', %s, %s, %s, %s, true)",
        (email, email, m.bcrypt.hashpw(password.encode(), m.bcrypt.gensalt()).decode(), is_admin),
    )


def _login(client, email="user@example.com", password="password123"):
    resp = client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=False)
    assert resp.status_code == 303


def test_export_allows_requests_within_the_limit(app_client):
    client, m = app_client
    _make_user(m)
    _login(client)

    for _ in range(10):
        resp = client.get("/api/export")
        assert resp.status_code == 200


def test_export_throttles_after_the_limit_is_exceeded(app_client):
    """Acceptance criterion: repeated requests actually get throttled."""
    client, m = app_client
    _make_user(m)
    _login(client)

    for _ in range(10):
        assert client.get("/api/export").status_code == 200

    resp = client.get("/api/export")
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) > 0


def test_export_rate_limit_is_scoped_per_user(app_client):
    """One account hitting its limit must not block a different account."""
    client, m = app_client
    _make_user(m, email="hammering@example.com")
    _make_user(m, email="other@example.com")

    _login(client, email="hammering@example.com")
    for _ in range(10):
        assert client.get("/api/export").status_code == 200
    assert client.get("/api/export").status_code == 429

    client.post("/auth/logout")
    _login(client, email="other@example.com")
    assert client.get("/api/export").status_code == 200


def test_admin_export_all_throttles_after_its_tighter_limit(app_client):
    """/admin/export-all loops every user's export in one call, so it gets a
    tighter budget (3/300s) than the single-user /api/export (10/60s)."""
    client, m = app_client
    _make_user(m, email="admin@example.com", is_admin=True)
    _login(client, email="admin@example.com")

    for _ in range(3):
        resp = client.get("/admin/export-all")
        assert resp.status_code == 200

    resp = client.get("/admin/export-all")
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


def test_export_rate_limit_helper_recovers_after_window_elapses(monkeypatch):
    """Unit-level check of _check_export_rate_limit itself: once the trailing
    window has fully elapsed, a previously-blocked key is allowed again."""
    import app.main as m

    state = {}
    monkeypatch.setattr(m, "_export_rate_limit_state", state)

    t = [1000.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: t[0])

    allowed, _ = m._check_export_rate_limit("k", max_requests=2, window_seconds=10)
    assert allowed
    allowed, _ = m._check_export_rate_limit("k", max_requests=2, window_seconds=10)
    assert allowed
    allowed, retry_after = m._check_export_rate_limit("k", max_requests=2, window_seconds=10)
    assert not allowed
    assert retry_after > 0

    t[0] += 10.5  # advance past the window
    allowed, _ = m._check_export_rate_limit("k", max_requests=2, window_seconds=10)
    assert allowed
