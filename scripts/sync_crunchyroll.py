#!/usr/bin/env python3
"""
Crunchyroll → AniList sync.

Uses Playwright (headless Chromium) to load Crunchyroll watch history with
the etp_rt session cookie — bypasses Cloudflare because it's a real browser.
Compares episode progress against AniList and updates any entries where
Crunchyroll is ahead.

Runs as a one-shot container. Exit 0 = success, Exit 1 = fatal error.
"""

import os
import sys
import time

import httpx
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

ETP_RT = os.environ["CRUNCHYROLL_ETP_RT"]
ANILIST_TOKEN = os.environ["ANILIST_TOKEN"]
ANILIST_USERNAME = os.environ["ANILIST_USERNAME"]
ANILIST_API = "https://graphql.anilist.co"

MAX_HISTORY = 50


def log(msg):
    print(f"[crunchysync] {msg}", flush=True)


def gql(query, variables=None, token=None):
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


def fetch_history():
    """Load Crunchyroll history page and intercept the internal API response."""
    captured = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        context.add_cookies([{
            "name": "etp_rt",
            "value": ETP_RT,
            "domain": ".crunchyroll.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "None",
        }])

        cr_api_urls: list[str] = []

        def on_response(response):
            url = response.url
            if "crunchyroll.com" in url and not url.endswith((".js", ".css", ".png", ".jpg", ".webp", ".svg", ".woff2")):
                cr_api_urls.append(url)
            if (
                "crunchyroll.com/content/v2" in url
                and "history" in url
            ):
                try:
                    captured.append(response.json())
                except Exception:
                    pass

        page = context.new_page()
        page.on("response", on_response)

        log("Loading Crunchyroll history page...")
        try:
            page.goto(
                "https://www.crunchyroll.com/history",
                wait_until="networkidle",
                timeout=45000,
            )
        except Exception as e:
            log(f"Page load warning: {e} — continuing with what was captured")

        # Give late API calls a moment to complete
        page.wait_for_timeout(3000)

        log(f"Final page URL: {page.url}")
        log(f"Crunchyroll API calls intercepted ({len(cr_api_urls)}):")
        for u in cr_api_urls[:30]:
            log(f"  {u}")

        # Detect redirect to login (cookie expired)
        if "login" in page.url or "sso" in page.url:
            browser.close()
            log("ERROR: Redirected to login — etp_rt cookie has expired. "
                "Extract a fresh cookie from your browser and update CRUNCHYROLL_ETP_RT.")
            sys.exit(1)

        browser.close()

    log(f"Intercepted {len(captured)} history API response(s)")

    history = []
    for resp in captured:
        for item in (resp.get("data") or [])[:MAX_HISTORY]:
            meta = (item.get("panel") or {}).get("episode_metadata") or {}
            series_title = meta.get("series_title", "").strip()
            if not series_title:
                continue
            try:
                ep = int(float(
                    meta.get("episode_number")
                    or meta.get("sequence_number")
                    or 0
                ))
            except (ValueError, TypeError):
                ep = 0
            if ep == 0:
                continue
            history.append({
                "series_title": series_title,
                "episode": ep,
            })

    return history


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

_search_cache: dict[str, int | None] = {}


def find_anilist_id(title: str) -> int | None:
    if title in _search_cache:
        return _search_cache[title]
    try:
        data = gql(SEARCH_QUERY, {"search": title})
        mid = data["Media"]["id"]
        log(f"  Matched '{title}' → AniList ID {mid} "
            f"({data['Media']['title']['english'] or data['Media']['title']['romaji']})")
        _search_cache[title] = mid
        return mid
    except Exception as e:
        log(f"  No AniList match for '{title}': {e}")
        _search_cache[title] = None
        return None


def get_progress(media_id: int) -> tuple[int | None, str | None]:
    try:
        data = gql(PROGRESS_QUERY, {"mediaId": media_id, "userName": ANILIST_USERNAME})
        entry = data.get("MediaList")
        if not entry:
            return None, None
        return entry["progress"], entry["status"]
    except Exception:
        return None, None


def main():
    log("Starting Crunchyroll → AniList sync")

    history = fetch_history()
    if not history:
        log("No history items found — check etp_rt cookie or Crunchyroll page structure")
        sys.exit(1)

    log(f"Found {len(history)} watched item(s) in history")

    # Keep highest episode seen per series
    best: dict[str, dict] = {}
    for item in history:
        title = item["series_title"]
        if title not in best or item["episode"] > best[title]["episode"]:
            best[title] = item

    updated = 0
    skipped = 0

    for title, item in best.items():
        media_id = find_anilist_id(title)
        if not media_id:
            skipped += 1
            continue

        current, status = get_progress(media_id)
        cr_ep = item["episode"]

        if current is None:
            log(f"  '{title}' not in your AniList — skipping (add it to AniList first)")
            skipped += 1
            continue

        if cr_ep > (current or 0):
            log(f"  Updating '{title}': ep {current} → {cr_ep}")
            gql(UPDATE_MUTATION, {"mediaId": media_id, "progress": cr_ep}, token=ANILIST_TOKEN)
            updated += 1
            time.sleep(0.6)  # stay well under AniList's 90 req/min limit
        else:
            log(f"  '{title}': AniList ({current}) already at or ahead of Crunchyroll ({cr_ep})")

    log(f"Done — {updated} updated, {skipped} skipped")


if __name__ == "__main__":
    main()
