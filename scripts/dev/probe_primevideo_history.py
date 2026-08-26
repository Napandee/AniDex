#!/usr/bin/env python3
"""
Dev-only diagnostic (not part of the app). Tests whether a plain cookie-authenticated
httpx client can replay Prime Video's watch-history API outside the browser — the
endpoint itself was found by live devtools capture (see
notes/2026-08-14-netflix-prime-sync-research.md's "Prime Video endpoint — CONFIRMED"
section for the full shape), but capture-from-a-browser and server-side-replay are two
different questions; this answers the second one, same role
scripts/dev/probe_netflix_falcor.py played for Netflix.

Meant to run via scripts/dev/probe-primevideo.sh, which sources
.env.primevideo.local (PRIMEVIDEO_COOKIE_HEADER) — see
scripts/dev/setup-primevideo-env.sh for where to find that value in devtools.

What this checks, in order:
  1. Cold call with NO nextToken — is page 1 served by this same endpoint, or does it
     404/error (meaning page 1 only ever arrives inline in the initial page HTML, and
     this endpoint is scroll-triggered pagination only)? Still unconfirmed as of the
     research notes above.
  2. If that works (or if a nextToken is supplied via PRIMEVIDEO_NEXT_TOKEN), walks a
     couple of pages forward using the response's own nextToken, printing:
       - HTTP status
       - top-level widget types present (sanity check we're hitting the right shape)
       - date-section headers seen
       - a season/movie title + gti + episode count breakdown, to eyeball whether the
         season-recurs-across-date-sections aggregation problem documented in the
         research notes is really as described
"""

import json
import os
import sys

import httpx

cookie_header = os.environ.get("PRIMEVIDEO_COOKIE_HEADER", "")
if not cookie_header:
    print("ERROR: set PRIMEVIDEO_COOKIE_HEADER first.", file=sys.stderr)
    print("See scripts/dev/setup-primevideo-env.sh for where to find this in devtools.", file=sys.stderr)
    sys.exit(1)

cookies = {}
for part in cookie_header.split(";"):
    part = part.strip()
    if "=" in part:
        k, _, v = part.partition("=")
        cookies[k.strip()] = v.strip()

HEADERS = {
    "accept": "*/*",
    "x-requested-with": "XMLHttpRequest",
    "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "referer": "https://www.primevideo.com/settings/watch-history",
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
}

BASE_URL = "https://www.primevideo.com/api/getWatchHistorySettingsPage"

client = httpx.Client(cookies=cookies, headers=HEADERS, timeout=30, follow_redirects=True)


def fetch_page(next_token: str | None) -> dict:
    params = {}
    if next_token:
        params["widgetArgs"] = json.dumps({"nextToken": next_token})
    resp = client.get(BASE_URL, params=params)
    print(f"--- HTTP {resp.status_code} ({'with' if next_token else 'no'} nextToken) ---")
    if resp.status_code != 200:
        print("Response headers:")
        for k, v in resp.headers.items():
            print(f"  {k}: {v}")
        print("Response body (first 2000 chars):")
        print(resp.text[:2000])
        resp.raise_for_status()
    return resp.json()


def summarize(data: dict) -> str | None:
    widgets = data.get("widgets", [])
    print(f"widget types present: {[w.get('widgetType') for w in widgets]}")

    wh = next((w for w in widgets if w.get("widgetType") == "watch-history"), None)
    if wh is None:
        print("!! no 'watch-history' widget in this response — unexpected shape")
        return None

    items = wh["content"]["content"]
    date_sections = items.get("titles", [])
    print(f"{len(date_sections)} date-section(s) in this page:")

    seen_gtis: dict[str, dict] = {}
    for section in date_sections:
        date = section.get("date")
        entries = section.get("titles", [])
        print(f"  {date}: {len(entries)} top-level entr(y/ies)")
        for entry in entries:
            gti = entry.get("gti")
            title = entry.get("title", {}).get("text")
            kind = entry.get("titleType")
            children = entry.get("children", [])
            bucket = seen_gtis.setdefault(gti, {"title": title, "kind": kind, "episode_texts": []})
            for child in children:
                bucket["episode_texts"].append(child.get("title", {}).get("text"))

    print()
    print(f"{len(seen_gtis)} distinct gti(s) across this page (season/movie level):")
    for gti, info in seen_gtis.items():
        print(f"  {info['kind']:>6}  {info['title']!r}  gti={gti}")
        for ep in info["episode_texts"]:
            print(f"          - {ep}")

    return items.get("nextToken")


def main() -> None:
    print("=" * 78)
    print("PAGE 1 — cold call, no nextToken (does this endpoint serve page 1 at all?)")
    print("=" * 78)
    try:
        data = fetch_page(None)
        next_token = summarize(data)
    except httpx.HTTPStatusError:
        print()
        print("Cold call failed — page 1 likely arrives inline in the initial page HTML,")
        print("not through this endpoint. Set PRIMEVIDEO_NEXT_TOKEN (captured from a real")
        print("browser 'load more' scroll) to test pagination from a known-good starting")
        print("point instead.")
        next_token = os.environ.get("PRIMEVIDEO_NEXT_TOKEN")
        if not next_token:
            sys.exit(1)

    pages_walked = 1
    max_pages = int(os.environ.get("PRIMEVIDEO_PROBE_MAX_PAGES", "3"))
    while next_token and pages_walked < max_pages:
        print()
        print("=" * 78)
        print(f"PAGE {pages_walked + 1} — via nextToken from previous page")
        print("=" * 78)
        data = fetch_page(next_token)
        next_token = summarize(data)
        pages_walked += 1

    print()
    print(f"Walked {pages_walked} page(s). {'More pages available (nextToken present).' if next_token else 'Reached the end (no nextToken).'}")


if __name__ == "__main__":
    main()
