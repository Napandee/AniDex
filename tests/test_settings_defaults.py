"""
Coverage for issue #258 — two Settings page default-state bugs, found together
2026-08-21:

  1. The AniList credentials card (`app/templates/settings.html` line 209)
     hardcoded `{% set al_expanded = true %}` instead of computing it the same
     way its Crunchyroll/Netflix siblings do — so it always rendered expanded
     regardless of whether it needed attention.
  2. The three notification toggles (telegram/discord/ntfy_enabled, lines
     592/623/645) used `settings.X_enabled != 'false'`, which defaults to
     *checked* for a never-configured (unset/None) value, since `None !=
     'false'` is True in Jinja. Should be `== 'true'` — checked only when a
     user has explicitly saved it enabled.

Same real-Postgres + real-TestClient pattern as tests/test_settings_build_version.py
and tests/test_credential_check.py's route-level layer: skipped entirely if no
Postgres is reachable, so `pytest tests/` still collects and passes without one.

The scenario most worth real coverage per the issue: a channel/card a user has
*explicitly* saved (true or false) must keep rendering that real saved choice —
this fix must only change the never-configured default, not stored preferences.
"""

import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM users")


def _register_and_login(client, email, password="correct horse battery staple"):
    resp = client.post(
        "/auth/register",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"


def _anilist_card_expanded(html: str) -> bool:
    """The AniList card's collapsible body carries a conditional 'open' class
    on the help-disclosure-panel div with id="cred-body-anilist"."""
    marker = '<div class="cred-card-body help-disclosure-panel open" id="cred-body-anilist">'
    return marker in html


def _anilist_header_aria_expanded(html: str) -> str:
    import re

    m = re.search(
        r'data-cred-toggle="cred-body-anilist" aria-expanded="(true|false)"', html
    )
    assert m, "could not find AniList card header toggle button in rendered HTML"
    return m.group(1)


def _checkbox_checked(html: str, name: str) -> bool:
    import re

    m = re.search(rf'<input type="checkbox" name="{name}"[^>]*>', html)
    assert m, f"could not find {name} checkbox in rendered HTML"
    return "checked" in m.group(0)


# ── AniList card default expand state ───────────────────────────────────────


def test_anilist_card_collapsed_by_default_when_never_configured(pg_conn, app_module, client):
    _register_and_login(client, "owner@example.com")

    resp = client.get("/settings")
    assert resp.status_code == 200
    assert not _anilist_card_expanded(resp.text), (
        "AniList card should start collapsed for a fresh user, same as Crunchyroll/Netflix"
    )
    assert _anilist_header_aria_expanded(resp.text) == "false"


def test_anilist_card_expanded_immediately_after_save(pg_conn, app_module, client):
    _register_and_login(client, "owner@example.com")

    resp = client.post(
        "/settings/credentials/anilist",
        data={"anilist_username": "someuser", "anilist_token": "tok123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?saved=credentials_anilist"

    resp = client.get("/settings?saved=credentials_anilist")
    assert resp.status_code == 200
    assert _anilist_card_expanded(resp.text), (
        "AniList card should expand right after its own credentials save, "
        "mirroring the Crunchyroll/Netflix cards' saved== convention"
    )


def test_anilist_card_expanded_when_needs_attention(pg_conn, app_module, client):
    _register_and_login(client, "owner@example.com")

    import json

    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", ("owner@example.com",))
        user_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (%s, %s, %s), (%s, %s, %s) "
            "ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value",
            (user_id, "anilist_username", "someuser", user_id, "anilist_token", "tok123"),
        )
        cur.execute(
            "INSERT INTO sync_log (user_id, type, status, run_at, steps) VALUES (%s, %s, %s, NOW(), %s)",
            (
                user_id,
                "full_sync",
                "error",
                json.dumps([{"service": "anilist_postgres", "status": "error"}]),
            ),
        )

    resp = client.get("/settings")
    assert resp.status_code == 200
    assert _anilist_card_expanded(resp.text), (
        "AniList card should expand when its most recent sync step errored, "
        "same as the Crunchyroll/Netflix needs_attention behavior"
    )


# ── Notification toggle default state ───────────────────────────────────────


def test_notification_toggles_unchecked_by_default_when_never_configured(pg_conn, app_module, client):
    _register_and_login(client, "owner@example.com")

    resp = client.get("/settings")
    assert resp.status_code == 200
    assert not _checkbox_checked(resp.text, "telegram_enabled"), (
        "telegram_enabled should be unchecked before any notification settings save"
    )
    assert not _checkbox_checked(resp.text, "discord_enabled"), (
        "discord_enabled should be unchecked before any notification settings save"
    )
    assert not _checkbox_checked(resp.text, "ntfy_enabled"), (
        "ntfy_enabled should be unchecked before any notification settings save"
    )


def test_notification_toggle_explicit_true_persists_across_reload(pg_conn, app_module, client):
    _register_and_login(client, "owner@example.com")

    resp = client.post(
        "/settings/notifications",
        data={
            "telegram_enabled": "on",
            "telegram_bot_token": "tok",
            "telegram_chat_id": "123",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # Fresh GET, not just the redirect target — proves the *stored* value
    # renders correctly, not merely a saved== query-flag artifact.
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert _checkbox_checked(resp.text, "telegram_enabled"), (
        "an explicitly-saved enabled telegram toggle must still render checked"
    )
    # Untouched siblings remain at their never-configured default.
    assert not _checkbox_checked(resp.text, "discord_enabled")
    assert not _checkbox_checked(resp.text, "ntfy_enabled")


def test_notification_toggle_explicit_false_persists_across_reload(pg_conn, app_module, client):
    _register_and_login(client, "owner@example.com")

    # Enable, then explicitly disable (omit the field, matching an unchecked
    # HTML checkbox submission) — must render unchecked, and this must be a
    # real stored 'false', not just re-hitting the never-configured default.
    client.post(
        "/settings/notifications",
        data={"telegram_enabled": "on", "telegram_bot_token": "tok", "telegram_chat_id": "123"},
        follow_redirects=False,
    )
    resp = client.post(
        "/settings/notifications",
        data={"telegram_bot_token": "tok", "telegram_chat_id": "123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.get("/settings")
    assert resp.status_code == 200
    assert not _checkbox_checked(resp.text, "telegram_enabled"), (
        "an explicitly-saved disabled telegram toggle must render unchecked"
    )

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT s.value FROM settings s JOIN users u ON u.id = s.user_id "
            "WHERE u.email = %s AND s.key = 'telegram_enabled'",
            ("owner@example.com",),
        )
        row = cur.fetchone()
    assert row is not None and row[0] == "false", (
        "expected a real stored 'false', not just an absent/never-configured row"
    )


def test_all_three_notification_channels_independently_true_and_false(pg_conn, app_module, client):
    """Same pattern, all three channels at once, one enabled/two disabled and
    vice versa — guards against a copy-paste bug that ties the three toggles
    together instead of keeping them independent."""
    _register_and_login(client, "owner@example.com")

    resp = client.post(
        "/settings/notifications",
        data={
            "discord_enabled": "on",
            "discord_webhook_url": "https://discord.com/api/webhooks/123/abcDEF",
            "ntfy_server_url": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.get("/settings")
    assert resp.status_code == 200
    assert not _checkbox_checked(resp.text, "telegram_enabled")
    assert _checkbox_checked(resp.text, "discord_enabled")
    assert not _checkbox_checked(resp.text, "ntfy_enabled")
