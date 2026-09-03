"""
Coverage for issue #474's runtime half — the migration-drift banner
(app/main.py's _nav_context computing nav_migration_drift, rendered by
base.html on every page) that makes a code-ahead-of-schema state hard to miss
regardless of which page an admin happens to load, on top of the existing
passive Admin > Instance Health card (#380).

The CI-gate half of #474 (pr-validate.yml failing a PR that adds a migrations/
file without a LATEST_MIGRATION bump) is shell/git logic embedded in workflow
YAML, not application code — validated manually against four scenarios
(add-without-bump fails, add-with-bump passes, no-migration-touch skips,
modify-existing-file-only skips) rather than covered here.

Needs a reachable Postgres via DATABASE_URL, same throwaway-instance pattern
as the rest of this suite — skipped entirely if one isn't available.
"""

import os

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")


def _seed_user(pg_conn, user_id, email, is_admin):
    import bcrypt

    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, "
            "is_active, is_admin) VALUES (%s, 'local', %s, %s, %s, true, %s)",
            (user_id, email, email, bcrypt.hashpw(b"password123", bcrypt.gensalt()).decode(), is_admin),
        )


def _login(client, email):
    resp = client.post(
        "/auth/login", data={"email": email, "password": "password123"}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text
    return resp


def _set_pending_migrations(pg_conn, app_module, count):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO migration_state (id, highest_applied_migration) VALUES (1, %s) "
            "ON CONFLICT (id) DO UPDATE SET highest_applied_migration = EXCLUDED.highest_applied_migration",
            (app_module.LATEST_MIGRATION - count,),
        )


def _clean(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE sessions, users RESTART IDENTITY CASCADE")
        cur.execute("DELETE FROM migration_state")


def test_admin_sees_banner_when_migrations_pending(pg_conn, app_module, client):
    _clean(pg_conn)
    _seed_user(pg_conn, 1, "admin@example.com", is_admin=True)
    _set_pending_migrations(pg_conn, app_module, 2)

    _login(client, "admin@example.com")
    home = client.get("/")

    assert '<div class="migration-drift-banner"' in home.text


def test_admin_sees_no_banner_when_fully_up_to_date(pg_conn, app_module, client):
    _clean(pg_conn)
    _seed_user(pg_conn, 1, "admin@example.com", is_admin=True)
    _set_pending_migrations(pg_conn, app_module, 0)

    _login(client, "admin@example.com")
    home = client.get("/")

    assert '<div class="migration-drift-banner"' not in home.text


def test_admin_sees_no_banner_when_migration_state_unknown(pg_conn, app_module, client):
    """No migration_state row at all (a fresh schema.sql install) means
    "unknown", not "pending" — must not show the banner on an unknowable
    state, same contract _pending_migration_count() already has for #466's
    notifier (see tests/test_health_signal_notifications.py's equivalent
    test_no_migration_state_row_no_alert)."""
    _clean(pg_conn)
    _seed_user(pg_conn, 1, "admin@example.com", is_admin=True)

    _login(client, "admin@example.com")
    home = client.get("/")

    assert '<div class="migration-drift-banner"' not in home.text


def test_non_admin_never_sees_banner_even_with_migrations_pending(pg_conn, app_module, client):
    """A non-admin can't act on this (no access to scripts/mark_migration_applied.sh
    against production), so it's never computed or shown for them — matches
    _nav_context's `if user and user["is_admin"]` guard, which also keeps the
    extra query off every non-admin page load."""
    _clean(pg_conn)
    _seed_user(pg_conn, 1, "user@example.com", is_admin=False)
    _set_pending_migrations(pg_conn, app_module, 5)

    _login(client, "user@example.com")
    home = client.get("/")

    assert '<div class="migration-drift-banner"' not in home.text


def test_logged_out_page_never_shows_banner(client):
    resp = client.get("/auth/login")
    assert '<div class="migration-drift-banner"' not in resp.text


def test_banner_text_names_the_pending_count(pg_conn, app_module, client):
    _clean(pg_conn)
    _seed_user(pg_conn, 1, "admin@example.com", is_admin=True)
    _set_pending_migrations(pg_conn, app_module, 3)

    _login(client, "admin@example.com")
    home = client.get("/")

    assert "3" in home.text
    assert "mark_migration_applied.sh" in home.text
