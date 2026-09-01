#!/usr/bin/env python3
"""
Shared AniList query/matching/update helpers, used by every sync_*.py script
(Crunchyroll, Netflix, Prime Video). Each sync script is invoked as its own
subprocess (see run_full_sync.py) with USER_ID/ANILIST_TOKEN/ANILIST_USERNAME/
DATABASE_URL in its env, so this module loads its own env rather than relying
on the importing script having done it first.
"""

import difflib
import json
import os
import re
import time
from datetime import datetime

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

ANILIST_TOKEN = os.environ["ANILIST_TOKEN"]
ANILIST_USERNAME = os.environ["ANILIST_USERNAME"]
ANILIST_API = "https://graphql.anilist.co"
DATABASE_URL = os.environ["DATABASE_URL"]
USER_ID = int(os.environ["USER_ID"])


MAX_RATE_LIMIT_RETRIES = 5


def _record_rate_limit(source: str, retry_after_seconds: float) -> None:
    """Issue #381 — visibility-only marker for Admin > Instance Health, read via
    app.main's _anilist_rate_limit_status(). This module runs as its own
    subprocess (see module docstring), so unlike app/outbox.py's in-process
    app.db, it opens a short-lived connection of its own for this rare event —
    never changes gql()'s own retry behavior above, which is untouched."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO anilist_rate_limit_state (id, source, retry_after_seconds, observed_at)
                VALUES (1, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET
                    source = EXCLUDED.source,
                    retry_after_seconds = EXCLUDED.retry_after_seconds,
                    observed_at = EXCLUDED.observed_at
                """,
                (source, int(retry_after_seconds)),
            )
        conn.commit()
    finally:
        conn.close()


def gql(query: str, variables: dict | None = None, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # AniList's rate limit (30 req/min at time of writing) is real to hit in
    # practice — confirmed live during #48's testing, not just a theoretical edge
    # case: a single sync with several search-fallback matches plus an update call
    # can trip it, especially on a large first-time pull. Retry with the delay
    # AniList itself specifies (Retry-After) rather than failing the whole sync.
    for attempt in range(MAX_RATE_LIMIT_RETRIES):
        resp = httpx.post(
            ANILIST_API,
            json={"query": query, "variables": variables or {}},
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 429 and attempt < MAX_RATE_LIMIT_RETRIES - 1:
            wait = float(resp.headers.get("retry-after", 5))
            _record_rate_limit("anilist_sync_common", wait)
            time.sleep(wait)
            continue
        break

    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"AniList error: {data['errors']}")
    return data["data"]


ALL_LISTS_QUERY = """
query ($userName: String) {
  MediaListCollection(userName: $userName, type: ANIME) {
    lists {
      entries {
        mediaId
        progress
        status
        repeat
        media {
          id
          episodes
          format
          title { romaji english }
        }
      }
    }
  }
}
"""

SEARCH_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    format
    title { romaji english }
  }
}
"""

MEDIA_METADATA_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    format
    episodes
    title { romaji english }
  }
}
"""


def fetch_user_list() -> tuple[dict[int, dict], dict[str, int]]:
    """Fetch all the user's AniList entries in one call.

    Returns (entries_by_id, title_index) where title_index maps
    lowercased romaji/english titles → mediaId for fast title matching.
    """
    data = gql(ALL_LISTS_QUERY, {"userName": ANILIST_USERNAME})
    entries: dict[int, dict] = {}
    title_index: dict[str, int] = {}

    for lst in data["MediaListCollection"]["lists"]:
        for entry in lst["entries"]:
            mid = entry["mediaId"]
            media = entry.get("media") or {}
            title_obj = media.get("title") or {}
            romaji = (title_obj.get("romaji") or "").strip()
            english = (title_obj.get("english") or "").strip()

            entries[mid] = {
                "status": entry["status"],
                "progress": entry["progress"] or 0,
                "repeat": entry["repeat"] or 0,
                "total_episodes": media.get("episodes"),
                "format": media.get("format"),
                "title": english or romaji or "",
            }

            for t in (romaji, english):
                if t:
                    title_index[t.lower()] = mid

    return entries, title_index


def load_user_list_from_db() -> tuple[dict[int, dict], dict[str, int]]:
    """Local-mirror equivalent of fetch_user_list() (issue #99) — reads the AniList
    library from Postgres's library_entries/anime tables instead of making a live
    AniList API call. Crunchyroll and Netflix sync were each independently calling
    fetch_user_list() once per pipeline run (2x redundant today, 3x once Prime Video
    exists); run_full_sync.py now runs the anilist_postgres step first specifically so
    this mirror is fresh before crunchyroll/netflix matching needs it.

    Same return shape as fetch_user_list() — callers can swap between the two with a
    one-line change, and find_anilist_id()/is_plausible_match() are unchanged either
    way, since only the *source* of this data changed, not its shape."""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    library_entries.anime_id AS media_id,
                    library_entries.status,
                    library_entries.progress,
                    library_entries.repeat_count,
                    anime.episodes AS total_episodes,
                    anime.format,
                    anime.title_romaji,
                    anime.title_english
                FROM library_entries
                JOIN anime ON anime.id = library_entries.anime_id
                WHERE library_entries.user_id = %s
            """, (USER_ID,))
            rows = cur.fetchall()
    finally:
        conn.close()

    entries: dict[int, dict] = {}
    title_index: dict[str, int] = {}

    for row in rows:
        mid = row["media_id"]
        romaji = (row["title_romaji"] or "").strip()
        english = (row["title_english"] or "").strip()

        entries[mid] = {
            "status": row["status"],
            "progress": row["progress"] or 0,
            "repeat": row["repeat_count"] or 0,
            "total_episodes": row["total_episodes"],
            "format": row["format"],
            "title": english or romaji or "",
        }

        for t in (romaji, english):
            if t:
                title_index[t.lower()] = mid

    return entries, title_index


# In-process only by default — issue #115: callers with a DB connection (every
# sync_*.py script) seed this from anilist_title_search_cache via seed_search_cache()
# before matching, and persist new entries back via search_cache_snapshot() after, so
# repeat searches for the same permanently-non-matching titles don't cost anything on a
# later sync/retry. Kept as a plain in-process dict here, not DB-backed directly.
#
# This module is NOT a pure AniList-API helper despite that framing having lived here
# historically (issue #414 corrected it) — load_walk_complete()/set_walk_complete(),
# resolve_or_create_user_list_entry()/ensure_anime_stub(), and the shared provider-sync
# scaffolding below (db_connect(), load_title_search_cache(), save_provider_state(),
# etc.) all carry a real psycopg2 dependency. The module docstring's actual boundary is
# "shared by every sync_*.py script", not "no DB access" — this dict just happens to be
# one piece that's genuinely in-process-only, not a module-wide rule.
_search_cache: dict[str, int | None] = {}


def seed_search_cache(entries: dict[str, int | None]) -> None:
    """Pre-populate the in-process search cache from persisted data (issue #115)."""
    _search_cache.update(entries)


def search_cache_snapshot() -> dict[str, int | None]:
    """Current contents of the in-process search cache — includes both seeded and
    newly-resolved entries, so a caller can upsert the whole thing back to
    anilist_title_search_cache unconditionally without tracking what's "new" (issue
    #115). Safe to call at any point, including after an interrupted/timed-out run,
    since it just reflects whatever's been resolved so far."""
    return dict(_search_cache)


# AniList's own sequel-naming conventions (issue #159) — small enough to keep as a
# plain lookup table rather than a general Roman-numeral algorithm; nothing this app
# matches against needs season numbers past single digits.
_ROMAN_NUMERALS = {2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII", 8: "VIII", 9: "IX", 10: "X"}


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def season_suffix_candidates(title: str, season_number: int) -> list[str]:
    """AniList's own sequel-naming conventions to try when CR reports a season
    number > 1 (issue #159). CR's series_title is franchise-level and identical
    across seasons (e.g. "Kingdom" for both season 1 and season 2), while AniList
    suffixes sequel entries with the season (e.g. "Kingdom 2nd Season"). Returned in
    the order they should be tried — most common convention first — since
    find_anilist_id() below stops at the first hit.

    Deliberately does NOT walk AniList's relations graph (SEQUEL edges) to find the
    real sequel title when none of these conventions match — see issue #159's "Out
    of scope"; that gap is what the manual override table exists to cover instead.
    """
    if season_number <= 1:
        return []
    candidates = [
        f"{title} {_ordinal(season_number)} Season",
        f"{title} Season {season_number}",
    ]
    roman = _ROMAN_NUMERALS.get(season_number)
    if roman:
        candidates.append(f"{title} {roman}")
    return candidates


def _search_anilist(query: str) -> int | None:
    """One AniList Media(search:) lookup for an exact query string, cached in
    _search_cache — the same in-process/persisted cache find_anilist_id() always
    used, just factored out so both the bare-title path and the season-suffix
    candidate path below share one cache and one exception-handling shape."""
    if query in _search_cache:
        return _search_cache[query]
    try:
        data = gql(SEARCH_QUERY, {"search": query})
        mid = data["Media"]["id"]
    except Exception:
        mid = None
    _search_cache[query] = mid
    return mid


def find_anilist_id(title: str, title_index: dict[str, int], season_number: int = 1) -> int | None:
    """Return AniList media ID for a watched series title.

    Checks the pre-built title index first (zero API calls for exact matches),
    then falls back to the search endpoint for unrecognised titles.

    season_number (issue #159): when > 1, CR's series_title is the franchise-level
    name (identical across seasons) while AniList suffixes sequel entries (e.g.
    "Kingdom 2nd Season"). In that case, try AniList's own sequel-naming
    conventions (season_suffix_candidates) against the index, then search, before
    falling back to the bare-title lookup below — same fallback ceiling as
    pre-#159 behavior when none of the conventions match. Only sync_crunchyroll.py
    passes season_number != 1 today; every sync_netflix.py call keeps defaulting to
    1, per issue #159's explicit CR-only v1 scope — Netflix's matching path is
    untouched.
    """
    normalized = title.lower()

    if season_number > 1:
        candidates = season_suffix_candidates(title, season_number)
        for candidate in candidates:
            cand_norm = candidate.lower()
            if cand_norm in title_index:
                return title_index[cand_norm]
        for candidate in candidates:
            mid = _search_anilist(candidate)
            if mid:
                title_index[candidate.lower()] = mid  # cache in index for any later duplicates
                return mid

    if normalized in title_index:
        return title_index[normalized]

    mid = _search_anilist(title)
    if mid:
        title_index[normalized] = mid  # cache in index for any later duplicates
    return mid


# Cached per-media_id (issue #387) — the create-decision path below fetches real
# format/episodes/title for a candidate before deciding to create, so the same
# candidate recurring within one sync run (or across the four provider scripts'
# separate subprocess invocations sharing nothing) only costs one AniList call.
# In-process only, like _search_cache — not worth persisting, this is a much
# smaller, shorter-lived lookup than the title-search cache.
_media_metadata_cache: dict[int, dict | None] = {}


def fetch_anilist_media_metadata(media_id: int) -> dict | None:
    """Real AniList format/episodes/title for a candidate media_id (issue #387).

    resolve_or_create_user_list_entry()'s synthetic placeholder used to leave
    format/total_episodes as None for any brand-new entry, which made
    is_plausible_match()'s checks silently no-op on exactly the highest-risk
    path (creating something never tracked before) — confirmed live: 14 of 16
    entries a single Prime Video sync auto-created were false-positive title
    matches to unrelated real anime, none of them caught. Fetching the
    candidate's real metadata before deciding to create closes that gap."""
    if media_id in _media_metadata_cache:
        return _media_metadata_cache[media_id]
    try:
        data = gql(MEDIA_METADATA_QUERY, {"id": media_id})
        media = data["Media"]
        title_obj = media.get("title") or {}
        result = {
            "format": media.get("format"),
            "episodes": media.get("episodes"),
            "title_romaji": (title_obj.get("romaji") or "").strip(),
            "title_english": (title_obj.get("english") or "").strip(),
        }
    except Exception:
        result = None
    _media_metadata_cache[media_id] = result
    return result


_PUNCT_RE = re.compile(r"[^\w\s]")

# Why this check has to exist at all (issue #387): AniList's Media(search:) query
# (SEARCH_QUERY above) is a single best-effort lookup with no "no confident
# match" / null-on-low-confidence behavior — it always returns its closest
# textual guess for whatever string it's given, never an explicit "nothing
# matched." find_anilist_id() getting a non-null media_id back therefore only
# ever means "AniList found something," never "AniList found the right thing" —
# a vague, non-anime query like "Wind River" or "The Boys" doesn't fail to
# resolve, it happily returns whatever real, valid anime AniList's own fuzzy
# ranking considered the closest hit, however unrelated the actual watched
# content was. The old code treated "found something" as sufficient grounds to
# create a brand-new AniList entry; this check exists because that conflation
# is the actual root cause of the 2026-08-26 incident, not a search-quality bug
# on AniList's side — every one of the 14 false positives that day resolved to
# a real, legitimate AniList catalog entry, just the wrong one.
#
# Empirically validated against that real incident data — every false-positive
# match that slipped through that day scores well below this threshold, every
# genuine match scores at or near 1.0, with a wide gap between them:
#   0.286  "Wind River" vs "Otona Joshi no Anime Time"            (wrong — reject)
#   0.400  "The Guest" vs "Gregory Horror Show: The Second Guest" (wrong — reject)
#   0.483  "The Proposal" vs "Ousama no Propose"                  (wrong — reject)
#   0.438  "The Boys" vs "Wakakusa Monogatari... Jo's Boys"       (wrong — reject)
#   0.593  "Season 3" vs "Dorohedoro Season 3"                    (wrong — reject;
#           also separately fixed at the parse layer, see sync_primevideo.py's
#           _parse_season_and_title(), so this case shouldn't even reach here —
#           kept as a reject at this threshold too, defense in depth)
#   1.000  "Beck" vs "BECK"                                       (right — accept)
#   1.000  "Ghost in The Shell: Stand Alone Complex" vs "Koukaku
#           Kidoutai: STAND ALONE COMPLEX" / "Ghost in the Shell:
#           Stand Alone Complex"                                  (right — accept)
TITLE_SIMILARITY_THRESHOLD = 0.6

# A candidate with neither a known format nor a known episode count has nothing
# for is_plausible_match()'s existing checks to compare against — a real,
# already-airing-or-finished anime someone has apparently already watched almost
# always has real publisher metadata on AniList by now, so unknown-on-both-axes
# is itself treated as a weak signal against creating, not neutral. Requires a
# near-exact title match to override it. ("The Proposal" → "Ousama no Propose",
# one of the incident's null-metadata cases, already fails the primary
# TITLE_SIMILARITY_THRESHOLD above on its own — this is genuine defense in depth
# for a case where format/episodes are unknown but the title happens to look
# closer, not the primary rejector for anything seen in the real incident.)
UNKNOWN_METADATA_TITLE_SIMILARITY_THRESHOLD = 0.85


def _normalize_title(s: str | None) -> str:
    return _PUNCT_RE.sub("", (s or "").lower()).strip()


def _title_similarity(watched_title: str, candidate_titles: list[str | None]) -> float:
    """Best-of similarity ratio between the watched title and whichever of the
    candidate's romaji/english titles scores higher — difflib.SequenceMatcher
    over normalized (lowercased, punctuation-stripped) strings. See
    TITLE_SIMILARITY_THRESHOLD's comment for the real data this was validated
    against."""
    normalized_watched = _normalize_title(watched_title)
    if not normalized_watched:
        return 0.0
    best = 0.0
    for candidate in candidate_titles:
        normalized_candidate = _normalize_title(candidate)
        if not normalized_candidate:
            continue
        ratio = difflib.SequenceMatcher(None, normalized_watched, normalized_candidate).ratio()
        best = max(best, ratio)
    return best


def load_walk_complete(conn, provider: str, user_id: int) -> bool:
    """Whether we've ever confirmed reviewing this account's full history for
    `provider` (issue #97/#104, tightened by #387) — distinct from
    compute_fetch_watermark()'s per-series max(), which only tells us the newest
    point we've matched, not whether unreviewed older history might still exist.
    A partial/interrupted full walk otherwise leaves the per-series max looking
    complete when it isn't, silently causing a later incremental sync to stop
    looking further back forever.

    Issue #387 — this used to take a `has_existing_state` fallback ("if
    sync-state rows already exist, assume the walk completed") whenever no
    explicit flag had been set, which was NOT safe as a general rule. That
    fallback existed only to cover CR/Netflix/Plex users who predated this flag
    (see migration 034, which backfills it explicitly, once, for exactly those
    three providers' existing users). Confirmed live: partial dev/debug runs
    against a real account during #352's investigation wrote a few real
    primevideo_sync_state rows before failing; the very next real run trusted
    that leftover state as "walk complete" via this exact fallback, flipping
    full_pull to False on a still-mostly-unwalked year of history and
    auto-creating 16 bogus AniList entries in one run. Only an explicit,
    persisted flag ever counts now — a provider with no flag row simply hasn't
    completed a walk yet, full stop, no inference.

    conn may be None (DRY_RUN mode, Netflix's existing precedent extended to
    every provider by #387's Part 2) — treated as a from-scratch first sync,
    matching every other DRY_RUN-guarded read in these scripts."""
    if conn is None:
        return False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM settings WHERE user_id = %s AND key = %s",
            (user_id, f"{provider}_walk_complete"),
        )
        row = cur.fetchone()
    if row is None:
        return False
    return row["value"].strip().lower() in ("1", "true", "yes")


def set_walk_complete(conn, provider: str, user_id: int, complete: bool) -> None:
    if conn is None:  # DRY_RUN — no DB writes at all, matching the rest of that mode
        return
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO settings (user_id, key, value) VALUES (%s, %s, %s)
            ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value
        """, (user_id, f"{provider}_walk_complete", "true" if complete else "false"))
    conn.commit()


def enqueue_outbox_update(conn, anime_id: int, source: str, status: str | None = None,
                           progress: int | None = None, repeat: int | None = None) -> None:
    """Local-first write (issue #100) — replaces the old direct, synchronous
    SaveMediaListEntry call this used to make. Applies the update to library_entries
    immediately (sync_status='pending', so sync_anilist.py's pull-refresh guard —
    upsert_library_entry()'s `WHERE sync_status = 'synced'` clause — can't clobber it,
    the exact same protection issue #18 already gives UI bulk-edits) and enqueues a
    status_sync_outbox row for the app's single shared worker (app/outbox.py) to
    actually deliver to AniList. This is what lets Crunchyroll/Netflix sync stop
    blocking their own fetch/match loop on AniList's rate limit — delivery is now
    decoupled and collectively rate-limited across every outbox source, not just this
    one provider's own sequential calls.

    Issue #252 — the library_entries write is an upsert (INSERT ... ON CONFLICT DO
    UPDATE), not a bare UPDATE, so this can also *create* a brand-new row for an
    anime the user has no prior library_entries row for at all — same pattern
    app/main.py's bulk_set_status() already uses for UI bulk edits. On the INSERT
    branch (no existing row), status/progress/repeat default to a placeholder
    ('PLANNING'/0/0) only if the caller didn't supply them — in practice every
    caller creating a genuinely new row (sync_crunchyroll.py/sync_netflix.py's
    incremental-sync auto-create path) always passes status='WATCHING' explicitly,
    so that placeholder should never actually be persisted. On the UPDATE branch
    (row already exists), behavior is unchanged from before: only the
    explicitly-provided columns are touched, exactly as the old bare UPDATE did.
    `anime_id` must already exist in the `anime` table before calling this for a
    row that doesn't exist yet — see ensure_anime_stub() below.

    Deliberately does not commit — runs inside the caller's own transaction, so it
    lands atomically together with that call's own state-tracking write
    (save_nf_state()/save_cr_state()), which does the actual commit. This replaces the
    old "anilist_update() must run BEFORE save_state(), not after" ordering
    requirement (a network call that could fail mid-flight) with something strictly
    safer: recording intent and advancing the watermark now either both happen or
    neither does, in one atomic commit — no network round-trip in between to fail.

    Supersedes (deletes) any not-yet-delivered row for this anime first, same as the
    UI bulk-edit endpoint does, so an older queued update can't fire after a newer one.
    """
    if status is None and progress is None and repeat is None:
        raise ValueError("enqueue_outbox_update requires at least one of status/progress/repeat")

    with conn.cursor() as cur:
        set_clauses = ["sync_status = 'pending'"]
        set_params: list = []
        if status is not None:
            set_clauses.append("status = %s")
            set_params.append(status)
        if progress is not None:
            set_clauses.append("progress = %s")
            set_params.append(progress)
        if repeat is not None:
            set_clauses.append("repeat_count = %s")
            set_params.append(repeat)

        insert_params = [
            USER_ID, anime_id,
            status if status is not None else "PLANNING",
            progress if progress is not None else 0,
            repeat if repeat is not None else 0,
        ]
        cur.execute(
            f"""
            INSERT INTO library_entries (user_id, anime_id, status, progress, repeat_count, sync_status)
            VALUES (%s, %s, %s, %s, %s, 'pending')
            ON CONFLICT (user_id, anime_id) DO UPDATE SET {', '.join(set_clauses)}
            """,
            insert_params + set_params,
        )
        cur.execute(
            "DELETE FROM status_sync_outbox WHERE user_id = %s AND anime_id = %s AND state IN ('pending', 'failed')",
            (USER_ID, anime_id),
        )
        cur.execute(
            """
            INSERT INTO status_sync_outbox (user_id, anime_id, source, status, progress, repeat_count)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (USER_ID, anime_id, source, status, progress, repeat),
        )


def resolve_or_create_user_list_entry(media_id: int, title: str, user_list: dict,
                                       full_pull: bool, conn,
                                       watched_format: str | None = None,
                                       watched_episode_count: int | None = None) -> str:
    """Issue #252 — the shared "is this title tracked yet, and if not, should this
    sync create a new AniList entry for it" decision. sync_crunchyroll.py and
    sync_netflix.py had identical `if media_id not in user_list: skip` gates before
    this issue; extracted here so both scripts share one tested implementation
    instead of duplicating the fix (and the regression risk of the two drifting).

    Issue #387 — the create path used to build its synthetic entry with
    format=None/total_episodes=None, then leave plausibility validation entirely
    to the CALLER's own separate is_plausible_match() call afterward — which made
    that check a silent no-op (both fields compared as falsy) on exactly the
    highest-risk path. Confirmed live: 14 of 16 entries a single Prime Video sync
    auto-created were false-positive title matches (e.g. "Wind River" watched on
    Prime matched to the real but unrelated anime "Otona Joshi no Anime Time"),
    none of them caught. Root cause of those 14: find_anilist_id()'s underlying
    AniList search is a single best-effort lookup with no "no confident match"
    signal — it always returns its closest textual guess, never an explicit
    "nothing matched," so a non-null media_id only ever means "AniList found
    something," never "AniList found the right thing." Every one of those 14
    resolved to a real, legitimate AniList catalog entry — just the wrong one.
    Validation now happens HERE, against real AniList metadata fetched for the
    candidate (fetch_anilist_media_metadata()) plus a title-similarity check
    (_title_similarity(), see TITLE_SIMILARITY_THRESHOLD's own comment for why
    that check specifically has to exist), before either ensure_anime_stub() or
    the `user_list` mutation happens — so an implausible candidate leaves no
    trace at all, not even the local `anime` stub row the old ordering used to
    write regardless of the caller's later check.
    Callers should still pass watched_format/watched_episode_count when they have
    them (every provider script's own fetch already computes these); omitting
    them just means the format/episode-count half of the check can't fire, same
    as passing None to is_plausible_match() directly always has.

    Mutates `user_list` in place on a create decision, adding a synthetic
    "brand-new" entry: status=None, progress=0, repeat=0, but — unlike before
    #387 — format/total_episodes are the REAL values fetched from AniList, not
    blank placeholders. process() in every provider script treats status=None as
    the unambiguous "no existing AniList row yet" sentinel (a real AniList
    entry's status is never None) and creates it via the existing outbox path
    with the resolved WATCHING default, at whatever progress the caller's own
    diff logic computes from there.

    Returns one of:
      "existing" — media_id was already in user_list; nothing changed here.
      "create"   — media_id was not tracked, full_pull is False (a normal
                   incremental sync), and the candidate passed real-metadata +
                   title-similarity validation: a synthetic entry was just added
                   to user_list, and — if conn is not None — a matching `anime`
                   stub row was upserted first (see ensure_anime_stub()) so the
                   outbox write's foreign-key constraint succeeds.
      "skip"     — media_id was not tracked, and either full_pull is True (the
                   original, unchanged, conservative behavior for the initial
                   full-history walk or a user-triggered Force Full Resync,
                   #20/#21 — never auto-create from a full backfill), the
                   metadata fetch itself failed (can't validate an unknown
                   candidate — conservative default), or the candidate failed
                   plausibility/title-similarity validation.

    conn may be None (DRY_RUN mode) — no DB write happens in that case, matching
    every other DRY_RUN-guarded write in each provider script; the synthetic
    user_list entry is still added on a would-be "create" so DRY_RUN's
    logging/process() path exercises the same decision it would make for real.
    The real-metadata fetch itself is a read, not a write — it still happens for
    real under DRY_RUN, same as find_anilist_id()'s search calls already do,
    since DRY_RUN's whole point is exercising the real matching/validation path
    without committing anything.
    """
    if media_id in user_list:
        return "existing"
    if full_pull:
        return "skip"

    metadata = fetch_anilist_media_metadata(media_id)
    if metadata is None:
        return "skip"

    synthetic_entry = {
        "status": None, "progress": 0, "repeat": 0,
        "total_episodes": metadata["episodes"], "format": metadata["format"],
        "title": title,
    }
    if not is_plausible_match(synthetic_entry, watched_format, watched_episode_count):
        return "skip"

    similarity = _title_similarity(title, [metadata["title_romaji"], metadata["title_english"]])
    if similarity < TITLE_SIMILARITY_THRESHOLD:
        return "skip"
    if (metadata["format"] is None and metadata["episodes"] is None
            and similarity < UNKNOWN_METADATA_TITLE_SIMILARITY_THRESHOLD):
        return "skip"

    if conn is not None:
        ensure_anime_stub(conn, media_id, title)
    user_list[media_id] = synthetic_entry
    return "create"


def ensure_anime_stub(conn, anime_id: int, title: str) -> None:
    """Issue #252 — the global `anime` table only ever gets a row via
    sync_anilist.py's upsert_anime(), which only ever runs for media on some
    user's *existing* AniList list. A title an incremental Crunchyroll/Netflix
    sync resolves via title-search but that isn't on anyone's list yet (the
    exact new-entry case this issue fixes) therefore has no local `anime` row —
    and library_entries.anime_id / status_sync_outbox.anime_id both carry a
    FOREIGN KEY REFERENCES anime(id), so enqueue_outbox_update() would fail with
    a foreign-key violation without this.

    Inserts the bare minimum (id + title_romaji, the only two NOT NULL columns)
    as a placeholder, deliberately ON CONFLICT DO NOTHING — never overwrites a
    real, richer anime row (e.g. one another user's sync already populated, or
    a prior stub already inserted here). The placeholder title is whatever the
    provider's own watch-history reported (CR's series_title / Netflix's
    seriesTitle), which may not exactly match AniList's romaji title — that's
    fine, it only has to hold until the next sync_anilist.py run enriches it
    with real data, which happens automatically once the entry this creates is
    actually pushed to AniList and shows up in the user's list on the next
    AniList→Postgres sync step (run_full_sync.py always runs that step right
    after crunchyroll/netflix)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (anime_id, title),
        )


def is_plausible_match(entry: dict, watched_format: str | None,
                        watched_episode_count: int | None) -> bool:
    """Guard against title collisions between AniList's catalog and what was
    actually watched. Netflix/Prime have mostly non-anime catalogs (unlike
    Crunchyroll's anime-only one), and a handful of titles share an exact name
    with an existing anime (live-action Death Note, Cowboy Bebop, One Piece).

    Not airtight against a same-format/similar-length collision — catches the
    cheap, common cases. A wrong progress number is a one-click manual fix, the
    same failure-mode ceiling already accepted for Crunchyroll sync.
    """
    al_format = (entry.get("format") or "").upper()
    al_total = entry.get("total_episodes")

    if watched_format and al_format:
        watched_is_movie = watched_format.upper() == "MOVIE"
        al_is_movie = al_format == "MOVIE"
        if watched_is_movie != al_is_movie:
            return False

    if watched_episode_count and al_total and watched_episode_count > al_total:
        return False

    return True


# ── Shared provider-sync scaffolding (issue #414) ────────────────────────────
# Every scripts/sync_*.py script (Crunchyroll/Netflix/Plex/Prime Video) used to
# define its own byte-identical (or near-identical, differing only by a per-provider
# table/label/prefix string) copy of everything below — db_connect(), the
# SYNC_RESULT-emitting helper, the log() prefix wrapper, compute_fetch_watermark(),
# the title-search-cache read/write pair, the per-series state/watermark upsert, and
# the walk_complete/FORCE_FULL_RESYNC orchestration in main(). Consolidated here so a
# future provider script (Jellyfin, #150-152) only has to implement its own HTTP
# client, not copy-paste this scaffolding a fifth time. process()'s actual diff/
# state-machine logic for the three "max-aggregated absolute episode number"
# providers (Crunchyroll/Plex/Prime Video) is also consolidated, as
# process_max_aggregated_progress() below — Netflix's own process() stays entirely
# separate in sync_netflix.py; its delta-count progress model (no absolute episode
# ordinal in Netflix's feed) is a real, documented design difference, not drift.

def db_connect():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def make_logger(prefix: str, user_id: int):
    """Returns a log(msg) function bound to a `[prefix] user={user_id} ` line —
    every provider script's own log() used to hardcode this format with just its
    own prefix string swapped in."""
    def log(msg):
        print(f"[{prefix}] user={user_id} {msg}", flush=True)
    return log


def emit_sync_result(entries_updated: int, entries_fetched: int, full_pull: bool, dry_run: bool) -> None:
    """Issue #46 — the only channel run_full_sync.py has for learning a sync step's
    real entries-touched count back from the subprocess; it parses this exact
    SYNC_RESULT: prefix out of captured stdout. Not emitted when dry_run is True —
    that mode is a local investigation tool, never invoked through run_full_sync.py."""
    if dry_run:
        return
    print(
        f"SYNC_RESULT: {json.dumps({'entries_updated': entries_updated, 'entries_fetched': entries_fetched, 'full_pull': full_pull})}",
        flush=True,
    )


def compute_fetch_watermark(state_map: dict[int, dict]) -> datetime | None:
    """The single cursor a provider's fetch_since() paginates against — the newest
    last_seen_watched_at across all series from the previous sync. Every provider's
    watch-history/viewing-activity feed is one chronological stream across all
    titles, so one watermark is enough to know when pagination has caught up, even
    though state is still tracked per-series."""
    values = [s["last_seen_watched_at"] for s in state_map.values() if s.get("last_seen_watched_at")]
    return max(values) if values else None


def load_title_search_cache(conn) -> dict[str, int | None]:
    """Global (not per-user) AniList title-search cache (issue #115) — a search
    result for a given title string is the same regardless of which user or
    provider is asking, so this is shared across the whole instance. `conn` may
    be None in DRY_RUN, matching that mode's "no DB reads at all" framing."""
    if conn is None:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT title, media_id FROM anilist_title_search_cache")
        return {row["title"]: row["media_id"] for row in cur.fetchall()}


def save_title_search_cache_entry(conn, title: str, media_id: int | None) -> None:
    """Persist one newly-resolved (or confirmed-no-match) title immediately, not
    batched at the end of the run — issue #115's whole point is durability across
    an interrupted sync, same principle as #104's walk_complete fix."""
    if conn is None:  # DRY_RUN — no DB writes at all, matching the rest of that mode
        return
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO anilist_title_search_cache (title, media_id) VALUES (%s, %s)
            ON CONFLICT (title) DO UPDATE SET media_id = EXCLUDED.media_id, cached_at = now()
        """, (title, media_id))
    conn.commit()


def save_provider_state(conn, table: str, user_id: int, anilist_id: int, title: str,
                         last_ep: int, rewatch: bool) -> None:
    """Per-series sync-state upsert shared by Crunchyroll/Plex/Prime Video (issue
    #414) — the three providers whose feeds carry a real absolute episode number
    and so track state in a `*_sync_state` table with this exact column shape
    (user_id, anilist_id, series_title, last_seen_episode, rewatch_in_progress,
    last_synced_at). `table` is always one of the small set of literal table names
    each provider script passes at its own call sites, never external input.
    Netflix's netflix_sync_state has a different shape (no last_seen_episode column
    — see sync_netflix.py's module docstring for why) and keeps its own
    save_nf_state(), not this function."""
    if conn is None:  # DRY_RUN — no DB writes at all, matching the rest of that mode
        return
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO {table} (user_id, anilist_id, series_title, last_seen_episode, rewatch_in_progress, last_synced_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (user_id, anilist_id) DO UPDATE SET
                series_title        = EXCLUDED.series_title,
                last_seen_episode   = EXCLUDED.last_seen_episode,
                rewatch_in_progress = EXCLUDED.rewatch_in_progress,
                last_synced_at      = now()
        """, (user_id, anilist_id, title, last_ep, rewatch))
    conn.commit()


def save_provider_watermark(conn, table: str, user_id: int, anilist_id: int, title: str,
                             watched_at) -> None:
    """Fetch-side watermark bookkeeping only, shared by Crunchyroll/Plex/Prime Video
    (issue #414) — updates just last_seen_watched_at, kept fully separate from
    save_provider_state()'s columns (last_seen_episode/rewatch_in_progress) so this
    never touches process()'s own state writes: the two functions' UPDATE SET
    clauses are column-disjoint, so call order between them within a single sync run
    doesn't matter. Takes the max with whatever's already stored so this can't
    regress the watermark. `table` — see save_provider_state()'s docstring."""
    if conn is None:  # DRY_RUN — no DB writes at all, matching the rest of that mode
        return
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO {table} (user_id, anilist_id, series_title, last_seen_watched_at, last_synced_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (user_id, anilist_id) DO UPDATE SET
                last_seen_watched_at = GREATEST(
                    COALESCE({table}.last_seen_watched_at, EXCLUDED.last_seen_watched_at),
                    EXCLUDED.last_seen_watched_at
                )
        """, (user_id, anilist_id, title, watched_at))
    conn.commit()


def determine_full_pull_watermark(conn, provider: str, user_id: int, state_map: dict,
                                   force_full_resync: bool, log_fn) -> tuple:
    """Shared walk_complete/FORCE_FULL_RESYNC orchestration (issue #414) — every
    provider script's main() had this exact same shape (only the log wording was
    identical too; nothing here varies per provider except what's passed in).
    Returns (watermark, full_pull) — full_pull is True on the initial full-history
    walk or a user-triggered Force Full Resync (#20/#21), in which case callers must
    keep the conservative skip-if-untracked behavior (see
    resolve_or_create_user_list_entry()'s docstring); False on a genuine incremental
    sync, where auto-creating a new AniList entry for a newly-matched title is safe."""
    walk_complete = load_walk_complete(conn, provider, user_id)
    if walk_complete and force_full_resync:
        log_fn("FORCE_FULL_RESYNC set — starting a fresh full walk (a previous walk had already completed)")
        set_walk_complete(conn, provider, user_id, False)  # persisted before the (possibly slow) fetch/process below,
        watermark = None                                   # so an interruption leaves an honest "not complete" state
    elif walk_complete:
        watermark = compute_fetch_watermark(state_map)
    else:
        log_fn("Full walk not yet complete — re-walking full history this run")
        watermark = None
    return watermark, watermark is None


def mark_walk_complete_if_reached_end(conn, provider: str, user_id: int, reached_true_end: bool,
                                       provider_label: str, log_fn) -> None:
    """Issue #104 — only mark the walk complete once every fetched title has actually
    been processed (or there was nothing to process at all); marking it right after
    fetch risks permanently stranding matches if the processing loop gets
    interrupted partway through. Every provider script calls this in the same two
    places: the early "nothing fetched" exit, and after the main processing loop."""
    if reached_true_end:
        set_walk_complete(conn, provider, user_id, True)
        log_fn(f"Reached true end of {provider_label} history — full walk marked complete")


def process_max_aggregated_progress(title: str, watched_ep: int, entry: dict, state: dict | None,
                                     provider_label: str, update_fn, save_state_fn) -> str:
    """Shared process() decision logic for the three providers whose watch-history
    feed carries a real, max-aggregated absolute episode number — Crunchyroll,
    Plex, and Prime Video (issue #414). Netflix's own process() (in
    sync_netflix.py) is NOT covered here: its Falcor feed has no absolute episode
    ordinal at all, so it counts distinct new episodes and adds that to AniList's
    current progress instead — a real, documented design difference (see that
    module's docstring), not the drift this consolidation targets.

    Each of the three callers used to carry this exact branching logic
    near-verbatim, differing only in variable names and the label used in two log
    messages ("CR"/"Plex"/"Prime Video") — their own comments admitted the copy was
    deliberate ("not re-derived here to avoid the two/three drifting"). Includes
    the #328 rewatch-clamp fix (the "new rewatch pass detected" branch below),
    which previously had to be manually ported into each copy by hand.

    update_fn(**kwargs) and save_state_fn(last_ep, rewatch) are the caller's own
    DRY_RUN-routing wrappers (each provider script's _update()/_save_state()) —
    passed in as closures rather than called by name here so each provider's own
    module-level functions (and any test monkeypatching them) stay exactly as much
    in control of the actual enqueue/persist calls as before this consolidation.
    """
    status = entry["status"]
    al_ep = entry["progress"]
    repeat = entry["repeat"]
    total = entry["total_episodes"]

    last_ep = state["last_seen_episode"] if state else al_ep
    rewatch_active = state["rewatch_in_progress"] if state else False

    # ── Issue #252: brand-new AniList entry, not yet on the user's list ──────
    # Callers only ever build a synthetic entry (for an incremental sync's
    # unmatched-title case) with status=None — a real AniList entry's status is
    # never None, so this is an unambiguous "create" sentinel. Must be checked
    # before every other branch below.
    if status is None:
        update_fn(progress=watched_ep, status="WATCHING")
        save_state_fn(watched_ep, False)
        return f"new AniList entry created → WATCHING ep {watched_ep}"

    # ── First-time seeing a COMPLETED series in this provider's history ───────
    # Without prior state we can't safely distinguish "rewatch" from "first sync".
    # Record state and do nothing — next sync will have a baseline.
    if state is None and status == "COMPLETED":
        save_state_fn(watched_ep, False)
        return "first-sync (COMPLETED) — state recorded, no change"

    # ── AniList status already REPEATING but rewatch not recorded in state ────
    if status == "REPEATING" and not rewatch_active:
        if watched_ep > al_ep:
            update_fn(progress=watched_ep)
            save_state_fn(watched_ep, True)
            return f"rewatch detected (already REPEATING) → progress {al_ep} → {watched_ep}"
        save_state_fn(watched_ep, True)
        return "rewatch detected (already REPEATING) — state recorded"

    # ── Rewatch: COMPLETED but this provider's episode dropped below last-seen ─
    # Must come BEFORE the no-change guard: watched_ep < last_ep satisfies that
    # guard and would short-circuit before we ever detect the rewatch.
    if status == "COMPLETED" and watched_ep < (last_ep or total or 999) and not rewatch_active:
        update_fn(progress=watched_ep, status="REPEATING")
        save_state_fn(watched_ep, True)
        return f"rewatch started → REPEATING ep {watched_ep}"

    # ── Rewatch: a new pass restarted while already mid-rewatch (issue #328) ──
    # watched_ep only ever reflects genuinely NEW watch activity (the fetch layer
    # already filtered out anything at/before the fetch watermark), so a fresh
    # episode number LOWER than the stored peak (last_ep) can only mean the user
    # rewatched an earlier episode. Without this branch, watched_ep never
    # numerically exceeds last_ep again until the user watches all the way back
    # past the OLD peak, and the final fallback below would otherwise silently
    # re-lock last_seen_episode at that stale peak on every future sync.
    if rewatch_active and watched_ep < last_ep:
        update_fn(progress=watched_ep)
        save_state_fn(watched_ep, True)
        return f"new rewatch pass detected (was at {last_ep}) → progress reset to {watched_ep}"

    # ── No progress since last sync ───────────────────────────────────────────
    if watched_ep <= last_ep and not rewatch_active:
        if watched_ep <= al_ep:
            save_state_fn(last_ep, rewatch_active)
            return f"no change ({provider_label}={watched_ep}, last_seen={last_ep})"
        # else: this provider is ahead of last_seen but AniList is still ahead of
        # (or equal to) this provider's own current episode — falls through to the
        # normal progress-advance branch below, which is the correct outcome.

    # ── Rewatch completion: REPEATING and reached total episodes ─────────────
    if rewatch_active and total and watched_ep >= total:
        update_fn(progress=watched_ep, status="COMPLETED", repeat=repeat + 1)
        save_state_fn(watched_ep, False)
        return f"rewatch complete → COMPLETED (repeat #{repeat + 1})"

    # ── Progress advance for active rewatch ───────────────────────────────────
    if rewatch_active and watched_ep > al_ep:
        update_fn(progress=watched_ep)
        save_state_fn(watched_ep, True)
        return f"rewatch progress {al_ep} → {watched_ep}"

    # ── DROPPED: user picked it back up ──────────────────────────────────────
    if status == "DROPPED" and watched_ep > last_ep:
        update_fn(progress=watched_ep, status="CURRENT")
        save_state_fn(watched_ep, False)
        return f"resumed after DROP → CURRENT ep {watched_ep}"

    # ── Normal progress advance (CURRENT, PAUSED) ─────────────────────────────
    if watched_ep > al_ep:
        update_fn(progress=watched_ep)
        save_state_fn(watched_ep, False)
        return f"progress {al_ep} → {watched_ep}"

    # Nothing to do
    save_state_fn(max(watched_ep, last_ep), rewatch_active)
    return f"AniList ({al_ep}) already at or ahead of {provider_label} ({watched_ep})"
