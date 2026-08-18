"""
Regression coverage for run_full_sync.py's per-step control flow (issue #62). The core
thing being guarded: a failure in one step (crunchyroll/netflix) must not prevent the
anilist_postgres step from running — that used to sys.exit(1) on the first failing step,
silently stopping the app's whole library refresh over an unrelated provider hiccup.

Also covers issue #91: each step's run() call now gets its own subprocess timeout
instead of relying solely on one blunt external timeout wrapping the whole script (see
app/main.py's _run_sync_task) — a slow step must time out as its own error, not starve
later steps or leave the pipeline hanging past any per-step budget.

No real DB/network/credentials are touched — psycopg2-backed helpers
(_start_log/_update_log_steps/_finish_log), load_settings(), and run() (the subprocess
wrapper each step calls) are all monkeypatched.
"""

import subprocess

import pytest

import run_full_sync as rfs


def _result(ok, entries_updated=None, entries_fetched=None, full_pull=None):
    return {"ok": ok, "entries_updated": entries_updated, "entries_fetched": entries_fetched, "full_pull": full_pull}


def _capture(monkeypatch, *, cr_ok=True, netflix_ok=True, anilist_ok=True,
             cr_configured=True, netflix_configured=True, timeout_step=None,
             cr_result=None, netflix_result=None, anilist_result=None):
    settings = {
        "anilist_token": "tok",
        "anilist_username": "user",
        "cr_etp_rt": "etp" if cr_configured else "",
        "netflix_cookie_header": "cookie" if netflix_configured else "",
        "netflix_profile_guid": "guid" if netflix_configured else "",
    }
    monkeypatch.setattr(rfs, "load_settings", lambda: settings)

    start_log_calls = []
    monkeypatch.setattr(
        rfs, "_start_log",
        lambda sync_type, trigger: (start_log_calls.append({"sync_type": sync_type, "trigger": trigger}), 1)[1],
    )
    monkeypatch.setattr(rfs, "_update_log_steps", lambda log_id, steps: None)

    finish_calls = []
    monkeypatch.setattr(
        rfs, "_finish_log",
        lambda log_id, status, entries_updated, error_msg, steps: finish_calls.append(
            {"status": status, "entries_updated": entries_updated, "error_msg": error_msg, "steps": list(steps)}
        ),
    )

    calls = []

    def fake_run(cmd, extra_env=None, cwd=None, timeout=None):
        calls.append((cmd, timeout))
        joined = " ".join(str(c) for c in cmd)
        if "sync_crunchyroll" in joined and timeout_step == "crunchyroll":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
        if "sync_netflix" in joined and timeout_step == "netflix":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
        if "sync_anilist" in joined and timeout_step == "anilist_postgres":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
        if "sync_crunchyroll" in joined:
            return cr_result if cr_result is not None else _result(cr_ok)
        if "sync_netflix" in joined:
            return netflix_result if netflix_result is not None else _result(netflix_ok)
        if "sync_anilist" in joined:
            return anilist_result if anilist_result is not None else _result(anilist_ok)
        return _result(True)

    monkeypatch.setattr(rfs, "run", fake_run)

    return calls, finish_calls, start_log_calls  # calls: list[(cmd, timeout)]


def _statuses(steps):
    return {s["service"]: s["status"] for s in steps}


def test_all_steps_configured_and_ok(monkeypatch):
    calls, finish_calls, start_log_calls = _capture(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        rfs.main()

    assert exc.value.code == 0
    result = finish_calls[0]
    assert result["status"] == "ok"
    assert _statuses(result["steps"]) == {"crunchyroll": "ok", "netflix": "ok", "anilist_postgres": "ok"}


def test_netflix_failure_does_not_block_anilist_pull(monkeypatch):
    # The core regression test for #62: a Netflix failure must not stop the
    # AniList→Postgres pull from running.
    calls, finish_calls, start_log_calls = _capture(monkeypatch, netflix_ok=False)

    with pytest.raises(SystemExit) as exc:
        rfs.main()

    assert exc.value.code == 0
    result = finish_calls[0]
    assert result["status"] == "partial"
    statuses = _statuses(result["steps"])
    assert statuses["netflix"] == "error"
    assert statuses["anilist_postgres"] == "ok"


def test_no_provider_credentials_skips_but_anilist_still_runs(monkeypatch):
    calls, finish_calls, start_log_calls = _capture(monkeypatch, cr_configured=False, netflix_configured=False)

    with pytest.raises(SystemExit) as exc:
        rfs.main()

    assert exc.value.code == 0
    result = finish_calls[0]
    assert result["status"] == "ok"
    assert _statuses(result["steps"]) == {"crunchyroll": "skipped", "netflix": "skipped", "anilist_postgres": "ok"}
    # skipped steps never call run() at all — only the anilist step should have
    assert all("sync_anilist" in " ".join(str(c) for c in cmd) for cmd, _timeout in calls)


def test_anilist_failure_is_always_error(monkeypatch):
    calls, finish_calls, start_log_calls = _capture(monkeypatch, anilist_ok=False)

    with pytest.raises(SystemExit) as exc:
        rfs.main()

    assert exc.value.code == 1
    result = finish_calls[0]
    assert result["status"] == "error"
    assert result["error_msg"] == "AniList → Postgres sync failed"


def test_missing_anilist_credentials_short_circuits(monkeypatch):
    calls, finish_calls, start_log_calls = _capture(monkeypatch)
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


def test_unexpected_exception_in_a_step_does_not_crash_pipeline(monkeypatch):
    calls, finish_calls, start_log_calls = _capture(monkeypatch)
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


def test_netflix_timeout_becomes_error_without_blocking_anilist_pull(monkeypatch):
    # Issue #91's core regression test: a step that hangs past its own timeout must
    # become that step's error, exactly like any other step failure — not starve
    # anilist_postgres or leave the pipeline hanging past its per-step budget.
    calls, finish_calls, start_log_calls = _capture(monkeypatch, timeout_step="netflix")

    with pytest.raises(SystemExit) as exc:
        rfs.main()

    assert exc.value.code == 0  # anilist_postgres still ok -> partial, not error
    result = finish_calls[0]
    assert result["status"] == "partial"
    statuses = {s["service"]: s for s in result["steps"]}
    assert statuses["netflix"]["status"] == "error"
    assert "timed out" in statuses["netflix"]["error_msg"]
    assert statuses["anilist_postgres"]["status"] == "ok"


def test_provider_steps_get_generous_timeout_anilist_gets_tighter_one(monkeypatch):
    # Regression coverage for the actual per-step timeout values wired into run() —
    # distinct from the previous test, which only checks that a timeout is handled
    # gracefully, not that each step requests the right budget.
    calls, finish_calls, start_log_calls = _capture(monkeypatch)

    with pytest.raises(SystemExit):
        rfs.main()

    timeouts_by_service = {}
    for cmd, timeout in calls:
        joined = " ".join(str(c) for c in cmd)
        if "sync_crunchyroll" in joined:
            timeouts_by_service["crunchyroll"] = timeout
        elif "sync_netflix" in joined:
            timeouts_by_service["netflix"] = timeout
        elif "sync_anilist" in joined:
            timeouts_by_service["anilist_postgres"] = timeout

    assert timeouts_by_service["crunchyroll"] == rfs.PROVIDER_STEP_TIMEOUT
    assert timeouts_by_service["netflix"] == rfs.PROVIDER_STEP_TIMEOUT
    assert timeouts_by_service["anilist_postgres"] == rfs.ANILIST_STEP_TIMEOUT
    assert rfs.ANILIST_STEP_TIMEOUT < rfs.PROVIDER_STEP_TIMEOUT


# ── Issue #46: real per-run entries-touched counts + manual/scheduled trigger ────────

def test_entries_updated_is_sum_of_non_skipped_steps(monkeypatch):
    # The core regression test for #46: the top-level entries_updated must reflect
    # real activity across all three channels, not just whatever anilist_postgres
    # reported (and definitely not a library-size COUNT(*)).
    calls, finish_calls, start_log_calls = _capture(
        monkeypatch,
        cr_result=_result(True, entries_updated=3, entries_fetched=5, full_pull=False),
        netflix_result=_result(True, entries_updated=2, entries_fetched=4, full_pull=False),
        anilist_result=_result(True, entries_updated=7, entries_fetched=340, full_pull=True),
    )

    with pytest.raises(SystemExit) as exc:
        rfs.main()

    assert exc.value.code == 0
    result = finish_calls[0]
    assert result["entries_updated"] == 3 + 2 + 7
    statuses = {s["service"]: s for s in result["steps"]}
    assert statuses["crunchyroll"]["entries_updated"] == 3
    assert statuses["crunchyroll"]["entries_fetched"] == 5
    assert statuses["crunchyroll"]["full_pull"] is False
    assert statuses["anilist_postgres"]["full_pull"] is True


def test_skipped_steps_never_contribute_to_entries_updated(monkeypatch):
    calls, finish_calls, start_log_calls = _capture(
        monkeypatch,
        cr_configured=False,
        netflix_configured=False,
        anilist_result=_result(True, entries_updated=9, entries_fetched=340, full_pull=True),
    )

    with pytest.raises(SystemExit) as exc:
        rfs.main()

    assert exc.value.code == 0
    result = finish_calls[0]
    assert result["entries_updated"] == 9


def test_error_step_contributes_zero_not_none_to_entries_updated(monkeypatch):
    calls, finish_calls, start_log_calls = _capture(
        monkeypatch,
        netflix_ok=False,
        anilist_result=_result(True, entries_updated=4, entries_fetched=340, full_pull=True),
    )

    with pytest.raises(SystemExit) as exc:
        rfs.main()

    assert exc.value.code == 0
    result = finish_calls[0]
    assert result["status"] == "partial"
    assert result["entries_updated"] == 4


def test_trigger_defaults_to_manual(monkeypatch):
    calls, finish_calls, start_log_calls = _capture(monkeypatch)
    monkeypatch.delenv("TRIGGER", raising=False)

    with pytest.raises(SystemExit):
        rfs.main()

    assert start_log_calls[0]["trigger"] == "manual"


def test_trigger_env_var_is_threaded_through_to_start_log(monkeypatch):
    calls, finish_calls, start_log_calls = _capture(monkeypatch)
    monkeypatch.setenv("TRIGGER", "scheduled")

    with pytest.raises(SystemExit):
        rfs.main()

    assert start_log_calls[0]["trigger"] == "scheduled"
