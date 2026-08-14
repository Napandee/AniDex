#!/usr/bin/env python3
"""
Dev-only diagnostic (not part of the app). Verifies the raw shape of Netflix's Shakti
viewingactivity response against what sync_netflix.py assumes (viewedItems / date /
seriesTitle / title / episode) BEFORE trusting the real sync against real AniList data.
No DB, no AniList calls, no writes anywhere — just prints Netflix's own JSON back at you.

Meant to run inside scripts/dev/Containerfile's image via scripts/dev/probe-netflix.sh
rather than directly on a host, so credentials never touch a shell command line — see
that script for the intended invocation.
"""

import json
import os
import re
import sys

import httpx

netflix_id = os.environ.get("NETFLIX_ID_COOKIE", "")
secure_netflix_id = os.environ.get("NETFLIX_SECURE_ID_COOKIE", "")

if not netflix_id or not secure_netflix_id:
    print("ERROR: set NETFLIX_ID_COOKIE and NETFLIX_SECURE_ID_COOKIE env vars first.", file=sys.stderr)
    sys.exit(1)

client = httpx.Client(
    cookies={"NetflixId": netflix_id, "SecureNetflixId": secure_netflix_id},
    headers={"User-Agent": "Mozilla/5.0"},
    timeout=30,
    follow_redirects=True,
)

print("Resolving build_id from https://www.netflix.com/browse ...")
resp = client.get("https://www.netflix.com/browse")
resp.raise_for_status()
match = re.search(r'"BUILD_IDENTIFIER"\s*:\s*"([^"]+)"', resp.text)
if not match:
    print("FAILED to find BUILD_IDENTIFIER in page state.", file=sys.stderr)
    print("Either the cookies are invalid/expired, or Netflix changed its page structure", file=sys.stderr)
    print("(the second case means sync_netflix.py's _resolve_build_id() needs updating).", file=sys.stderr)
    sys.exit(1)
build_id = match.group(1)
print(f"build_id = {build_id}\n")

url = f"https://www.netflix.com/api/shakti/{build_id}/viewingactivity"
print(f"Fetching {url} (page 0, pgSize=5) ...\n")
resp = client.get(url, params={"pg": 0, "pgSize": 5})
resp.raise_for_status()
data = resp.json()

print("=" * 78)
print("RAW RESPONSE (pretty-printed)")
print("=" * 78)
print(json.dumps(data, indent=2))

print()
print("=" * 78)
print("WHAT sync_netflix.py ASSUMES vs WHAT'S ACTUALLY THERE")
print("=" * 78)
items = data.get("viewedItems")
print(f"data.get('viewedItems')  ->  {'FOUND, ' + str(len(items)) + ' items' if items is not None else 'MISSING — top-level key is probably named something else, check the raw JSON above'}")

if items:
    first = items[0]
    print(f"\nFirst item's keys: {sorted(first.keys())}")
    print(f"  item.get('date')          -> {first.get('date')!r}  (expected: epoch milliseconds)")
    print(f"  item.get('seriesTitle')   -> {first.get('seriesTitle')!r}  (expected: present for episodes, absent for movies)")
    print(f"  item.get('title')         -> {first.get('title')!r}")
    print(f"  item.get('episode')       -> {first.get('episode')!r}  (expected: int episode index, or absent)")
    print("\nIf any of the above say something other than what's expected, that's exactly")
    print("what needs fixing in _item_watched_at() / _is_episode() / _item_episode_number()")
    print("in scripts/sync_netflix.py before running it for real.")
