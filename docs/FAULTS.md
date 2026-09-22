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
failure twice is what justifies a hook rather than a note. If a fault happens
again, edit the existing entry's `Recurred:` to yes and add the date — do not
append a second entry.

No credentials, IPs or hostnames here — this file is about process and stays
public-safe. A fault needing private detail records the shape here and points at
the private tier.

---

## 2026-09-09 — five guard versions passed their own tests while wide open

**What:** a git-safety hook shipped five times (17/17, 26/26, 35/35, 47/47,
65/65 green) each time carrying a complete bypass: `git -C` for all rules;
every other global option; every line after the first; `git add -- .` and
force-suppression from a later command; a `#` in an argument and a reachable
sentinel byte.
**Why:** each fix added parsing to close the last gap, and the added parsing
created the next one. Precision on raw command text costs more than it buys.
**Guard:** the guard was narrowed to three regex checks with no tokenizer.
Bulk-add is now an anchored whole-string match only — it catches the recorded
fault and misses chained forms, deliberately. Since 2026-09-10, section D of
`test-guard-git.sh` also asserts against `settings.json` itself — hook
declared, no unbraced `$VAR` under exec form, path resolves to an executable,
`--public` present — because the script being correct proves nothing about
whether the hook runs.
**Recurred:** yes — five times in one implementation, then a sixth time one
level up on 2026-09-10 (#515): the script was correct and its suite was 93/93
green, but `settings.json` named it with an unbraced `$CLAUDE_PROJECT_DIR`
under exec form (`args` present means no shell, so nothing expanded it). The
hook never spawned and every command was allowed. Same shape — green tests,
dead guard — which is why the wiring assertion above exists.

## 2026-09-08 — bulk `git add` published private files to a public repo

**What:** in a sibling repo running this same estate-wide guard, `git add -A`
staged `.claude/context` (a symlink to the private claude-context repo) and
`.claude/scratch/` into a public repo — twice in one session, the second time
after the first had been found and fixed.
**Why:** the second branch was cut from the default branch, which did not yet
carry the `.gitignore` entries; they existed only on the feature branch.
`git add -A` then swept in whatever was untracked.
**Guard:** `PreToolUse` deny on bulk `git add` in public repos
(`guard-git.sh --public`, rule R3), and a deny on any staging that names
`.claude/context` or `.claude/scratch` regardless of form (rule R1). Both
messages cite this entry. Primary record: `AniFillerPedia/docs/FAULTS.md`,
same date and title.
**Recurred:** yes — twice on 2026-09-08. That recurrence is why this is a
hook and not a note, and why the guard was installed here with `--public`
before this repo had ever hit it.

## 2026-09-08 — a gitignore rule silently stopped matching a symlink

**What:** `.gitignore` carried `.claude/context/`. Replacing the real
directory with a symlink made the rule stop matching, leaving the link
trackable in a public repo.
**Why:** a trailing slash matches a **directory**; git treats a symlink as a
file. The rule was correct for the layout it was written against and wrong
for the layout that replaced it, with no warning from git.
**Guard:** this repo's `.gitignore` writes the pattern without a trailing
slash (`.claude/context`) and says why inline; `guard-git.sh` rule R1 denies
staging that path regardless of what `.gitignore` does. Primary record:
`AniFillerPedia/docs/FAULTS.md`, same date and title.
**Recurred:** no — but found twice in the same session, once in `.gitignore`
and once in a verifier making the same assumption.

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
