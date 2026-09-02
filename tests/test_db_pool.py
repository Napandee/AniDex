"""
Coverage for issue #462 — app/db.py's get_conn() switched from opening and
closing a brand-new psycopg2 connection per query to checking one out of a
psycopg2.pool.ThreadedConnectionPool and returning it when done.

Real-Postgres pattern matching tests/test_settings_defaults.py and friends:
skipped entirely if no Postgres is reachable, so `pytest tests/` still
collects and passes without one. Three things get real coverage here, per
the issue's own acceptance criteria:

  1. Connections are actually reused across calls, not reopened each time
     (proven via pg_backend_pid() staying the same across sequential calls
     issued from the same thread).
  2. A query error doesn't leak a connection stuck in an aborted-transaction
     state back into the pool — the classic bad-finally bug the issue calls
     out by name. A failing query followed by a clean query must both
     succeed, and the pool's checked-out count must return to zero after
     each get_conn() block, exception or not.
  3. Concurrent load from multiple threads actually works — this is the
     scenario connection pooling exists for in the first place.
"""

import os
import sys
import threading
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")


def _try_connect():
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=2)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def db_module():
    if not _try_connect():
        pytest.skip(
            f"No reachable Postgres at {DATABASE_URL} — this suite needs a real "
            "throwaway instance (same one .github/workflows/pr-validate.yml provisions)."
        )
    from app import db as db_module

    return db_module


def _checked_out_count(db_module):
    # ThreadedConnectionPool tracks in-use connections in _used; every
    # get_conn() block must leave this back at 0 once its `with` exits,
    # exception or not. The pool itself is built lazily on first real query
    # (not at import time — see app/db.py's module docstring comment), so
    # if nothing has queried yet in this test run, "0 checked out" is true
    # by construction rather than by inspecting a pool object that doesn't
    # exist yet.
    pool = db_module._pool
    return 0 if pool is None else len(pool._used)


def test_pool_reuses_connections(db_module):
    pid_1 = db_module.fetchone("SELECT pg_backend_pid() AS pid")["pid"]
    pid_2 = db_module.fetchone("SELECT pg_backend_pid() AS pid")["pid"]
    pid_3 = db_module.fetchone("SELECT pg_backend_pid() AS pid")["pid"]
    assert pid_1 == pid_2 == pid_3, (
        "sequential calls from the same thread should reuse the same pooled "
        "server-side connection instead of opening a fresh one each time"
    )
    assert _checked_out_count(db_module) == 0


def test_failed_query_does_not_leak_or_leave_connection_aborted(db_module):
    assert _checked_out_count(db_module) == 0

    with pytest.raises(psycopg2.Error):
        db_module.fetchone("SELECT * FROM this_table_does_not_exist_xyz")

    # The failing query must not have left a connection checked out...
    assert _checked_out_count(db_module) == 0

    # ...and the connection get_conn() rolled back must be immediately
    # reusable, not stuck in Postgres's "current transaction is aborted"
    # state (the exact failure mode a bad finally/rollback would produce).
    row = db_module.fetchone("SELECT 1 AS one")
    assert row["one"] == 1
    assert _checked_out_count(db_module) == 0


def test_select_without_explicit_commit_does_not_leave_idle_transaction(db_module):
    # fetchall/fetchone never call commit() — before pooling, closing the
    # connection after every call made this a non-issue. With pooling, the
    # connection survives past the query, so get_conn() must proactively end
    # the implicit transaction a plain SELECT opens (rollback is a safe
    # no-op either way) rather than handing back an idle-in-transaction
    # connection for the next caller to inherit.
    db_module.fetchall("SELECT 1")
    with db_module.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM pg_stat_activity WHERE pid = pg_backend_pid()"
            )
            state = cur.fetchone()[0]
    assert state != "idle in transaction"


def test_concurrent_load(db_module):
    errors = []
    results = []
    lock = threading.Lock()

    def worker(n):
        try:
            row = db_module.fetchone("SELECT %s::int AS n", (n,))
            with lock:
                results.append(row["n"])
        except Exception as exc:  # pragma: no cover - failure path
            with lock:
                errors.append(exc)

    # Comfortably under the default maxconn=20 — ThreadedConnectionPool.getconn()
    # raises PoolError immediately rather than blocking/queuing when the pool is
    # exhausted, so this stays well clear of that rather than risking a flaky
    # false failure under CI scheduling jitter.
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"concurrent queries raised: {errors}"
    assert sorted(results) == list(range(10))
    assert _checked_out_count(db_module) == 0


def test_execute_and_execute_returning_still_commit(db_module):
    db_module.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _pool_test_462 (id serial primary key, val text)"
    )
    row = db_module.execute_returning(
        "INSERT INTO _pool_test_462 (val) VALUES (%s) RETURNING id, val", ("hello",)
    )
    assert row["val"] == "hello"
    fetched = db_module.fetchone(
        "SELECT val FROM _pool_test_462 WHERE id = %s", (row["id"],)
    )
    assert fetched["val"] == "hello"
    assert _checked_out_count(db_module) == 0
