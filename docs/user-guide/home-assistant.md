# Home Assistant integration

`GET /api/ha/status` returns a single combined JSON payload — sync health, watch-next
queue length/title, and episodes airing today/this week — meant to be polled by Home
Assistant's built-in RESTful `sensor:` integration rather than only viewed inside
AniDex. This is a plain read-only REST endpoint, not a bespoke HA add-on or HACS
integration — one less thing to install and keep updated.

## Setup

1. Issue a personal access token in **Settings → Personal access tokens** (the same
   tokens used for the [MCP server](../mcp.md)) — read scope is enough, this endpoint
   never writes anything.
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
   ```

That's it — no HA restart-triggering integration to install, just a REST sensor
polling a URL like any other.

## Auth notes

This endpoint only accepts a personal access token (`Authorization: Bearer ...`) — no
session-cookie fallback, since the caller is a headless poller rather than a browser
session.
