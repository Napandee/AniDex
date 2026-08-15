"""
Regression coverage for run_full_sync.py's per-step control flow (issue #62). The core
thing being guarded: a failure in one step (crunchyroll/netflix) must not prevent the
anilist_postgres step from running — that used to sys.exit(1) on the first failing step,
silently stopping the app's whole library refresh over an unrelated provider hiccup.

No real DB/network/filesystem/credentials are touched — psycopg2-backed helpers
(_start_log/_update_log_steps/_finish_log) and load_settings() are monkeypatched, and
CRUNCHYEXPORTER_DIR is redirected to a pytest tmp_path so the crunchyroll step's
mkdir/config-write don't hit the real /opt/crunchyexporter path.
"""

import pytest

import run_full_sync as rfs


def _capture(monkeypatch, *, cr_ok=True, netflix_ok=True, anilist_ok=True,
             cr_configured=True, netflix_configured=True):
    settings = {
        "anilist_token": "tok",
        "anilist_username": "user",
        "cr_etp_rt": "etp" if cr_configured else "",
        "netflix_cookie_header": "cookie" if netflix_configured else "",
        "netflix_profile_guid": "guid" if netflix_configured else "",
    }
    monkeypatch.setattr(rfs, "load_settings", lambda: settings)
    monkeypatch.setattr(rfs, "_start_log", lambda: 1)
    monkeypatch.setattr(rfs, "_update_log_steps", lambda log_id, steps: None)

    finish_calls = []
    monkeypatch.setattr(
        rfs, "_finish_log",
        lambda log_id, status, entries_updated, error_msg, steps: finish_calls.append(
            {"status": status, "entries_updated": entries_updated, "error_msg": error_msg, "steps": list(steps)}
        ),
    )

    calls = []

    def fake_run(cmd, extra_env=None, cwd=None):
        calls.append(cmd)
        joined = " ".join(str(c) for c in cmd)
        if "main.py" in joined and "fetch" in joined:
            return cr_ok
        if "sync_crunchyroll" in joined:
            return cr_ok
        if "sync_netflix" in joined:
            return netflix_ok
        if "sync_anilist" in joined:
            return anilist_ok
        return True

    monkeypatch.setattr(rfs, "run", fake_run)

    return calls, finish_calls


def _statuses(steps):
    return {s["service"]: s["status"] for s in steps}


def test_all_steps_configured_and_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(rfs, "CRUNCHYEXPORTER_DIR", tmp_path)
    calls, finish_calls = _capture(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        rfs.main()

    assert exc.value.code == 0
    result = finish_calls[0]
    assert result["status"] == "ok"
    assert _statuses(result["steps"]) == {"crunchyroll": "ok", "netflix": "ok", "anilist_postgres": "ok"}


def test_netflix_failure_does_not_block_anilist_pull(monkeypatch, tmp_path):
    # The core regression test for #62: a Netflix failure must not stop the
    # AniList→Postgres pull from running.
    monkeypatch.setattr(rfs, "CRUNCHYEXPORTER_DIR", tmp_path)
    calls, finish_calls = _capture(monkeypatch, netflix_ok=False)

    with pytest.raises(SystemExit) as exc:
        rfs.main()

    assert exc.value.code == 0
    result = finish_calls[0]
    assert result["status"] == "partial"
    statuses = _statuses(result["steps"])
    assert statuses["netflix"] == "error"
    assert statuses["anilist_postgres"] == "ok"


def test_no_provider_credentials_skips_but_anilist_still_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(rfs, "CRUNCHYEXPORTER_DIR", tmp_path)
    calls, finish_calls = _capture(monkeypatch, cr_configured=False, netflix_configured=False)

    with pytest.raises(SystemExit) as exc:
        rfs.main()

    assert exc.value.code == 0
    result = finish_calls[0]
    assert result["status"] == "ok"
    assert _statuses(result["steps"]) == {"crunchyroll": "skipped", "netflix": "skipped", "anilist_postgres": "ok"}
    # skipped steps never call run() at all — only the anilist step should have
    assert all("sync_anilist" in " ".join(str(c) for c in cmd) for cmd in calls)


def test_anilist_failure_is_always_error(monkeypatch, tmp_path):
    monkeypatch.setattr(rfs, "CRUNCHYEXPORTER_DIR", tmp_path)
    calls, finish_calls = _capture(monkeypatch, anilist_ok=False)

    with pytest.raises(SystemExit) as exc:
        rfs.main()

    assert exc.value.code == 1
    result = finish_calls[0]
    assert result["status"] == "error"
    assert result["error_msg"] == "AniList → Postgres sync failed"


def test_missing_anilist_credentials_short_circuits(monkeypatch, tmp_path):
    monkeypatch.setattr(rfs, "CRUNCHYEXPORTER_DIR", tmp_path)
    calls, finish_calls = _capture(monkeypatch)
    monkeypatch.setattr(rfs, "load_settings", lambda: {"anilist_token": "", "anilist_username": ""})
    # conftest.py sets ANILIST_TOKEN/ANILIST_USERNAME env defaults so *importing*
    # scripts doesn't need real credentials — clear them here so the env-var fallback
    # in main() doesn't mask this test's "nothing configured at all" scenario.
    monkeypatch.setenv("ANILIST_TOKEN", "")
    monkeypatch.setenv("ANILIST_USERNAME", "")

    with pytest.raises(SystemExit) as exc:
        rfs.main()

    assert exc.value.code == 1
    result = finish_calls[0]
    assert result["status"] == "error"
    assert result["steps"] == []
    assert calls == []  # no step function ever ran


def test_unexpected_exception_in_a_step_does_not_crash_pipeline(monkeypatch, tmp_path):
    monkeypatch.setattr(rfs, "CRUNCHYEXPORTER_DIR", tmp_path)
    calls, finish_calls = _capture(monkeypatch)
    monkeypatch.setattr(
        rfs, "_do_crunchyroll",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(SystemExit) as exc:
        rfs.main()

    assert exc.value.code == 0  # anilist_postgres still ok -> partial, not error
    result = finish_calls[0]
    assert result["status"] == "partial"
    statuses = {s["service"]: s for s in result["steps"]}
    assert statuses["crunchyroll"]["status"] == "error"
    assert "boom" in statuses["crunchyroll"]["error_msg"]
    assert statuses["anilist_postgres"]["status"] == "ok"
