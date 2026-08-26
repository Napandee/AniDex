"""
Coverage for issue #353 — app.main's _run_sync_task() used to only log the
captured run_full_sync.py subprocess output on an outright non-zero exit.
But run_full_sync.py's own exit code is 0 for BOTH "ok" and "partial" overall
status (partial means some provider step failed but anilist_postgres itself
succeeded) — so a partial run's real per-step error detail (the actual
exception/HTTP status/traceback, not just sync_log.steps[]'s generic wrapper
message) was captured once and then silently discarded, with nothing in
docker logs pointing at it. Confirmed the hard way in production (issue
#352) — the only way to see a real Prime Video 403 was to manually re-run
the sync inside the container.

Pure unit tests: subprocess.run and the post-sync notification/streaming-check
hooks are all monkeypatched, so no real DB or process is touched. Uses
pytest's caplog to assert on the actual logged content.
"""

import logging
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture()
def app_module(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-353-key")
    import app.main as m

    # These run after the branch under test and each hit the DB — no-op them so
    # this stays a pure unit test of the logging behavior itself.
    monkeypatch.setattr(m, "_notify_sync_outcome", lambda *a, **kw: None)
    monkeypatch.setattr(m, "_check_streaming_availability", lambda *a, **kw: None)
    return m


def _fake_result(returncode, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_partial_run_logs_real_output_as_warning(app_module, monkeypatch, caplog):
    real_error_detail = (
        "[primevideosync] user=1 ERROR: Prime Video fetch failed: "
        "Client error '403 Forbidden' for url 'https://www.primevideo.com/api/getWatchHistorySettingsPage'\n"
        "[run_full_sync] user=1 Full sync pipeline complete — overall status: partial\n"
    )
    monkeypatch.setattr(
        app_module.subprocess, "run",
        lambda *a, **kw: _fake_result(0, stdout=real_error_detail),
    )

    with caplog.at_level(logging.WARNING, logger="anime_tracker"):
        app_module._run_sync_task(user_id=1)

    assert app_module._get_sync_state(1)["last_result"] == "ok"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "partial failure" in warnings[0].message
    # The real per-step detail — the whole reason #353 exists — must actually be
    # in the logged text, not just a generic "sync completed" line.
    assert "403 Forbidden" in warnings[0].message


def test_fully_ok_run_logs_at_info_not_warning(app_module, monkeypatch, caplog):
    clean_output = "[run_full_sync] user=1 Full sync pipeline complete — overall status: ok\n"
    monkeypatch.setattr(
        app_module.subprocess, "run",
        lambda *a, **kw: _fake_result(0, stdout=clean_output),
    )

    with caplog.at_level(logging.INFO, logger="anime_tracker"):
        app_module._run_sync_task(user_id=1)

    assert app_module._get_sync_state(1)["last_result"] == "ok"
    assert not any(r.levelno == logging.WARNING for r in caplog.records)
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("Sync completed" in r.message for r in infos)


def test_outright_failure_still_logs_real_output_as_error(app_module, monkeypatch, caplog):
    crash_detail = "Traceback (most recent call last):\n  ...\npsycopg2.errors.InFailedSqlTransaction: boom\n"
    monkeypatch.setattr(
        app_module.subprocess, "run",
        lambda *a, **kw: _fake_result(1, stdout=crash_detail),
    )

    with caplog.at_level(logging.ERROR, logger="anime_tracker"):
        app_module._run_sync_task(user_id=1)

    assert app_module._get_sync_state(1)["last_result"] == "error"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("InFailedSqlTransaction" in r.message for r in errors)


def test_output_truncated_to_last_4000_chars_not_unbounded(app_module, monkeypatch, caplog):
    huge_output = ("x" * 10000) + "\noverall status: partial\nreal error here"
    monkeypatch.setattr(
        app_module.subprocess, "run",
        lambda *a, **kw: _fake_result(0, stdout=huge_output),
    )

    with caplog.at_level(logging.WARNING, logger="anime_tracker"):
        app_module._run_sync_task(user_id=1)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "real error here" in warnings[0].message  # the tail survives truncation
    assert len(warnings[0].message) < 5000  # not the full 10000+ chars
