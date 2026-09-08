# Faults

Things that went wrong, and the guard each one produced. Newest first.

Add an entry when something failed in a way that **could recur** and would cost
time or damage if it did. Not typos, not one-off environment hiccups.

Format — all four fields, every time:

    ## YYYY-MM-DD — one-line title
    **What:** the observable failure
    **Why:** the actual cause, not the symptom
    **Guard:** what now prevents it (hook, check, doc), or "none — judgement only"
    **Recurred:** yes/no

`Recurred:` is the field that earns its place. A first slip is noise; the same
failure twice is what justifies a hook rather than a note.

No credentials, IPs or hostnames here — this file is about process and stays
public-safe. A fault needing private detail records the shape here and points at
the private tier.

---

## 2026-09-08 — checked: not exposed to the AniList outage that blocked a sibling repo

**What:** checked whether AniDex carries the same live-third-party-API test
dependency that blocked a sibling repo's required CI check for four days (see
`project-template/docs/FAULTS.md`, "a required status check depended on a live
third-party API").
**Why:** AniDex also integrates with AniList, so the same class of failure
looked plausible without actually checking.
**Guard:** AniDex's tests mock AniList via `monkeypatch` rather than calling
the live API (`tests/test_sync_manga_data.py`'s `_install_full_pipeline_mocks`)
— confirmed by CI staying green on 2026-09-06 and 2026-09-08 while the live
AniList API was returning 403.
**Recurred:** no.

## 2026-08-13 — dependency PR merged on green CI, but the smoke test never touched a template

**What:** a Dependabot fastapi 0.115.5 → 0.141.1 bump merged after CI went
green; every template route 500'd in production
(`TypeError: unhashable type: 'dict'`) until caught live.
**Why:** the PR's smoke test only hit an unauthenticated `HTMLResponse` route
that never touched Jinja — a green check didn't prove templates still
rendered under the new Starlette version.
**Guard:** `pr-validate.yml` now boots the built image against a real
throwaway Postgres and asserts `GET /auth/login` returns 200 with actual
rendered template content, closing the specific gap. Still only covers one
route — a dependency bump touching the request/response/templating path
still needs a manual render check before merging.
**Recurred:** no.
