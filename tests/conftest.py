import os
import sys
from pathlib import Path

# scripts/*.py read several env vars at import time (module-level os.environ[...]
# lookups) since they're written to run as standalone subprocess entry points, not
# as an importable package. Set harmless dummy values here — before pytest imports
# any test module — so importing them for testing never needs real credentials or
# a reachable database. Tests that need specific values override via monkeypatch.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("USER_ID", "1")
os.environ.setdefault("ANILIST_TOKEN", "test-token")
os.environ.setdefault("ANILIST_USERNAME", "test-user")
# app.main reads ANILIST_MOCK once at import time (module-level constant) — set
# here, before any test module gets a chance to trigger that first import, so
# tests that exercise the rating/status/progress write paths (issue #208's MCP
# write tools and their underlying routes) never attempt a real AniList call
# just because some other test file happened to import app.main first without
# this set. A previous local attempt at this same thing in
# tests/test_pat_and_mcp_server.py's live_app fixture used the wrong env var
# name (MOCK_ANILIST instead of ANILIST_MOCK) and silently never took effect —
# fixed there too, but this conftest-level default is the one that's actually
# guaranteed to run first.
os.environ.setdefault("ANILIST_MOCK", "1")

# app.config fails loudly at import time if SETTINGS_ENCRYPTION_KEY is missing
# (issue #310 — no safe random fallback, since a key that changes on restart would
# make already-encrypted data permanently unreadable). Generate a fixed dummy key
# once here, before any test module imports app.config/app.main, so the whole
# suite gets a valid Fernet key without needing a real one. Tests that specifically
# exercise the "missing/invalid key" failure path override this via monkeypatch +
# a fresh subprocess/reimport, since app.config only evaluates the env var once at
# import time.
if "SETTINGS_ENCRYPTION_KEY" not in os.environ:
    from cryptography.fernet import Fernet

    os.environ["SETTINGS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import psycopg2
import pytest

# ── Shared Postgres-backed app/client fixtures (issue #464) ─────────────────
#
# Before this, ~33-63 individual test files each independently redefined the
# same `pg_conn`/`app_module`/`client` trio (identical DROP-SCHEMA-and-reload
# logic, identical TestClient wiring, only a cosmetically different dummy
# SESSION_SECRET_KEY string per file — nothing in the suite ever asserts on
# that value, so a single shared one is exactly as good). Centralizing it here
# means a real bug in the shared setup (e.g. #310's SETTINGS_ENCRYPTION_KEY
# handling, or a future schema-reset change) only needs fixing once, instead
# of risking silent per-file drift the way scattered fixtures let the fastapi/
# starlette #50 incident's smoke test look like real verification without
# actually being it (see CLAUDE.local.md's "Dependency PR review process").
#
# A file with its own reasons to differ (a non-standard schema reset, extra
# app_module setup) is unaffected — this only replaces exact duplicates.

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()


def _try_connect():
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=2)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except Exception:
        return None


@pytest.fixture(scope="module")
def pg_conn():
    conn = _try_connect()
    if conn is None:
        pytest.skip(
            f"No reachable Postgres at {DATABASE_URL} — this suite needs a real "
            "throwaway instance (same one .github/workflows/pr-validate.yml provisions)."
        )
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(SCHEMA_SQL)
    yield conn
    conn.close()


@pytest.fixture()
def app_module(pg_conn, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as m

    return m


@pytest.fixture()
def client(app_module):
    from starlette.testclient import TestClient

    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_ip_login_rate_limit_state():
    """app.main._ip_login_attempts (issue #359's IP-aggregate login throttle) is
    process-global, in-memory state — module caching means every test file's
    `import app.main as m` returns the same object, so /auth/login attempts made by
    unrelated tests earlier in the same pytest run would otherwise accumulate toward
    the same per-IP budget (most tests share the TestClient's default IP, since only
    tests written with the throttle specifically in mind pass X-Forwarded-For) and
    start spuriously 429ing completely unrelated login-driven tests partway through
    the suite. Reset it before every test rather than relying on each file's own
    fixtures to remember to."""
    m = sys.modules.get("app.main")
    if m is not None:
        m._ip_login_attempts.clear()
    yield

# ── CSRF-transparent TestClient (issue #312) ────────────────────────────────
#
# Issue #312 added a global CSRF-token requirement on every state-changing route
# (app/main.py's _csrf_protect dependency). Close to 200 existing tests across
# this suite drive the app through `starlette.testclient.TestClient` instances
# built by ~20+ near-identical, independently-defined `client`/`app_module`
# fixtures scattered across individual test files (no single shared fixture to
# patch instead) — each one calling client.post/put/patch/delete(...) the way a
# real browser's fetch()/<form> would, but with no CSRF token attached, since
# none of that test code existed with CSRF in mind.
#
# Fixing this by hand-editing every one of those call sites (or every
# _register_and_login/_login helper) would be enormous and easy to get
# partially wrong. Instead, this monkeypatches TestClient.request itself, once,
# here — since `.post()`/`.put()`/`.patch()`/`.delete()`/`.get()` all funnel
# through `self.request(...)` internally (httpx.Client's own implementation),
# patching this one method transparently covers literally every TestClient
# instance created anywhere in the suite, regardless of which file's fixture
# constructed it or which convenience method a test calls.
#
# What it does, for any unsafe-method (POST/PUT/PATCH/DELETE) call: lazily mints
# one fixed per-client token, splices it directly into the client's signed
# "session" cookie (merging with — never clobbering — whatever's already in
# there: "sid", pending_2fa_user_id, etc.) using the SAME encoding
# SessionMiddleware itself uses, then attaches that same token as the
# X-CSRF-Token header on the request. The session's secret key is read straight
# off the already-registered SessionMiddleware instance
# (app.user_middleware / Middleware(...).kwargs["secret_key"]) rather than
# assumed to be some fixed string — this suite's many per-file app_module
# fixtures set SESSION_SECRET_KEY to different values (or don't set it at all,
# falling back to app.main's own random-per-process key), and since app.main is
# only ever really imported once per pytest process (module caching), whichever
# value won that race is the one that's actually live — introspecting it
# directly is the only way this works unconditionally.
#
# This deliberately does NOT touch real HTTP at all to bootstrap the token (no
# extra GET request) — an early version of this tried seeding the token via a
# real `GET /auth/login` first, but that route calls _no_users_exist() (a real
# DB query) before rendering, which risked colliding with the narrow,
# call-order-sensitive db.fetchone/db.fetchall mocks several existing tests
# already rely on for unrelated reasons. Constructing the cookie value directly
# sidesteps that entirely — zero extra requests, zero DB touches.
#
# A test that specifically wants to exercise the real, unprotected/rejected
# path (issue #312's own acceptance criterion: "a real live test... confirm
# it's rejected") can pass `skip_csrf_autoinject=True` to bypass this and drive
# the real, undoctored flow — see tests/test_csrf_protection.py, which is
# exactly what verifies this app's actual CSRF protection is real and not just
# assumed-away by this fixture.
import base64
import json

import httpx
from itsdangerous import TimestampSigner
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

_CSRF_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_REAL_TESTCLIENT_REQUEST = TestClient.request


def _session_secret_key(asgi_app):
    """The secret_key this app's already-registered SessionMiddleware instance
    was actually constructed with — see the module comment above for why this
    can't just be a hardcoded guess. None if the app has no SessionMiddleware
    at all (shouldn't happen for any real app_module fixture in this suite)."""
    for mw in getattr(asgi_app, "user_middleware", []):
        if mw.cls is SessionMiddleware:
            return mw.kwargs.get("secret_key")
    return None


def _csrf_autoinject_request(self, method, url, **kwargs):
    if kwargs.pop("skip_csrf_autoinject", False):
        return _REAL_TESTCLIENT_REQUEST(self, method, url, **kwargs)

    if method.upper() in _CSRF_UNSAFE_METHODS:
        secret_key = _session_secret_key(getattr(self, "app", None))
        if secret_key is not None:
            token = getattr(self, "_csrf_test_token", None)
            if token is None:
                token = "test-suite-csrf-token"
                self._csrf_test_token = token
                signer = TimestampSigner(str(secret_key))
                session_dict = {}
                existing_raw = self.cookies.get("session")
                if existing_raw:
                    try:
                        unsigned = signer.unsign(existing_raw.encode("utf-8"), max_age=None)
                        session_dict = json.loads(base64.b64decode(unsigned))
                    except Exception:
                        # Garbage/foreign-signature cookie (e.g. a test that
                        # deliberately seeded an invalid one) — start fresh
                        # rather than propagating an unrelated failure into
                        # every unsafe-method call this client ever makes.
                        session_dict = {}
                session_dict["csrf_token"] = token
                payload = base64.b64encode(json.dumps(session_dict).encode("utf-8"))
                self.cookies.set("session", signer.sign(payload).decode("utf-8"))

            headers = httpx.Headers(kwargs.get("headers"))
            if "x-csrf-token" not in headers:
                headers["X-CSRF-Token"] = token
                kwargs["headers"] = headers

    return _REAL_TESTCLIENT_REQUEST(self, method, url, **kwargs)


TestClient.request = _csrf_autoinject_request
