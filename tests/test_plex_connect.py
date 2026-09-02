"""
Coverage for issue #153's Plex OAuth PIN connect flow — the /settings/plex/*
routes in app/main.py. The actual plex.tv network calls live in
app/plex_auth.py (create_pin/poll_pin/list_server_resources/pick_connection);
mocked here at that module boundary, same split test_credential_check.py
already uses for Crunchyroll/Netflix's own httpx boundary.

Needs a real Postgres + a real FastAPI TestClient driving the actual HTTP
routes — same "skip entirely if Postgres isn't reachable" pattern as
test_credential_check.py, so `pytest tests/` still collects and passes
without one.
"""

import os
from pathlib import Path

import psycopg2
import pytest


_next_user_id = [3000]


def _make_local_user(pg_conn, app_module, password="testpassword123"):
    _next_user_id[0] += 1
    uid = _next_user_id[0]
    email = f"plex-test-{uid}@example.com"
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


def test_settings_page_shows_plex_not_connected_by_default(client, logged_in):
    page = client.get("/settings")
    assert page.status_code == 200
    assert 'data-provider="plex"' in page.text
    assert "Connect Plex" in page.text  # i18n key resolved to real copy, not just present as an id
    assert "plex-connect-btn" in page.text


def test_connect_returns_pin_and_auth_url(client, logged_in, app_module, monkeypatch):
    monkeypatch.setattr(
        app_module.plex_auth, "create_pin",
        lambda: {"id": 12345, "code": "ABCD", "auth_url": "https://app.plex.tv/auth#?code=ABCD"},
    )
    resp = client.post("/settings/plex/connect")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["pin_id"] == 12345
    assert data["auth_url"] == "https://app.plex.tv/auth#?code=ABCD"


def test_connect_requires_login(client):
    resp = client.post("/settings/plex/connect")
    assert resp.status_code in (401, 403)


def test_poll_reports_pending_before_the_pin_is_claimed(client, logged_in, app_module, monkeypatch):
    monkeypatch.setattr(app_module.plex_auth, "poll_pin", lambda pin_id: None)
    resp = client.get("/settings/plex/poll/12345")
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending"}


def test_poll_stores_credentials_and_reports_connected_once_claimed(client, logged_in, app_module, monkeypatch, pg_conn):
    monkeypatch.setattr(app_module.plex_auth, "poll_pin", lambda pin_id: "real-account-token")
    monkeypatch.setattr(
        app_module.plex_auth, "list_server_resources",
        lambda account_token: [{
            "name": "My Plex Server",
            "accessToken": "real-server-token",
            "connections": [{"uri": "https://192-168-1-5.example.plex.direct:32400", "local": True}],
        }],
    )
    monkeypatch.setattr(app_module.plex_auth, "pick_connection", lambda server, timeout=3.0: server["connections"][0]["uri"])

    resp = client.get("/settings/plex/poll/12345")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "connected"
    assert data["server_name"] == "My Plex Server"

    # Stored (encrypted for the two token fields) via the real config module —
    # decrypt to confirm the actual round-trip, not just that *some* value landed.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT key, value FROM settings WHERE user_id = %s", (logged_in,))
        rows = {r[0]: r[1] for r in cur.fetchall()}
    assert app_module.config.decrypt_secret(rows["plex_account_token"]) == "real-account-token"
    assert app_module.config.decrypt_secret(rows["plex_server_token"]) == "real-server-token"
    assert rows["plex_server_base_url"] == "https://192-168-1-5.example.plex.direct:32400"
    assert rows["plex_server_name"] == "My Plex Server"

    # Settings page now shows the connected state, not the Connect button
    # (the polling JS's own `getElementById('plex-connect-btn')` call is
    # static markup present either way, so check the actual button tag).
    page = client.get("/settings")
    assert 'id="plex-connect-btn"' not in page.text
    assert "status-chip--cred-connected" in page.text


def test_poll_reports_error_when_no_server_found(client, logged_in, app_module, monkeypatch):
    monkeypatch.setattr(app_module.plex_auth, "poll_pin", lambda pin_id: "real-account-token")
    monkeypatch.setattr(app_module.plex_auth, "list_server_resources", lambda account_token: [])

    resp = client.get("/settings/plex/poll/12345")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "no Plex server" in data["error"]


def test_disconnect_clears_all_four_settings_keys(client, logged_in, app_module, pg_conn):
    for key, value in [
        ("plex_account_token", "acct"), ("plex_server_token", "srv"),
        ("plex_server_base_url", "https://example.plex.direct:32400"),
        ("plex_server_name", "My Server"),
    ]:
        app_module.config.set_value(logged_in, key, value)

    resp = client.post("/settings/plex/disconnect", follow_redirects=False)
    assert resp.status_code == 303
    assert "saved=plex_disconnected" in resp.headers["location"]

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM settings WHERE user_id = %s AND key = 'plex_server_name'",
            (logged_in,),
        )
        row = cur.fetchone()
    assert row[0] == ""
