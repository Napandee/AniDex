#!/usr/bin/env python3
"""
Dev-only diagnostic (not part of the app). Tests Netflix's Falcor pathEvaluator API —
found by capturing the real "show more" request on /viewingactivity, which turned out
to be a JSON-graph data call (`callPath: ["aui", "viewingActivity", 1, 50]`), not the
Shakti REST endpoint probe_netflix_shakti.py tests (which consistently 421s) and not
the HTML-fragment GraphQL BFF call the page's initial render uses.

Most of the x-netflix.* headers below look like static device-type constants (browser/
OS/client-type identifiers) rather than per-session secrets, so they're hardcoded here
— only the cookie jar and profile guid are read from the environment.

Meant to run via scripts/dev/probe-netflix-falcor.sh, which sources .env.netflix.local
(NETFLIX_COOKIE_HEADER, NETFLIX_PROFILE_GUID) — see that file's comments for where to
find those two values in devtools.
"""

import json
import os
import re
import sys
import uuid

import httpx

cookie_header = os.environ.get("NETFLIX_COOKIE_HEADER", "")
profile_guid = os.environ.get("NETFLIX_PROFILE_GUID", "")

if not cookie_header or not profile_guid:
    print("ERROR: set NETFLIX_COOKIE_HEADER and NETFLIX_PROFILE_GUID env vars first.", file=sys.stderr)
    print("See scripts/dev/setup-netflix-env.sh for where to find these in devtools.", file=sys.stderr)
    sys.exit(1)

cookies = {}
for part in cookie_header.split(";"):
    part = part.strip()
    if "=" in part:
        k, _, v = part.partition("=")
        cookies[k.strip()] = v.strip()

# A real browser sends very different headers for a plain page navigation
# (/browse) than for an XHR/fetch API call — the x-netflix.* and content-type
# headers below are specific to the pathEvaluator XHR and don't belong on the
# page load. Keep the client's default headers minimal/navigation-like; apply
# the API-specific ones only per-request on the actual Falcor POST.
BROWSER_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
}

API_HEADERS = {
    "accept": "*/*",
    "content-type": "application/x-www-form-urlencoded",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "x-netflix.browsername": "Chrome",
    "x-netflix.browserversion": "151",
    "x-netflix.clienttype": "akira",
    "x-netflix.esnprefix": "NFCDCH-LX-",
    "x-netflix.nq.stack": "prod",
    "x-netflix.osfullname": "Linux",
    "x-netflix.osname": "Linux",
    "x-netflix.osversion": "0.0.0",
    "x-netflix.client.request.name": "ui/xhrUnclassified",
    "x-netflix.request.attempt": "1",
    "x-netflix.request.client.context": '{"appstate":"foreground"}',
    "x-netflix.request.routing": (
        '{"path":"/nq/aui/endpoint/%5E1.0.0-web/pathEvaluator","control_tag":"auinqweb"}'
    ),
}

client = httpx.Client(cookies=cookies, headers=BROWSER_HEADERS, timeout=30, follow_redirects=True)

print("Resolving build_id from https://www.netflix.com/browse ...")
resp = client.get("https://www.netflix.com/browse")
if resp.status_code != 200:
    print(f"--- browse fetch failed: HTTP {resp.status_code} ---")
    print(resp.text[:1000])
resp.raise_for_status()
match = re.search(r'"BUILD_IDENTIFIER"\s*:\s*"([^"]+)"', resp.text)
if not match:
    print("FAILED to find BUILD_IDENTIFIER — cookies may be invalid/expired.", file=sys.stderr)
    sys.exit(1)
build_id = match.group(1)
print(f"build_id = {build_id}\n")

url = "https://www.netflix.com/api/aui/pathEvaluator/web/%5E2.0.0"
params = {
    "method": "call",
    "callPath": json.dumps(["aui", "viewingActivity", 1, 50]),
    "falcor_server": "0.1.0",
}
body = {"param": json.dumps({"guid": profile_guid})}
post_headers = {
    **API_HEADERS,
    "x-netflix.uiversion": build_id,
    "x-netflix.request.id": uuid.uuid4().hex,
    "referer": "https://www.netflix.com/viewingactivity",
    "origin": "https://www.netflix.com",
}

print(f"POSTing {url} with callPath=[\"aui\",\"viewingActivity\",1,50] ...\n")
resp = client.post(url, params=params, data=body, headers=post_headers)

print(f"--- HTTP {resp.status_code} ---")
if resp.status_code != 200:
    print("Response headers:")
    for k, v in resp.headers.items():
        print(f"  {k}: {v}")
    print("Response body (first 2000 chars):")
    print(resp.text[:2000])
    sys.exit(1)

data = resp.json()
print("=" * 78)
print("RAW RESPONSE (pretty-printed, first 4000 chars)")
print("=" * 78)
print(json.dumps(data, indent=2)[:4000])
