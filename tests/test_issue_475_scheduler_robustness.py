"""
Coverage for issue #475 — scheduler robustness: explicit max_instances/
misfire_grace_time on every APScheduler job, a log+notify path when a job is
skipped because its previous run is still executing, and confirmation that
the sequential per-user sync loop in _scheduled_full_sync already can't be
blocked indefinitely by one hung provider call (subprocess.run's existing
timeout=1500 bounds it — see the #475 comments on that call and on
_scheduled_full_sync's docstring for why this issue didn't need a second,
redundant timeout layer).

Two test styles, matching existing precedent in this suite:
  - Real Postgres (tests/test_health_signal_notifications.py's pattern) for
    the job-registration tuning and the overlap-notify path, since the
    overlap listener notifies real admin users looked up from the DB.
  - Pure unit tests, no Postgres (tests/test_issue_353_sync_logging.py's
    pattern) for the per-user-timeout loop behavior, mocking subprocess.run
    directly rather than needing real sync credentials/providers.
"""

import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── 1. Every scheduler job declares max_instances/misfire_grace_time ───────────

def test_all_scheduler_jobs_have_explicit_tuning(pg_conn, app_module, client):
    app_module._apply_schedule()
    jobs = app_module._scheduler.get_jobs()
    assert len(jobs) >= 12, "sanity check — _apply_schedule should have registered every job"
    for job in jobs:
        assert job.max_instances == 1, f"{job.id} should declare max_instances=1"
        assert job.misfire_grace_time is not None, f"{job.id} should declare a misfire_grace_time"
        assert job.misfire_grace_time > 0


# ── 2. A job skipped due to still-running is logged and notifies every admin ───

@pytest.fixture(autouse=True)
def _clean_users(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM users")


def _make_user(pg_conn, user_id, is_admin):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, is_admin) "
            "VALUES (%s, 'local', %s, %s, %s)",
            (user_id, f"test{user_id}", f"test{user_id}@example.com", is_admin),
        )


@pytest.fixture()
def sent(app_module, monkeypatch):
    captured = []
    monkeypatch.setattr(
        app_module, "notify", lambda user_id, title, body: captured.append((user_id, title, body))
    )
    return captured


def test_overlap_skip_logs_warning_and_notifies_admins(pg_conn, app_module, client, sent, caplog):
    _make_user(pg_conn, 1, is_admin=True)
    _make_user(pg_conn, 2, is_admin=False)

    fake_event = SimpleNamespace(job_id="daily_sync")
    with caplog.at_level(logging.WARNING, logger="anime_tracker"):
        app_module._on_job_max_instances(fake_event)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("daily_sync" in r.message and "skipped" in r.message for r in warnings)

    assert len(sent) == 1, "only the admin should be notified, not the non-admin user"
    user_id, title, body = sent[0]
    assert user_id == 1
    assert "overlap" in title.lower()
    assert "daily_sync" in body


def test_overlap_skip_with_no_admins_still_logs_no_crash(pg_conn, app_module, client, sent, caplog):
    _make_user(pg_conn, 1, is_admin=False)

    fake_event = SimpleNamespace(job_id="weekly_recommender")
    with caplog.at_level(logging.WARNING, logger="anime_tracker"):
        app_module._on_job_max_instances(fake_event)

    assert sent == []
    assert any("weekly_recommender" in r.message for r in caplog.records)


# ── 3. Per-user timeout: a hung provider call can't block subsequent users ─────
# Pure unit tests, no Postgres — mirrors test_issue_353_sync_logging.py's
# monkeypatched-subprocess pattern rather than needing real sync credentials.

@pytest.fixture()
def sync_app_module(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-475-key")
    import app.main as m

    monkeypatch.setattr(m, "_notify_sync_outcome", lambda *a, **kw: None)
    monkeypatch.setattr(m, "_check_streaming_availability", lambda *a, **kw: None)
    return m


def test_hung_sync_times_out_and_is_recorded_as_error(sync_app_module, monkeypatch):
    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="run_full_sync.py", timeout=kw.get("timeout", 1500))

    monkeypatch.setattr(sync_app_module.subprocess, "run", fake_run)

    sync_app_module._run_sync_task(user_id=1)

    assert sync_app_module._get_sync_state(1)["last_result"] == "error"
    assert sync_app_module._get_sync_state(1)["running"] is False


def test_scheduled_full_sync_continues_past_a_hung_user(sync_app_module, monkeypatch):
    """The real regression this issue guards against: one user's sync hanging
    (here, timing out) must not stop the loop from reaching the next user."""
    processed = []

    def fake_run_sync_task(user_id, script, trigger="scheduled"):
        state = sync_app_module._get_sync_state(user_id)
        if user_id == 1:
            # Simulate exactly what subprocess.run(timeout=1500) does on a hang:
            # raises, caller's state never gets marked done by this function
            # itself — _scheduled_full_sync's own except-block is what resets it.
            raise subprocess.TimeoutExpired(cmd="run_full_sync.py", timeout=1500)
        state["last_result"] = "ok"
        state["running"] = False
        processed.append(user_id)

    monkeypatch.setattr(sync_app_module, "_run_sync_task", fake_run_sync_task)
    monkeypatch.setattr(
        sync_app_module, "_users_with_sync_credentials",
        lambda: [{"id": 1}, {"id": 2}, {"id": 3}],
    )

    sync_app_module._scheduled_full_sync()

    # User 1 hung/timed out — the loop's own except-block must still mark it
    # done (not stuck "running" forever) — and users 2 and 3 must both still
    # have been reached and processed.
    assert sync_app_module._get_sync_state(1)["running"] is False
    assert sync_app_module._get_sync_state(1)["last_result"] == "error"
    assert processed == [2, 3]
