# AniDex — Prime Video Cookie Sync

A small companion browser extension (issue #390) that keeps a self-hosted
[AniDex](https://github.com/Napandee/AniDex) instance's stored Prime Video
cookie fresh, without the manual copy-paste into Settings that's otherwise
needed every time it expires.

## Why

Amazon's Prime Video web session has (at least) two tiers: ordinary
browsing/playback survives a long time, but the "account settings" tier the
watch-history API sits behind expires much faster, independent of anything
AniDex's request shape does. A cookie captured once and pasted into Settings
goes stale as soon as that tier rotates — often within a day. Reading the
browser's live cookie jar on each sync is always current for as long as the
browser itself is still logged into Prime Video, the same principle every
other Prime Video scrobbler/exporter tool relies on (confirmed by reading
[`universal-trakt-scrobbler`](https://github.com/trakt-tools/universal-trakt-scrobbler)'s
own source — see issue #390 for the full writeup).

This extension's **only** job is keeping that cookie fresh in AniDex's own
storage. It does not fetch your watch history, match titles against AniList,
or create/update anything in your library — that all stays exactly as
`scripts/sync_primevideo.py` already does on the AniDex server side.

## What it does

- Reads your browser's current `primevideo.com` cookies (`cookies` permission,
  read-only — never sent anywhere except your own AniDex instance).
- Sends them to your AniDex instance's `POST /api/pat/primevideo-cookie`
  endpoint, authenticated with a Personal Access Token you generate yourself.
- Runs automatically once an hour (`chrome.alarms`), and on-demand via the
  toolbar button.

## What it does NOT do

- No `webRequest` permission, no request interception/rewriting.
- No DOM scraping, no page-content access.
- No scrobbling, no watch-history fetching, no AniList calls of any kind.
- No telemetry, no third-party servers — the only network destination is the
  AniDex instance URL you configure.

## Install (unpacked — this isn't published to a web store)

1. Open `chrome://extensions` (or your Chromium-based browser's equivalent).
2. Enable "Developer mode" (top right).
3. Click "Load unpacked" and select this `browser-extension/primevideo-cookie-sync/`
   directory.
4. Click the extension's icon → it opens the options page automatically the
   first time (or right-click the icon → Options).

**Firefox users:** this manifest targets Manifest V3 for Chrome/Chromium.
Firefox's MV3 support has some differences (notably around
`background.service_worker` vs. `background.scripts`, and
`optional_host_permissions` support) — this hasn't been adapted for Firefox
yet. If you need it there, the core logic in `background.js` should port with
manifest changes only; no rewrite needed.

## Configure

1. In your AniDex instance, go to **Settings → API Access** and generate a
   new Personal Access Token with **read + write** scope. (Read-only tokens
   are rejected by design — see issue #390.)
2. Open this extension's options page.
3. Enter your AniDex instance's URL (e.g. `https://anidex.example.com`) and
   the token you just generated.
4. Click **Save & sync now**. The browser will ask you to confirm permission
   to reach that specific URL — this is expected (every AniDex instance is
   self-hosted somewhere different, so the extension can't declare a fixed
   host upfront).

## Using it

- The toolbar icon shows a badge after each sync attempt: a green check for
  success, a red X for a real failure (unreachable instance, rejected
  token), a yellow `?` if you're not currently logged into Prime Video in
  this browser, or a yellow `!` if the extension isn't configured yet.
- Click the toolbar icon any time to sync immediately instead of waiting for
  the hourly alarm.
- Check the extension's service worker console (`chrome://extensions` →
  this extension → "service worker" link) for detailed error logs if
  something's failing silently.

## Security notes

- The AniDex URL and PAT are stored in this extension's own local
  (`chrome.storage.local`) storage — not synced to Google/your browser
  account, not visible to any other extension or web page.
- Revoke the PAT from AniDex's Settings → API Access page at any time to cut
  this extension off immediately.
- This extension only reads cookies for `primevideo.com` — it cannot see
  cookies for any other site.
