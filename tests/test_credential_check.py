"""
Coverage for issue #188 — the redesigned Settings → Credentials cards, their
status pills, and the "Test connection" action.

Two layers, same split test_totp_2fa.py/test_admin_instance_health.py already
use:
  - app.credential_check's check_anilist/check_crunchyroll/check_netflix are
    pure functions wrapping the exact login/auth code the sync scripts run
    (CrunchyrollHistory._login, NetflixHistory._fetch_page, anilist_sync_common's
    gql()) — covered here with the network mocked at the httpx boundary
    (httpx.Client.post/get for CR/Netflix, the module-level httpx.post for
    AniList's gql()), never hitting the real services.
  - The route-level behavior (status pills, /settings/credentials/<provider>
    save endpoints not clobbering each other, /api/credentials/test/<provider>)
    needs a real Postgres + a real FastAPI TestClient driving the actual HTTP
    routes — same "skip entirely if Postgres isn't reachable" pattern as
    test_totp_2fa.py, so `pytest tests/` still collects and passes without one.
"""

import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import sync_crunchyroll as cr_mod
import sync_netflix as nf_mod
import sync_primevideo as pv_mod
import anilist_sync_common as asc_mod

from app import credential_check as cc


# ── Fakes for the httpx boundary ─────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    @property
    def is_success(self):
        return 200 <= self.status_code < 300


# ── check_anilist ─────────────────────────────────────────────────────────────

def test_check_anilist_blank_username_fails_without_network(monkeypatch):
    def fail_if_called(*a, **k):
        raise AssertionError("should never hit the network for a blank username")

    monkeypatch.setattr(asc_mod.httpx, "post", fail_if_called)
    ok, detail = cc.check_anilist("", "some-token")
    assert ok is False
    assert "username" in detail.lower()


def test_check_anilist_valid_credential(monkeypatch):
    monkeypatch.setattr(
        asc_mod.httpx, "post",
        lambda *a, **k: _FakeResponse(200, {"data": {"MediaListCollection": {"hasNextChunk": False}}}),
    )
    ok, detail = cc.check_anilist("napandee", "good-token")
    assert ok is True
    assert "napandee" in detail


def test_check_anilist_invalid_username_fails(monkeypatch):
    monkeypatch.setattr(
        asc_mod.httpx, "post",
        lambda *a, **k: _FakeResponse(200, {"errors": [{"message": "User not found"}]}),
    )
    ok, detail = cc.check_anilist("no-such-user", "some-token")
    assert ok is False
    assert "not found" in detail.lower() or "error" in detail.lower()


# ── check_crunchyroll ─────────────────────────────────────────────────────────

def test_check_crunchyroll_blank_fails_without_network(monkeypatch):
    def fail_if_called(*a, **k):
        raise AssertionError("should never hit the network for a blank credential")

    monkeypatch.setattr(cr_mod.httpx.Client, "post", fail_if_called)
    ok, detail = cc.check_crunchyroll("")
    assert ok is False
    assert "etp_rt" in detail.lower() or "no crunchyroll" in detail.lower()


def test_check_crunchyroll_valid_credential(monkeypatch):
    def fake_post(self, url, **kwargs):
        assert "auth/v1/token" in url
        return _FakeResponse(200, {"access_token": "fake-access-token"})

    def fake_get(self, url, **kwargs):
        assert "accounts/v1/me" in url
        return _FakeResponse(200, {"account_id": "acct-123"})

    monkeypatch.setattr(cr_mod.httpx.Client, "post", fake_post)
    monkeypatch.setattr(cr_mod.httpx.Client, "get", fake_get)
    ok, detail = cc.check_crunchyroll("real-etp-rt-value")
    assert ok is True
    assert "valid" in detail.lower()


def test_check_crunchyroll_invalid_credential(monkeypatch):
    def fake_post(self, url, **kwargs):
        return _FakeResponse(401, text="invalid grant")

    monkeypatch.setattr(cr_mod.httpx.Client, "post", fake_post)
    ok, detail = cc.check_crunchyroll("expired-etp-rt-value")
    assert ok is False
    assert "401" in detail or "login failed" in detail.lower()


# ── check_netflix ─────────────────────────────────────────────────────────────

def test_check_netflix_blank_fails_without_network(monkeypatch):
    def fail_if_called(*a, **k):
        raise AssertionError("should never hit the network for a blank credential")

    monkeypatch.setattr(nf_mod.httpx.Client, "get", fail_if_called)
    ok, detail = cc.check_netflix("", "")
    assert ok is False


def test_check_netflix_valid_credential(monkeypatch):
    def fake_get(self, url, **kwargs):
        return _FakeResponse(200, text='window.netflix = {"BUILD_IDENTIFIER": "abc123"};')

    def fake_post(self, url, **kwargs):
        return _FakeResponse(200, {
            "jsonGraph": {"aui": {"viewingActivity": {"value": {"viewedItems": [{"movieID": 1}]}}}}
        })

    monkeypatch.setattr(nf_mod.httpx.Client, "get", fake_get)
    monkeypatch.setattr(nf_mod.httpx.Client, "post", fake_post)
    ok, detail = cc.check_netflix("NetflixId=abc; SecureNetflixId=def", "profile-guid-123")
    assert ok is True
    assert "valid" in detail.lower()


def test_check_netflix_invalid_credential(monkeypatch):
    def fake_get(self, url, **kwargs):
        # No BUILD_IDENTIFIER in the page — the real signature of an expired/invalid session.
        return _FakeResponse(200, text="<html>please log in</html>")

    monkeypatch.setattr(nf_mod.httpx.Client, "get", fake_get)
    ok, detail = cc.check_netflix("stale-cookie", "profile-guid-123")
    assert ok is False
    assert "build_id" in detail.lower() or "expired" in detail.lower()


# ── check_primevideo ──────────────────────────────────────────────────────────

def test_check_primevideo_blank_fails_without_network(monkeypatch):
    def fail_if_called(*a, **k):
        raise AssertionError("should never hit the network for a blank credential")

    monkeypatch.setattr(pv_mod.httpx.Client, "get", fail_if_called)
    ok, detail = cc.check_primevideo("")
    assert ok is False
    assert "no prime video" in detail.lower()


def test_check_primevideo_valid_credential(monkeypatch):
    def fake_get(self, url, **kwargs):
        assert "getWatchHistorySettingsPage" in url
        return _FakeResponse(200, {"widgets": [{"widgetType": "watch-history"}]})

    monkeypatch.setattr(pv_mod.httpx.Client, "get", fake_get)
    ok, detail = cc.check_primevideo("session-id=abc; ubid-main=def")
    assert ok is True
    assert "valid" in detail.lower()


def test_check_primevideo_invalid_credential(monkeypatch):
    def fake_get(self, url, **kwargs):
        return _FakeResponse(403, text="not authorized")

    monkeypatch.setattr(pv_mod.httpx.Client, "get", fake_get)
    ok, detail = cc.check_primevideo("stale-cookie")
    assert ok is False
    assert "403" in detail


# ── Route-level coverage (real Postgres + real TestClient) ──────────────────


_next_user_id = [2000]


def _make_local_user(pg_conn, app_module, password="testpassword123"):
    _next_user_id[0] += 1
    uid = _next_user_id[0]
    email = f"cred-test-{uid}@example.com"
    pw_hash = app_module.bcrypt.hashpw(password.encode("utf-8"), app_module.bcrypt.gensalt()).decode("utf-8")
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash) "
            "VALUES (%s, 'local', %s, %s, %s)",
            (uid, email, email, pw_hash),
        )
    return uid, email, password


@pytest.fixture()
def logged_in(pg_conn, app_module, client):
    uid, email, password = _make_local_user(pg_conn, app_module)
    resp = client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=False)
    assert resp.status_code == 303
    return uid


def test_status_pill_not_connected_when_nothing_configured(client, logged_in):
    page = client.get("/settings")
    assert page.status_code == 200
    # AniList card is required and shows "Not connected" by default with nothing saved.
    assert "status-chip--cred-not_connected" in page.text


def test_saving_anilist_credential_flips_pill_to_connected(client, logged_in):
    resp = client.post(
        "/settings/credentials/anilist",
        data={"anilist_username": "napandee", "anilist_token": "some-token"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?saved=credentials_anilist"

    page = client.get("/settings")
    assert "status-chip--cred-connected" in page.text


def test_saving_one_card_does_not_clobber_another(client, logged_in, pg_conn):
    """The pre-#188 single shared /settings/credentials endpoint unconditionally
    overwrote anilist_username on every submit — saving just the Crunchyroll card
    must not blank out an already-saved AniList username."""
    client.post(
        "/settings/credentials/anilist",
        data={"anilist_username": "napandee", "anilist_token": "tok"},
        follow_redirects=False,
    )
    resp = client.post(
        "/settings/credentials/crunchyroll",
        data={"cr_etp_rt": "some-etp-rt"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    page = client.get("/settings")
    assert 'value="napandee"' in page.text


def test_test_connection_endpoint_reports_failure_for_bad_credential(client, logged_in, monkeypatch):
    monkeypatch.setattr(
        asc_mod.httpx, "post",
        lambda *a, **k: _FakeResponse(200, {"errors": [{"message": "User not found"}]}),
    )
    resp = client.post(
        "/api/credentials/test/anilist",
        json={"anilist_username": "no-such-user", "anilist_token": "tok"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False


def test_test_connection_endpoint_reports_success_for_good_credential(client, logged_in, monkeypatch):
    monkeypatch.setattr(
        asc_mod.httpx, "post",
        lambda *a, **k: _FakeResponse(200, {"data": {"MediaListCollection": {"hasNextChunk": False}}}),
    )
    resp = client.post(
        "/api/credentials/test/anilist",
        json={"anilist_username": "napandee", "anilist_token": "tok"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True


def test_test_connection_falls_back_to_saved_value_when_field_left_blank(client, logged_in, monkeypatch):
    """Note: this patches CrunchyrollHistory._login (not the underlying
    httpx.Client, unlike the pure unit tests above) — starlette's TestClient
    is itself built on an httpx.Client, so patching httpx.Client.post/get at
    the class level here would intercept the test's own request to the app
    along with CrunchyrollHistory's real one."""
    client.post(
        "/settings/credentials/crunchyroll",
        data={"cr_etp_rt": "already-saved-etp-rt"},
        follow_redirects=False,
    )

    seen = {}

    def fake_login(self):
        seen["etp_rt"] = self.etp_rt
        self._access_token = "fake-token"
        self._account_id = "fake-account"

    monkeypatch.setattr(cr_mod.CrunchyrollHistory, "_login", fake_login)

    resp = client.post("/api/credentials/test/crunchyroll", json={"cr_etp_rt": ""})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert seen["etp_rt"] == "already-saved-etp-rt"


def test_guide_toggle_state_does_not_affect_form_submission(client, logged_in):
    """The "How do I get this?" guide is purely a client-side <details>-style
    disclosure (aria-expanded/.open on a sibling panel) — it must never gate
    whether the credential form underneath it still submits normally. Simulated
    here by simply POSTing the form regardless of the guide's rendered
    aria-expanded state, since there's no server-side coupling between the two
    to break in the first place — this pins that down as a regression guard."""
    page = client.get("/settings")
    assert 'data-cred-toggle="cred-guide-anilist"' in page.text

    resp = client.post(
        "/settings/credentials/anilist",
        data={"anilist_username": "guide-toggle-test", "anilist_token": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    page = client.get("/settings")
    assert 'value="guide-toggle-test"' in page.text
