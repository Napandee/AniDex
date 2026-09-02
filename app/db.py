import os
import threading
import psycopg2
import psycopg2.extras
import psycopg2.pool
from contextlib import contextmanager

DATABASE_URL = os.environ["DATABASE_URL"]

# Small pool sized for this app's actual scale (a handful of users, not
# thousands) — every FastAPI request, in-process APScheduler job, and MCP
# server tool call shares this one pool. Override via env vars if a specific
# deployment ever needs to tune it.
_POOL_MIN_CONN = int(os.environ.get("DB_POOL_MIN_CONN", "2"))
_POOL_MAX_CONN = int(os.environ.get("DB_POOL_MAX_CONN", "20"))

# Built lazily on first real use, not at import time. Two reasons:
#
# 1. ThreadedConnectionPool.__init__ eagerly opens `minconn` real
#    connections immediately — this module is imported (transitively, via
#    app.main) at test-collection time by test files that don't gate that
#    import behind a Postgres-reachability check, and the suite's documented
#    guarantee is that `pytest tests/` still collects and passes with no
#    Postgres reachable at all. Building the pool at first actual query
#    keeps that guarantee (import stays connection-free, matching the
#    plain-connect-per-call code this replaces).
# 2. Non-obvious psycopg2 behavior worth flagging: minconn isn't just "how
#    many connections to open at startup" — _putconn() only keeps a
#    returned connection in the reusable idle pool while
#    len(self._pool) < minconn; with minconn=0 every connection gets
#    closed on return instead of reused, silently defeating pooling
#    entirely. So minconn must stay a real positive number (it does, above)
#    — it's pool *construction* that's deferred here, not minconn itself.
_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    _POOL_MIN_CONN, _POOL_MAX_CONN, DATABASE_URL
                )
    return _pool


def close_pool():
    """Close every pooled connection. Called on app shutdown; a no-op if the
    pool was never actually built (nothing ever queried the DB).

    Resets the singleton back to None (not just closed) rather than leaving
    a dead pool object behind — the next real query then transparently
    rebuilds a fresh one via _get_pool(), instead of every future getconn()
    permanently raising PoolError("connection pool is closed"). This matters
    for more than tidiness: the test suite creates many independent
    TestClient(app) instances against this one shared app.db module across a
    single pytest process, and each one's `with TestClient(...)` context
    exit fires this same shutdown handler — without the reset, the first
    test file to exit its client would poison every later test's queries
    for the rest of the run."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


@contextmanager
def get_conn():
    pool = _get_pool()
    conn = pool.getconn()
    discard = False
    try:
        yield conn
    finally:
        # Always reset transaction state before the connection goes back to
        # the pool — a caller that raised mid-transaction leaves it aborted,
        # and even a plain SELECT (fetchall/fetchone never commit) leaves it
        # idle-in-transaction otherwise. rollback() is a harmless no-op after
        # an explicit commit() already ended the transaction. If rollback()
        # itself fails, the connection is broken at the network/server level
        # rather than just mid-transaction — discard it instead of returning
        # a dead connection to the pool for the next caller to inherit.
        try:
            conn.rollback()
        except psycopg2.Error:
            discard = True
        pool.putconn(conn, close=discard)


def fetchall(query, params=None):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchall()


def fetchone(query, params=None):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchone()


def execute(query, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
        conn.commit()


def execute_returning(query, params=None):
    """Like execute(), but for INSERT/UPDATE ... RETURNING — fetches and commits."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            row = cur.fetchone()
        conn.commit()
        return row
