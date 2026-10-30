"""ANIDEX_DISABLE_SCHEDULER=1 keeps the APScheduler jobs from being registered or
started (demo record stack). Default (unset) behaviour is unchanged."""

from apscheduler.schedulers.background import BackgroundScheduler


def _fresh_scheduler(app_module, monkeypatch):
    sched = BackgroundScheduler(timezone="UTC")
    monkeypatch.setattr(app_module, "_scheduler", sched)
    monkeypatch.setattr(app_module.outbox, "start_worker", lambda: None)
    return sched


def test_scheduler_disabled_by_env(pg_conn, app_module, monkeypatch):
    sched = _fresh_scheduler(app_module, monkeypatch)
    monkeypatch.setenv("ANIDEX_DISABLE_SCHEDULER", "1")

    app_module.startup()

    assert sched.get_jobs() == []
    assert not sched.running

    # Shutdown must not trip over the never-started scheduler.
    monkeypatch.setattr(app_module.outbox, "stop_worker", lambda: None)
    monkeypatch.setattr(app_module.db, "close_pool", lambda: None)
    app_module.shutdown()


def test_scheduler_enabled_by_default(pg_conn, app_module, monkeypatch):
    sched = _fresh_scheduler(app_module, monkeypatch)
    monkeypatch.delenv("ANIDEX_DISABLE_SCHEDULER", raising=False)

    app_module.startup()
    try:
        assert sched.running
        assert len(sched.get_jobs()) >= 12
    finally:
        sched.shutdown(wait=False)
