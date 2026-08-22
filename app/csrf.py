"""CSRF token generation/validation (issue #312).

Framework-agnostic like app/sessions.py, app/pat.py, app/privacy.py — takes plain
values (the session mapping, a header/form string), not a fastapi Request, so it can
be unit-tested without spinning up the ASGI app. The FastAPI-specific wiring (the
dependency that reads Request.session/headers/form, and the app-level
`dependencies=` registration) lives in app/main.py next to _require_user/
_require_admin, the same split this file's siblings already use.

Where the token lives: the existing signed-cookie session (Starlette's
SessionMiddleware, `request.session`), NOT the server-side `sessions` table from
issue #82, and NOT a new migration. The issue explicitly asks for "generated once,
stored in the session, the existing server-side session store is the natural home
for it, not a second mechanism" — and the signed cookie genuinely *is* that same
mechanism here, just one layer up: `sessions` table rows exist because a session
needs to be resolvable from a bare opaque token handed back by an arbitrary future
request (that's what session_token_hash's lookup is for), and revocable/listable
independently of any single request. A CSRF token needs neither of those things —
it is only ever compared against the SAME already-authenticated request's own
session, nothing ever looks it up by value the way resolve_session() does for
"sid". That makes the signed, tamper-evident SessionMiddleware cookie exactly the
right home: no schema migration, and forging it requires the same SESSION_SECRET_KEY
forging "sid" itself would.

Generated once per session, not per request: app/main.py's get_current_user()
preserves this key across its "not authenticated -> clear stale session" branch the
same way it already preserves _PENDING_2FA_SESSION_KEY, specifically so an anonymous
visitor with two open tabs (e.g. two /auth/login page loads) doesn't have the older
tab's embedded token invalidated by a newer, unrelated request clearing the session
out from under it.
"""

import secrets

SESSION_KEY = "csrf_token"
HEADER_NAME = "X-CSRF-Token"
FORM_FIELD = "csrf_token"

_TOKEN_BYTES = 32  # same size as sessions.create_session's token — 256 bits of
                   # secrets.token_urlsafe entropy, not brute-forceable


def get_or_create_token(session) -> str:
    """Return this session's CSRF token, generating and storing a fresh one the
    first time this is called for a given session. Called once per request from
    app/main.py's _nav_context (which runs on every templates.TemplateResponse),
    so a token is always available to embed as a hidden field or expose to
    script.js's fetch()-header patch — including on the very first anonymous page
    load, before any login has happened, since login itself is a state-changing
    POST that needs the same protection as everything else (issue #312 is "every
    state-changing route", not just authenticated ones)."""
    token = session.get(SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        session[SESSION_KEY] = token
    return token


def token_is_valid(session, supplied: str | None) -> bool:
    """True only if `supplied` is a non-empty string matching this session's
    current CSRF token exactly. Uses secrets.compare_digest (constant-time) — same
    reasoning as every other secret comparison in this app (recovery codes,
    session-token hashes): a naive `==` leaks timing information about how many
    leading characters matched, exactly the kind of oracle a token like this
    should be immune to. A missing expected token (nothing in the session yet —
    shouldn't happen in practice, since every response capable of carrying a form
    or triggering a fetch() already ran through get_or_create_token first, but
    fails closed rather than being treated as "anything goes") or a missing
    supplied token both count as invalid."""
    expected = session.get(SESSION_KEY)
    if not expected or not supplied:
        return False
    return secrets.compare_digest(expected, supplied)
