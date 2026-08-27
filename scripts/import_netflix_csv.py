#!/usr/bin/env python3
"""
Netflix CSV export import — bootstrap fallback for large/first-time accounts (#98).

Netflix's own "download all" viewing-activity export (Account → Profile & Parental
Controls → Viewing activity → Download all) is a `Title,Date` CSV covering a user's
entire history in one file — no pagination, no rate limit, no timeout risk. This exists
because a live Force Full Resync against a real ~2000-row history (issue #97's own
motivating incident) repeatedly timed out mid-walk. Importing the export sidesteps the
live walk entirely for that one-time backfill.

This is a new *source* feeding the exact same per-series bounded-diff pipeline
sync_netflix.py's live path already uses — not a new/separate write path or matching
threshold. Reused as-is from sync_netflix.py: aggregate_by_series(), _new_episode_count(),
process() (and its _update()/_save_state() writers), load_nf_state(), ensure_table(),
the title-search cache helpers. Reused from anilist_sync_common.py: find_anilist_id(),
is_plausible_match(), load_user_list_from_db(), seed_search_cache(), set_walk_complete()
(issue #387 — moved here from sync_netflix.py, now shared by all four provider scripts).

Invoked as its own subprocess by app/main.py's upload route, same USER_ID/DATABASE_URL/
ANILIST_TOKEN/ANILIST_USERNAME env-var contract as every other scripts/sync_*.py script
(see anilist_sync_common.py's module docstring) — one positional arg, the path to the
uploaded CSV file on disk.

Title-parsing heuristic: Netflix's export encodes TV hierarchy with colons —
"Series: Season N: Episode Title" or, for shows without a season segment,
"Series: Episode Title" — while movies are usually colon-free. A bare "split on the
first colon" can't be trusted on its own: a real movie title can contain a colon too
(e.g. "Mission: Impossible - Dead Reckoning"). So a colon is only ever treated as a
hierarchy separator when either (a) it's followed by a recognizable "Season N" marker
(a pattern that essentially never appears in a movie title), or (b) the text before the
first colon exactly matches a title already in the user's own AniList library — i.e. we
only split on the ambiguous single-colon shape when it's independently corroborated by
data already known to be real. Anything else is treated as one atomic candidate title
(the common case for a bare movie), which then either matches AniList directly (e.g. an
anime movie whose own real title contains a colon) or is skipped as unmatched — the same
graceful-degradation the live path already has for anything find_anilist_id() can't
resolve.

Malformed rows (missing Title/Date, an unparseable date) are skipped and counted, not a
hard failure of the whole import — a completely missing Title/Date column pair (the
whole file doesn't look like a Netflix export) is the one case that IS a hard failure,
raised as ValueError for the caller to surface.

Exit 0 = success (even if some rows were malformed/unmatched), exit 1 = fatal error
(bad file, DB unreachable, etc).
"""

import csv
import json
import os
import re
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

import sync_netflix as nf
from anilist_sync_common import (
    find_anilist_id, is_plausible_match, load_user_list_from_db, seed_search_cache,
)

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
USER_ID = int(os.environ["USER_ID"])

DATE_FORMATS = ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d")

SEASON_MARKER_RE = re.compile(r"^(.+?):\s*Season\s+\d+\b", re.IGNORECASE)


def log(msg: str) -> None:
    print(f"[netflixcsvimport] user={USER_ID} {msg}", flush=True)


def _parse_date(raw: str) -> datetime | None:
    raw = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def extract_series_title(raw_title: str, title_index: dict[str, int]) -> tuple[str, bool]:
    """Returns (candidate_title, is_episode_guess) for one CSV title string — see
    module docstring for the reasoning. `title_index` is the AniList library's
    lowercased-title → mediaId map (same one find_anilist_id() checks first), used
    here purely to corroborate an ambiguous single-colon split, not to do any
    matching itself."""
    raw_title = raw_title.strip()

    m = SEASON_MARKER_RE.match(raw_title)
    if m:
        return m.group(1).strip(), True

    if ":" in raw_title:
        prefix = raw_title.split(":", 1)[0].strip()
        if prefix and prefix.lower() in title_index:
            return prefix, True

    return raw_title, False


def parse_csv_rows(csv_path: str, title_index: dict[str, int]) -> tuple[list[dict], int]:
    """Returns (items, malformed_count). `items` are in the exact shape
    sync_netflix.aggregate_by_series() expects from a raw Falcor viewedItems page
    ("series", "seriesTitle", "title", "movieID", "date" — see that function and
    sync_netflix._is_episode()/_item_watched_at()) so it can be reused unchanged.

    movieID is set to the row's own raw title text — a stable, unique-per-episode
    identifier for a real export (Netflix episode titles are unique per episode),
    which is exactly what _new_episode_count()'s dedup-by-movie_id needs to avoid
    double-counting a title that appears twice (e.g. a rewatch on a different date).

    Raises ValueError if the file doesn't have the expected Title/Date columns at
    all (the one case that's a hard failure, not a per-row skip)."""
    items: list[dict] = []
    malformed = 0

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "Title" not in reader.fieldnames or "Date" not in reader.fieldnames:
            raise ValueError(
                f"CSV missing expected 'Title'/'Date' columns — found: {reader.fieldnames}"
            )

        for row in reader:
            raw_title = (row.get("Title") or "").strip()
            raw_date = (row.get("Date") or "").strip()
            if not raw_title or not raw_date:
                malformed += 1
                continue

            watched_at = _parse_date(raw_date)
            if watched_at is None:
                malformed += 1
                continue

            series_title, is_episode = extract_series_title(raw_title, title_index)
            items.append({
                "series": 1 if is_episode else None,
                "seriesTitle": series_title,
                "title": series_title,
                "movieID": raw_title,
                "date": int(watched_at.timestamp() * 1000),
            })

    return items, malformed


def run_import(csv_path: str) -> dict:
    """Runs the full import against the DB/AniList for USER_ID, returns a summary dict.
    Mirrors sync_netflix.main()'s matching/processing loop (see that function) almost
    line for line — same functions, same order, same skip conditions — just fed from a
    parsed CSV instead of a live Falcor fetch."""
    conn = nf.db_connect()
    nf.ensure_table(conn)
    nf_state_map = nf.load_nf_state(conn)
    log(f"Loaded Netflix sync state for {len(nf_state_map)} series")

    user_list, title_index = load_user_list_from_db()
    log(f"Loaded {len(user_list)} AniList entries, {len(title_index)} title variants indexed")

    title_search_cache = nf.load_title_search_cache(conn)
    seed_search_cache(title_search_cache)
    log(f"Loaded {len(title_search_cache)} cached AniList title-search results")

    try:
        raw_items, malformed = parse_csv_rows(csv_path, title_index)
    except ValueError:
        conn.close()
        raise

    log(f"Parsed {len(raw_items)} usable rows ({malformed} malformed rows skipped)")

    watched_by_series = nf.aggregate_by_series(raw_items)
    log(f"{len(watched_by_series)} unique series/movies found in export")

    updated = skipped = no_change = 0

    for title, agg in sorted(watched_by_series.items()):
        normalized = title.lower()
        in_index_before = normalized in title_index
        media_id = find_anilist_id(title, title_index)
        if not in_index_before and title not in title_search_cache:
            nf.save_title_search_cache_entry(conn, title, media_id)
            title_search_cache[title] = media_id

        if not media_id:
            log(f"  ✗ No AniList match: '{title}'")
            skipped += 1
            continue

        if media_id not in user_list:
            log(f"  ✗ Not in your AniList: '{title}'")
            skipped += 1
            continue

        nf_state = nf_state_map.get(media_id)
        per_series_watermark = nf_state.get("last_seen_watched_at") if nf_state else None
        new_count, watched_at = nf._new_episode_count(agg["items"], per_series_watermark)
        if watched_at is None:
            # Nothing for this series is actually newer than its own last-seen
            # point — same "spurious inclusion" case sync_netflix.main() skips
            # without counting, see its own comment there.
            continue

        entry = dict(user_list[media_id])
        entry["anilist_id"] = media_id
        watched_ep = 1 if agg["watched_format"] == "MOVIE" else entry["progress"] + new_count
        watched = {
            "watched_format": agg["watched_format"],
            "watched_at": watched_at,
            "new_count": new_count,
            "episode": watched_ep,
        }

        if not is_plausible_match(entry, watched["watched_format"], watched_ep or None):
            log(f"  ✗ Implausible match, skipping: '{title}' "
                f"(AniList format={entry.get('format')}, total_eps={entry.get('total_episodes')}; "
                f"watched format={watched['watched_format']}, ep={watched_ep})")
            skipped += 1
            continue

        try:
            result = nf.process(title, watched, entry, nf_state, conn)
            log(f"  '{title}': {result}")
            if "→" in result:
                updated += 1
            else:
                no_change += 1
        except Exception as e:
            log(f"  ERROR processing '{title}': {e}")
            skipped += 1

    # The export is Netflix's own authoritative full history — a successful import
    # (this point reached without a fatal error) fully satisfies "have we ever
    # reviewed the complete history," same as the live path reaching true end of
    # pagination (issue #97's walk_complete). Set unconditionally, not gated on
    # updated > 0 — a CSV full of titles the user's AniList library doesn't (yet)
    # cover is still a complete review of Netflix's own record.
    nf.set_walk_complete(conn, "netflix", USER_ID, True)
    log("Netflix CSV export treated as authoritative — full walk marked complete")

    conn.close()
    return {
        "updated": updated,
        "no_change": no_change,
        "skipped": skipped,
        "malformed": malformed,
        "total_series": len(watched_by_series),
    }


def main():
    log("Starting Netflix CSV import")

    if len(sys.argv) != 2:
        log("ERROR: usage: import_netflix_csv.py <csv_path>")
        sys.exit(1)

    try:
        summary = run_import(sys.argv[1])
    except ValueError as e:
        log(f"ERROR: {e}")
        sys.exit(1)
    except Exception as e:
        log(f"ERROR: import failed: {e}")
        sys.exit(1)

    log(f"Done — {summary['updated']} updated, {summary['no_change']} unchanged, "
        f"{summary['skipped']} skipped/unmatched, {summary['malformed']} malformed rows "
        f"skipped, {summary['total_series']} unique series/movies in export")
    print(f"IMPORT_RESULT: {json.dumps(summary)}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
