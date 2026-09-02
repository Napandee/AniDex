"""
Coverage for issue #359 — /auth/login (and /auth/login/2fa) had no aggregate,
per-IP throttle, only the existing per-account lockout (_LOGIN_MAX_ATTEMPTS/
_LOGIN_LOCKOUT_MINUTES). An attacker with a list of valid invited emails could
still fire unlimited attempts spread across many different accounts from a
single IP, since each account's own failed_login_attempts counter is untouched
by requests against other accounts.

Verified against a real Postgres and a real FastAPI TestClient driving the
actual HTTP routes end to end — same pattern as tests/test_totp_2fa.py, which
this file borrows its fixtures from.

Covers:
  1. Enough attempts from one IP, spread across many different accounts,
     eventually get throttled (429), independent of any single account's own
     lockout state.
  2. Normal usage (a handful of attempts) is never throttled.
  3. Two different source IPs are tracked independently — one IP hitting the
     threshold doesn't throttle a different IP.
  4. The throttle also covers /auth/login/2fa, not just /auth/login.
  5. The existing per-account lockout behavior is untouched by this change.
"""

import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


_next_user_id = [2000]


def _make_local_user(pg_conn, password_hash, email=None):
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


def _login(client, email, password=PASSWORD, ip="203.0.113.1"):
    return client.post(
        "/auth/login",
        data={"email": email, "password": password},
        headers={"x-forwarded-for": ip},
        follow_redirects=False,
    )


# ── 1. Many attempts across many accounts from one IP get throttled ────────────────

def test_many_attempts_across_different_accounts_from_one_ip_get_throttled(client, pg_conn, app_module, bcrypt_hash):
    ip = "198.51.100.7"
    max_attempts = app_module._IP_LOGIN_MAX_ATTEMPTS

    # Spread failed attempts across brand-new accounts each time, so no single
    # account's own _LOGIN_MAX_ATTEMPTS (5) lockout ever triggers — proves the
    # throttle is really IP-aggregate, not just the existing per-account one.
    last_resp = None
    for i in range(max_attempts + 1):
        _, email = _make_local_user(pg_conn, bcrypt_hash)
        last_resp = _login(client, email, password="wrong-password", ip=ip)

    assert last_resp.status_code == 429
    assert "Too many login attempts" in last_resp.text


def test_throttle_also_fires_for_a_mix_of_correct_and_incorrect_passwords(client, pg_conn, app_module, bcrypt_hash):
    """Every attempt counts toward the IP budget, not just failed ones — a real
    credential-stuffing run interleaves guesses with occasional real hits."""
    ip = "198.51.100.8"
    max_attempts = app_module._IP_LOGIN_MAX_ATTEMPTS

    last_resp = None
    for i in range(max_attempts + 1):
        _, email = _make_local_user(pg_conn, bcrypt_hash)
        password = PASSWORD if i % 3 == 0 else "wrong-password"
        last_resp = _login(client, email, password=password, ip=ip)

    assert last_resp.status_code == 429


# ── 2. Normal usage is never throttled ──────────────────────────────────────────────

def test_a_handful_of_attempts_is_not_throttled(client, pg_conn, app_module, bcrypt_hash):
    ip = "198.51.100.9"
    _, email = _make_local_user(pg_conn, bcrypt_hash)

    for _ in range(3):
        resp = _login(client, email, password="wrong-password", ip=ip)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login?error=Invalid+email+or+password"

    good = _login(client, email, ip=ip)
    assert good.status_code == 303
    assert good.headers["location"] == "/"


# ── 3. Different source IPs are tracked independently ──────────────────────────────

def test_different_ips_have_independent_budgets(client, pg_conn, app_module, bcrypt_hash):
    ip_a = "198.51.100.10"
    ip_b = "198.51.100.11"
    max_attempts = app_module._IP_LOGIN_MAX_ATTEMPTS

    # Exhaust ip_a's budget.
    for _ in range(max_attempts + 1):
        _, email = _make_local_user(pg_conn, bcrypt_hash)
        _login(client, email, password="wrong-password", ip=ip_a)

    _, email_a = _make_local_user(pg_conn, bcrypt_hash)
    resp_a = _login(client, email_a, password="wrong-password", ip=ip_a)
    assert resp_a.status_code == 429

    # ip_b has made zero requests so far — must not be affected by ip_a's throttle.
    _, email_b = _make_local_user(pg_conn, bcrypt_hash)
    resp_b = _login(client, email_b, ip=ip_b)
    assert resp_b.status_code == 303
    assert resp_b.headers["location"] == "/"


# ── 4. The throttle also covers /auth/login/2fa ────────────────────────────────────

def test_ip_throttle_also_applies_to_2fa_verification_endpoint(client, pg_conn, app_module, bcrypt_hash):
    import pyotp

    ip = "198.51.100.12"
    user_id, email = _make_local_user(pg_conn, bcrypt_hash)
    _login(client, email, ip=ip)

    setup_page = client.get("/settings/2fa/setup", headers={"x-forwarded-for": ip})
    import re

    secret = re.search(r'class="twofa-secret-key">([A-Z2-7]+)<', setup_page.text).group(1)
    code = pyotp.TOTP(secret).now()
    confirm = client.post(
        "/settings/2fa/setup", data={"code": code}, headers={"x-forwarded-for": ip}, follow_redirects=False
    )
    assert confirm.status_code == 303

    client.post("/auth/logout", headers={"x-forwarded-for": ip}, follow_redirects=False)
    _login(client, email, ip=ip)  # re-enters the pending-2fa flow

    max_attempts = app_module._IP_LOGIN_MAX_ATTEMPTS
    # /auth/login above already used one slot on this IP's budget this test —
    # only need a few more wrong-code posts to cross the threshold.
    last_resp = None
    for _ in range(max_attempts):
        last_resp = client.post(
            "/auth/login/2fa", data={"code": "000000"}, headers={"x-forwarded-for": ip}, follow_redirects=False
        )
        if last_resp.status_code == 429:
            break

    assert last_resp.status_code == 429
    assert "Too many login attempts" in last_resp.text


# ── 5. The existing per-account lockout is untouched ───────────────────────────────

def test_per_account_lockout_still_fires_independently_of_ip_throttle(client, pg_conn, app_module, bcrypt_hash):
    ip = "198.51.100.13"
    _, email = _make_local_user(pg_conn, bcrypt_hash)

    last_resp = None
    # One extra request beyond the threshold: the lockout is written to the DB on
    # the Nth failed attempt, but that same response still reflects pre-lockout
    # state — the lockout message only appears starting on request N+1.
    for _ in range(app_module._LOGIN_MAX_ATTEMPTS + 1):
        last_resp = _login(client, email, password="wrong-password", ip=ip)

    # Locked out on the ACCOUNT, well before the much-higher IP-wide threshold.
    assert last_resp.status_code == 303
    assert "Too+many+failed+attempts" in last_resp.headers["location"]
