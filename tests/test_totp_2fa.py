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


def _make_local_user(pg_conn, password_hash, email=None, is_admin=False):
    """Inserts a user row directly (bypassing the registration HTTP flow, which isn't
    what this suite is testing) with a real bcrypt password hash already set, so tests
    can log in against it immediately."""
    _next_user_id[0] += 1
    uid = _next_user_id[0]
    email = email or f"user{uid}@example.com"
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_admin) "
            "VALUES (%s, 'local', %s, %s, %s, %s)",
            (uid, email, email, password_hash, is_admin),
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


# ── Post-#83 security-review regression coverage ────────────────────────────────────
#
# The findings below were caught by a follow-up code review of the original PR, not
# by this test suite — added here specifically so they can't silently regress.


def test_abandoned_2fa_prompt_cannot_hijack_a_later_different_login(client, pg_conn, local_user):
    """Finding #1 (session identity-switch bug): entering account X's correct
    password sets a pending_2fa_user_id=X session key and routes to the second-
    factor prompt. Previously nothing cleared that key on any OTHER path that could
    go on to establish a session — so abandoning the prompt and then logging into a
    DIFFERENT account Y in the same browser session left session["user_id"] == Y but
    pending_2fa_user_id still == X. Revisiting /auth/login/2fa afterward with a valid
    code for X would silently switch the authenticated identity back to X with no
    re-auth of Y at all. _set_authenticated_session now pops pending_2fa_user_id on
    every path that grants a session, closing this off."""
    x_id, x_email = local_user
    _login(client, x_email)
    x_secret, _codes = _enable_2fa(client, pg_conn, x_id)
    client.post("/auth/logout", follow_redirects=False)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT last_login_at FROM users WHERE id = %s", (x_id,))
        x_last_login_before = cur.fetchone()[0]  # set by the setup-flow login above

    # Enter X's correct password — this is the step that plants the stale pending
    # key — then abandon the 2FA prompt entirely.
    step1 = _login(client, x_email)
    assert step1.status_code == 303
    assert step1.headers["location"] == "/auth/login/2fa"

    # Now log into a DIFFERENT account Y in the same browser session (same
    # TestClient / cookie jar), without ever completing X's second factor.
    import bcrypt as _bcrypt

    y_password = "a totally different password 42"
    y_hash = _bcrypt.hashpw(y_password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
    y_id, y_email = _make_local_user(pg_conn, y_hash)

    step2 = client.post("/auth/login", data={"email": y_email, "password": y_password}, follow_redirects=False)
    assert step2.status_code == 303
    assert step2.headers["location"] == "/"

    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200  # logged in as Y now
    with pg_conn.cursor() as cur:
        cur.execute("SELECT last_login_at FROM users WHERE id = %s", (y_id,))
        assert cur.fetchone()[0] is not None  # Y's login actually completed

    # The stale pending_2fa_user_id=X key must be gone — revisiting the 2FA prompt
    # must NOT let X's code silently take over the session.
    prompt = client.get("/auth/login/2fa", follow_redirects=False)
    assert prompt.status_code == 303
    assert prompt.headers["location"] == "/auth/login"  # no pending flow left to resume

    # Even a technically-valid code for X, submitted directly, must not switch the
    # session's identity — there's no pending flow for it to attach to.
    import pyotp as _pyotp

    hijack_attempt = client.post(
        "/auth/login/2fa", data={"code": _pyotp.TOTP(x_secret).now()}, follow_redirects=False
    )
    assert hijack_attempt.status_code == 303
    assert hijack_attempt.headers["location"] == "/auth/login"

    # X's last_login_at must be UNCHANGED from before this whole sequence — proves
    # the session never actually re-authenticated as X, even though the hijack
    # attempt supplied a genuinely valid code for X's secret.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT last_login_at FROM users WHERE id = %s", (x_id,))
        assert cur.fetchone()[0] == x_last_login_before


def test_direct_non_2fa_login_also_clears_stale_pending_key(client, pg_conn, local_user):
    """Defensive half of finding #1: even a direct (no-2FA) login must clear any
    leftover pending_2fa_user_id, not just the register/OAuth paths."""
    x_id, x_email = local_user
    _login(client, x_email)
    x_secret, _codes = _enable_2fa(client, pg_conn, x_id)
    client.post("/auth/logout", follow_redirects=False)

    _login(client, x_email)  # plants pending_2fa_user_id = x_id

    import bcrypt as _bcrypt

    y_password = "yet another password 99"
    y_hash = _bcrypt.hashpw(y_password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
    _y_id, y_email = _make_local_user(pg_conn, y_hash)  # no 2FA — direct login path

    resp = client.post("/auth/login", data={"email": y_email, "password": y_password}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    prompt = client.get("/auth/login/2fa", follow_redirects=False)
    assert prompt.status_code == 303
    assert prompt.headers["location"] == "/auth/login"


# ── Finding #2: /settings/2fa/disable's password check is now rate-limited ─────────

def test_disable_2fa_password_check_is_rate_limited(client, pg_conn, local_user):
    user_id, email = local_user
    _login(client, email)
    _enable_2fa(client, pg_conn, user_id)

    app_module_login_max = 5  # matches _LOGIN_MAX_ATTEMPTS, shared with the login password check
    for _ in range(app_module_login_max):
        resp = client.post("/settings/2fa/disable", data={"password": "definitely wrong"}, follow_redirects=False)
        assert resp.status_code == 303

    with pg_conn.cursor() as cur:
        cur.execute("SELECT failed_login_attempts, locked_until FROM users WHERE id = %s", (user_id,))
        attempts, locked_until = cur.fetchone()
    assert attempts == app_module_login_max
    assert locked_until is not None  # locked out after the same threshold as login

    # Even the CORRECT password is now rejected while locked — this is the actual
    # regression guard: previously there was no lockout at all, so unlimited guesses
    # against a stolen session cookie could eventually strip 2FA off silently.
    locked_attempt = client.post("/settings/2fa/disable", data={"password": PASSWORD}, follow_redirects=False)
    assert locked_attempt.status_code == 303
    assert locked_attempt.headers["location"].startswith("/settings?twofa_error=Too+many+failed+attempts")

    with pg_conn.cursor() as cur:
        cur.execute("SELECT totp_enabled FROM users WHERE id = %s", (user_id,))
        assert cur.fetchone()[0] is True  # still enabled — lockout blocked even the right password


def test_disable_2fa_shares_password_lockout_with_login(client, pg_conn, local_user):
    """The disable-password check and the login-password check draw from the SAME
    failed_login_attempts/locked_until budget (deliberate design choice — this is a
    password check like any other), so exhausting it via one blocks the other too.

    Mirrors the threat model finding #2 is actually about: a stolen SESSION COOKIE
    (still logged in) but not the password — so the session stays authenticated
    throughout the guessing, same as a real hijacked-cookie attacker's would."""
    user_id, email = local_user
    _login(client, email)
    _enable_2fa(client, pg_conn, user_id)

    for _ in range(5):
        client.post("/settings/2fa/disable", data={"password": "nope"}, follow_redirects=False)

    client.post("/auth/logout", follow_redirects=False)

    # Logged out now — but the lockout is on the account (failed_login_attempts/
    # locked_until columns), not the session, so a fresh login attempt with the
    # correct password is also blocked.
    resp = _login(client, email)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/auth/login?error=Too+many+failed+attempts")


# ── Finding #4: password reset must not re-arm the TOTP-code lockout ───────────────

def test_password_reset_does_not_clear_totp_code_lockout(client, pg_conn, local_user):
    user_id, email = local_user
    _login(client, email)
    secret, _codes = _enable_2fa(client, pg_conn, user_id)
    client.post("/auth/logout", follow_redirects=False)

    _login(client, email)
    for _ in range(5):
        client.post("/auth/login/2fa", data={"code": "000000"}, follow_redirects=False)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT totp_failed_attempts, totp_locked_until FROM users WHERE id = %s", (user_id,))
        totp_attempts, totp_locked_until = cur.fetchone()
    assert totp_attempts == 5
    assert totp_locked_until is not None

    # Admin-mediated password reset (issue's existing flow) — insert a usable token
    # directly, same as admin_reset_password would. Stored hashed (issue #358),
    # same as the real insert path — the URL/lookup still uses the raw token.
    from app.sessions import hash_token as _hash_token

    token = "test-reset-token-totp-lockout"
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (%s, %s, now() + interval '1 hour')",
            (_hash_token(token), user_id),
        )
    reset_resp = client.post(
        f"/auth/reset-password/{token}", data={"password": "a brand new password 123"}, follow_redirects=False
    )
    assert reset_resp.status_code == 303

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT failed_login_attempts, locked_until, totp_failed_attempts, totp_locked_until "
            "FROM users WHERE id = %s",
            (user_id,),
        )
        pw_attempts, pw_locked, totp_attempts_after, totp_locked_after = cur.fetchone()

    assert pw_attempts == 0 and pw_locked is None  # the password counter DOES reset, as before
    # ...but the TOTP-code counter must be untouched — this is the actual regression
    # guard: an attacker mid-way through brute-forcing the code must not get a fresh
    # budget just because an unrelated password reset happened.
    assert totp_attempts_after == totp_attempts
    assert totp_locked_after == totp_locked_until


# ── Finding #5: admin can force-disable 2FA for a locked-out user ──────────────────

def test_admin_can_force_disable_2fa(client, pg_conn):
    import bcrypt as _bcrypt

    admin_password = "admin password 123"
    admin_hash = _bcrypt.hashpw(admin_password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
    _admin_id, admin_email = _make_local_user(pg_conn, admin_hash, is_admin=True)

    user_password = "locked out user password"
    user_hash = _bcrypt.hashpw(user_password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
    target_id, target_email = _make_local_user(pg_conn, user_hash)

    # Target enables 2FA, then logs out — one shared TestClient/cookie jar used
    # sequentially (not concurrently), so no second TestClient/lifespan startup is
    # needed for what's really just "two different people using the browser at
    # different times."
    client.post("/auth/login", data={"email": target_email, "password": user_password})
    setup_page = client.get("/settings/2fa/setup")
    secret = SECRET_RE.search(setup_page.text).group(1)
    import pyotp as _pyotp

    client.post("/settings/2fa/setup", data={"code": _pyotp.TOTP(secret).now()})
    client.post("/auth/logout", follow_redirects=False)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT totp_enabled FROM users WHERE id = %s", (target_id,))
        assert cur.fetchone()[0] is True

    # Admin force-disables it — the only path available if the target lost both
    # their authenticator and all recovery codes.
    client.post("/auth/login", data={"email": admin_email, "password": admin_password})
    resp = client.post(f"/admin/users/{target_id}/disable-2fa", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"

    with pg_conn.cursor() as cur:
        cur.execute("SELECT totp_enabled, totp_secret FROM users WHERE id = %s", (target_id,))
        enabled, secret_after = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM totp_recovery_codes WHERE user_id = %s", (target_id,))
        remaining = cur.fetchone()[0]
    assert enabled is False
    assert secret_after is None
    assert remaining == 0

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT action, target_user_id FROM admin_audit_log WHERE action = 'totp_disabled_by_admin'"
        )
        audit_row = cur.fetchone()
    assert audit_row is not None
    assert audit_row[1] == target_id


def test_non_admin_cannot_force_disable_2fa(client, pg_conn, local_user):
    user_id, email = local_user
    _login(client, email)
    _enable_2fa(client, pg_conn, user_id)

    resp = client.post(f"/admin/users/{user_id}/disable-2fa", follow_redirects=False)
    assert resp.status_code == 403

    with pg_conn.cursor() as cur:
        cur.execute("SELECT totp_enabled FROM users WHERE id = %s", (user_id,))
        assert cur.fetchone()[0] is True  # untouched — non-admin was rejected


# ── Finding #7: crossing the TOTP lockout threshold ends the pending flow at once ──

def test_totp_lockout_threshold_immediately_ends_pending_flow(client, pg_conn, local_user):
    user_id, email = local_user
    _login(client, email)
    _enable_2fa(client, pg_conn, user_id)
    client.post("/auth/logout", follow_redirects=False)

    _login(client, email)
    for _ in range(4):
        resp = client.post("/auth/login/2fa", data={"code": "000000"}, follow_redirects=False)
        assert resp.headers["location"] == "/auth/login/2fa?error=Invalid+code"

    # The 5th wrong code crosses the lockout threshold — must redirect straight to
    # /auth/login with the lockout message, not back to /auth/login/2fa.
    fifth = client.post("/auth/login/2fa", data={"code": "000000"}, follow_redirects=False)
    assert fifth.status_code == 303
    assert fifth.headers["location"].startswith("/auth/login?error=Too+many+failed+attempts")

    # And the pending flow is gone immediately — no extra round trip needed.
    prompt = client.get("/auth/login/2fa", follow_redirects=False)
    assert prompt.status_code == 303
    assert prompt.headers["location"] == "/auth/login"


# ── Finding #8: GET /settings/2fa/setup enforces the same TTL as POST ──────────────

def test_setup_page_regenerates_secret_once_pending_state_expires(client, pg_conn, local_user):
    user_id, email = local_user
    _login(client, email)

    first = client.get("/settings/2fa/setup")
    first_secret = SECRET_RE.search(first.text).group(1)

    # Force the in-process pending state to look expired without waiting 10 real
    # minutes — reach into app.main's module-level store directly, the same one the
    # route itself reads/writes.
    import app.main as _m

    state = _m._totp_setup_state[user_id]
    state["started_at"] = state["started_at"] - _m.timedelta(minutes=_m._TOTP_SETUP_TTL_MINUTES + 1)

    second = client.get("/settings/2fa/setup")
    second_secret = SECRET_RE.search(second.text).group(1)
    assert second_secret != first_secret  # GET issued a fresh one instead of re-serving the stale QR

    # And POST accepts a code generated against the FRESH secret the user was just
    # shown — no mismatch between what GET rendered and what POST will accept.
    import pyotp as _pyotp

    confirm = client.post(
        "/settings/2fa/setup", data={"code": _pyotp.TOTP(second_secret).now()}, follow_redirects=False
    )
    assert confirm.status_code == 303
    assert confirm.headers["location"] == "/settings/2fa/recovery-codes"


# ── Post-#82/#83 reconciliation: get_current_user's session-clear must preserve ────
# ── pending_2fa_user_id ─────────────────────────────────────────────────────────────
#
# Issue #82 (server-side session store, merged to main after this branch) rewrote
# get_current_user to unconditionally request.session.clear() whenever it can't
# resolve a valid "sid" — deliberate, meant to scrub stale pre-#82 `{"user_id": N}`
# cookies. But a request mid-way through the TOTP second-factor flow ALSO has no
# "sid" yet (that's the whole point — it isn't authenticated until the code is
# verified), and get_current_user runs on every single page render via the
# _nav_context context processor. Without preserving pending_2fa_user_id across that
# clear, simply loading the /auth/login/2fa PAGE (a GET, before the user has typed
# anything) would silently wipe the pending flow out of the cookie that gets sent
# back — the user would see the code-entry form, but submitting a code afterward
# would already be too late.


def test_viewing_the_2fa_prompt_page_does_not_wipe_the_pending_flow(client, pg_conn, local_user):
    user_id, email = local_user
    _login(client, email)
    secret, _codes = _enable_2fa(client, pg_conn, user_id)
    client.post("/auth/logout", follow_redirects=False)

    login_resp = _login(client, email)
    assert login_resp.status_code == 303
    assert login_resp.headers["location"] == "/auth/login/2fa"

    # This is the step that would trigger the bug: rendering the page runs
    # _nav_context -> get_current_user, which (pre-fix) unconditionally cleared the
    # whole session because there's no "sid" yet at this point in the flow.
    prompt_page = client.get("/auth/login/2fa", follow_redirects=False)
    assert prompt_page.status_code == 200

    # The pending flow must still be intact — submitting a real code now must
    # succeed, not bounce to /auth/login for "no pending flow."
    submit = client.post(
        "/auth/login/2fa", data={"code": pyotp.TOTP(secret).now()}, follow_redirects=False
    )
    assert submit.status_code == 303
    assert submit.headers["location"] == "/"

    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200


def test_viewing_the_2fa_prompt_page_multiple_times_still_preserves_it(client, pg_conn, local_user):
    """Guards against a narrower fix that only preserves the key once (e.g. a
    one-shot flag) rather than on every clear — a user might reload the prompt page,
    check their phone, reload again, etc. before ever submitting a code."""
    user_id, email = local_user
    _login(client, email)
    secret, _codes = _enable_2fa(client, pg_conn, user_id)
    client.post("/auth/logout", follow_redirects=False)

    _login(client, email)
    for _ in range(3):
        page = client.get("/auth/login/2fa", follow_redirects=False)
        assert page.status_code == 200

    submit = client.post(
        "/auth/login/2fa", data={"code": pyotp.TOTP(secret).now()}, follow_redirects=False
    )
    assert submit.status_code == 303
    assert submit.headers["location"] == "/"
