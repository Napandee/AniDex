"""Pluggable notification delivery.

Callers pass a channel-neutral title + plain-text body to notify(); each Channel
implementation renders it for its own transport. Telegram is the first Channel here —
Discord/ntfy plug into the same protocol (see issues #54/#55).
"""

import html
import logging
import re
from typing import Protocol

import httpx

from app import config

log = logging.getLogger("anime_tracker")

# Discord webhook URLs are always this exact shape — used both to validate what a user
# saves in Settings and, defensively, before ever sending. The host is otherwise fully
# user-supplied, which without this check would let a user point the server at an
# arbitrary internal address (SSRF) via their own notification settings.
DISCORD_WEBHOOK_RE = re.compile(r"^https://(?:discord|discordapp)\.com/api/webhooks/\d+/[\w.\-]+$")

# Discord's hard cap on a webhook message's `content` field.
DISCORD_CONTENT_LIMIT = 2000


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


CHANNELS: list[Channel] = [TelegramChannel(), DiscordChannel()]


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
