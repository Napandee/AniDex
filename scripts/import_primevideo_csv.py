#!/usr/bin/env python3
"""
Prime Video watch-history CSV import — fallback for #389, alongside the existing
live cookie-header sync (scripts/sync_primevideo.py), never a replacement for it.

Why this exists: the live sync sits behind Amazon's "account settings" session
tier, which expires far faster than general browsing/playback — independently
confirmed both by universal-trakt-scrobbler's own maintainer and directly in that
project's shipping source (see anilist_sync_common.py's load_walk_complete()
docstring and migration 034's header for the full incident writeup). This import
path sidesteps that entirely: a one-time export while the user is genuinely
present, same as capturing a fresh cookie already requires, but not something
that needs redoing every time the session tier silently expires.

*** CSV FORMAT CONFIDENCE — READ BEFORE TRUSTING THIS AGAINST A REAL ACCOUNT ***
Unlike Netflix, Amazon does NOT offer a one-click "download my Prime Video watch
history as CSV" button inside account settings — this was the original premise of
issue #389, and research done while implementing it found that premise doesn't
hold up. Every community tool for exporting this data (gitzain/prime-video-
history-to-csv, twocaretcat/caret-collective's watch-history-exporter,
universal-trakt-scrobbler itself) screen-scrapes or replays the live API from
inside an authenticated browser tab — the exact same account-settings session
problem this issue exists to work around, not a genuine alternative to it. The
one real official mechanism found is Amazon's "Request My Data" privacy portal
(privacy.amazon.com), which lists a "Digital.PrimeVideo.ViewingHistory" category
— but its exact export format (CSV vs Amazon's own Ion serialization vs JSON),
turnaround time, and column layout could NOT be confirmed via research; no solid
source documents it precisely.

Given that, this parser is deliberately format-agnostic rather than hardcoded to
an assumed column layout: it reads the CSV header row and matches column names
against plausible synonyms (_TITLE_COLUMN_SYNONYMS etc. below), case-
insensitively, optional columns degrade gracefully, and it fails loudly with the
actual header row it found if it can't confidently identify a title+date pair —
never silently guesses a wrong mapping. This has NOT been validated against a
real exported file. Before this is considered production-ready: get a real
"Request My Data" Prime Video export, confirm the column synonyms below actually
match (extend _*_COLUMN_SYNONYMS if not), and confirm the date-format list covers
whatever format that export actually uses.

Conservative by design, unlike sync_netflix.py's CSV importer's own precedent:
never auto-creates a new AniList entry (only updates already-tracked titles,
same "if media_id not in user_list: skip" rule Netflix's importer uses) AND,
unlike Netflix's importer, does NOT mark primevideo_walk_complete=True on
completion. Given #387 happened *because* full_pull incorrectly became False
once walk_complete flipped True, and given genuine uncertainty here about
whether a Request-My-Data export is even a complete history (unlike Netflix's
clearly-labeled "download all"), leaving that flag untouched means this import
can never be the thing that flips a future live sync run into auto-create mode
— only a real, fully-walked live sync (or an explicit Force Full Resync) can.

Reused as-is from sync_primevideo.py: db_connect(), ensure_table(),
load_pv_state(), save_pv_state()/save_watermark() (via process()), process()
itself, load_title_search_cache()/save_title_search_cache_entry(),
_parse_season_and_title()/_parse_episode_number() (Prime's own inconsistent
season/episode text-embedding heuristics, reused here in case a real export
turns out to embed season/episode in the title string the same way the live
API's display_title does, rather than as separate columns). Reused from
anilist_sync_common.py: find_anilist_id(), is_plausible_match(),
load_user_list_from_db(), seed_search_cache().

Invoked as its own subprocess by app/main.py's upload route, same
USER_ID/DATABASE_URL/ANILIST_TOKEN/ANILIST_USERNAME env-var contract as every
other scripts/sync_*.py script — one positional arg, the path to the uploaded
CSV file on disk.

Malformed rows (missing title/date, an unparseable date, no usable episode
signal at all) are skipped and counted, not a hard failure of the whole import —
a completely missing title/date column pair (the file doesn't look like any
recognized export shape at all) is the one case that IS a hard failure, raised
as ValueError for the caller to surface.

Exit 0 = success (even if some rows were malformed/unmatched), exit 1 = fatal
error (bad file, DB unreachable, etc).
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

import sync_primevideo as pv
from anilist_sync_common import (
    find_anilist_id, is_plausible_match, load_user_list_from_db, seed_search_cache,
)

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
USER_ID = int(os.environ["USER_ID"])

# See module docstring's "CSV FORMAT CONFIDENCE" section — none of these are
# confirmed against a real export, they're a best-effort net of plausible names
# gathered from how Amazon/community tools describe this data elsewhere.
_TITLE_COLUMN_SYNONYMS = {"title", "content title", "program title", "content name", "name", "series"}
_DATE_COLUMN_SYNONYMS = {"date", "timestamp", "watched date", "view date", "playback time", "watched on", "date watched"}
_SEASON_COLUMN_SYNONYMS = {"season", "season number"}
_EPISODE_COLUMN_SYNONYMS = {"episode", "episode number"}
_TYPE_COLUMN_SYNONYMS = {"content type", "type", "title type", "media type"}

# Beyond Netflix's own DATE_FORMATS precedent — Amazon's internal data (confirmed
# live in sync_primevideo.py's own API responses) uses epoch milliseconds, and a
# privacy-export tends toward ISO 8601 with a time component, so both are covered
# defensively alongside the simpler date-only formats.
_DATE_FORMATS = (
    "%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S",
    "%B %d, %Y", "%d %B %Y",
)


def log(msg: str) -> None:
    print(f"[primevideocsvimport] user={USER_ID} {msg}", flush=True)


def _find_column(fieldnames: list[str], synonyms: set[str]) -> str | None:
    for name in fieldnames:
        if name.strip().lower() in synonyms:
            return name
    return None


def _parse_date(raw: str) -> datetime | None:
    raw = raw.strip()
    if raw.isdigit():
        # Epoch seconds vs milliseconds — Amazon's own live API uses ms (see
        # sync_primevideo.py's PrimeVideoHistory), 13 digits is the ms-since-2001+
        # range vs 10 digits for seconds; a plain length check is enough here,
        # there's no ambiguity in practice for a real watch-history timestamp.
        try:
            value = int(raw)
            ts = value / 1000 if len(raw) >= 13 else value
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_csv_rows(csv_path: str) -> tuple[dict[tuple[str, int], dict], int]:
    """Returns ({(normalized_title, season): {"title", "season", "episode",
    "watched_at", "watched_format"}}, malformed_count) — same aggregated shape
    sync_primevideo.py's parse_items() produces from the live fetch (episode is
    the most-recently-watched one for that title+season, not the highest — same
    "most recently watched wins" rule, so a rewatch started from episode 1
    surfaces as episode 1 for process() to detect).

    Raises ValueError if the file doesn't have a recognizable title+date column
    pair at all — the one hard failure, not a per-row skip."""
    best: dict[tuple[str, int], dict] = {}
    malformed = 0

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        title_col = _find_column(fieldnames, _TITLE_COLUMN_SYNONYMS)
        date_col = _find_column(fieldnames, _DATE_COLUMN_SYNONYMS)
        if not title_col or not date_col:
            raise ValueError(
                f"CSV doesn't look like a recognized Prime Video watch-history export "
                f"— couldn't find a title/date column pair. Found columns: {fieldnames}"
            )
        season_col = _find_column(fieldnames, _SEASON_COLUMN_SYNONYMS)
        episode_col = _find_column(fieldnames, _EPISODE_COLUMN_SYNONYMS)
        type_col = _find_column(fieldnames, _TYPE_COLUMN_SYNONYMS)

        for row in reader:
            raw_title = (row.get(title_col) or "").strip()
            raw_date = (row.get(date_col) or "").strip()
            if not raw_title or not raw_date:
                malformed += 1
                continue

            watched_at = _parse_date(raw_date)
            if watched_at is None:
                malformed += 1
                continue

            is_movie = False
            if type_col:
                is_movie = "movie" in (row.get(type_col) or "").lower()

            if season_col and (row.get(season_col) or "").strip().isdigit():
                title, season = raw_title, int(row[season_col])
            else:
                # No dedicated season column — fall back to Prime's own
                # inconsistent season-embedded-in-title heuristic (see
                # sync_primevideo.py's _parse_season_and_title() docstring for
                # the real variants it was built against).
                title, season = pv._parse_season_and_title(raw_title)

            if not title:
                malformed += 1
                continue

            episode: int | None
            if is_movie:
                episode = 1
                watched_format = "MOVIE"
            elif episode_col and (row.get(episode_col) or "").strip().isdigit():
                episode = int(row[episode_col])
                watched_format = "TV"
            else:
                episode = pv._parse_episode_number(raw_title)
                watched_format = "TV"

            if episode is None:
                # No episode/season signal at all and not flagged as a movie —
                # nothing for process()'s absolute-episode-number logic to work
                # with. Graceful degradation, same as any other unmatched row.
                malformed += 1
                continue

            key = (title.lower(), season)
            existing = best.get(key)
            if not existing or watched_at > existing["watched_at"]:
                best[key] = {
                    "title": title, "season": season, "episode": episode,
                    "watched_at": watched_at, "watched_format": watched_format,
                }

    return best, malformed


def run_import(csv_path: str) -> dict:
    """Runs the full import against the DB/AniList for USER_ID, returns a summary
    dict. Mirrors sync_primevideo.py's main() matching/processing loop closely —
    same functions, same skip conditions — fed from a parsed CSV instead of a
    live fetch. See module docstring for why this never auto-creates a new
    AniList entry and never touches primevideo_walk_complete, both deliberate
    divergences from sync_netflix.py's own CSV importer precedent."""
    conn = pv.db_connect()
    pv.ensure_table(conn)
    state_map = pv.load_pv_state(conn)
    log(f"Loaded Prime Video sync state for {len(state_map)} series")

    user_list, title_index = load_user_list_from_db()
    log(f"Loaded {len(user_list)} AniList entries, {len(title_index)} title variants indexed")

    title_search_cache = pv.load_title_search_cache(conn)
    seed_search_cache(title_search_cache)
    log(f"Loaded {len(title_search_cache)} cached AniList title-search results")

    try:
        watched_by_key, malformed = parse_csv_rows(csv_path)
    except ValueError:
        conn.close()
        raise

    log(f"Parsed {len(watched_by_key)} unique season/movie(s) ({malformed} malformed rows skipped)")

    updated = skipped = no_change = 0

    for (_norm_title, season), watched in sorted(watched_by_key.items()):
        title = watched["title"]
        media_id = find_anilist_id(title, title_index, season_number=season)
        if title not in title_search_cache:
            pv.save_title_search_cache_entry(conn, title, media_id)
            title_search_cache[title] = media_id

        if not media_id:
            log(f"  ✗ No AniList match: '{title}'" + (f" (season {season})" if season > 1 else ""))
            skipped += 1
            continue

        # Deliberately never resolve_or_create_user_list_entry() here — a CSV
        # import is a bulk backfill exactly like the live sync's first full walk,
        # and #387 happened because that exact scenario auto-created dozens of
        # false-positive entries. Same conservative "never auto-create from a
        # bulk import" rule sync_netflix.py's own CSV importer already uses.
        if media_id not in user_list:
            log(f"  ✗ Not in your AniList: '{title}'")
            skipped += 1
            continue

        entry = dict(user_list[media_id])
        entry["anilist_id"] = media_id

        if not is_plausible_match(entry, watched["watched_format"], watched["episode"]):
            log(f"  ✗ Implausible match, skipping: '{title}' "
                f"(AniList format={entry.get('format')}, total_eps={entry.get('total_episodes')}; "
                f"watched format={watched['watched_format']}, ep={watched['episode']})")
            skipped += 1
            continue

        pv_state = state_map.get(media_id)
        try:
            result = pv.process(title, watched["episode"], entry, pv_state, conn)
            log(f"  '{title}': {result}")
            pv.save_watermark(conn, media_id, title, watched["watched_at"])
            if "→" in result:
                updated += 1
            else:
                no_change += 1
        except Exception as e:
            log(f"  ERROR processing '{title}': {e}")
            skipped += 1

    conn.close()
    return {
        "updated": updated,
        "no_change": no_change,
        "skipped": skipped,
        "malformed": malformed,
        "total_series": len(watched_by_key),
    }


def main():
    log("Starting Prime Video CSV import")

    if len(sys.argv) != 2:
        log("ERROR: usage: import_primevideo_csv.py <csv_path>")
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
