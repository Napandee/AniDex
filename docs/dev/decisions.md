# Decisions

Architectural decisions and why they were made. Appended, never rewritten — a
superseded decision is marked superseded rather than deleted.

- **Tech stack**: Python + FastAPI + Jinja2 + psycopg2 — server-rendered HTML, no
  frontend build step. Uvicorn inside a slim Python Docker image.
- **Stats**: built-in stats page served from `/stats` — no external dashboard dependency.
- **Multi-user** (pivoted from the original single-user design, Aug 2026): the app now
  has its own auth layer rather than relying solely on the reverse proxy / access
  control tool in front of it. Local email+password is the zero-config default; Google
  and Discord OAuth are optional, admin-configured per-instance via
  `/admin/oauth-settings`. Signup is invite-only except for the very first account on
  an empty `users` table, which bootstraps as admin automatically — invites for
  subsequent accounts are issued via the separate `/admin/invites` endpoint. Every
  route and sync script (`run_full_sync.py`,
  `sync_anilist.py`, `sync_crunchyroll.py`, `run_recommender.py`) is scoped to the
  logged-in/invoking user; the sync schedule *time* stays instance-wide (one cron
  trigger regardless of user count), gated to admins in Admin → Instance Config.
  Account linking
  (attaching Google/Discord to an existing account) is explicit-only — never automatic
  by email match, since that would mean trusting a bare provider-supplied email as
  proof of identity. Linking only ever happens via `/settings/link/{provider}` while
  already authenticated, through a callback route (`/auth/link-callback/{provider}`)
  kept deliberately separate from the ordinary login callback so it can't fire as a
  side effect of an ordinary login click. Reverse-proxy access control (Cloudflare
  Access, etc.) is still expected as an outer layer, especially while invite-only
  signup keeps this to a small trusted group — the app's own auth doesn't replace it,
  it adds a second, inner gate. OAuth client id/secret are configured once,
  instance-wide (`instance_config`, not per-user) — an invited user never sees or
  enters a secret, they just click Connect/Sign-in and authenticate with their own
  provider account, same as any "Sign in with Google" button anywhere. The one
  per-user admin step that *does* exist is Google-specific, not something this app's
  code controls: if the Google OAuth app is left in Google's "Testing" publishing
  status (the norm here, to skip Google's app-verification review for a small
  invite-only instance — see #7), Google restricts sign-in to accounts the admin has
  explicitly added as a test user in the GCP console (OAuth consent screen →
  Audience → Test users, cap 100), so a newly invited user can't complete Google
  login until that's done. Discord has no equivalent gate for the `identify`/`email`
  scopes this app requests — any Discord account can connect immediately. Verified
  end-to-end for both providers 2026-08-17 (#7 Google, #60 Discord). TOTP-based 2FA
  (issue #83) is optional per local account, enrolled from Settings via QR code
  (`pyotp`), with hashed one-time recovery codes for the lost-device case — it applies
  only to local email+password login, not to Google/Discord sign-in. A server-side
  session store (issue #82, `app/sessions.py`) replaced the bare signed-cookie session
  that previously had no concept of an individual session to list or revoke; Settings
  now shows active sessions and lets a user revoke any of them. Admins also get a
  dedicated Data Quality tab (issue #202, distinct from the existing Instance Health
  readout) surfacing sync drift and orphaned rows across users.
- **License**: GPL-3.0. Dependency audit (Aug 2026) confirmed no dependency — including
  `crunchyexporter-cli`, vendored via git rather than pip — imposes a stricter license
  that would have constrained this choice.
- **One sync path, not two**: there used to be a second, standalone `crunchysync`
  image/container; it was removed (Aug 2026) because it was strictly redundant with
  the in-app scheduler and manual "Sync Now" trigger. Don't reintroduce a second
  container for this without a real new reason — isolation/scheduling needs the
  first one never actually served. `crunchyexporter-cli` itself (vendored via git
  into the main Dockerfile, the thing the "second container" duplicated the pin
  of) was later retired entirely (issue #35) once `sync_crunchyroll.py` grew its
  own direct-API fetch matching `sync_netflix.py`'s pattern — nothing in this repo
  vendors a third-party CR/Netflix client anymore.
