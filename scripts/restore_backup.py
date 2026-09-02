#!/usr/bin/env python3
"""Issue #469 — restore an instance_backups export (or an admin/export-all zip;
same shape) back into library_entries/personal_notes, with a minimal `anime`
stub for any title the target DB doesn't already have.

This restores the app's personal-layer data (library status/score/progress,
drop reasons, tags, notes, favorites) — NOT a full disaster-recovery restore.
Per issue #469's own scope: `instance_backups` is an app-data JSON export, not
a pg_dump-level Postgres backup, and this script doesn't create user accounts
(the export never contains user rows — email/password/OAuth identity has no
place in a per-user library export). The realistic scenario this covers is
"library/notes data got corrupted or wiped but the users table and schema are
intact" — restoring a fully-destroyed instance from scratch would also need a
real Postgres-level backup (a separate, larger problem noted in #469 as out of
scope) to get `users` back before this script has anywhere to attach rows to.

Each `{user_id}.json` in the zip is the exact list-of-dicts shape
`_export_user_library()` in app/main.py produces. For every entry:
  - upsert a minimal `anime` row (id = anilist_id) with the fields the export
    carries, without clobbering fields the export doesn't carry (cover image,
    description, external_links, ...) on an existing row — those get filled
    back in by the next real AniList sync regardless, since `anime` is fully
    rebuildable per CLAUDE.md's Data Model section.
  - upsert `library_entries` (status/score/progress/repeat_count/dates).
  - upsert `personal_notes` if the entry carries any personal-layer field.

A `user_id` in the zip that doesn't exist in the target `users` table is
skipped with a warning rather than failing the whole restore (library_entries/
personal_notes both FK to users(id) ON DELETE CASCADE — inserting for a
nonexistent user isn't just wrong, it's a straight FK-violation).

Usage:
    DATABASE_URL=postgresql://... scripts/restore_backup.py --zip backup.zip [--dry-run]
"""

import argparse
import json
import os
import sys
import zipfile

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ["DATABASE_URL"]

# Fields _export_user_library() emits that map straight onto anime columns.
_ANIME_FIELDS = ("title_romaji", "title_english", "format", "episodes", "season", "season_year")
_PERSONAL_NOTE_FIELDS = (
    "drop_reason", "notes", "personal_tags", "mood_tags",
    "watch_next_priority", "anilist_id_override", "favorite",
)


def _upsert_anime(cur, entry: dict) -> None:
    genres = json.dumps(entry.get("genres") or [])
    cur.execute(
        """
        INSERT INTO anime (id, title_romaji, title_english, format, episodes, season, season_year, average_score, genres)
        VALUES (%(anilist_id)s, %(title_romaji)s, %(title_english)s, %(format)s, %(episodes)s, %(season)s, %(season_year)s, %(anilist_score)s, %(genres)s)
        ON CONFLICT (id) DO UPDATE SET
            title_romaji  = EXCLUDED.title_romaji,
            title_english = EXCLUDED.title_english,
            format        = EXCLUDED.format,
            episodes      = EXCLUDED.episodes,
            season        = EXCLUDED.season,
            season_year   = EXCLUDED.season_year,
            average_score = EXCLUDED.average_score,
            genres        = EXCLUDED.genres
        """,
        {**{f: entry.get(f) for f in _ANIME_FIELDS}, "anilist_id": entry["anilist_id"],
         "anilist_score": entry.get("anilist_score"), "genres": genres},
    )


def _upsert_library_entry(cur, user_id: int, entry: dict) -> None:
    cur.execute(
        """
        INSERT INTO library_entries (user_id, anime_id, status, score, progress, repeat_count, start_date, finish_date, anilist_updated_at)
        VALUES (%(user_id)s, %(anime_id)s, %(status)s, %(score)s, %(progress)s, %(repeat_count)s, %(start_date)s, %(finish_date)s, %(anilist_updated_at)s)
        ON CONFLICT (user_id, anime_id) DO UPDATE SET
            status              = EXCLUDED.status,
            score               = EXCLUDED.score,
            progress            = EXCLUDED.progress,
            repeat_count        = EXCLUDED.repeat_count,
            start_date          = EXCLUDED.start_date,
            finish_date         = EXCLUDED.finish_date,
            anilist_updated_at  = EXCLUDED.anilist_updated_at
        """,
        {
            "user_id": user_id,
            "anime_id": entry["anilist_id"],
            "status": entry["status"],
            "score": entry.get("my_score"),
            "progress": entry.get("progress"),
            "repeat_count": entry.get("repeat_count"),
            "start_date": entry.get("start_date"),
            "finish_date": entry.get("finish_date"),
            "anilist_updated_at": entry.get("anilist_updated_at"),
        },
    )


def _upsert_personal_notes(cur, user_id: int, entry: dict) -> None:
    if not any(entry.get(f) for f in _PERSONAL_NOTE_FIELDS):
        return
    cur.execute(
        """
        INSERT INTO personal_notes (user_id, anime_id, drop_reason, notes, personal_tags, mood_tags, watch_next_priority, anilist_id_override, favorite)
        VALUES (%(user_id)s, %(anime_id)s, %(drop_reason)s, %(notes)s, %(personal_tags)s, %(mood_tags)s, %(watch_next_priority)s, %(anilist_id_override)s, %(favorite)s)
        ON CONFLICT (user_id, anime_id) DO UPDATE SET
            drop_reason          = EXCLUDED.drop_reason,
            notes                = EXCLUDED.notes,
            personal_tags        = EXCLUDED.personal_tags,
            mood_tags            = EXCLUDED.mood_tags,
            watch_next_priority  = EXCLUDED.watch_next_priority,
            anilist_id_override  = EXCLUDED.anilist_id_override,
            favorite             = EXCLUDED.favorite
        """,
        {
            "user_id": user_id,
            "anime_id": entry["anilist_id"],
            "drop_reason": entry.get("drop_reason"),
            "notes": entry.get("notes"),
            "personal_tags": json.dumps(entry.get("personal_tags") or []),
            "mood_tags": json.dumps(entry.get("mood_tags") or []),
            "watch_next_priority": entry.get("watch_next_priority"),
            "anilist_id_override": entry.get("anilist_id_override"),
            "favorite": entry.get("favorite"),
        },
    )


def restore(zip_path: str, dry_run: bool) -> None:
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id FROM users")
            known_user_ids = {row["id"] for row in cur.fetchall()}

            entries_restored = 0
            entries_skipped = 0
            users_skipped = set()

            with zipfile.ZipFile(zip_path) as zf:
                for name in zf.namelist():
                    if not name.endswith(".json"):
                        continue
                    # Names are "{user_id}.json" (scheduled backups) or
                    # "{user_id}_{email}.json" (admin/export-all) — same prefix either way.
                    user_id = int(name.split("_", 1)[0].removesuffix(".json"))

                    if user_id not in known_user_ids:
                        users_skipped.add(user_id)
                        continue

                    entries = json.loads(zf.read(name))
                    for entry in entries:
                        if dry_run:
                            entries_restored += 1
                            continue
                        try:
                            _upsert_anime(cur, entry)
                            _upsert_library_entry(cur, user_id, entry)
                            _upsert_personal_notes(cur, user_id, entry)
                            entries_restored += 1
                        except Exception as exc:  # noqa: BLE001 — report and keep going, one bad row shouldn't sink the whole restore
                            entries_skipped += 1
                            print(f"  ! skipped anilist_id={entry.get('anilist_id')} for user {user_id}: {exc}", file=sys.stderr)

            if dry_run:
                conn.rollback()
                print(f"[dry-run] would restore {entries_restored} entries; database untouched.")
            else:
                conn.commit()
                print(f"Restored {entries_restored} entries ({entries_skipped} skipped on error).")

            if users_skipped:
                print(
                    f"Skipped {len(users_skipped)} user id(s) not present in the target `users` table: "
                    f"{sorted(users_skipped)} — restore each user's own account first (this script never "
                    f"creates users; the export doesn't contain account/credential data).",
                    file=sys.stderr,
                )
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zip", required=True, help="Path to a downloaded instance_backups / export-all zip")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report what would be restored, without writing to the database")
    args = parser.parse_args()

    if not os.path.isfile(args.zip):
        print(f"No such file: {args.zip}", file=sys.stderr)
        sys.exit(1)

    restore(args.zip, args.dry_run)


if __name__ == "__main__":
    main()
