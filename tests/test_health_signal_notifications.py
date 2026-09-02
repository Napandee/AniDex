"""
Coverage for issue #466 — pushing the pending-migration-count (#380) and AniList
rate-limit (#381) health signals through the existing notify() dispatcher
(app/notify.py, issue #51) to every admin user, instead of leaving them pull-only on
the admin Instance Health page.

Verified against a real Postgres, same pattern as tests/test_admin_instance_health.py
and tests/test_planning_coverage_notify.py: exercises app.main._check_health_signals
directly against the real schema.sql shape, with app.main.notify monkeypatched to
capture what would have been sent instead of hitting a real channel.

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available, so `pytest tests/` still collects and passes on a machine with no Postgres
running.

Covers:
  1. No admin users → no notify() calls attempted at all (nobody to tell).
  2. A pending-migration count notifies every admin, naming the count.
  3. The same pending-migration count on a later tick does not re-notify (dedup).
  4. A pending-migration count that grows further does re-notify.
  5. A pending-migration count that resolves (drops to 0) then recurs re-notifies —
     the dedup state is cleared on resolution, not stuck forever.
  6. A non-admin user is never notified, even when an admin is.
  7. An active AniList rate-limit event notifies every admin, naming the source.
  8. The same rate-limit event (same observed_at) does not re-notify.
  9. An inactive (elapsed) rate-limit event does not notify at all.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM users")
        cur.execute("DELETE FROM instance_config")
        cur.execute("DELETE FROM migration_state")
        cur.execute("DELETE FROM anilist_rate_limit_state")


@pytest.fixture()
def sent(app_module, monkeypatch):
    captured = []
    monkeypatch.setattr(
        app_module, "notify", lambda user_id, title, body: captured.append((user_id, title, body))
    )
    return captured


def _make_user(pg_conn, user_id, is_admin):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, is_admin) "
            "VALUES (%s, 'local', %s, %s, %s)",
            (user_id, f"test{user_id}", f"test{user_id}@example.com", is_admin),
        )


def _set_pending_migrations(pg_conn, app_module, count):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO migration_state (id, highest_applied_migration) VALUES (1, %s) "
            "ON CONFLICT (id) DO UPDATE SET highest_applied_migration = EXCLUDED.highest_applied_migration",
            (app_module.LATEST_MIGRATION - count,),
        )


def _set_rate_limit(pg_conn, source, retry_after_seconds, ago_seconds=0):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anilist_rate_limit_state (id, source, retry_after_seconds, observed_at) "
            "VALUES (1, %s, %s, now() - make_interval(secs => %s)) "
            "ON CONFLICT (id) DO UPDATE SET source = EXCLUDED.source, "
            "retry_after_seconds = EXCLUDED.retry_after_seconds, observed_at = EXCLUDED.observed_at",
            (source, retry_after_seconds, ago_seconds),
        )


# ── Pending migrations ──────────────────────────────────────────────────────


def test_no_admin_users_no_alert_attempted(pg_conn, app_module, sent):
    _make_user(pg_conn, 1, is_admin=False)
    _set_pending_migrations(pg_conn, app_module, 3)

    app_module._check_health_signals()

    assert sent == []


def test_pending_migrations_notifies_every_admin(pg_conn, app_module, sent):
    _make_user(pg_conn, 1, is_admin=True)
    _make_user(pg_conn, 2, is_admin=True)
    _set_pending_migrations(pg_conn, app_module, 3)

    app_module._check_health_signals()

    assert len(sent) == 2
    user_ids = {call[0] for call in sent}
    assert user_ids == {1, 2}
    for _, title, body in sent:
        assert "pending migration" in title.lower()
        assert "3" in body


def test_non_admin_user_never_notified(pg_conn, app_module, sent):
    _make_user(pg_conn, 1, is_admin=True)
    _make_user(pg_conn, 2, is_admin=False)
    _set_pending_migrations(pg_conn, app_module, 1)

    app_module._check_health_signals()

    user_ids = {call[0] for call in sent}
    assert user_ids == {1}


def test_same_pending_count_does_not_renotify(pg_conn, app_module, sent):
    _make_user(pg_conn, 1, is_admin=True)
    _set_pending_migrations(pg_conn, app_module, 2)

    app_module._check_health_signals()
    assert len(sent) == 1

    app_module._check_health_signals()
    assert len(sent) == 1  # still just the one alert, no repeat on the same value


def test_growing_pending_count_renotifies(pg_conn, app_module, sent):
    _make_user(pg_conn, 1, is_admin=True)
    _set_pending_migrations(pg_conn, app_module, 2)
    app_module._check_health_signals()
    assert len(sent) == 1

    _set_pending_migrations(pg_conn, app_module, 5)
    app_module._check_health_signals()

    assert len(sent) == 2
    assert "5" in sent[1][2]


def test_resolved_then_recurring_pending_count_renotifies(pg_conn, app_module, sent):
    _make_user(pg_conn, 1, is_admin=True)
    _set_pending_migrations(pg_conn, app_module, 2)
    app_module._check_health_signals()
    assert len(sent) == 1

    # Resolved: marker catches up to LATEST_MIGRATION, 0 pending.
    _set_pending_migrations(pg_conn, app_module, 0)
    app_module._check_health_signals()
    assert len(sent) == 1  # no alert for "back to healthy"

    # Recurs with the exact same count as before — must alert again, since the
    # dedup state was cleared on resolution rather than staying stuck at "2".
    _set_pending_migrations(pg_conn, app_module, 2)
    app_module._check_health_signals()
    assert len(sent) == 2


def test_no_migration_state_row_no_alert(pg_conn, app_module, sent):
    """migration_state with no row means "unknown", not "pending" — must not
    alert on unknown state, same contract _pending_migration_count() already
    has for the admin page (issue #380)."""
    _make_user(pg_conn, 1, is_admin=True)

    app_module._check_health_signals()

    assert sent == []


# ── AniList rate limit ──────────────────────────────────────────────────────


def test_active_rate_limit_notifies_every_admin(pg_conn, app_module, sent):
    _make_user(pg_conn, 1, is_admin=True)
    _set_rate_limit(pg_conn, "outbox", retry_after_seconds=3600)

    app_module._check_health_signals()

    assert len(sent) == 1
    _, title, body = sent[0]
    assert "rate limit" in title.lower()
    assert "outbox" in body


def test_same_rate_limit_event_does_not_renotify(pg_conn, app_module, sent):
    _make_user(pg_conn, 1, is_admin=True)
    _set_rate_limit(pg_conn, "outbox", retry_after_seconds=3600)

    app_module._check_health_signals()
    assert len(sent) == 1

    app_module._check_health_signals()
    assert len(sent) == 1  # same observed_at, no repeat


def test_new_rate_limit_event_renotifies(pg_conn, app_module, sent):
    _make_user(pg_conn, 1, is_admin=True)
    _set_rate_limit(pg_conn, "outbox", retry_after_seconds=3600)
    app_module._check_health_signals()
    assert len(sent) == 1

    # A later, distinct 429 (different observed_at) while still active.
    _set_rate_limit(pg_conn, "sync_anilist", retry_after_seconds=60)
    app_module._check_health_signals()

    assert len(sent) == 2
    assert "sync_anilist" in sent[1][2]


def test_inactive_rate_limit_no_alert(pg_conn, app_module, sent):
    """A 429 from hours ago, past its own retry-after window, isn't a current
    problem — same "active" contract _anilist_rate_limit_status() already has
    for the admin page (issue #381)."""
    _make_user(pg_conn, 1, is_admin=True)
    _set_rate_limit(pg_conn, "outbox", retry_after_seconds=60, ago_seconds=7200)

    app_module._check_health_signals()

    assert sent == []


def test_no_rate_limit_row_no_alert(pg_conn, app_module, sent):
    _make_user(pg_conn, 1, is_admin=True)

    app_module._check_health_signals()

    assert sent == []
