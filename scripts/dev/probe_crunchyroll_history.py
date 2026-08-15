#!/usr/bin/env python3
"""
Dev-only diagnostic (not part of the app). Confirms the real request/response shape
of Crunchyroll's /content/v2/{account_id}/watch-history endpoint before it gets used
for issue #45's incremental fetch — same "probe before you build" discipline
probe_netflix_falcor.py used for #48 (that one caught a wrong endpoint assumption
that consistently 421'd; this one exists to catch a wrong *ordering* assumption,
since the vendored crunchyexporter-cli source (src/crunchyroll/{auth,history}.py at
pinned commit 1855e567) has no ordering documentation at all — it just pages until a
short page, with no watermark).

Login/account_id resolution below is copied faithfully from that vendored source
(CRAuth.login_with_etp_rt / _fetch_account_id) since it's the confirmed-working
auth flow for this account type; only the watch-history fetch itself and the
ordering check are new.

MOST IMPORTANT OUTPUT: the "ORDERING CHECK" section at the end. issue #45's whole
watermark/early-stop design assumes the feed is newest-first (mirroring Netflix's
confirmed-live behavior) — if this prints a MISMATCH, do not implement fetch_since()
watermarking against this endpoint without redesigning around whatever order it
actually returns.
"""

import base64
import json
import os
import sys
import uuid

import httpx

etp_rt = os.environ.get("CRUNCHYROLL_ETP_RT", "")

if not etp_rt:
    print("ERROR: set CRUNCHYROLL_ETP_RT env var first (the etp_rt browser cookie).", file=sys.stderr)
    sys.exit(1)

CR_API_BASE = "https://beta-api.crunchyroll.com"
# Public web client id, embedded in CR's own web app — documented widely in
# open-source CR tools, copied from the vendored crunchyexporter-cli source.
CLIENT_ID = "noaihdevm_6iyg0a8l0q"

client = httpx.Client(timeout=30)

print("Logging in with etp_rt cookie...")
resp = client.post(
    f"{CR_API_BASE}/auth/v1/token",
    headers={"Authorization": f"Basic {base64.b64encode(f'{CLIENT_ID}:'.encode()).decode()}"},
    cookies={"etp_rt": etp_rt},
    data={
        "grant_type": "etp_rt_cookie",
        "scope": "offline_access",
        "device_id": str(uuid.uuid4()),
        "device_name": "Chrome on Windows",
        "device_type": "com.crunchyroll.desktop.windows",
    },
)
if resp.status_code != 200:
    print(f"--- login failed: HTTP {resp.status_code} ---")
    print(resp.text[:1000])
    sys.exit(1)
access_token = resp.json()["access_token"]
print("Login OK.\n")

print("Resolving account_id from /accounts/v1/me ...")
resp = client.get(f"{CR_API_BASE}/accounts/v1/me", headers={"Authorization": f"Bearer {access_token}"})
resp.raise_for_status()
account_id = resp.json()["account_id"]
print(f"account_id = {account_id}\n")

auth_headers = {"Authorization": f"Bearer {access_token}"}


def fetch_page(page: int) -> list[dict]:
    url = f"{CR_API_BASE}/content/v2/{account_id}/watch-history"
    resp = client.get(
        url,
        params={"page_size": 20, "page": page, "locale": "en-US"},
        headers=auth_headers,
    )
    print(f"GET watch-history page={page} -> HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:1000])
        sys.exit(1)
    return resp.json().get("data", [])


page1 = fetch_page(1)
print()
print("=" * 78)
print("RAW page 1 (pretty-printed, first 3000 chars)")
print("=" * 78)
print(json.dumps(page1[:3], indent=2)[:3000])

if page1:
    print()
    print(f"Keys present per item: {sorted(page1[0].keys())}")
    print(f"Keys present in panel: {sorted(page1[0].get('panel', {}).keys())}")

page2 = fetch_page(2)

print()
print("=" * 78)
print("ORDERING CHECK")
print("=" * 78)


def dates(items: list[dict]) -> list[str]:
    return [it.get("date_played") or "" for it in items]


p1_dates = dates(page1)
p2_dates = dates(page2)
print(f"page 1 date_played sequence: {p1_dates}")
print(f"page 2 date_played sequence: {p2_dates}")

all_dates = p1_dates + p2_dates
non_increasing = all(a >= b for a, b in zip(all_dates, all_dates[1:]) if a and b)

if non_increasing and p1_dates and p2_dates and p1_dates[-1] >= p2_dates[0]:
    print("\n✓ NEWEST-FIRST CONFIRMED — page 1 through page 2 is monotonically "
          "non-increasing by date_played. Safe to build a newest-first watermark "
          "early-stop against this endpoint.")
else:
    print("\n✗ MISMATCH — dates are NOT monotonically non-increasing across pages 1-2. "
          "Do NOT assume newest-first ordering for the real fetch_since() "
          "implementation without investigating further.")

print()
print("fully_watched values seen:", sorted({it.get("fully_watched") for it in page1 + page2}))
print("episode_number sample:", [
    (it.get("panel", {}).get("episode_metadata", {}) or {}).get("episode_number")
    for it in page1[:5]
])
