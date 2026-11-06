# Setting up integrations

## Notifications (optional)

Configured per-user under **Settings → Notifications**. Four channels are supported,
each with its own on/off toggle so you can run any combination of them:

- New episode alerts for anything in your Watching/Planning list
- Daily sync success/failure notification — a cookie-based provider (Netflix, Prime
  Video) whose session has expired gets a distinct "reconnect needed" message instead
  of a generic failure
- Weekly digest of upcoming episodes
- Airing-schedule-change alerts when a tracked title's next-episode air date shifts
- Monthly recap of the prior month's watch activity

**Telegram** — set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (in Settings, or in `.env`
before first boot). Create a bot via [@BotFather](https://t.me/BotFather) to get a token;
your chat ID can be retrieved by messaging [@userinfobot](https://t.me/userinfobot).

**Discord** — paste a channel webhook URL into Settings. Create one via a Discord
channel's *Edit Channel → Integrations → Webhooks*. Discord/ntfy have no `.env` fallback —
they're Settings-only, since they're free-text URLs rather than a fixed provider host.

**ntfy** — set a topic (and optionally a non-default server URL and auth token if
self-hosting ntfy) in Settings. Uses [ntfy.sh](https://ntfy.sh) by default; no signup
needed, just pick an unguessable topic name and subscribe to it in the ntfy app.

**Web Push** — no credential needed, just click Enable and grant the browser's own
permission prompt. Opted into per-device (installed PWA or browser tab), not per-account
— enabling it on one device doesn't enable it on another.

See the [notifications guide](../user-guide/notifications.md) for more.

## Home Assistant integration (optional)

`GET /api/ha/status` returns a single combined JSON payload — sync health, watch-next
queue length/title, and episodes airing today/this week — meant to be polled from Home
Assistant's built-in RESTful sensor integration rather than only viewed inside AniDex.

Authenticate with a personal access token (**Settings → Personal access tokens** — the
same tokens issued for the MCP server; read scope is enough, this endpoint never
writes). Example `configuration.yaml`:

```yaml
rest:
  - resource: https://your-anidex-host/api/ha/status
    headers:
      Authorization: !secret anidex_pat
    scan_interval: 900
    sensor:
      - name: "AniDex Sync Status"
        value_template: "{{ value_json.sync.last_result }}"
      - name: "AniDex Queue Length"
        value_template: "{{ value_json.queue.length }}"
        unit_of_measurement: "shows"
      - name: "AniDex Next Up"
        value_template: "{{ value_json.queue.next_up }}"
      - name: "AniDex Episodes Airing Today"
        value_template: "{{ value_json.airing.today }}"
        unit_of_measurement: "episodes"
```

Store the raw `adx_pat_...` token in `secrets.yaml` as `anidex_pat: "Bearer adx_pat_..."`
— the `Authorization` header needs the `Bearer ` prefix included in the secret value
itself, since `!secret` substitutes the whole header value verbatim.

See the [Home Assistant guide](../user-guide/home-assistant.md) for the full setup
walkthrough, and [the MCP server doc](../mcp.md) — personal access tokens are shared
between this integration and MCP clients.

## MCP server (optional)

AniDex exposes an [MCP](https://modelcontextprotocol.io) server at `/mcp`, running
inside the same app process, so an AI client (Claude Code, or any other MCP-compatible
client) can read — and, with a `read_write`-scoped token, write — your own library,
notes, stats, and recommendation data. Auth uses the same personal access tokens as the
Home Assistant integration above (**Settings → Personal access tokens**).

See [docs/mcp.md](../mcp.md) for the full tool list, scopes, and example client
config.
