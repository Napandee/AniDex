"""Pluggable notification delivery.

Callers pass a channel-neutral title + plain-text body to notify(); each Channel
implementation renders it for its own transport. Telegram is the first Channel here —
Discord/ntfy plug into the same protocol (see issues #54/#55).
"""

import html
import ipaddress
import json
import logging
import re
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from pywebpush import WebPushException, webpush

from app import config, db, vapid

log = logging.getLogger("anime_tracker")

# Discord webhook URLs are always this exact shape — used both to validate what a user
# saves in Settings and, defensively, before ever sending. The host is otherwise fully
# user-supplied, which without this check would let a user point the server at an
# arbitrary internal address (SSRF) via their own notification settings.
DISCORD_WEBHOOK_RE = re.compile(r"^https://(?:discord|discordapp)\.com/api/webhooks/\d+/[\w.\-]+$")

# Discord's hard cap on a webhook message's `content` field.
DISCORD_CONTENT_LIMIT = 2000

# ntfy topics are meant to be short unguessable slugs, not arbitrary strings — this also
# keeps the topic out of the request's path/query structure entirely, unlike the server
# URL below (which, unlike Discord's fixed host, is deliberately user-configurable so
# self-hosting works — see issue #55).
NTFY_TOPIC_RE = re.compile(r"^[\w.\-]+$")

# Conservative cap in line with ntfy's own default server-side message-size limit
# (~4096 bytes) — self-hosted servers can raise this, but there's no way to know a given
# server's actual configured limit from here, so this errs on the safe side.
NTFY_CONTENT_LIMIT = 4000


class Channel(Protocol):
    key: str

    def is_configured(self, user_id: int) -> bool: ...
    def send(self, user_id: int, title: str, body: str) -> None: ...


class TelegramChannel:
    key = "telegram"

    def is_configured(self, user_id: int) -> bool:
        return bool(
            config.get(user_id, "telegram_bot_token") and config.get(user_id, "telegram_chat_id")
        )

    def send(self, user_id: int, title: str, body: str) -> None:
        token = config.get(user_id, "telegram_bot_token")
        chat_id = config.get(user_id, "telegram_chat_id")
        text = f"<b>{html.escape(title)}</b>\n{html.escape(body)}" if title else html.escape(body)
        try:
            httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception as e:
            # Not str(e): httpx exceptions often embed the request URL, and the bot
            # token lives in that URL — logging it verbatim would leak a credential.
            log.warning("Telegram send failed for user %s: %s", user_id, type(e).__name__)


class DiscordChannel:
    key = "discord"

    def is_configured(self, user_id: int) -> bool:
        webhook_url = config.get(user_id, "discord_webhook_url")
        return bool(webhook_url and DISCORD_WEBHOOK_RE.match(webhook_url))

    def send(self, user_id: int, title: str, body: str) -> None:
        webhook_url = config.get(user_id, "discord_webhook_url")
        content = f"**{title}**\n{body}" if title else body
        if len(content) > DISCORD_CONTENT_LIMIT:
            content = content[: DISCORD_CONTENT_LIMIT - 1] + "…"
        try:
            httpx.post(webhook_url, json={"content": content}, timeout=10)
        except Exception as e:
            # Not str(e): the webhook URL itself is a bearer credential and often
            # appears in httpx's exception message — logging it verbatim would leak it.
            log.warning("Discord send failed for user %s: %s", user_id, type(e).__name__)


def ntfy_server_url(user_id: int) -> str:
    return (config.get(user_id, "ntfy_server_url") or "https://ntfy.sh").rstrip("/")


def ntfy_host_blocked(server_url: str) -> bool:
    """True if server_url's host is link-local (169.254.0.0/16, including the
    169.254.169.254 cloud-metadata address used for instance credential theft).

    Deliberately narrower than Discord's fixed-host allowlist: ntfy is meant to be
    self-hostable, so RFC1918 private addresses and loopback are NOT blocked here —
    those are exactly where a real self-hosted server legitimately lives (see issue
    #55's own scope). Link-local has no such legitimate use for this field, so it's
    the one range worth blocking outright. This is a literal-address check only, not
    DNS-rebinding-proof — a reasonable-effort mitigation for this app's trust model
    (invite-only, admin-managed instances), not a hardened boundary.
    """
    host = urlsplit(server_url).hostname or ""
    try:
        return ipaddress.ip_address(host).is_link_local
    except ValueError:
        return False  # a hostname, not a literal IP -- not resolved/checked further


class NtfyChannel:
    key = "ntfy"

    def is_configured(self, user_id: int) -> bool:
        topic = config.get(user_id, "ntfy_topic")
        server_url = ntfy_server_url(user_id)
        return bool(
            topic and NTFY_TOPIC_RE.match(topic)
            and server_url.startswith(("http://", "https://"))
            and not ntfy_host_blocked(server_url)
        )

    def send(self, user_id: int, title: str, body: str) -> None:
        server_url = ntfy_server_url(user_id)
        topic = config.get(user_id, "ntfy_topic")
        auth_token = config.get(user_id, "ntfy_auth_token")
        body_bytes = body.encode("utf-8")
        if len(body_bytes) > NTFY_CONTENT_LIMIT:
            # NTFY_CONTENT_LIMIT is a byte count, not a character count — truncating the
            # str directly and only then encoding can overshoot it (e.g. "…" alone is 3
            # bytes in UTF-8). Truncate on bytes instead, decoding with errors="ignore"
            # to drop any multi-byte character left dangling at the cut point.
            ellipsis = "…".encode("utf-8")
            truncated = body_bytes[: NTFY_CONTENT_LIMIT - len(ellipsis)].decode("utf-8", errors="ignore")
            body_bytes = truncated.encode("utf-8") + ellipsis
        # Bytes, not str: ntfy's Title header carries the message title verbatim, and
        # httpx raises UnicodeEncodeError on a str header value outside ASCII — anime
        # titles routinely aren't (e.g. non-English romanizations).
        headers = {"Title": title.encode("utf-8")} if title else {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        try:
            httpx.post(f"{server_url}/{topic}", content=body_bytes, headers=headers, timeout=10)
        except Exception as e:
            # Not str(e): the auth token isn't in the URL here, but keep the same
            # exception-type-only logging convention as the other channels for consistency.
            log.warning("ntfy send failed for user %s: %s", user_id, type(e).__name__)


class WebPushChannel:
    key = "webpush"

    def is_configured(self, user_id: int) -> bool:
        # "Configured" here means "subscribed at least one device" — unlike the
        # other three channels, there's no separate token/URL to save first;
        # subscribing (a browser permission grant, issue #377) IS the setup.
        return db.fetchone(
            "SELECT 1 FROM push_subscriptions WHERE user_id = %s LIMIT 1", (user_id,)
        ) is not None

    def send(self, user_id: int, title: str, body: str) -> None:
        _, private_pem = vapid.get_or_create_keypair()
        payload = json.dumps({"title": title, "body": body})
        subs = db.fetchall(
            "SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = %s",
            (user_id,),
        )
        for sub in subs:
            subscription_info = {
                "endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
            }
            try:
                webpush(
                    subscription_info=subscription_info,
                    data=payload,
                    vapid_private_key=private_pem,
                    # sub is a contact URI push services may use to reach the sender
                    # about this VAPID identity (per the spec) — not a secret, not
                    # user-facing; a fixed placeholder is standard practice for a
                    # self-hosted instance with no operator contact field to draw from.
                    vapid_claims={"sub": "mailto:admin@localhost"},
                    timeout=10,
                )
            except WebPushException as e:
                # 404/410 means the push service itself has discarded this
                # subscription (uninstalled, browser data cleared, etc.) — remove it
                # so every future notify() doesn't keep retrying a dead endpoint.
                status = e.response.status_code if e.response is not None else None
                if status in (404, 410):
                    db.execute("DELETE FROM push_subscriptions WHERE id = %s", (sub["id"],))
                else:
                    log.warning("Web Push send failed for user %s: %s: %s", user_id, type(e).__name__, e)
            except Exception as e:
                # Issue #377 — a real bug here (VAPID key stored in a format
                # pywebpush couldn't deserialize) was caught by this exact except
                # clause and logged as only "ValueError" with no message, which
                # would have made the real cause invisible in production logs —
                # found live, not from a log line, and fixed by including str(e)
                # here so a future failure like it is actually diagnosable.
                log.warning("Web Push send failed for user %s: %s: %s", user_id, type(e).__name__, e)


CHANNELS: list[Channel] = [TelegramChannel(), DiscordChannel(), NtfyChannel(), WebPushChannel()]


def notify(user_id: int, title: str, body: str) -> None:
    """Fire-and-forget fan-out to every channel a user has enabled and configured.

    A channel is enabled unless explicitly turned off — `{key}_enabled` defaults to "on"
    when unset, so a user who already had a channel's credentials configured before this
    per-channel toggle existed (e.g. Telegram, previously the only channel and implicitly
    "on" whenever configured) keeps getting notifications without touching Settings.
    """
    for ch in CHANNELS:
        if config.get(user_id, f"{ch.key}_enabled") != "false" and ch.is_configured(user_id):
            ch.send(user_id, title, body)
