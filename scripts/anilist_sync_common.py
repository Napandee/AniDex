#!/usr/bin/env python3
"""
Shared AniList query/matching/update helpers, used by every sync_*.py script
(Crunchyroll, Netflix, Prime Video). Each sync script is invoked as its own
subprocess (see run_full_sync.py) with USER_ID/ANILIST_TOKEN/ANILIST_USERNAME/
DATABASE_URL in its env, so this module loads its own env rather than relying
on the importing script having done it first.
"""

import difflib
import os
import re
import time

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
# later sync/retry. Kept as a plain in-process dict here (not DB-backed directly) so
# this module stays a pure AniList-API helper with no psycopg2 dependency of its own.
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
