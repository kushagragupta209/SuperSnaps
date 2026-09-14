"""
db_pg.py — thin Postgres compatibility layer for cloud deployment.

Ledger runs against local SQLite by default (zero setup, matches the
original design). Setting DATABASE_URL switches app.py to Postgres
instead — e.g. when deployed on Render, pointed at a Supabase Postgres
project, so a laptop instance and a cloud instance can share one always-
current database instead of drifting apart as two separate SQLite files.

This module exists so that switch didn't require rewriting every one of
app.py's ~40 query call sites — only get_db()/init_db() needed to change.
It transparently handles the differences between sqlite3's interface (what
all that existing code already speaks) and psycopg2's:

  - '?' placeholders          -> '%s' (psycopg2's paramstyle)
  - sqlite3.Row dict access   -> psycopg2.extras.RealDictCursor (row["col"]
                                  and dict(row) both keep working unchanged)
  - cur.lastrowid             -> auto-appends "RETURNING id" to INSERT
                                  statements (skipped for `settings`, which
                                  has no id column) and captures it
                                  transparently, so
                                  `cur = db.execute(...); cur.lastrowid`
                                  keeps working exactly as it did under sqlite3

Parameterless queries (no second argument to .execute()) are sent to
psycopg2 WITHOUT triggering its %-substitution at all — this matters
because a few queries embed a literal '%' directly in SQL text (LIKE
patterns) rather than as a bound value, most notably the raw SQL the chat
feature's LLM generates on the fly (see agent.generate_chat_sql) — passing
those through substitution would make psycopg2 try to interpret '%' as a
format specifier and crash.
"""

import os
import re

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # only required when DATABASE_URL is actually set
    psycopg2 = None


_INSERT_RE = re.compile(r"^\s*insert\s+into\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)
_RETURNING_RE = re.compile(r"\breturning\b", re.IGNORECASE)

# Tables with no `id` primary key — never auto-append RETURNING id for these.
_NO_ID_TABLES = {"settings"}


def is_postgres_configured() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


class PGCursor:
    """Wraps a real psycopg2 cursor; adds a sqlite3-style .lastrowid attribute."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid = None

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)

    @property
    def rowcount(self):
        return self._cursor.rowcount


class PGConnection:
    """
    Wraps a psycopg2 connection. .execute() mimics sqlite3.Connection's
    shortcut method (create a cursor, run the query, return the cursor) so
    every existing `db.execute(sql, params).fetchone()` / `for row in
    db.execute(sql):` call site in app.py keeps working unchanged.
    """

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        pg_sql = sql.replace("?", "%s")

        table_match = _INSERT_RE.match(sql)
        auto_returning = False
        if (
            table_match
            and table_match.group(1).lower() not in _NO_ID_TABLES
            and not _RETURNING_RE.search(sql)
        ):
            pg_sql = pg_sql.rstrip().rstrip(";") + " RETURNING id"
            auto_returning = True

        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if params:
            cur.execute(pg_sql, params)
        else:
            # Deliberately omit the second argument entirely rather than
            # passing () — psycopg2 only skips %-substitution when no
            # params argument is given at all, which matters for raw SQL
            # (e.g. LLM-generated chat queries) containing a literal '%'.
            cur.execute(pg_sql)

        wrapped = PGCursor(cur)
        if auto_returning:
            try:
                row = cur.fetchone()
                wrapped.lastrowid = row["id"] if row else None
            except psycopg2.ProgrammingError:
                wrapped.lastrowid = None
        return wrapped

    def executescript(self, sql):
        """Runs a multi-statement DDL string in one call, same as sqlite3.executescript()."""
        cur = self._conn.cursor()
        cur.execute(sql)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def connect():
    """
    Opens a new Postgres connection using DATABASE_URL, wrapped for
    sqlite3-style usage. Raises a clear error if psycopg2 isn't installed
    or DATABASE_URL isn't set, rather than a cryptic one deeper in Flask.
    """
    if psycopg2 is None:
        raise RuntimeError(
            "DATABASE_URL is set but psycopg2 isn't installed — "
            "run: pip install psycopg2-binary"
        )
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("db_pg.connect() called without DATABASE_URL set.")
    raw_conn = psycopg2.connect(database_url)
    return PGConnection(raw_conn)
