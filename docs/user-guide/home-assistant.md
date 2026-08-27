# Home Assistant integration

`GET /api/ha/status` returns a single combined JSON payload — sync health, watch-next
queue length/title, and episodes airing today/this week — meant to be polled by Home
Assistant's built-in RESTful `sensor:` integration rather than only viewed inside
AniDex. This is a plain read-only REST endpoint, not a bespoke HA add-on or HACS
integration — one less thing to install and keep updated.

## Setup

1. Issue a personal access token from **Settings → API Access**'s **Personal access
   tokens** section (the same tokens used for the [MCP server](../mcp.md)) — read
   scope is enough, this endpoint never writes anything.
2. Store the raw token in HA's `secrets.yaml`:

   ```yaml
   anidex_pat: "Bearer adx_pat_..."
   ```

   The `Bearer ` prefix has to be part of the secret value itself — `!secret`
   substitutes the whole header value verbatim, HA doesn't add the prefix for you.

3. Add a `rest:` sensor block to `configuration.yaml`:

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
         - name: "AniDex Episodes Airing This Week"
           value_template: "{{ value_json.airing.this_week }}"
           unit_of_measurement: "episodes"
         - name: "AniDex Sync Running"
           value_template: "{{ value_json.sync.running }}"
         - name: "AniDex Last Synced"
           value_template: "{{ value_json.sync.last_synced }}"
           device_class: timestamp
   ```

That's it — no HA restart-triggering integration to install, just a REST sensor
polling a URL like any other.

## Full payload shape

The example above wires up most of the payload, but not every field — here's the
complete real response:

```json
{
  "sync": {
    "running": false,
    "last_result": "ok",
    "last_synced": "2026-08-27T04:31:12+00:00",
    "steps": [
      {"service": "anilist", "status": "ok"},
      {"service": "crunchyroll", "status": "ok"},
      {"service": "netflix", "status": "ok"},
      {"service": "plex", "status": "skipped"},
      {"service": "primevideo", "status": "error"}
    ]
  },
  "queue": {
    "length": 12,
    "next_up": "Frieren: Beyond Journey's End"
  },
  "airing": {
    "today": 2,
    "this_week": 9
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `sync.running` | boolean | A sync is in progress right now. |
| `sync.last_result` | string or `null` | `"ok"`, `"partial"`, or `"error"` from the most recent completed run; `null` while `running` is true or before any sync has ever completed. |
| `sync.last_synced` | ISO 8601 timestamp or `null` | Most recent `library_entries` write for this user, across any provider. |
| `sync.steps` | array | One entry per pipeline step from the most recent run (`anilist`/`crunchyroll`/`netflix`/`plex`/`primevideo`), each `{"service": ..., "status": ...}` — `status` is `"ok"`, `"skipped"` (provider not connected), or `"error"`. |
| `queue.length` | integer | Count of Planning/Paused entries — the same scope the Watch Queue page uses. |
| `queue.next_up` | string or `null` | Title of the top-priority queue entry, `null` if the queue is empty. |
| `airing.today` | integer | Episodes airing today (in your configured timezone, Settings → Preferences) for anything in your library. |
| `airing.this_week` | integer | Episodes airing in the next 7 days (rolling window, not a calendar week). |

## Auth notes

This endpoint only accepts a personal access token (`Authorization: Bearer ...`) — no
session-cookie fallback, since the caller is a headless poller rather than a browser
session.
