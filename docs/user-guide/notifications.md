# Notifications

Configured per-user under **Settings → Notifications**. Three channels, each with its
own independent on/off toggle — run any combination:

- **New episode alerts** for anything in your Watching/Planning list
- **Daily sync success/failure** notification
- **Weekly digest** of upcoming episodes

## Telegram

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in Settings (or `.env` before first
boot). Create a bot via [@BotFather](https://t.me/BotFather) to get a token; get your
chat ID by messaging [@userinfobot](https://t.me/userinfobot).

## Discord

Paste a channel webhook URL into Settings. Create one via a Discord channel's *Edit
Channel → Integrations → Webhooks*.

## ntfy

Set a topic (and optionally a non-default server URL and auth token, if self-hosting
ntfy) in Settings. Uses [ntfy.sh](https://ntfy.sh) by default — no signup needed, just
pick an unguessable topic name and subscribe to it in the ntfy app.

Discord and ntfy are Settings-only (no `.env` fallback), since they're free-text
URLs/topics rather than a fixed provider host the way Telegram's bot token is.

## Credentials

Every credential above (bot tokens, webhook URLs, auth tokens) is stored encrypted at
rest in the database — the plaintext value is never visible again once saved, only
whether a value is currently set.
