#!/usr/bin/env python3
"""
migrate_to_postgres.py — one-time transfer of an existing local ledger.db
into Postgres (your Supabase project), so Ledger's data survives Render's
ephemeral filesystem across restarts/redeploys.

Safe to re-run: every INSERT uses ON CONFLICT DO NOTHING, so running this
twice won't duplicate rows — it'll just skip whatever's already migrated.
Explicitly inserts the SAME id values SQLite already assigned (rather than
letting Postgres generate new ones), so foreign keys like
flight_price_history.flight_id keep pointing at the right row without any
remapping. Afterward, fixes up each table's SERIAL sequence so future
inserts don't collide with the migrated ids.

Usage:
    export DATABASE_URL="postgresql://...your Supabase connection string..."
    python migrate_to_postgres.py [path/to/ledger.db]

    (defaults to ./ledger.db if no path is given)
"""

import sys
import sqlite3
from pathlib import Path

import db_pg
import app as ledger_app  # reuses _init_db_postgres() so the schema is guaranteed to exist first


TABLES = [
    # (table, columns) — order matters: parents before children, so
    # foreign keys (wishlist_id, flight_id) always exist by the time a
    # child row references them.
    ("purchases", ["id", "name", "price", "purchased_on", "created_at"]),
    ("settings", ["key", "value"]),  # no id column — different conflict target below
    ("monthly_expenses", ["id", "year", "month", "category", "amount", "raw_text", "created_at"]),
    ("wishlist", ["id", "name", "price", "created_at", "product_url", "platform"]),
    ("wishlist_price_history", ["id", "wishlist_id", "price", "checked_at"]),
    ("day_expenses", ["id", "date", "merchant", "category", "amount", "source", "created_at"]),
    ("flights", ["id", "origin", "destination", "departure_date", "return_date", "adults",
                 "travel_class", "target_price", "notify_email", "current_price", "lowest_price",
                 "active", "created_at"]),
    ("flight_price_history", ["id", "flight_id", "price", "checked_at"]),
]


def migrate(sqlite_path: str):
    if not db_pg.is_postgres_configured():
        print("ERROR: DATABASE_URL is not set. Export it to your Postgres "
              "connection string first.")
        sys.exit(1)

    if not Path(sqlite_path).exists():
        print(f"ERROR: {sqlite_path} does not exist.")
        sys.exit(1)

    print("Ensuring Postgres schema exists (via app._init_db_postgres())...")
    ledger_app._init_db_postgres()

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    pg = db_pg.connect()

    for table, columns in TABLES:
        try:
            rows = sqlite_conn.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
        except sqlite3.OperationalError as exc:
            print(f"  {table}: skipped ({exc})")
            continue

        if not rows:
            print(f"  {table}: 0 rows (nothing to migrate)")
            continue

        placeholders = ", ".join("?" for _ in columns)
        col_list = ", ".join(columns)
        conflict_target = "key" if table == "settings" else "id"

        inserted = 0
        for row in rows:
            values = tuple(row[c] for c in columns)
            pg.execute(
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                f"ON CONFLICT ({conflict_target}) DO NOTHING",
                values,
            )
            inserted += 1
        pg.commit()
        print(f"  {table}: {inserted} row(s) migrated (duplicates skipped automatically)")

        if table != "settings":
            pg.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
            )
            pg.commit()

    sqlite_conn.close()
    pg.close()
    print("\nDone. Your local ledger.db data now also lives in Postgres.")
    print("The local ledger.db file itself is untouched — this only copies, never deletes.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "ledger.db"
    migrate(path)
