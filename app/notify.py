"""Pluggable notification delivery.

Callers pass a channel-neutral title + plain-text body to notify(); each Channel
implementation renders it for its own transport. Telegram is the first Channel here —
Discord/ntfy plug into the same protocol (see issues #54/#55).
"""

import html
import logging
from typing import Protocol

import httpx

from app import config

log = logging.getLogger("anime_tracker")


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
            log.warning("Telegram send failed for user %s: %s", user_id, e)


CHANNELS: list[Channel] = [TelegramChannel()]


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
