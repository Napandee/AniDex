"""
Coverage for issue #83 — TOTP-based two-factor authentication for local accounts.

Verified against a real Postgres and a real FastAPI TestClient driving the actual
HTTP routes end to end (not mocked handlers) — same "verify against real Postgres"
pattern as tests/test_recommendation_snooze.py and tests/test_admin_instance_health.py,
extended here to also exercise the real request/response/session cycle, since this
feature is defined almost entirely by its routes (login second-factor prompt, settings
enable/disable flow) rather than by a single extractable pure function.

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available, so `pytest tests/` still collects and passes on a machine with no
Postgres running.

Covers the acceptance-criteria scenarios from issue #83:
  1. Enabling 2FA requires a valid confirmation code (wrong code is rejected, nothing
     is enabled).
  2. Login prompts for and validates a TOTP code once 2FA is enabled for that account.
  3. Login succeeds without any second-factor prompt when 2FA is not enabled.
  4. Disabling 2FA requires re-entering the current password first.
  5. A recovery code consumes correctly and can't be reused (issue #83's chosen
     lost-authenticator recovery mechanism).
"""

import os
import re
import sys
from pathlib import Path

import psycopg2
import pyotp
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()

SECRET_RE = re.compile(r'class="twofa-secret-key">([A-Z2-7]+)<')
RECOVERY_CODE_RE = re.compile(r'<span>([0-9a-f]{4}-[0-9a-f]{4})</span>')


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
    """Import app.main lazily (after DATABASE_URL/SESSION_SECRET_KEY are set) so it
    picks up the throwaway Postgres, same pattern as the other route/query tests in
    this suite."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as m

    return m


@pytest.fixture()
def client(app_module):
    from starlette.testclient import TestClient

    with TestClient(app_module.app) as c:
        yield c


_next_user_id = [1000]


def _make_local_user(pg_conn, password_hash, email=None):
    """Inserts a user row directly (bypassing the registration HTTP flow, which isn't
    what this suite is testing) with a real bcrypt password hash already set, so tests
    can log in against it immediately."""
    _next_user_id[0] += 1
    uid = _next_user_id[0]
    email = email or f"user{uid}@example.com"
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash) "
            "VALUES (%s, 'local', %s, %s, %s)",
            (uid, email, email, password_hash),
        )
    return uid, email


PASSWORD = "correct horse battery staple"


@pytest.fixture()
def bcrypt_hash(app_module):
    return app_module.bcrypt.hashpw(PASSWORD.encode("utf-8"), app_module.bcrypt.gensalt()).decode("utf-8")


@pytest.fixture()
def local_user(pg_conn, bcrypt_hash):
    return _make_local_user(pg_conn, bcrypt_hash)


def _login(client, email, password=PASSWORD):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=False)


def _enable_2fa(client, pg_conn, user_id):
    """Drives the real setup flow end to end: GET the setup page, scrape the secret
    out of the rendered manual-entry fallback (same text a real user would copy), and
    POST back a code computed the same way a real authenticator app would. Returns
    (secret, recovery_codes)."""
    setup_page = client.get("/settings/2fa/setup")
    assert setup_page.status_code == 200
    m = SECRET_RE.search(setup_page.text)
    assert m, "expected the setup page to render the manual-entry secret"
    secret = m.group(1)

    code = pyotp.TOTP(secret).now()
    confirm = client.post("/settings/2fa/setup", data={"code": code}, follow_redirects=False)
    assert confirm.status_code == 303
    assert confirm.headers["location"] == "/settings/2fa/recovery-codes"

    codes_page = client.get("/settings/2fa/recovery-codes")
    assert codes_page.status_code == 200
    codes = RECOVERY_CODE_RE.findall(codes_page.text)
    assert len(codes) == 8

    with pg_conn.cursor() as cur:
        cur.execute("SELECT totp_enabled FROM users WHERE id = %s", (user_id,))
        assert cur.fetchone()[0] is True

    return secret, codes


# ── 1. Enabling 2FA requires a valid confirmation code ─────────────────────────────

def test_enable_2fa_rejects_wrong_code(client, pg_conn, local_user):
    user_id, email = local_user
    _login(client, email)

    setup_page = client.get("/settings/2fa/setup")
    m = SECRET_RE.search(setup_page.text)
    secret = m.group(1)

    # Deliberately wrong code — not derived from the real secret.
    wrong_code = "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"
    resp = client.post("/settings/2fa/setup", data={"code": wrong_code}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/settings/2fa/setup?error=")

    with pg_conn.cursor() as cur:
        cur.execute("SELECT totp_enabled, totp_secret FROM users WHERE id = %s", (user_id,))
        enabled, stored_secret = cur.fetchone()
    assert enabled is False
    assert stored_secret is None


def test_enable_2fa_accepts_valid_code(client, pg_conn, local_user):
    user_id, email = local_user
    _login(client, email)
    _enable_2fa(client, pg_conn, user_id)


# ── 2. Login prompts for and validates a TOTP code when enabled ────────────────────

def test_login_prompts_for_totp_when_enabled(client, pg_conn, local_user):
    user_id, email = local_user
    _login(client, email)
    _enable_2fa(client, pg_conn, user_id)
    client.post("/auth/logout", follow_redirects=False)

    resp = _login(client, email)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login/2fa"

    # Not actually logged in yet — home page must still redirect to login.
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 303
    assert "/auth/login" in home.headers["location"]


def test_login_2fa_rejects_wrong_code_then_accepts_right_one(client, pg_conn, local_user):
    user_id, email = local_user
    _login(client, email)
    secret, _codes = _enable_2fa(client, pg_conn, user_id)
    client.post("/auth/logout", follow_redirects=False)

    _login(client, email)

    wrong = client.post("/auth/login/2fa", data={"code": "000000"}, follow_redirects=False)
    assert wrong.status_code == 303
    assert wrong.headers["location"] == "/auth/login/2fa?error=Invalid+code"

    home = client.get("/", follow_redirects=False)
    assert home.status_code == 303  # still not logged in

    right = client.post(
        "/auth/login/2fa", data={"code": pyotp.TOTP(secret).now()}, follow_redirects=False
    )
    assert right.status_code == 303
    assert right.headers["location"] == "/"

    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200  # now actually logged in


# ── 3. Login succeeds without a TOTP prompt when 2FA is not enabled ────────────────

def test_login_without_2fa_enabled_logs_straight_in(client, local_user):
    _, email = local_user
    resp = _login(client, email)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200


# ── 4. Disabling requires re-auth ───────────────────────────────────────────────────

def test_disable_2fa_requires_correct_password(client, pg_conn, local_user):
    user_id, email = local_user
    _login(client, email)
    _enable_2fa(client, pg_conn, user_id)

    wrong = client.post("/settings/2fa/disable", data={"password": "not the password"}, follow_redirects=False)
    assert wrong.status_code == 303
    assert wrong.headers["location"] == "/settings?twofa_error=Incorrect+password"

    with pg_conn.cursor() as cur:
        cur.execute("SELECT totp_enabled FROM users WHERE id = %s", (user_id,))
        assert cur.fetchone()[0] is True  # still enabled — wrong password didn't disable it

    right = client.post("/settings/2fa/disable", data={"password": PASSWORD}, follow_redirects=False)
    assert right.status_code == 303
    assert right.headers["location"] == "/settings?saved=twofa_disabled"

    with pg_conn.cursor() as cur:
        cur.execute("SELECT totp_enabled, totp_secret FROM users WHERE id = %s", (user_id,))
        enabled, secret = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM totp_recovery_codes WHERE user_id = %s", (user_id,))
        remaining_codes = cur.fetchone()[0]
    assert enabled is False
    assert secret is None
    assert remaining_codes == 0  # recovery codes cleared alongside the secret


def test_disabled_account_logs_in_without_2fa_prompt(client, pg_conn, local_user):
    """Follow-up guard: once disabled, login must go back to the direct (no-2FA) path."""
    user_id, email = local_user
    _login(client, email)
    _enable_2fa(client, pg_conn, user_id)
    client.post("/settings/2fa/disable", data={"password": PASSWORD}, follow_redirects=False)
    client.post("/auth/logout", follow_redirects=False)

    resp = _login(client, email)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


# ── 5. Recovery codes consume correctly and can't be reused ────────────────────────

def test_recovery_code_logs_in_and_cannot_be_reused(client, pg_conn, local_user):
    user_id, email = local_user
    _login(client, email)
    _secret, codes = _enable_2fa(client, pg_conn, user_id)
    client.post("/auth/logout", follow_redirects=False)

    _login(client, email)
    first_use = client.post("/auth/login/2fa", data={"code": codes[0]}, follow_redirects=False)
    assert first_use.status_code == 303
    assert first_use.headers["location"] == "/"

    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200

    client.post("/auth/logout", follow_redirects=False)

    _login(client, email)
    second_use = client.post("/auth/login/2fa", data={"code": codes[0]}, follow_redirects=False)
    assert second_use.status_code == 303
    assert second_use.headers["location"] == "/auth/login/2fa?error=Invalid+code"

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM totp_recovery_codes WHERE user_id = %s AND used_at IS NOT NULL",
            (user_id,),
        )
        assert cur.fetchone()[0] == 1  # exactly the one code got marked used
