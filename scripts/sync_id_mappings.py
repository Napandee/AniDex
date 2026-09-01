#!/usr/bin/env python3
"""
Standalone global job — refreshes anidb_mal_mapping_cache (issue #447) from
Fribb/anime-lists (github.com/Fribb/anime-lists), a community-maintained,
auto-updated dataset mapping AniDB/MAL/AniList (and other platform) ids
against each other.

Confirmed live before building this (2026-09-01): the repo pushes on a
roughly weekly cadence (auto-committed, last push same day as this check),
263 stars, `anime-list-mini.json` is ~5.9MB / 42,870 rows, each row already
merged per AniList entry — of the ~20.7k rows carrying an `anilist_id`, 91%
also carry `mal_id` and 63% also carry `anidb_id`. No license file — a real,
documented gap, accepted because this app only ever reads the file (never
redistributes it) and it's the same de facto community-standard table other
trackers (Kitsu/Simkl-adjacent tooling) already depend on the same way.

Unlike sync_filler_data.py/sync_manga_data.py, there's no per-row sync-state
table and no per-title external API calls to rate-limit or space out: this is
a single JSON fetch, fully replacing the local cache table in one pass every
run. A stale row just means the mapping cache is up to a week behind
upstream, same staleness tolerance the two catalog jobs above already accept
for their own weekly/daily data.

Consumed by scripts/sync_plex.py's Guid-based fast path (issue #447) — see
that module's docstring for the exact Guid-prefix formats resolved against
this table.

Runs weekly — see app/main.py's `id_mapping_refresh` scheduler job.

Usage:
    python scripts/sync_id_mappings.py

Requires .env (or env vars) with: DATABASE_URL
"""

import os
import sys
from datetime import datetime, timezone

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

ANIME_LISTS_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-mini.json"

# Polite but not paranoid — this is a single GitHub-hosted static file, not a
# rate-limited API, so no inter-request sleep is needed the way
# sync_manga_data.py/sync_filler_data.py need against their live APIs.
FETCH_TIMEOUT = 30


def fetch_mapping_rows() -> list[dict]:
    """Fetches and returns the raw anime-list-mini.json rows. Each row is a
    dict of platform_id fields per anime; only anidb_id/anilist_id/mal_id are
    used here, the rest (tvdb_id, kitsu_id, etc.) are ignored — this app has
    no use for them today, and re-fetching the same source later is cheap if
    that changes."""
    resp = httpx.get(ANIME_LISTS_URL, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list):
        raise ValueError(f"Expected a JSON array from {ANIME_LISTS_URL}, got {type(rows).__name__}")
    return rows


def parse_mapping_rows(rows: list[dict]) -> list[tuple[int, int | None, int | None]]:
    """Filters to rows carrying a usable anilist_id and coerces anidb_id/mal_id
    to int-or-None — the upstream ids are already integers in the source JSON,
    but this stays defensive against an occasional malformed row rather than
    letting one bad entry abort the whole sync."""
    out = []
    for row in rows:
        anilist_id = row.get("anilist_id")
        if not isinstance(anilist_id, int):
            continue
        anidb_id = row.get("anidb_id")
        anidb_id = anidb_id if isinstance(anidb_id, int) else None
        mal_id = row.get("mal_id")
        mal_id = mal_id if isinstance(mal_id, int) else None
        if anidb_id is None and mal_id is None:
            continue
        out.append((anilist_id, anidb_id, mal_id))
    return out


def db_connect():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def replace_mapping_cache(conn, mappings: list[tuple[int, int | None, int | None]], now: datetime) -> None:
    """Replaces the whole table in one transaction (delete-then-bulk-insert,
    not per-row upserts) — this is a full-refresh dataset with no per-row
    history worth preserving between runs, unlike manga_adaptation_cache
    (which layers new findings onto old ones over many partial runs)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM anidb_mal_mapping_cache")
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO anidb_mal_mapping_cache (anilist_id, anidb_id, mal_id, synced_at) VALUES %s",
            [(anilist_id, anidb_id, mal_id, now) for anilist_id, anidb_id, mal_id in mappings],
        )
    conn.commit()


def main() -> None:
    print("Fetching AniDB/MAL -> AniList id mappings from Fribb/anime-lists...")
    try:
        rows = fetch_mapping_rows()
    except Exception as e:
        print(f"ERROR: failed to fetch/parse {ANIME_LISTS_URL}: {e}")
        sys.exit(1)

    mappings = parse_mapping_rows(rows)
    print(f"Parsed {len(mappings)} usable mappings out of {len(rows)} total rows.")
    if not mappings:
        print("No usable mappings parsed — leaving the existing cache untouched.")
        sys.exit(1)

    conn = db_connect()
    try:
        replace_mapping_cache(conn, mappings, datetime.now(timezone.utc))
    finally:
        conn.close()

    print(f"Done — anidb_mal_mapping_cache now has {len(mappings)} rows.")


if __name__ == "__main__":
    main()
