# AniDex — Project Context for Claude Code

## Purpose

A personal anime tracking, rating, and recommendation site. AniList is the system of
record for catalog data and list status; this app adds the personal layer AniList's own
UI doesn't support well (drop reasons, custom tags, a real "watch next" queue) and pulls
it all into one self-hosted page.

## Scope

AniDex is a personal anime/manga tracker: library, progress, notes, ratings,
recommendations, and sync with external providers.

**Out of scope — do not build these:** anything that turns AniDex into a public
dataset or a multi-tenant SaaS, and anything that duplicates AniFillerPedia's
filler/canon data. Full boundary in `docs/dev/scope.md` — read it before
starting a feature that looks adjacent.

## Deploy

Merge to `main` builds and ships automatically via a self-hosted runner. Public
detail in `docs/dev/deploy.md`; host, runner and credential detail is private —
`.claude/context/deploy.md`.

## Guardrails — Non-Negotiable

- Track bugs, enhancements, and research spikes as GitHub issues (use
  `.github/ISSUE_TEMPLATE/task.md`) before starting work on them, not just in commit
  messages or chat — the reasoning behind scope/tradeoffs needs to be findable later
  without digging through history. When work actually starts: assign the issue
  (`gh issue edit <n> --add-assignee Napandee`) and reference it in the eventual
  commit(s) with a closing keyword (`Fixes #n` / `Closes #n`) so it auto-closes on
  merge — that's the real link between an issue and the code that resolved it, not
  a manual comment.
- Merge multi-commit feature branches with a real merge commit (`gh pr merge --merge`),
  not squash — pass the flag explicitly rather than relying on whatever the repo's
  default merge method happens to be. Each commit stays individually walkable/revertable
  in `main`'s real history instead of folded into one. (`feature/multi-user` was
  squash-merged before this was decided — nothing was actually lost, since GitHub's
  squash concatenates every commit message into the squash commit's body and the PR
  page keeps the original commits browsable regardless — but don't rely on that as the
  plan going forward.)
- Never commit secrets, tokens, or API keys. Env vars only — never hardcoded, never logged.
- If a secret, internal IP, or internal filesystem path is ever committed anyway:
  rotating/invalidating the credential is necessary but not sufficient on its own.
  Forward-fixing (removing it from the latest commit) leaves the original value fully
  intact and retrievable in every commit before that fix — treat the repo as still
  compromised until the plaintext is actually gone from history
  (`git filter-repo` + force-push), not just from the current tip. This happened for
  real (2026-08-12 Postgres password, fixed forward only; still sitting in history
  until a full pre-public-release audit caught it on 2026-08-16).
- Before this repo is ever made public: a force-push history rewrite is necessary but
  **not sufficient** if the repo has any PR history. GitHub retains every merged/closed
  PR's original commits via server-side `refs/pull/N/head` refs — and serves them
  directly on the PR's own web page — regardless of what happens to the branches
  afterward; there's no client-side way to remove these. Either get GitHub Support to
  purge cached PR data first, or recreate the repo from scratch (new empty repo, push
  only clean history, migrate open issues, retire the old repo as a permanently-private
  archive). Confirmed the hard way on 2026-08-16 — a branch-only rewrite left the
  original secrets fully browsable through old PRs even after the "clean" push.
- The app writes to AniList only via three endpoints: rating (`POST /api/anime/{id}/rating`),
  status (`POST /api/anime/{id}/status`), and progress (`POST /api/anime/{id}/progress`),
  all using `SaveMediaListEntry`. Never add further AniList mutations to the app without
  explicit agreement.
- **MCP server exposure (issue #171 decided the shape; #207 and #208 built it):**
  AniDex exposes an MCP server (`app/mcp_server.py`, mounted at `/mcp` on the existing
  FastAPI app — no separate process/container) for external AI clients (Claude Code,
  self-configured MCP clients) to read, and now write, a user's own
  library/notes/stats/recommendation data, authenticated via per-user personal access
  tokens issued in Settings (GitHub-PAT style — Bearer token, revocable, no OAuth
  authorization-server role for this app; `app/pat.py`). #207 shipped the read-only v1
  foundation (`list_library_entries`/`list_personal_notes`/`list_recommendations`/
  `get_stats`). #208 added write-capable tools on top of it
  (`update_personal_notes`/`bulk_apply_tags`/`set_rating`/`set_status`/`set_progress`),
  each calling straight into the same private write-logic helper its corresponding
  HTTP route already uses — never a new write path of its own. Every write tool's
  parameter schema takes explicit anime/entry id(s) — never a filter/query expression
  that resolves to an unbounded set at execution time — to bound the blast radius of a
  single LLM reasoning pass; this is enforced structurally (asserted at the JSON-schema
  level in `tests/test_pat_and_mcp_server.py`), not just by convention, and must stay
  true for any future tool added here. Every PAT carries a read/read_write `scope`
  (`personal_access_tokens.scope`, migration 021) — read tools work with either scope,
  write tools require read_write, and every token issued before #208 shipped defaulted
  to read-only rather than silently gaining write access. MCP exposure is a new access
  surface, not a new AniList write path — it must never bypass the app's own internal
  endpoints or the guardrail above.
- Ask before any schema migration that could drop or alter existing columns/data —
  additive migrations (new nullable column, new table) are fine to just do.
- Ask before changing the deploy pipeline (GitHub Actions workflows, image name) — changes
  here affect the live deployment path.
- The deploy pipeline uses `docker pull` + `docker run` (no Compose) driven by the GitHub
  Actions workflow. Compose files in `compose/` exist solely for container managers that
  track containers via Compose; they are not the deployment mechanism.

## Where the detail lives

Read these when the task calls for them — they are not loaded by default.

- `docs/dev/decisions.md` — before proposing anything that changes the data
  model, auth, or provider sync. Check it before re-litigating a decision.
- `docs/dev/architecture.md` — before changing how services fit together.
- `docs/dev/data-model.md` — schema-level detail; never hand-edit synced tables.
- `docs/dev/data-sources.md` — before changing an import or sync path.
- `docs/dev/deploy.md` — when changing how AniDex ships.
- `docs/dev/scope.md` — the full in/out-of-scope boundary.
- `docs/` — user-facing documentation (`user-guide/`, `admin/`, `mcp.md`).
  Different audience: keep developer detail in `docs/dev/`.
