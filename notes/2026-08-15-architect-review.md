# Architect review — baseline snapshot

Not tied to a tracking issue — this is a standing-back review of the whole project
(database fit, file/module size, worker vs. in-process job architecture, test
coverage), done to establish a baseline to check back against, not a spike ahead of
planned work. If any finding below turns into actual work, file the normal
issue-first (`.github/ISSUE_TEMPLATE/task.md`) at that point — the items in
"Candidate follow-ups" are explicitly *not* filed as issues yet.

Full narrative review (rationale, alternatives considered, why each pivot was or
wasn't recommended) is in the session transcript; this note captures the durable
parts — the verdicts and the numbers to diff against next time.

## Verdicts

- **Postgres**: right choice, keep it. Schema (`schema.sql`) correctly separates
  AniList-sourced / personal-layer / auth tables via FK+cascade+`user_id` scoping;
  JSONB columns are backed by GIN indexes, not unindexed. SQLite would remove a
  container but lose that indexing and the multi-user FK model, for no real benefit
  given Postgres already holds live data. Not revisiting unless scale changes
  materially.
- **`app/main.py`**: the one real "getting big" concern. Routes already break
  cleanly by prefix (see metrics below) — splitting into FastAPI `APIRouter`s is a
  mechanical, low-risk refactor, not a rewrite. Not recommending service
  decomposition (microservices) — wrong tool for single-container homelab scale.
- **`scripts/*.py`**: healthy. Already decomposed by data source with shared logic
  extracted (`anilist_sync_common.py`). `sync_netflix.py` and `run_recommender.py`
  are the two growing largest — watch, not urgent.
- **Sync/recommender as real workers (task queue)**: not recommended. Workload is
  "once a day, a handful of users" — APScheduler in-process is right-sized. A
  broker (Celery/RQ+Redis) would add ops burden to solve a concurrency problem this
  project doesn't have. Open question, not yet checked: does the manual "Sync Now"
  trigger block the request thread? If so, fix is a background thread in-process,
  not a queue.
- **Testing**: the real ROI gap. See metrics below — `app/main.py` (all HTTP-facing
  behavior) and `run_recommender.py` (pure scoring logic) have zero coverage. The
  2026-08-13 fastapi-bump outage (see `CLAUDE.local.md`) is a direct symptom of this
  shape of gap, not a one-off.
- **Everything else** (stack choice, deployment pipeline, multi-user/auth design,
  secrets handling): solid, no changes recommended.

## Baseline metrics — 2026-08-15

| Metric | Value |
|---|---|
| `app/main.py` line count | 2,427 |
| `app/main.py` route count (`@app.get/post/put/delete`) | 45 |
| `app/main.py` top-level `def`/`class` count | 75 |
| Route breakdown by prefix | `/api` 13, `/auth` 10, `/settings` 8, `/admin` 5, `/recommendations` 2, `/anime` 2, `/upcoming` `/stats` `/search` `/queue` `/` 1 each |
| `scripts/sync_netflix.py` | 552 lines |
| `scripts/run_recommender.py` | 495 lines |
| `scripts/sync_anilist.py` | 343 lines |
| `scripts/sync_crunchyroll.py` | 299 lines |
| `scripts/anilist_sync_common.py` | 193 lines |
| `scripts/sync_airing_schedule.py` | 149 lines |
| `scripts/run_full_sync.py` | 185 lines |
| `schema.sql` | 246 lines, 14 tables, 7 indexes (2 GIN) |
| `tests/` total | 342 lines across 4 files |
| Modules with test coverage | `anilist_sync_common.py`, `sync_crunchyroll.py`, `sync_netflix.py` |
| Modules with **zero** test coverage | `app/main.py` (2,427 lines), `run_recommender.py` (495 lines), `sync_anilist.py`, `sync_airing_schedule.py` |
| `app/db.py` connection handling | `psycopg2.connect()` per query, no pooling |
| Deployment | single app container + single Postgres container, GH Actions → self-hosted runner, no queue/broker |

## Candidate follow-ups (not filed as issues — file if/when actually starting work)

1. Split `app/main.py` into `APIRouter`s by prefix (`auth`, `admin`, `settings`,
   `api`, `pages`).
2. Add test coverage — start with `run_recommender.py`'s scoring functions (pure,
   no DB needed), then `TestClient`-based route tests for `app/main.py` (natural to
   pair with #1, since a router split gives natural test-file boundaries).
3. Check whether "Sync Now" blocks the request thread; if so, move it to a
   background thread (not a queue).
4. Revisit `app/db.py` connection pooling only if the app is ever observed to be
   slow under concurrent load — not before.

## How to use this in a week

Re-run the metrics above (`wc -l`, route/grep counts) and compare. Growth in
`app/main.py` or continued zero coverage on it/`run_recommender.py` without any of
the candidate follow-ups being picked up is the signal that this should turn into
actual filed issues rather than staying a "someday" note.
