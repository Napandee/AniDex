"""
Coverage for issue #319 — encrypting telegram_bot_token/discord_webhook_url/
ntfy_auth_token at rest (extends #310's ENCRYPTED_KEYS allowlist to these three
notification-channel credentials).

The allowlist/round-trip/migration coverage for these three keys lives in
tests/test_issue_310_credential_encryption.py (that file's own tests were
extended, not duplicated, since it already owns the generic
encrypt_secret()/decrypt_secret()/ENCRYPTED_KEYS/migrate_settings() machinery
these three keys reuse unchanged).

This file covers the one thing that's genuinely specific to #319: the
acceptance criterion that "a real Telegram/Discord/ntfy notification send
still succeeds using the encrypted-then-decrypted value" — i.e. that
app/notify.py's three Channel.send() implementations, which call
config.get(user_id, ...) for their credentials, actually receive the real
decrypted plaintext and use it correctly in the outbound HTTP call. Mocks
httpx.post (no real network call) and asserts on what was actually sent, not
just that config.get() returns the right string in isolation — proving the
encryption layer is transparent all the way through to a real send, not just
to the config module's own return value.

Needs a reachable Postgres via DATABASE_URL, same skip-if-unavailable pattern
tests/test_issue_310_credential_encryption.py uses.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()

from app import config, notify


_next_user_id = [7000]


def _make_user(pg_conn):
    _next_user_id[0] += 1
    uid = _next_user_id[0]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, is_admin) "
            "VALUES (%s, 'local', %s, %s, false)",
            (uid, f"user{uid}", f"user{uid}@example.com"),
        )
    return uid


def test_telegram_send_uses_the_real_decrypted_token(pg_conn):
    uid = _make_user(pg_conn)
    config.set_value(uid, "telegram_bot_token", "123456789:REAL_FAKE_BOT_TOKEN")
    config.set_value(uid, "telegram_chat_id", "987654321")

    # Confirm the raw DB row is genuinely ciphertext before proving send() still
    # works — otherwise this test wouldn't actually be exercising the encrypted
    # path at all.
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM settings WHERE user_id = %s AND key = 'telegram_bot_token'", (uid,)
        )
        raw = cur.fetchone()[0]
    assert raw != "123456789:REAL_FAKE_BOT_TOKEN"

    with patch("app.notify.httpx.post") as mock_post:
        notify.TelegramChannel().send(uid, "Test Title", "Test body")

    assert mock_post.call_count == 1
    (url,), kwargs = mock_post.call_args
    assert url == "https://api.telegram.org/bot123456789:REAL_FAKE_BOT_TOKEN/sendMessage"
    assert kwargs["json"]["chat_id"] == "987654321"


def test_discord_send_uses_the_real_decrypted_webhook_url(pg_conn):
    uid = _make_user(pg_conn)
    webhook = "https://discord.com/api/webhooks/123456789012345678/RealFakeWebhookTokenValue"
    config.set_value(uid, "discord_webhook_url", webhook)

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM settings WHERE user_id = %s AND key = 'discord_webhook_url'", (uid,)
        )
        raw = cur.fetchone()[0]
    assert raw != webhook

    with patch("app.notify.httpx.post") as mock_post:
        notify.DiscordChannel().send(uid, "Test Title", "Test body")

    assert mock_post.call_count == 1
    (url,), kwargs = mock_post.call_args
    assert url == webhook
    assert "Test Title" in kwargs["json"]["content"]


def test_ntfy_send_uses_the_real_decrypted_auth_token(pg_conn):
    uid = _make_user(pg_conn)
    config.set_value(uid, "ntfy_auth_token", "tk_RealFakeNtfyAuthToken")
    config.set_value(uid, "ntfy_topic", "my-anidex-topic")

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM settings WHERE user_id = %s AND key = 'ntfy_auth_token'", (uid,)
        )
        raw = cur.fetchone()[0]
    assert raw != "tk_RealFakeNtfyAuthToken"

    with patch("app.notify.httpx.post") as mock_post:
        notify.NtfyChannel().send(uid, "Test Title", "Test body")

    assert mock_post.call_count == 1
    _args, kwargs = mock_post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer tk_RealFakeNtfyAuthToken"
