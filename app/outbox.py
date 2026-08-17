"""Background worker draining status_sync_outbox — the async AniList push half of
local-first bulk status edits (issue #18).

A bulk status edit lands in library_entries (marked sync_status='pending') and an
outbox row immediately; this module's worker thread then pushes each queued item to
AniList and, on success, flips the row back to sync_status='synced' and deletes the
outbox row. Rows only ever sit in status_sync_outbox while in flight or failed —
a successful push removes the row rather than marking it, so the drain query and
outbox size both stay small in the steady state.

Deliberately not APScheduler: the existing scheduled jobs are all coarse CronTriggers
(tightest is hourly), but this needs to drain promptly right after an enqueue. Runs as
a plain daemon thread inside the app process instead, woken via a threading.Event on
enqueue with a periodic fallback sweep (in case a wake() is missed, or the app restarts
with rows still pending/in_progress from before).
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


def _push_one(item: dict) -> tuple[bool, str | None]:
    """One push attempt to AniList for a single outbox item. Returns (ok, error)."""
    # Imported lazily (not at module top-level) to avoid a circular import — app.main
    # imports this module for its startup/shutdown hooks and the bulk endpoint, and
    # this module needs a few of app.main's constants/helpers in return.
    from app.main import (
        ANILIST_API,
        ANILIST_MOCK,
        SAVE_STATUS_MUTATION,
        STATUS_TO_ANILIST,
        _get_anilist_token,
    )

    if ANILIST_MOCK:
        return True, None

    token = _get_anilist_token(item["user_id"])
    if not token:
        return False, "AniList token not configured"

    try:
        resp = httpx.post(
            ANILIST_API,
            json={
                "query": SAVE_STATUS_MUTATION,
                "variables": {
                    "mediaId": item["anime_id"],
                    "status": STATUS_TO_ANILIST.get(item["status"], item["status"]),
                },
            },
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code == 429:
            return False, f"rate_limited:{resp.headers.get('Retry-After', 60)}"
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
            with db.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE library_entries SET status = %s, sync_status = 'synced'
                        WHERE user_id = %s AND anime_id = %s
                        """,
                        (item["status"], item["user_id"], item["anime_id"]),
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
        "Outbox item %s exhausted retries pushing anime_id=%s status=%s for user_id=%s: %s",
        item["id"], item["anime_id"], item["status"], item["user_id"], error,
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
