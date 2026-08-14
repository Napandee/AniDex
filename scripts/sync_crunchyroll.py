#!/usr/bin/env python3
"""
Crunchyroll → AniList sync (safe, state-aware).

Reads the history.json produced by crunchyexporter-cli fetch, compares against
the last-known state in Postgres, then updates AniList with the correct logic:

  CURRENT / PAUSED   + CR ahead          → advance progress
  DROPPED            + CR ahead          → advance progress + set CURRENT
  COMPLETED          + CR ep < last-seen → set REPEATING, advance progress (rewatch started)
  REPEATING (no state recorded)          → record rewatch as active, advance progress if needed
  REPEATING          + CR ahead          → advance progress
  REPEATING          + CR >= total eps   → set COMPLETED, increment repeat counter

Note: CR history is max-aggregated (highest episode ever watched per series).
A rewatch starting from ep 1 won't lower cr_ep unless old episodes age out of
history. The REPEATING handler is therefore the reliable rewatch detection path
— the user changes status to REPEATING in the app and sync picks it up on next
run; the COMPLETED+drop-below-last-seen path catches history-trimming edge cases.

Never goes backwards on progress. Never touches score or notes.

Exit 0 = success, Exit 1 = fatal error.
"""

import json
import os
import sys

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from anilist_sync_common import anilist_update, fetch_user_list, find_anilist_id

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
USER_ID = int(os.environ["USER_ID"])
HISTORY_PATH = os.environ.get("HISTORY_PATH", "/crunchyexporter/data/history.json")


def log(msg):
    print(f"[crunchysync] user={USER_ID} {msg}", flush=True)


# ── History parsing ───────────────────────────────────────────────────────────

def load_history(path: str) -> dict[str, int]:
    """Parse history.json → {series_title: most_recently_watched_episode}.

    Uses watched_at (ISO-8601) to pick the most recently watched episode per
    series, not the highest episode number. This correctly handles rewatches:
    if ep 12 was watched months ago and ep 1 was watched yesterday, we return
    ep 1 as the current position so the sync can detect a rewatch in progress.
    """
    with open(path) as f:
        data = json.load(f)

    # crunchyexporter-cli stores episodes as a list under an "episodes" key.
    if isinstance(data, dict) and "episodes" in data:
        raw = data["episodes"]
        items = raw if isinstance(raw, list) else list(raw.values())
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = list(data.values())
    else:
        return {}

    # Track (watched_at, episode_number) per series; ISO-8601 sorts lexicographically.
    # Items without watched_at fall back to "" which sorts before any real date.
    best: dict[str, tuple[str, int]] = {}
    for item in items:
        title = (item.get("series_title") or "").strip()
        if not title:
            continue
        try:
            ep = int(float(item.get("episode_number") or 0))
        except (ValueError, TypeError):
            ep = 0
        if ep == 0:
            continue
        watched_at = item.get("watched_at") or ""
        if title not in best or watched_at > best[title][0]:
            best[title] = (watched_at, ep)

    return {title: ep for title, (_, ep) in best.items()}


# ── Postgres ──────────────────────────────────────────────────────────────────

def db_connect():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def ensure_table(conn):
    """Defensive fallback if run against a DB that somehow skipped schema.sql/migrations
    — matches the current multi-user schema (composite PK) so it can never create a
    table shape schema.sql wouldn't recognize. In normal operation this is a no-op
    since the table already exists by the time any sync script runs."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cr_sync_state (
                user_id                INTEGER NOT NULL,
                anilist_id             INTEGER NOT NULL,
                series_title           TEXT,
                last_seen_episode      INTEGER NOT NULL DEFAULT 0,
                rewatch_in_progress    BOOLEAN NOT NULL DEFAULT FALSE,
                last_synced_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id, anilist_id)
            )
        """)
    conn.commit()


def load_cr_state(conn) -> dict[int, dict]:
    """Return {anilist_id: {last_seen_episode, rewatch_in_progress}} for this user."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT anilist_id, last_seen_episode, rewatch_in_progress "
            "FROM cr_sync_state WHERE user_id = %s",
            (USER_ID,),
        )
        return {row["anilist_id"]: dict(row) for row in cur.fetchall()}


def save_cr_state(conn, anilist_id: int, title: str, last_ep: int, rewatch: bool):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO cr_sync_state (user_id, anilist_id, series_title, last_seen_episode, rewatch_in_progress, last_synced_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (user_id, anilist_id) DO UPDATE SET
                series_title        = EXCLUDED.series_title,
                last_seen_episode   = EXCLUDED.last_seen_episode,
                rewatch_in_progress = EXCLUDED.rewatch_in_progress,
                last_synced_at      = now()
        """, (USER_ID, anilist_id, title, last_ep, rewatch))
    conn.commit()


# ── Sync logic ────────────────────────────────────────────────────────────────

def process(title: str, cr_ep: int, entry: dict, cr_state: dict | None,
            conn) -> str:
    """
    Apply update logic for one series. Returns a short description of action taken.
    cr_state may be None on first sync for this series.
    """
    status = entry["status"]
    al_ep = entry["progress"]
    repeat = entry["repeat"]
    total = entry["total_episodes"]
    al_id = None  # resolved by caller; passed via entry for convenience
    anilist_id = entry["anilist_id"]

    last_ep = cr_state["last_seen_episode"] if cr_state else al_ep
    rewatch_active = cr_state["rewatch_in_progress"] if cr_state else False

    # ── First-time seeing a COMPLETED series in CR history ────────────────────
    # Without prior state we can't safely distinguish "rewatch" from "first sync".
    # Record state and do nothing — next sync will have a baseline.
    if cr_state is None and status == "COMPLETED":
        save_cr_state(conn, anilist_id, title, cr_ep, False)
        return "first-sync (COMPLETED) — state recorded, no change"

    # ── AniList status already REPEATING but rewatch not recorded in state ────
    # Handles: user changes status to REPEATING in the app/AniList before sync
    # runs. Set rewatch_active so subsequent syncs advance progress correctly.
    # anilist_update() must run BEFORE save_cr_state(), not after — if the write
    # throws (a transient AniList error, e.g. rate-limiting), saving state anyway
    # would mark this watermark as handled while the real progress update never
    # landed, and since the sync only re-considers activity newer than the stored
    # watermark, that miss would never get retried on a later run. Confirmed live
    # via the identical bug in sync_netflix.py's equivalent branch (issue #48).
    if status == "REPEATING" and not rewatch_active:
        if cr_ep > al_ep:
            anilist_update(anilist_id, progress=cr_ep)
            save_cr_state(conn, anilist_id, title, cr_ep, True)
            return f"rewatch detected (already REPEATING) → progress {al_ep} → {cr_ep}"
        save_cr_state(conn, anilist_id, title, cr_ep, True)
        return "rewatch detected (already REPEATING) — state recorded"

    # ── Rewatch: COMPLETED but CR episode dropped below last-seen ────────────
    # Must come BEFORE the no-change guard: cr_ep < last_ep satisfies that guard
    # and would short-circuit before we ever detect the rewatch.
    if status == "COMPLETED" and cr_ep < (last_ep or total or 999) and not rewatch_active:
        anilist_update(anilist_id, progress=cr_ep, status="REPEATING")
        save_cr_state(conn, anilist_id, title, cr_ep, True)
        return f"rewatch started → REPEATING ep {cr_ep}"

    # ── No progress since last sync ───────────────────────────────────────────
    if cr_ep <= last_ep and not rewatch_active:
        if cr_ep > al_ep:
            # AniList is behind but we already processed this — shouldn't happen often
            pass
        else:
            save_cr_state(conn, anilist_id, title, last_ep, rewatch_active)
            return f"no change (CR={cr_ep}, last_seen={last_ep})"

    # ── Rewatch completion: REPEATING and reached total episodes ─────────────
    if rewatch_active and total and cr_ep >= total:
        anilist_update(anilist_id, progress=cr_ep, status="COMPLETED", repeat=repeat + 1)
        save_cr_state(conn, anilist_id, title, cr_ep, False)
        return f"rewatch complete → COMPLETED (repeat #{repeat + 1})"

    # ── Progress advance for active rewatch ───────────────────────────────────
    if rewatch_active and cr_ep > al_ep:
        anilist_update(anilist_id, progress=cr_ep)
        save_cr_state(conn, anilist_id, title, cr_ep, True)
        return f"rewatch progress {al_ep} → {cr_ep}"

    # ── DROPPED: user picked it back up ──────────────────────────────────────
    if status == "DROPPED" and cr_ep > last_ep:
        anilist_update(anilist_id, progress=cr_ep, status="CURRENT")
        save_cr_state(conn, anilist_id, title, cr_ep, False)
        return f"resumed after DROP → CURRENT ep {cr_ep}"

    # ── Normal progress advance (CURRENT, PAUSED) ─────────────────────────────
    if cr_ep > al_ep:
        anilist_update(anilist_id, progress=cr_ep)
        save_cr_state(conn, anilist_id, title, cr_ep, False)
        return f"progress {al_ep} → {cr_ep}"

    # Nothing to do
    save_cr_state(conn, anilist_id, title, max(cr_ep, last_ep), rewatch_active)
    return f"AniList ({al_ep}) already at or ahead of CR ({cr_ep})"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("Starting Crunchyroll → AniList sync")

    if not os.path.exists(HISTORY_PATH):
        log(f"ERROR: history file not found at {HISTORY_PATH}")
        sys.exit(1)

    log(f"Loading history from {HISTORY_PATH}...")
    history = load_history(HISTORY_PATH)
    log(f"Parsed {len(history)} unique series from CR history")

    if not history:
        log("No history found — nothing to do")
        sys.exit(0)

    log("Fetching AniList library (one call)...")
    user_list, title_index = fetch_user_list()
    log(f"Loaded {len(user_list)} AniList entries, {len(title_index)} title variants indexed")

    conn = db_connect()
    ensure_table(conn)
    cr_state_map = load_cr_state(conn)
    log(f"Loaded CR sync state for {len(cr_state_map)} series")

    updated = skipped = no_change = index_hits = search_hits = 0

    for title, cr_ep in sorted(history.items()):
        normalized = title.lower()
        in_index_before = normalized in title_index
        media_id = find_anilist_id(title, title_index)
        if in_index_before and media_id:
            index_hits += 1
        elif media_id:
            search_hits += 1
        if not media_id:
            log(f"  ✗ No AniList match: '{title}'")
            skipped += 1
            continue

        if media_id not in user_list:
            log(f"  ✗ Not in your AniList: '{title}'")
            skipped += 1
            continue

        entry = dict(user_list[media_id])
        entry["anilist_id"] = media_id
        cr_state = cr_state_map.get(media_id)

        try:
            result = process(title, cr_ep, entry, cr_state, conn)
            log(f"  '{title}': {result}")
            if "→" in result:
                updated += 1
            else:
                no_change += 1
        except Exception as e:
            log(f"  ERROR processing '{title}': {e}")
            skipped += 1

    conn.close()
    log(f"Done — {updated} updated, {no_change} unchanged, {skipped} skipped/unmatched "
        f"({index_hits} index hits, {search_hits} API searches)")


if __name__ == "__main__":
    main()
