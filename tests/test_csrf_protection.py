"""
Coverage for issue #312 — CSRF token protection on every state-changing route.

Deliberately drives every unsafe-method request in this file with
`skip_csrf_autoinject=True` (see tests/conftest.py's TestClient.request patch)
— that patch exists purely so the OTHER ~800 tests in this suite, none of which
know or care about CSRF, don't all have to hand-carry a token. This file is the
opposite: it exists specifically to prove the real, undoctored
app/main.py::_csrf_protect dependency and app/csrf.py actually work, so every
POST/PATCH/DELETE here is either a genuine two-step flow (GET a page for a
real session-bound token, scrape it out of the rendered HTML, then submit with
that real value) or a deliberate attack-shaped request (no token at all, or a
wrong one) — never the conftest.py shortcut. Plain GET requests below don't
need the flag at all: _csrf_protect only ever looks at unsafe methods, so the
conftest.py patch never touches a GET regardless.

skip_csrf_autoinject only exists on TestClient.request() itself (the patch
target), not on the .get()/.post()/... convenience wrappers (those have fixed
keyword-only signatures with no **kwargs passthrough) — so every
POST/PATCH/DELETE below goes through client.request("POST", ...) rather than
client.post(...).

Covers issue #312's acceptance criteria:
  1. A request with a valid token, tied to the current session, succeeds.
  2. A request with no token is rejected (a real HTTP-level 403 through the
     real route — not a unit test asserting a bare function returns False).
  3. A request with a stale/mismatched token is rejected the same way.
  4. A spot-check across several distinct real routes — a Form-encoded page
     route, a JSON /api/* route, and an admin-only route — not just one.
  5. The OAuth callback "exemption" (GET, so CSRF doesn't apply) still works,
     and hasn't been broadened into a real path-based bypass list — see
     app/main.py's _csrf_protect docstring for why no such list exists at all.
  6. A "cross-origin-shaped" form POST (foreign Origin/Referer, real cookies,
     no token — exactly what a real forged cross-site form submission looks
     like from the server's point of view) is genuinely rejected.

Needs a reachable Postgres via DATABASE_URL, same skip-if-unavailable pattern
tests/test_oauth_state_csrf.py and tests/test_collections.py already use.
"""

import os
import re
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()


PASSWORD = "correct horse battery staple"
_META_TOKEN_RE = re.compile(r'<meta name="csrf-token" content="([^"]*)">')


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM collections")
        cur.execute("DELETE FROM invites")
        cur.execute("DELETE FROM sessions")
        cur.execute("DELETE FROM users")


def _real_token_from(html: str) -> str:
    m = _META_TOKEN_RE.search(html)
    assert m, "no <meta name=\"csrf-token\"> found in rendered page — base.html regression?"
    return m.group(1)


def _register(client, email="owner@example.com", password=PASSWORD, token=None):
    """Real two-step registration: GET the register page for a real,
    session-bound token (unless the caller supplies its own, for the
    rejection-path tests), then POST with it — always via
    skip_csrf_autoinject=True so this file never relies on conftest.py's
    test-suite convenience patch."""
    if token is None:
        page = client.get("/auth/register")
        token = _real_token_from(page.text)
    return client.request(
        "POST",
        "/auth/register",
        data={"email": email, "password": password, "csrf_token": token},
        follow_redirects=False,
        skip_csrf_autoinject=True,
    )


# ── 1 & 4. Valid token succeeds, across several distinct real routes ────────


def test_registration_with_a_real_token_succeeds(client):
    """First account on an empty `users` table — the Form-field delivery path
    (a real <form method="post"> submission), not the header path."""
    resp = _register(client)
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/"


def test_valid_token_via_header_also_succeeds_on_a_json_api_route(client):
    """The other delivery path — script.js's fetch()-header patch — exercised
    against a real JSON /api/* route (POST /api/collections), not a Form one."""
    _register(client)
    page = client.get("/")
    token = _real_token_from(page.text)

    resp = client.request(
        "POST",
        "/api/collections",
        json={"name": "Cozy TV", "filters": {"status": "WATCHING"}},
        headers={"X-CSRF-Token": token},
        skip_csrf_autoinject=True,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


def test_valid_token_succeeds_on_an_admin_only_route(client):
    """A third, distinct route shape: an admin-gated Form POST
    (POST /admin/invites) — the first registered user bootstraps as admin."""
    _register(client)
    page = client.get("/admin")
    token = _real_token_from(page.text)

    resp = client.request(
        "POST",
        "/admin/invites",
        data={"email": "invitee@example.com", "csrf_token": token},
        follow_redirects=False,
        skip_csrf_autoinject=True,
    )
    assert resp.status_code == 303, resp.text


def test_rewatch_notes_form_carries_its_own_real_token(client, pg_conn):
    """Regression test: every other test in this file scrapes its token from
    base.html's <meta name="csrf-token"> tag, which only proves the CSRF
    mechanism itself works — it can't catch a specific <form> template that
    forgot its own hidden csrf_token field (exactly the bug this test caught
    in review: notes.html's rewatch-notes form, the *second* <form> on the
    page, was missing it while the first — the general notes form — had it).
    This test scrapes the token straight out of that second form's own hidden
    input, not the meta tag, so a future template-level omission like this
    one fails here instead of shipping silently."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji) VALUES (999998, 'Rewatch Test Anime') "
            "ON CONFLICT (id) DO NOTHING"
        )
    _register(client)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", ("owner@example.com",))
        user_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, repeat_count) "
            "VALUES (%s, 999998, 'COMPLETED', 1)",
            (user_id,),
        )

    page = client.get("/anime/999998/notes")
    rewatch_form_match = re.search(
        r'action="/anime/999998/rewatch-notes/1"[^>]*>.*?name="csrf_token" value="([^"]*)"',
        page.text,
        re.DOTALL,
    )
    assert rewatch_form_match, (
        "rewatch-notes <form> has no csrf_token hidden field — regression of "
        "the bug caught in this PR's review"
    )
    rewatch_token = rewatch_form_match.group(1)
    assert rewatch_token, "rewatch-notes form's csrf_token field is present but empty"

    resp = client.request(
        "POST",
        "/anime/999998/rewatch-notes/1",
        data={"note": "hit different the second time", "back": "COMPLETED", "csrf_token": rewatch_token},
        follow_redirects=False,
        skip_csrf_autoinject=True,
    )
    assert resp.status_code in (200, 303), resp.text


def test_valid_token_succeeds_on_the_specific_interactive_features_the_issue_calls_out(client, pg_conn):
    """Issue #312 explicitly names bulk edit, rating, progress, and tags as the
    features most at risk of a silent CSRF-rollout regression. Spot-checks all
    four real routes in one pass. Rating/progress only ever UPDATE an existing
    library_entries row (a harmless no-op against a nonexistent one — see
    _apply_rating_change/_apply_progress_change), but bulk-status and
    bulk-tags both INSERT (library_entries / personal_notes respectively),
    which does hit a real FK against `anime` — so this seeds one minimal
    anime row first. Not the point of this test (that's what
    test_collections.py-style route tests are for) — just enough to reach
    the CSRF gate's own pass/fail outcome without an unrelated FK error
    masking it."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji) VALUES (999999, 'Test Anime') "
            "ON CONFLICT (id) DO NOTHING"
        )
    _register(client)
    page = client.get("/")
    token = _real_token_from(page.text)
    headers = {"X-CSRF-Token": token}

    rating_resp = client.request(
        "POST", "/api/anime/999999/rating", json={"score": 4}, headers=headers, skip_csrf_autoinject=True
    )
    assert rating_resp.status_code == 200, rating_resp.text

    progress_resp = client.request(
        "POST", "/api/anime/999999/progress", json={"progress": 3}, headers=headers, skip_csrf_autoinject=True
    )
    assert progress_resp.status_code == 200, progress_resp.text

    bulk_status_resp = client.request(
        "POST",
        "/api/anime/bulk-status",
        json={"status": "WATCHING", "anime_ids": [999999]},
        headers=headers,
        skip_csrf_autoinject=True,
    )
    assert bulk_status_resp.status_code == 200, bulk_status_resp.text

    bulk_tags_resp = client.request(
        "POST",
        "/api/anime/bulk-tags",
        json={"tags": "cozy, comfort", "anime_ids": [999999]},
        headers=headers,
        skip_csrf_autoinject=True,
    )
    assert bulk_tags_resp.status_code == 200, bulk_tags_resp.text


def test_missing_token_is_rejected_on_the_same_interactive_features(client):
    """The inverse of the spot-check above — same four routes, no token at
    all, all four must be genuinely blocked (403), not silently let through."""
    _register(client)

    for method_path_kwargs in [
        ("POST", "/api/anime/999999/rating", {"json": {"score": 4}}),
        ("POST", "/api/anime/999999/progress", {"json": {"progress": 3}}),
        ("POST", "/api/anime/bulk-status", {"json": {"status": "WATCHING", "anime_ids": [999999]}}),
        ("POST", "/api/anime/bulk-tags", {"json": {"tags": "cozy", "anime_ids": [999999]}}),
    ]:
        method, path, kwargs = method_path_kwargs
        resp = client.request(method, path, skip_csrf_autoinject=True, **kwargs)
        assert resp.status_code == 403, f"{path} should reject a request with no CSRF token: {resp.text}"


# ── 2. Missing token is rejected ─────────────────────────────────────────────


def test_registration_with_no_token_at_all_is_rejected(client):
    """No csrf_token field, no X-CSRF-Token header — the plain "forgot to send
    it" / "attacker can't read it" case."""
    page = client.get("/auth/register")
    assert "csrf-token" in page.text  # sanity: a real session/token did get seeded

    resp = client.request(
        "POST",
        "/auth/register",
        data={"email": "no-token@example.com", "password": PASSWORD},
        follow_redirects=False,
        skip_csrf_autoinject=True,
    )
    assert resp.status_code == 403, resp.text
    assert "csrf" in resp.text.lower()

    # And nothing was actually created — the rejection genuinely blocked the
    # state change, not just returned an error status alongside it happening
    # anyway.
    with psycopg2.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM users WHERE email = %s", ("no-token@example.com",))
        assert cur.fetchone()[0] == 0


def test_missing_token_is_rejected_on_an_authenticated_json_route_too(client):
    """Same check past the login boundary, not just on the public
    registration route — a missing token on an authenticated JSON POST is
    rejected before _require_user_api even gets a chance to run (dependencies
    solve in the same pass; either can 4xx first, but the point is the write
    never happens)."""
    _register(client)
    resp = client.request(
        "POST",
        "/api/collections",
        json={"name": "No Token Collection", "filters": {}},
        skip_csrf_autoinject=True,
    )
    assert resp.status_code == 403, resp.text

    with psycopg2.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM collections WHERE name = %s", ("No Token Collection",))
        assert cur.fetchone()[0] == 0


# ── 3. Stale / mismatched token is rejected ──────────────────────────────────


def test_wrong_token_value_is_rejected(client):
    """A token that's well-formed but simply doesn't match this session's real
    one — the "attacker guesses/fabricates a value" shape."""
    resp = _register(client, token="totally-fabricated-not-the-real-token")
    assert resp.status_code == 403, resp.text


def test_a_different_sessions_valid_token_is_rejected(client):
    """A real, currently-valid token — just minted for a DIFFERENT session,
    not this one. Proves the check is tied to the current session's own
    token, not merely "looks like a plausible token". Two sessions are
    produced sequentially against the SAME client (clearing cookies between
    them to force a fresh one) rather than via a second TestClient — this
    app's scheduler/outbox worker start on the shared app instance's startup
    event, and a second `with TestClient(app_module.app)` context on the same
    already-running app instance collides with that (SchedulerAlreadyRunning),
    so this deliberately stays within one TestClient's lifecycle."""
    session_a_page = client.get("/auth/register")
    session_a_token = _real_token_from(session_a_page.text)

    client.cookies.clear()  # force a brand-new session — session B
    session_b_page = client.get("/auth/register")
    session_b_token = _real_token_from(session_b_page.text)
    assert session_b_token != session_a_token  # sanity: genuinely different sessions

    resp = client.request(
        "POST",
        "/auth/register",
        data={"email": "owner@example.com", "password": PASSWORD, "csrf_token": session_a_token},
        follow_redirects=False,
        skip_csrf_autoinject=True,
    )
    assert resp.status_code == 403, resp.text
    _ = session_b_token  # (current session at time of the POST — the one that should have been used)


def test_stale_token_from_before_a_session_reset_is_rejected(client):
    """Register+login (which mints/keeps a real token), grab it, then force a
    session reset the same way an expired/revoked session would (clearing
    cookies simulates a client that lost its session — e.g. an expired
    server-side session row) and confirm the OLD token no longer validates
    against the NEW session that replaces it."""
    _register(client)
    page = client.get("/")
    stale_token = _real_token_from(page.text)

    client.cookies.clear()  # simulate a dead/expired session — fresh cookie jar
    resp = client.request(
        "POST",
        "/api/collections",
        json={"name": "Stale Token Collection", "filters": {}},
        headers={"X-CSRF-Token": stale_token},
        skip_csrf_autoinject=True,
    )
    assert resp.status_code == 403, resp.text


# ── 5. OAuth callback GET routes are unaffected (issue #313's own coverage
#      already exercises the state-param check itself; this just confirms
#      _csrf_protect's safe-method skip doesn't get in the way of a GET) ────


def test_oauth_login_redirect_is_unaffected_by_csrf_protection(client, monkeypatch):
    monkeypatch.setenv("DISCORD_CLIENT_ID", "test-discord-client-id")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "test-discord-client-secret")
    resp = client.get("/auth/login/discord", follow_redirects=False)
    assert resp.status_code in (302, 307), resp.text


# ── 6. A genuinely cross-origin-shaped forged POST is rejected end-to-end ───


def test_cross_origin_shaped_form_post_without_token_is_rejected(client):
    """The actual attack this issue closes: the victim is logged in (real
    session cookie, sent automatically by the browser/TestClient the same way
    a real browser would), but the forged request comes from a foreign
    Origin/Referer (an attacker's page can't read this session's real CSRF
    token — same-origin policy — so it can never attach the right value) and
    carries no token at all. This must be genuinely rejected at the real
    route, not merely asserted against a bare validator function."""
    _register(client)

    resp = client.request(
        "POST",
        "/settings/display",
        data={"timezone": "Europe/London", "language": "en", "theme": "dark"},
        headers={
            "Origin": "https://evil.example.com",
            "Referer": "https://evil.example.com/forged-form.html",
        },
        follow_redirects=False,
        skip_csrf_autoinject=True,
    )
    assert resp.status_code == 403, resp.text

    # Confirm the forged request didn't actually change anything either.
    with psycopg2.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = 'theme'")
        row = cur.fetchone()
        assert row is None or row[0] != "dark"


def test_same_flow_with_the_real_token_attached_succeeds(client):
    """Sanity companion to the test above: the exact same request shape (same
    foreign Origin/Referer headers even — those are never part of this app's
    check, only the token is) succeeds once the real per-session token is
    attached, proving the 403 above is specifically about the missing token
    and not some other Origin/Referer-based rejection this app doesn't
    actually implement."""
    _register(client)
    page = client.get("/settings")
    token = _real_token_from(page.text)

    resp = client.request(
        "POST",
        "/settings/display",
        data={"timezone": "Europe/London", "language": "en", "theme": "dark", "csrf_token": token},
        headers={
            "Origin": "https://evil.example.com",
            "Referer": "https://evil.example.com/forged-form.html",
        },
        follow_redirects=False,
        skip_csrf_autoinject=True,
    )
    assert resp.status_code == 303, resp.text


# ── app/csrf.py's pure functions, unit-tested directly (no DB, no HTTP) ─────
# Mirrors how app/sessions.py's own token-hashing helper is framework-agnostic
# and independently testable — see that module's docstring.


def test_get_or_create_token_is_stable_across_calls_for_the_same_session():
    from app import csrf

    session = {}
    first = csrf.get_or_create_token(session)
    second = csrf.get_or_create_token(session)
    assert first == second
    assert session[csrf.SESSION_KEY] == first


def test_get_or_create_token_differs_across_independent_sessions():
    from app import csrf

    assert csrf.get_or_create_token({}) != csrf.get_or_create_token({})


def test_token_is_valid_true_only_for_an_exact_match():
    from app import csrf

    session = {}
    token = csrf.get_or_create_token(session)
    assert csrf.token_is_valid(session, token) is True
    assert csrf.token_is_valid(session, token + "x") is False
    assert csrf.token_is_valid(session, None) is False
    assert csrf.token_is_valid({}, token) is False
    assert csrf.token_is_valid({}, None) is False
