"""Background worker draining status_sync_outbox — the async AniList push half of
local-first writes. Originally built for UI bulk-status edits (issue #18); issue #100
extended the same outbox to also carry Crunchyroll/Netflix/Prime-originated progress
updates, so this one worker now delivers every source under a single, collective
AniList rate-limit budget instead of each provider script making its own independent,
blocking SaveMediaListEntry calls. The `source` column on status_sync_outbox exists
purely for observability — the drain/delivery logic below treats every row identically
regardless of where it came from.

A local-first write lands in library_entries (marked sync_status='pending') and an
outbox row immediately; this module's worker thread then pushes each queued item to
AniList and, on success, flips the row back to sync_status='synced' and deletes the
outbox row. Rows only ever sit in status_sync_outbox while in flight or failed —
a successful push removes the row rather than marking it, so the drain query and
outbox size both stay small in the steady state.

Deliberately not APScheduler: the existing scheduled jobs are all coarse CronTriggers
(tightest is hourly), but this needs to drain promptly right after an enqueue. Runs as
a plain daemon thread inside the app process instead, woken via a threading.Event on
enqueue with a periodic fallback sweep (in case a wake() is missed, or the app restarts
with rows still pending/in_progress from before) — the fallback sweep is also how rows
enqueued by a scripts/sync_*.py subprocess (which can't reach this in-process Event)
get picked up, within SWEEP_INTERVAL seconds of being written.
"""

import logging
import threading
import time

import httpx

from app import db

log = logging.getLogger("anime_tracker")

SWEEP_INTERVAL = 30       # seconds — fallback poll if a wake() was missed or the app restarted
INTER_ITEM_SLEEP = 0.8    # seconds — same AniList pacing precedent as sync_anilist.py's INTER_CHUNK_SLEEP
MAX_ATTEMPTS = 5          # matches sync_anilist.py's gql(retries=5) precedent
BASE_BACKOFF = 2.0        # seconds, doubles per attempt: 2, 4, 8, 16s

_wake_event = threading.Event()
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None


def wake() -> None:
    """Call right after committing new/reset outbox rows to drain promptly instead of
    waiting for the next fallback sweep."""
    _wake_event.set()


def _record_rate_limit(source: str, retry_after_seconds: int) -> None:
    """Issue #381 — visibility-only marker for Admin > Instance Health, read via
    app.main's _anilist_rate_limit_status(). Never changes retry behavior itself
    (that's the existing MAX_ATTEMPTS/BASE_BACKOFF logic below, untouched)."""
    db.execute(
        """
        INSERT INTO anilist_rate_limit_state (id, source, retry_after_seconds, observed_at)
        VALUES (1, %s, %s, now())
        ON CONFLICT (id) DO UPDATE SET
            source = EXCLUDED.source,
            retry_after_seconds = EXCLUDED.retry_after_seconds,
            observed_at = EXCLUDED.observed_at
        """,
        (source, retry_after_seconds),
    )


def _push_one(item: dict) -> tuple[bool, str | None]:
    """One push attempt to AniList for a single outbox item. Returns (ok, error).

    Issue #100 — builds variables from whichever of status/progress/repeat_count the
    row actually carries (a UI bulk-status edit only ever sets status; a Crunchyroll/
    Netflix-originated row may carry progress alone, or progress with status/repeat) —
    the schema guarantees at least one is non-null (status_sync_outbox's CHECK
    constraint), so there's always something to send."""
    # Imported lazily (not at module top-level) to avoid a circular import — app.main
    # imports this module for its startup/shutdown hooks and the bulk endpoint, and
    # this module needs a few of app.main's constants/helpers in return.
    from app.main import (
        ANILIST_API,
        ANILIST_MOCK,
        SAVE_MEDIA_LIST_MUTATION,
        STATUS_TO_ANILIST,
        _get_anilist_token,
    )

    if ANILIST_MOCK:
        return True, None

    token = _get_anilist_token(item["user_id"])
    if not token:
        return False, "AniList token not configured"

    variables: dict = {"mediaId": item["anime_id"]}
    if item["status"] is not None:
        variables["status"] = STATUS_TO_ANILIST.get(item["status"], item["status"])
    if item["progress"] is not None:
        variables["progress"] = item["progress"]
    if item["repeat_count"] is not None:
        variables["repeat"] = item["repeat_count"]

    try:
        resp = httpx.post(
            ANILIST_API,
            json={"query": SAVE_MEDIA_LIST_MUTATION, "variables": variables},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            _record_rate_limit("outbox", retry_after)
            return False, f"rate_limited:{retry_after}"
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            return False, str(data["errors"])
        return True, None
    except Exception as e:
        return False, str(e)


def _process_item(item: dict) -> None:
    db.execute(
        "UPDATE status_sync_outbox SET state = 'in_progress', updated_at = now() WHERE id = %s",
        (item["id"],),
    )

    attempt = item["attempts"]
    error = None
    while attempt < MAX_ATTEMPTS:
        ok, error = _push_one(item)
        if ok:
            # The target field values were already written to library_entries when
            # this row was enqueued (local-first — see enqueue_outbox_update() /
            # bulk_set_status()); the only thing left to do here is flip the dirty
            # flag back to 'synced' now that AniList has actually confirmed it, and
            # remove the row (issue #100 — no longer duplicates status/progress/repeat
            # into this UPDATE, since that would just be re-writing the same values
            # library_entries already holds).
            with db.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE library_entries SET sync_status = 'synced' WHERE user_id = %s AND anime_id = %s",
                        (item["user_id"], item["anime_id"]),
                    )
                    cur.execute("DELETE FROM status_sync_outbox WHERE id = %s", (item["id"],))
                conn.commit()
            return

        if error and error.startswith("rate_limited:"):
            wait = int(error.split(":", 1)[1])
            time.sleep(min(wait, 60))
            continue  # doesn't burn a normal attempt — AniList itself asked us to wait

        attempt += 1
        db.execute(
            "UPDATE status_sync_outbox SET attempts = %s, last_error = %s, updated_at = now() WHERE id = %s",
            (attempt, error, item["id"]),
        )
        if attempt < MAX_ATTEMPTS:
            time.sleep(BASE_BACKOFF * (2 ** (attempt - 1)))

    log.error(
        "Outbox item %s (source=%s) exhausted retries pushing anime_id=%s "
        "status=%s progress=%s repeat=%s for user_id=%s: %s",
        item["id"], item["source"], item["anime_id"],
        item["status"], item["progress"], item["repeat_count"], item["user_id"], error,
    )
    db.execute(
        "UPDATE status_sync_outbox SET state = 'failed', attempts = %s, last_error = %s, updated_at = now() WHERE id = %s",
        (attempt, error, item["id"]),
    )


def _drain_once() -> None:
    # 'in_progress' is included so a row left mid-push by a hard-killed previous run
    # gets picked back up on the next sweep after restart. 'failed' is deliberately
    # excluded — only the retry endpoint resets a row back to 'pending', which the
    # next sweep then sees naturally.
    rows = db.fetchall(
        "SELECT * FROM status_sync_outbox WHERE state IN ('pending', 'in_progress') "
        "ORDER BY created_at LIMIT 100"
    )
    for item in rows:
        if _stop_event.is_set():
            return
        _process_item(item)
        time.sleep(INTER_ITEM_SLEEP)


def _run() -> None:
    log.info("Outbox worker started")
    while not _stop_event.is_set():
        try:
            _drain_once()
        except Exception:
            log.exception("Outbox drain cycle failed")
        _wake_event.wait(timeout=SWEEP_INTERVAL)
        _wake_event.clear()
    log.info("Outbox worker stopped")


def start_worker() -> None:
    global _worker_thread
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_run, name="outbox-worker", daemon=True)
    _worker_thread.start()


def stop_worker() -> None:
    _stop_event.set()
    _wake_event.set()  # unblock wait() so the loop observes _stop_event promptly
    if _worker_thread:
        _worker_thread.join(timeout=5)
