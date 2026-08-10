#!/usr/bin/env python3
"""
Crunchyroll → AniList sync (safe, additive-only).

Fetches watch history from Crunchyroll's beta API using the etp_rt cookie,
then updates AniList progress — but only if Crunchyroll is ahead, and never
for entries the user has marked COMPLETED or DROPPED.

Exit 0 = success, Exit 1 = fatal error.
"""

import base64
import os
import sys
import time
import uuid

import httpx
from dotenv import load_dotenv

load_dotenv()

ETP_RT = os.environ["CRUNCHYROLL_ETP_RT"]
ANILIST_TOKEN = os.environ["ANILIST_TOKEN"]
ANILIST_USERNAME = os.environ["ANILIST_USERNAME"]

CR_API = "https://beta-api.crunchyroll.com"
CR_CLIENT_ID = "noaihdevm_6iyg0a8l0q"  # public web client
ANILIST_API = "https://graphql.anilist.co"
PAGE_SIZE = 100


def log(msg):
    print(f"[crunchysync] {msg}", flush=True)


# ── Crunchyroll ──────────────────────────────────────────────────────────────

def cr_token() -> tuple[str, str]:
    """Exchange etp_rt for a Bearer token and account_id."""
    basic = base64.b64encode(f"{CR_CLIENT_ID}:".encode()).decode()
    resp = httpx.post(
        f"{CR_API}/auth/v1/token",
        headers={"Authorization": f"Basic {basic}"},
        cookies={"etp_rt": ETP_RT},
        data={
            "grant_type": "etp_rt_cookie",
            "scope": "offline_access",
            "device_id": str(uuid.uuid4()),
            "device_name": "Chrome on Windows",
            "device_type": "com.crunchyroll.desktop.windows",
        },
        timeout=20,
    )
    if resp.status_code == 401:
        log("ERROR: etp_rt cookie rejected — it has expired. Extract a fresh "
            "cookie from your browser and update CRUNCHYROLL_ETP_RT.")
        sys.exit(1)
    resp.raise_for_status()
    token = resp.json()["access_token"]

    me = httpx.get(
        f"{CR_API}/accounts/v1/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    me.raise_for_status()
    account_id = me.json()["account_id"]
    return token, account_id


def fetch_history(token: str, account_id: str) -> list[dict]:
    """Fetch full watch history, paginated."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    all_items: list[dict] = []
    page = 1
    while True:
        resp = httpx.get(
            f"{CR_API}/content/v2/{account_id}/watch-history",
            headers=headers,
            params={"page_size": PAGE_SIZE, "page": page, "locale": "en-US"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or []
        all_items.extend(data)
        if len(data) < PAGE_SIZE:
            break
        page += 1
    return all_items


def parse_history(raw: list[dict]) -> dict[str, int]:
    """Return {series_title: highest_episode_watched}."""
    best: dict[str, int] = {}
    for item in raw:
        meta = (item.get("panel") or {}).get("episode_metadata") or {}
        title = (meta.get("series_title") or "").strip()
        if not title:
            continue
        try:
            ep = int(float(meta.get("episode_number") or meta.get("sequence_number") or 0))
        except (ValueError, TypeError):
            ep = 0
        if ep == 0:
            continue
        if title not in best or ep > best[title]:
            best[title] = ep
    return best


# ── AniList ──────────────────────────────────────────────────────────────────

def gql(query: str, variables: dict | None = None, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.post(
        ANILIST_API,
        json={"query": query, "variables": variables or {}},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"AniList error: {data['errors']}")
    return data["data"]


SEARCH_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    title { romaji english }
  }
}
"""

PROGRESS_QUERY = """
query ($mediaId: Int, $userName: String) {
  MediaList(mediaId: $mediaId, userName: $userName) {
    progress
    status
  }
}
"""

UPDATE_MUTATION = """
mutation ($mediaId: Int, $progress: Int) {
  SaveMediaListEntry(mediaId: $mediaId, progress: $progress) {
    id
    progress
  }
}
"""

# Statuses we must never touch — user has made a deliberate decision
TERMINAL_STATUSES = {"COMPLETED", "DROPPED"}

_search_cache: dict[str, int | None] = {}


def find_anilist_id(title: str) -> int | None:
    if title in _search_cache:
        return _search_cache[title]
    try:
        data = gql(SEARCH_QUERY, {"search": title})
        mid = data["Media"]["id"]
        en = data["Media"]["title"]["english"] or data["Media"]["title"]["romaji"]
        log(f"  Matched '{title}' → AniList #{mid} ({en})")
        _search_cache[title] = mid
        return mid
    except Exception as e:
        log(f"  No AniList match for '{title}': {e}")
        _search_cache[title] = None
        return None


def get_list_entry(media_id: int) -> tuple[int | None, str | None]:
    try:
        data = gql(PROGRESS_QUERY, {"mediaId": media_id, "userName": ANILIST_USERNAME})
        entry = data.get("MediaList")
        if not entry:
            return None, None
        return entry["progress"], entry["status"]
    except Exception:
        return None, None


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log("Starting Crunchyroll → AniList sync")

    log("Authenticating with Crunchyroll...")
    token, account_id = cr_token()
    log(f"Authenticated — account {account_id}")

    log("Fetching watch history...")
    raw = fetch_history(token, account_id)
    log(f"Fetched {len(raw)} history entries")

    history = parse_history(raw)
    log(f"Parsed {len(history)} unique series")

    if not history:
        log("No watchable history found — nothing to do")
        sys.exit(0)

    updated = skipped = protected = 0

    for title, cr_ep in history.items():
        media_id = find_anilist_id(title)
        if not media_id:
            skipped += 1
            continue

        current_ep, status = get_list_entry(media_id)

        if current_ep is None:
            log(f"  '{title}' not in your AniList — skipping")
            skipped += 1
            continue

        if status in TERMINAL_STATUSES:
            log(f"  '{title}' is {status} on AniList — leaving untouched")
            protected += 1
            continue

        if cr_ep > (current_ep or 0):
            log(f"  Updating '{title}': ep {current_ep} → {cr_ep}")
            gql(UPDATE_MUTATION, {"mediaId": media_id, "progress": cr_ep},
                token=ANILIST_TOKEN)
            updated += 1
            time.sleep(0.7)  # stay under AniList's 90 req/min limit
        else:
            log(f"  '{title}': AniList ({current_ep}) already at or ahead of Crunchyroll ({cr_ep})")

    log(f"Done — {updated} updated, {protected} protected (COMPLETED/DROPPED), {skipped} skipped")


if __name__ == "__main__":
    main()
