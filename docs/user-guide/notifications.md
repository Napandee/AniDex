# Notifications

Configured per-user under **Settings → Notifications**. Four channels, each with its
own independent on/off toggle — run any combination:

- **New episode alerts** for anything in your Watching/Planning list
- **Daily sync success/failure** notification
- **Weekly digest** of upcoming episodes
- **Airing-schedule-change alerts** when a tracked Watching/Planning title's next-episode
  air date shifts meaningfully (not on every minor schedule-cache refresh)
- **Monthly recap** of the prior month's watch activity, on the 1st of each month

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

## Web Push

No credential to enter — click **Enable** under Settings → Notifications and grant the
browser's own permission prompt. Works from an installed PWA or an ordinary browser tab;
unlike the other three channels, this one is opted into per-device rather than
configured once for the whole account, so enabling it on your phone doesn't also enable
it on your laptop.

## Credentials

Every credential above (bot tokens, webhook URLs, auth tokens) is stored encrypted at
rest in the database — the plaintext value is never visible again once saved, only
whether a value is currently set.
