import os
from dotenv import load_dotenv

load_dotenv()
"""
Ledger — a local-first personal purchase timeline + monthly expense tracker.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:8000

Natural-language expense parsing lives in agent.py, not here — see that
file for how to enable the Groq-backed parser and how to add new agent
features later.
"""

import math
import re
import sqlite3
import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from datetime import datetime, date
from pathlib import Path

from flask import Flask, g, jsonify, render_template, request, abort

import agent
import flights
import telegram_sync
import sara
import db_pg
import auth

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "ledger.db"

app = Flask(__name__)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

def get_db():
    """
    Local SQLite by default (zero setup). If DATABASE_URL is set (e.g.
    deployed on Render, pointed at your Supabase Postgres project — the
    same one already holding pending_expenses for Telegram sync), uses
    that instead via db_pg.py, so the database survives Render's
    ephemeral filesystem across restarts/redeploys.
    """
    if "db" not in g:
        if db_pg.is_postgres_configured():
            g.db = db_pg.connect()
        else:
            g.db = sqlite3.connect(DB_PATH)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    if db_pg.is_postgres_configured():
        _init_db_postgres()
    else:
        _init_db_sqlite()


def _init_db_sqlite():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            id TEXT PRIMARY KEY,
            email TEXT,
            display_name TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS purchases (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            price       REAL NOT NULL,
            purchased_on TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            key     TEXT NOT NULL,
            value   TEXT,
            UNIQUE(user_id, key)
        );

        -- One row per (year, month) holding the parsed recurring-expense
        -- categories for that month, e.g. rent / food / fuel.
        CREATE TABLE IF NOT EXISTS monthly_expenses (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            year       INTEGER NOT NULL,
            month      INTEGER NOT NULL,
            category   TEXT NOT NULL,
            amount     REAL NOT NULL,
            raw_text   TEXT,
            created_at TEXT NOT NULL
        );

        -- Wishlist: products the user wants to buy next, used to project
        -- how many months of savings it'll take to afford each one.
        CREATE TABLE IF NOT EXISTS wishlist (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            price       REAL NOT NULL,
            created_at  TEXT NOT NULL,
            product_url TEXT,
            platform    TEXT
        );

        CREATE TABLE IF NOT EXISTS wishlist_price_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            wishlist_id INTEGER NOT NULL,
            price       REAL NOT NULL,
            checked_at  TEXT NOT NULL,
            FOREIGN KEY (wishlist_id) REFERENCES wishlist(id) ON DELETE CASCADE
        );

        -- Day-wise expenses extracted from an uploaded screenshot (order
        -- history, bank/UPI statement, etc.) — one row per order/transaction,
        -- unlike monthly_expenses which is one row per category per month.
        CREATE TABLE IF NOT EXISTS day_expenses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            merchant    TEXT,
            category    TEXT NOT NULL,
            amount      REAL NOT NULL,
            source      TEXT,
            created_at  TEXT NOT NULL
        );

        -- Flight fare trackers: one row per route/date the user wants to
        -- watch, with the latest and lowest-seen fare cached for fast
        -- reads. Full history lives in flight_price_history.
        CREATE TABLE IF NOT EXISTS financial_goals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            name        TEXT NOT NULL,
            target_amount REAL NOT NULL,
            current_amount REAL NOT NULL DEFAULT 0,
            target_date TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            UNIQUE(user_id, name)
        );

        CREATE TABLE IF NOT EXISTS flights (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            origin         TEXT NOT NULL,
            destination    TEXT NOT NULL,
            departure_date TEXT NOT NULL,
            return_date    TEXT,
            adults         INTEGER NOT NULL DEFAULT 1,
            travel_class   TEXT NOT NULL DEFAULT 'ECONOMY',
            target_price   REAL,
            notify_email   TEXT,
            current_price  REAL,
            lowest_price   REAL,
            active         INTEGER NOT NULL DEFAULT 1,
            created_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS flight_price_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            flight_id  INTEGER NOT NULL,
            price      REAL NOT NULL,
            checked_at TEXT NOT NULL,
            FOREIGN KEY (flight_id) REFERENCES flights(id) ON DELETE CASCADE
        );
        """
    )
    # Lightweight migration for existing Ledger databases.
    columns = {r[1] for r in db.execute("PRAGMA table_info(wishlist)").fetchall()}
    if "product_url" not in columns:
        db.execute("ALTER TABLE wishlist ADD COLUMN product_url TEXT")
    if "platform" not in columns:
        db.execute("ALTER TABLE wishlist ADD COLUMN platform TEXT")
    flight_columns = {r[1] for r in db.execute("PRAGMA table_info(flights)").fetchall()}
    if "telegram_chat_id" not in flight_columns:
        db.execute("ALTER TABLE flights ADD COLUMN telegram_chat_id INTEGER")
    if "notify_telegram" not in flight_columns:
        db.execute("ALTER TABLE flights ADD COLUMN notify_telegram INTEGER NOT NULL DEFAULT 1")
    db.commit()
    db.close()


def _init_db_postgres():
    """
    Same schema as _init_db_sqlite, translated to Postgres: SERIAL instead
    of AUTOINCREMENT, inline REFERENCES instead of a trailing FOREIGN KEY
    clause, and `ADD COLUMN IF NOT EXISTS` instead of SQLite's PRAGMA-based
    introspection.

    Also creates pending_expenses here — see telegram_sync.py — so this
    single init_db() call sets up everything Telegram sync needs too, now
    that it lives in the same database Ledger itself uses, instead of
    being reached through a separate REST-only path.
    """
    db = db_pg.connect()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS purchases (
            id           SERIAL PRIMARY KEY,
            name         TEXT NOT NULL,
            price        DOUBLE PRECISION NOT NULL,
            purchased_on TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS monthly_expenses (
            id         SERIAL PRIMARY KEY,
            year       INTEGER NOT NULL,
            month      INTEGER NOT NULL,
            category   TEXT NOT NULL,
            amount     DOUBLE PRECISION NOT NULL,
            raw_text   TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS wishlist (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            price       DOUBLE PRECISION NOT NULL,
            created_at  TEXT NOT NULL,
            product_url TEXT,
            platform    TEXT
        );

        CREATE TABLE IF NOT EXISTS wishlist_price_history (
            id          SERIAL PRIMARY KEY,
            wishlist_id INTEGER NOT NULL REFERENCES wishlist(id) ON DELETE CASCADE,
            price       DOUBLE PRECISION NOT NULL,
            checked_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS day_expenses (
            id          SERIAL PRIMARY KEY,
            date        TEXT NOT NULL,
            merchant    TEXT,
            category    TEXT NOT NULL,
            amount      DOUBLE PRECISION NOT NULL,
            source      TEXT,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS financial_goals (
            id          SERIAL PRIMARY KEY,
            user_id     TEXT NOT NULL,
            name        TEXT NOT NULL,
            target_amount DOUBLE PRECISION NOT NULL,
            current_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
            target_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, name)
        );

        CREATE TABLE IF NOT EXISTS flights (
            id             SERIAL PRIMARY KEY,
            origin         TEXT NOT NULL,
            destination    TEXT NOT NULL,
            departure_date TEXT NOT NULL,
            return_date    TEXT,
            adults         INTEGER NOT NULL DEFAULT 1,
            travel_class   TEXT NOT NULL DEFAULT 'ECONOMY',
            target_price   DOUBLE PRECISION,
            notify_email   TEXT,
            current_price  DOUBLE PRECISION,
            lowest_price   DOUBLE PRECISION,
            active         INTEGER NOT NULL DEFAULT 1,
            created_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS flight_price_history (
            id         SERIAL PRIMARY KEY,
            flight_id  INTEGER NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
            price      DOUBLE PRECISION NOT NULL,
            checked_at TEXT NOT NULL
        );

        -- Telegram/Sara multi-user tables.
        CREATE TABLE IF NOT EXISTS telegram_sara_messages (
            id                    SERIAL PRIMARY KEY,
            chat_id               BIGINT NOT NULL,
            telegram_message_id   BIGINT,
            role                  TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content               TEXT NOT NULL,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_telegram_sara_messages_chat
            ON telegram_sara_messages (chat_id, id);

        CREATE TABLE IF NOT EXISTS telegram_user_links (
            chat_id   BIGINT PRIMARY KEY,
            user_id   UUID NOT NULL,
            linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        -- Telegram's durable inbox (see telegram_sync.py) — written by the
        -- Cloudflare Worker via Supabase's REST API, read here directly.
        CREATE TABLE IF NOT EXISTS pending_expenses (
            id                  SERIAL PRIMARY KEY,
            chat_id             BIGINT NOT NULL,
            raw_text            TEXT NOT NULL,
            telegram_message_id BIGINT NOT NULL,
            sent_at             TIMESTAMPTZ NOT NULL,
            processed           BOOLEAN NOT NULL DEFAULT FALSE,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_pending_expenses_unprocessed
            ON pending_expenses (processed, sent_at);

        ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS product_url TEXT;
        ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS platform TEXT;
        ALTER TABLE flights ADD COLUMN IF NOT EXISTS telegram_chat_id BIGINT;
        ALTER TABLE flights ADD COLUMN IF NOT EXISTS notify_telegram BOOLEAN NOT NULL DEFAULT TRUE;
        """
    )
    db.commit()
    db.close()


# Runs on both `python app.py` (local dev) and `gunicorn app:app` (Render/
# production, which imports this module rather than executing it as
# __main__ — calling init_db() only inside the old `if __name__ ==
# "__main__":` block would mean gunicorn never creates the schema at
# all). CREATE TABLE IF NOT EXISTS is idempotent, so re-running this on
# every worker boot is safe.
init_db()


# --------------------------------------------------------------------------- #
# Routes — authentication / multi-user foundation
# --------------------------------------------------------------------------- #

@app.before_request
def enforce_api_auth():
    if not request.path.startswith("/api/"):
        return None

    public = {"/api/auth/config", "/api/auth/me"}

    if request.path in public:
        return None

    # Initial Telegram/Sara request.
    # The user_id is resolved later from chat_id -> telegram_user_links.
    if (
        request.path == "/api/telegram/sara"
        and request.headers.get("X-Telegram-Sara-Secret")
        == os.environ.get("TELEGRAM_SARA_SECRET")
    ):
        return None

    # Normal Supabase-authenticated requests.
    if auth.get_user_from_token(auth.get_bearer_token(request)):
        return None

    # Internal Sara tool calls after the Telegram user has been resolved.
    internal_secret = request.headers.get("X-Telegram-Sara-Secret")
    internal_user_id = request.headers.get("X-Telegram-User-Id")

    if (
        internal_secret
        and internal_user_id
        and os.environ.get("TELEGRAM_SARA_SECRET") == internal_secret
    ):
        return None

    return jsonify({"error": "Authentication required."}), 401


@app.route("/api/auth/config", methods=["GET"])
def auth_config():
    # The anon/publishable key is intentionally safe to expose to the browser.
    # Never expose SUPABASE_SERVICE_KEY here.
    return jsonify({
        "configured": auth.is_configured(),
        "supabase_url": auth.SUPABASE_URL if auth.is_configured() else None,
        "supabase_anon_key": auth.SUPABASE_ANON_KEY if auth.is_configured() else None,
    })


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    user = auth.get_user_from_token(auth.get_bearer_token(request))
    if not user:
        return jsonify({"authenticated": False}), 401
    return jsonify({"authenticated": True, "user": user})


def _ensure_user_columns_sqlite(db):
    """Add nullable ownership columns without changing existing records."""
    db.execute("CREATE TABLE IF NOT EXISTS telegram_user_links (chat_id INTEGER PRIMARY KEY, user_id TEXT NOT NULL)")
    for table in ("purchases", "settings", "monthly_expenses", "wishlist", "day_expenses", "flights"):
        columns = {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if "user_id" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT")
    # Older SQLite databases used key as the primary key. Rebuild that table
    # once so identical setting keys can exist for different accounts.
    settings_info = db.execute("PRAGMA table_info(settings)").fetchall()
    key_is_primary = any(r[1] == "key" and r[5] == 1 for r in settings_info)
    if key_is_primary:
        db.execute("DROP TABLE IF EXISTS settings_new")
        db.execute("""
            CREATE TABLE settings_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                key TEXT NOT NULL,
                value TEXT,
                UNIQUE(user_id, key)
            )
        """)
        has_user_id = any(r[1] == "user_id" for r in settings_info)
        if has_user_id:
            db.execute("INSERT INTO settings_new (user_id, key, value) SELECT user_id, key, value FROM settings")
        else:
            db.execute("INSERT INTO settings_new (key, value) SELECT key, value FROM settings")
        db.execute("DROP TABLE settings")
        db.execute("ALTER TABLE settings_new RENAME TO settings")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_settings_user_key ON settings(user_id, key)")
    db.commit()


def _ensure_user_columns_postgres(db):
    """Add nullable ownership columns without changing existing records."""
    for table in ("purchases", "settings", "monthly_expenses", "wishlist", "day_expenses", "flights"):
        db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS user_id UUID")
    db.execute("ALTER TABLE settings DROP CONSTRAINT IF EXISTS settings_pkey")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_settings_user_key ON settings(user_id, key)")
    db.commit()


def ensure_user_columns():
    db = get_db()
    if db_pg.is_postgres_configured():
        _ensure_user_columns_postgres(db)
    else:
        _ensure_user_columns_sqlite(db)


def current_user():
    """Return the Supabase user represented by this request's Bearer token."""
    if hasattr(g, "current_user"):
        return g.current_user
    token_user = auth.get_user_from_token(auth.get_bearer_token(request))
    if token_user:
        g.current_user = token_user
        return g.current_user
    internal_user_id = request.headers.get("X-Telegram-User-Id")
    internal_secret = request.headers.get("X-Telegram-Sara-Secret")
    if internal_user_id and os.environ.get("TELEGRAM_SARA_SECRET") == internal_secret:
        g.current_user = {"id": internal_user_id, "email": None}
        return g.current_user
    g.current_user = None
    return None


def require_user():
    user = current_user()
    if not user:
        abort(401, description="Authentication required.")
    return user


# Ownership columns are nullable during migration so existing single-user
# records remain intact until they are explicitly assigned to an account.
with app.app_context():
    ensure_user_columns()

@app.route("/api/migration/status", methods=["GET"])
def migration_status():
    """Report legacy financial rows that have not been assigned to an account."""
    require_user()
    db = get_db()
    tables = (
        "purchases",
        "settings",
        "monthly_expenses",
        "wishlist",
        "day_expenses",
        "flights",
    )
    counts = {}
    for table in tables:
        counts[table] = db.execute(
            "SELECT COUNT(*) AS count FROM " + table + " WHERE user_id IS NULL"
        ).fetchone()["count"]
    return jsonify({"unowned": counts, "total": sum(counts.values())})


@app.route("/api/migration/claim", methods=["POST"])
def migration_claim():
    """Disabled after the one-time legacy ownership migration completed."""
    require_user()
    return jsonify({
        "error": "Migration is already complete.",
        "message": "There are no unowned legacy records left to claim.",
    }), 410

@app.route("/api/profile", methods=["GET", "POST"])
def profile():
    """Read or create the signed-in user's profile."""
    user = require_user()
    db = get_db()
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        display_name = (data.get("display_name") or "").strip() or None
        if db_pg.is_postgres_configured():
            db.execute("""INSERT INTO profiles (id, email, display_name) VALUES (?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET email=excluded.email,
                display_name=excluded.display_name, updated_at=now()""",
                (user["id"], user.get("email"), display_name))
        else:
            db.execute("""INSERT INTO profiles (id, email, display_name) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET email=excluded.email,
                display_name=excluded.display_name, updated_at=datetime('now')""",
                (user["id"], user.get("email"), display_name))
        db.commit()
    row = db.execute("SELECT id, email, display_name, created_at, updated_at FROM profiles WHERE id = ?", (user["id"],)).fetchone()
    if not row:
        if db_pg.is_postgres_configured():
            db.execute("INSERT INTO profiles (id, email) VALUES (?, ?) ON CONFLICT (id) DO NOTHING", (user["id"], user.get("email")))
        else:
            db.execute("INSERT OR IGNORE INTO profiles (id, email) VALUES (?, ?)", (user["id"], user.get("email")))
        db.commit()
        row = db.execute("SELECT id, email, display_name, created_at, updated_at FROM profiles WHERE id = ?", (user["id"],)).fetchone()
    return jsonify(dict(row))


# --------------------------------------------------------------------------- #
# Routes — pages
# --------------------------------------------------------------------------- #

@app.route("/")
def index():
    return render_template("index.html")


# --------------------------------------------------------------------------- #
# Routes — API: purchases (timeline)
# --------------------------------------------------------------------------- #

@app.route("/api/purchases", methods=["GET"])
def list_purchases():
    user = require_user()
    db = get_db()
    rows = db.execute(
        "SELECT * FROM purchases WHERE user_id = ? ORDER BY purchased_on DESC, id DESC",
        (user["id"],)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/purchases", methods=["POST"])
def add_purchase():
    user = require_user()
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    price = data.get("price")
    purchased_on = data.get("purchased_on") or date.today().isoformat()

    if not name:
        return jsonify({"error": "Product name is required."}), 400
    try:
        price = float(price)
        if price < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Price must be a positive number."}), 400

    db = get_db()
    cur = db.execute(
        "INSERT INTO purchases (name, price, purchased_on, created_at, user_id) VALUES (?, ?, ?, ?, ?)",
        (name, price, purchased_on, datetime.utcnow().isoformat(), user["id"]),
    )
    db.commit()
    new_row = db.execute("SELECT * FROM purchases WHERE id = ? AND user_id = ?", (cur.lastrowid, user["id"])).fetchone()
    return jsonify(dict(new_row)), 201


@app.route("/api/purchases/<int:purchase_id>", methods=["DELETE"])
def delete_purchase(purchase_id):
    user = require_user()
    db = get_db()
    cur = db.execute("DELETE FROM purchases WHERE id = ? AND user_id = ?", (purchase_id, user["id"]))
    if cur.rowcount == 0:
        return jsonify({"error": "Purchase not found."}), 404
    db.commit()
    return jsonify({"deleted": purchase_id})


# --------------------------------------------------------------------------- #
# Routes — API: settings (monthly salary)
# --------------------------------------------------------------------------- #

@app.route("/api/settings/salary", methods=["GET"])
def get_salary():
    user = require_user()
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = 'monthly_salary' AND user_id = ?", (user["id"],)).fetchone()
    return jsonify({"monthly_salary": float(row["value"]) if row else None})


@app.route("/api/settings/salary", methods=["POST"])
def set_salary():
    user = require_user()
    data = request.get_json(force=True)
    try:
        salary = float(data.get("monthly_salary"))
        if salary < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Salary must be a positive number."}), 400

    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value, user_id) VALUES ('monthly_salary', ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
        (str(salary), user["id"]),
    )
    db.commit()
    return jsonify({"monthly_salary": salary})


@app.route("/api/settings/savings-percent", methods=["GET"])
def get_savings_percent():
    user = require_user()
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = 'savings_percent' AND user_id = ?", (user["id"],)).fetchone()
    return jsonify({"savings_percent": float(row["value"]) if row else None})


@app.route("/api/settings/savings-percent", methods=["POST"])
def set_savings_percent():
    user = require_user()
    """Blank/null clears the goal so insights fall back to the 50/30/20 default."""
    data = request.get_json(force=True)
    raw = data.get("savings_percent")

    db = get_db()
    if raw is None or raw == "":
        db.execute("DELETE FROM settings WHERE key = 'savings_percent' AND user_id = ?", (user["id"],))
        db.commit()
        return jsonify({"savings_percent": None})

    try:
        pct = float(raw)
        if not (0 <= pct <= 100):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Savings goal must be a percentage between 0 and 100."}), 400

    db.execute(
        "INSERT INTO settings (key, value, user_id) VALUES ('savings_percent', ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
        (str(pct), user["id"]),
    )
    db.commit()
    return jsonify({"savings_percent": pct})


# --------------------------------------------------------------------------- #
# Routes — API: spending insights (LLM/rule-based monthly review + budget)
# --------------------------------------------------------------------------- #

def _month_spend(db, year, month, user_id):
    """Returns (purchases_total, fixed_total, fixed_rows) for one (year, month)."""
    month_prefix = f"{year:04d}-{month:02d}"
    purchases_total = db.execute(
        "SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE purchased_on LIKE ? AND user_id = ?",
        (f"{month_prefix}%", user_id),
    ).fetchone()["s"]
    fixed_rows = db.execute(
        "SELECT category, amount FROM monthly_expenses WHERE year = ? AND month = ? AND user_id = ? ORDER BY amount DESC",
        (year, month, user_id),
    ).fetchall()
    fixed_total = sum(r["amount"] for r in fixed_rows)
    return purchases_total, fixed_total, [dict(r) for r in fixed_rows]


@app.route("/api/insights", methods=["GET"])
def insights():
    user = require_user()
    """
    Judges the current month's spending against salary/savings goal and
    recent history, and proposes a next-month budget. See agent.py's
    analyze_spending() / INSIGHTS_SYSTEM_PROMPT for the full response shape
    and how the next-month budget is derived.
    """
    db = get_db()
    today = date.today()

    month_purchases, fixed_total, fixed_expenses = _month_spend(db, today.year, today.month, user["id"])

    ytd_purchases = db.execute(
        "SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE purchased_on LIKE ? AND user_id = ?",
        (f"{today.year:04d}-%", user["id"]),
    ).fetchone()["s"]

    # Last 6 months including the current one, oldest first.
    history = []
    for i in range(5, -1, -1):
        y, m = today.year, today.month - i
        while m <= 0:
            m += 12
            y -= 1
        p, f, _ = _month_spend(db, y, m, user["id"])
        history.append({"year": y, "month": m, "purchases": round(p, 2), "fixed_expenses": round(f, 2)})

    salary_row = db.execute("SELECT value FROM settings WHERE key = 'monthly_salary' AND user_id = ?", (user["id"],)).fetchone()
    salary = float(salary_row["value"]) if salary_row else None

    savings_row = db.execute("SELECT value FROM settings WHERE key = 'savings_percent' AND user_id = ?", (user["id"],)).fetchone()
    savings_percent = float(savings_row["value"]) if savings_row else None

    context = {
        "monthly_salary": salary,
        "year": today.year,
        "month": today.month,
        "month_purchases": round(month_purchases, 2),
        "fixed_total": round(fixed_total, 2),
        "fixed_expenses": fixed_expenses,
        "ytd_purchases": round(ytd_purchases, 2),
        "history_last_6_months": history,
        "savings_percent": savings_percent,
        "recommended_savings_percent": agent.DEFAULT_SAVINGS_PERCENT,
    }

    result, source = agent.analyze_spending(context)
    return jsonify({**result, "source": source})


# --------------------------------------------------------------------------- #
# Routes — API: profile (job/field of work + interests, for recommendations)
# --------------------------------------------------------------------------- #

@app.route("/api/settings/profile", methods=["GET"])
def get_profile():
    user = require_user()
    db = get_db()
    rows = db.execute(
        "SELECT key, value FROM settings WHERE key IN ('profession', 'interests') AND user_id = ?", (user["id"],)
    ).fetchall()
    values = {r["key"]: r["value"] for r in rows}
    return jsonify({
        "profession": values.get("profession", ""),
        "interests": values.get("interests", ""),
    })


@app.route("/api/settings/profile", methods=["POST"])
def set_profile():
    user = require_user()
    data = request.get_json(force=True)
    profession = (data.get("profession") or "").strip()
    interests = (data.get("interests") or "").strip()

    db = get_db()
    for key, value in (("profession", profession), ("interests", interests)):
        db.execute(
            "INSERT INTO settings (key, value, user_id) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (key, value, user["id"]),
        )
    db.commit()
    return jsonify({"profession": profession, "interests": interests})


# --------------------------------------------------------------------------- #
# Routes — API: product recommendations
# --------------------------------------------------------------------------- #

@app.route("/api/recommendations", methods=["GET"])
def recommendations():
    user = require_user()
    db = get_db()
    purchase_rows = db.execute(
        "SELECT name, price FROM purchases WHERE user_id = ? ORDER BY purchased_on DESC, id DESC LIMIT 20",
        (user["id"],)
    ).fetchall()
    purchases = [dict(r) for r in purchase_rows]

    profile_rows = db.execute(
        "SELECT key, value FROM settings WHERE key IN ('profession', 'interests') AND user_id = ?",
        (user["id"],)
    ).fetchall()
    values = {r["key"]: r["value"] for r in profile_rows}

    items, source = agent.recommend_products(
        purchases, values.get("profession", ""), values.get("interests", "")
    )
    return jsonify({"items": items, "source": source})


# --------------------------------------------------------------------------- #
# Routes — API: wishlist + savings projection
# --------------------------------------------------------------------------- #

@app.route("/api/wishlist", methods=["GET"])
def list_wishlist():
    user = require_user()
    """
    Returns each wishlist item alongside a savings projection: how much is
    being saved per month right now (salary minus this month's fixed
    expenses and purchases), and how many months at that rate it'd take to
    afford each item.
    """
    db = get_db()
    rows = db.execute(
        "SELECT * FROM wishlist WHERE user_id = ? ORDER BY price ASC, id ASC", (user["id"],)
    ).fetchall()
    items = [dict(r) for r in rows]

    salary_row = db.execute("SELECT value FROM settings WHERE key = 'monthly_salary' AND user_id = ?", (user["id"],)).fetchone()
    salary = float(salary_row["value"]) if salary_row else None

    today = date.today()
    month_prefix = f"{today.year:04d}-{today.month:02d}"
    month_purchases = db.execute(
        "SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE purchased_on LIKE ? AND user_id = ?",
        (f"{month_prefix}%", user_id),
    ).fetchone()["s"]
    fixed_total = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM monthly_expenses WHERE year = ? AND month = ? AND user_id = ?",
        (today.year, today.month, user["id"]),
    ).fetchone()["s"]

    monthly_savings = None
    if salary is not None:
        monthly_savings = round(salary - fixed_total - month_purchases, 2)

    for item in items:
        if monthly_savings is None:
            item["months_to_afford"] = None
            item["projected_date"] = None
            item["note"] = "Add your monthly salary to see a savings projection."
        elif monthly_savings <= 0:
            item["months_to_afford"] = None
            item["projected_date"] = None
            item["note"] = "You're not currently saving anything this month — projection unavailable."
        else:
            months = math.ceil(item["price"] / monthly_savings)
            item["months_to_afford"] = months
            year = today.year + (today.month - 1 + months) // 12
            month = (today.month - 1 + months) % 12 + 1
            item["projected_date"] = f"{year:04d}-{month:02d}"
            item["note"] = None

    return jsonify({
        "items": items,
        "monthly_savings": monthly_savings,
    })


@app.route("/api/wishlist", methods=["POST"])
def add_wishlist():
    user = require_user()
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    price = data.get("price")
    product_url = (data.get("product_url") or "").strip() or None
    platform = (data.get("platform") or "").strip() or None

    if not name:
        return jsonify({"error": "Product name is required."}), 400
    try:
        price = float(price)
        if price <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Price must be a positive number."}), 400

    db = get_db()
    cur = db.execute(
        "INSERT INTO wishlist (name, price, created_at, product_url, platform, user_id) VALUES (?, ?, ?, ?, ?, ?)",
        (name, price, datetime.utcnow().isoformat(), product_url, platform, user["id"]),
    )
    item_id = cur.lastrowid
    db.execute(
        "INSERT INTO wishlist_price_history (wishlist_id, price, checked_at) VALUES (?, ?, ?)",
        (item_id, price, datetime.utcnow().isoformat()),
    )
    db.commit()
    new_row = db.execute("SELECT * FROM wishlist WHERE id = ? AND user_id = ?", (item_id, user["id"])).fetchone()
    return jsonify(dict(new_row)), 201


def _detect_platform(url):
    host = urlparse(url).netloc.lower().replace("www.", "")
    if "amazon." in host or host.startswith("amzn."):
        return "Amazon"
    if "flipkart.com" in host:
        return "Flipkart"
    return None


def _parse_price(value):
    if value is None:
        return None
    match = re.search(r"(?:₹|INR|Rs\.?\s*)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", str(value), re.I)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _extract_product_from_page(html):
    soup = BeautifulSoup(html, "html.parser")
    title = None
    price = None

    for selector in ["meta[property='og:title']", "meta[name='twitter:title']"]:
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            title = tag["content"].strip()
            break
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)

    price_selectors = [
        "meta[property='product:price:amount']",
        "meta[itemprop='price']",
        "meta[name='twitter:data1']",
        "[itemprop='price']",
    ]
    for selector in price_selectors:
        tag = soup.select_one(selector)
        if tag:
            price = _parse_price(tag.get("content") or tag.get("value") or tag.get_text(" ", strip=True))
            if price:
                break

    if price is None:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or script.get_text())
                candidates = data if isinstance(data, list) else [data]
                for obj in candidates:
                    if not isinstance(obj, dict):
                        continue
                    offers = obj.get("offers") or {}
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    candidate = offers.get("price") if isinstance(offers, dict) else None
                    price = _parse_price(candidate)
                    if price:
                        title = title or obj.get("name")
                        break
                if price:
                    break
            except Exception:
                continue

    return title, price


def _fetch_product(url):
    platform = _detect_platform(url)
    if not platform:
        raise ValueError("Only Amazon and Flipkart product links are supported.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Please provide a valid product URL.")

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept-Language": "en-IN,en;q=0.9",
    }
    response = requests.get(url, headers=headers, timeout=12)
    response.raise_for_status()
    title, price = _extract_product_from_page(response.text)
    if not price:
        raise ValueError(f"{platform} did not expose a readable current price. Try opening the product page and pasting the full link again.")
    return {"name": title or "Untitled product", "price": price, "platform": platform}


@app.route("/api/wishlist/import", methods=["POST"])
def import_wishlist_product():
    user = require_user()
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Product URL is required."}), 400
    try:
        product = _fetch_product(url)
    except requests.RequestException:
        # Store pages often block automated requests. Do not fail the wishlist
        # flow: return a graceful confirmation step so tracking can start with
        # a user-supplied current price.
        platform = _detect_platform(url)
        return jsonify({
            "needs_price_confirmation": True,
            "platform": platform,
            "name": "Product from " + (platform or "store"),
            "message": "We could not read the live price automatically. Enter the current price to start tracking it."
        }), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM wishlist WHERE product_url = ? AND user_id = ?", (url, user["id"])).fetchone()
    if existing:
        item_id = existing["id"]
        db.execute("UPDATE wishlist SET name = ?, price = ?, platform = ? WHERE id = ? AND user_id = ?", (product["name"], product["price"], product["platform"], item_id, user["id"]))
    else:
        cur = db.execute(
            "INSERT INTO wishlist (name, price, created_at, product_url, platform, user_id) VALUES (?, ?, ?, ?, ?, ?)",
            (product["name"], product["price"], datetime.utcnow().isoformat(), url, product["platform"], user["id"]),
        )
        item_id = cur.lastrowid
    db.execute("INSERT INTO wishlist_price_history (wishlist_id, price, checked_at) VALUES (?, ?, ?)", (item_id, product["price"], datetime.utcnow().isoformat()))
    db.commit()
    row = db.execute("SELECT * FROM wishlist WHERE id = ? AND user_id = ?", (item_id, user["id"])).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/wishlist/<int:item_id>/history", methods=["GET"])
def wishlist_price_history(item_id):
    user = require_user()
    db = get_db()
    item = db.execute("SELECT * FROM wishlist WHERE id = ? AND user_id = ?", (item_id, user["id"])).fetchone()
    if not item:
        return jsonify({"error": "Wishlist item not found."}), 404
    rows = db.execute("SELECT price, checked_at FROM wishlist_price_history WHERE wishlist_id = ? ORDER BY checked_at ASC", (item_id,)).fetchall()
    return jsonify({"item": dict(item), "history": [dict(r) for r in rows]})


@app.route("/api/wishlist/<int:item_id>/refresh", methods=["POST"])
def refresh_wishlist_price(item_id):
    user = require_user()
    db = get_db()
    item = db.execute("SELECT * FROM wishlist WHERE id = ? AND user_id = ?", (item_id, user["id"])).fetchone()
    if not item:
        return jsonify({"error": "Wishlist item not found."}), 404
    if not item["product_url"]:
        return jsonify({"error": "This wishlist item has no store URL."}), 400
    try:
        product = _fetch_product(item["product_url"])
    except requests.RequestException:
        return jsonify({
            "needs_price_confirmation": True,
            "platform": item["platform"],
            "name": item["name"],
            "message": "The store blocked the live price check. Enter the current price to add a new history point."
        }), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    now = datetime.utcnow().isoformat()
    db.execute("UPDATE wishlist SET name = ?, price = ?, platform = ? WHERE id = ? AND user_id = ?", (product["name"], product["price"], product["platform"], item_id, user["id"]))
    db.execute("INSERT INTO wishlist_price_history (wishlist_id, price, checked_at) VALUES (?, ?, ?)", (item_id, product["price"], now))
    db.commit()
    return jsonify({"name": product["name"], "price": product["price"], "platform": product["platform"]})


@app.route("/api/wishlist/<int:item_id>/manual-price", methods=["POST"])
def manual_wishlist_price(item_id):
    user = require_user()
    data = request.get_json(force=True)
    try:
        price = float(data.get("price"))
        if price <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Price must be a positive number."}), 400
    db = get_db()
    item = db.execute("SELECT * FROM wishlist WHERE id = ? AND user_id = ?", (item_id, user["id"])).fetchone()
    if not item:
        return jsonify({"error": "Wishlist item not found."}), 404
    now = datetime.utcnow().isoformat()
    db.execute("UPDATE wishlist SET price = ? WHERE id = ? AND user_id = ?", (price, item_id, user["id"]))
    db.execute("INSERT INTO wishlist_price_history (wishlist_id, price, checked_at) VALUES (?, ?, ?)", (item_id, price, now))
    db.commit()
    return jsonify({"id": item_id, "price": price, "checked_at": now})


@app.route("/api/wishlist/<int:item_id>", methods=["DELETE"])
def delete_wishlist(item_id):
    user = require_user()
    db = get_db()
    cur = db.execute("DELETE FROM wishlist WHERE id = ? AND user_id = ?", (item_id, user["id"]))
    if cur.rowcount == 0:
        return jsonify({"error": "Wishlist item not found."}), 404
    db.commit()
    return jsonify({"deleted": item_id})


# --------------------------------------------------------------------------- #
# Routes — API: flight fare tracking
# --------------------------------------------------------------------------- #
# Fare lookups go through flights.py (SerpApi's Google Flights engine). See
# that file for provider notes and how to set SERPAPI_API_KEY.

@app.route("/api/flights/search", methods=["POST"])
def search_flights():
    user = require_user()
    """
    Preview lookup used while filling out the tracker form — returns every
    fare SerpApi found (airline + price, sorted cheapest first) without
    saving anything to the database. Distinct from add_flight()/check_flight_fare(),
    which persist a tracker and its price history.
    """
    data = request.get_json(force=True)
    origin = (data.get("origin") or "").strip().upper()
    destination = (data.get("destination") or "").strip().upper()
    departure_date = (data.get("departure_date") or "").strip()
    return_date = (data.get("return_date") or "").strip() or None
    travel_class = (data.get("travel_class") or "ECONOMY").strip().upper()

    if len(origin) != 3 or not origin.isalpha():
        return jsonify({"error": "Origin must be a 3-letter airport code."}), 400
    if len(destination) != 3 or not destination.isalpha():
        return jsonify({"error": "Destination must be a 3-letter airport code."}), 400
    if not departure_date:
        return jsonify({"error": "Departure date is required."}), 400

    try:
        adults = int(data.get("adults") or 1)
        if adults < 1:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Travellers must be a positive whole number."}), 400

    try:
        fares = flights.fetch_all_fares(
            origin, destination, departure_date, return_date, adults, travel_class
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException:
        return jsonify({"error": "Could not reach the flight pricing provider. Try again shortly."}), 502

    return jsonify({"fares": fares})


@app.route("/api/flights", methods=["GET"])
def list_flights():
    user = require_user()
    db = get_db()
    rows = db.execute("SELECT * FROM flights WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/flights", methods=["POST"])
def add_flight():
    user = require_user()
    data = request.get_json(force=True)
    origin = (data.get("origin") or "").strip().upper()
    destination = (data.get("destination") or "").strip().upper()
    departure_date = (data.get("departure_date") or "").strip()
    return_date = (data.get("return_date") or "").strip() or None
    travel_class = (data.get("travel_class") or "ECONOMY").strip().upper()
    notify_email = (data.get("notify_email") or "").strip() or None
    telegram_chat_id = data.get("telegram_chat_id")
    if telegram_chat_id not in (None, ""):
        try:
            telegram_chat_id = int(telegram_chat_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Telegram chat id must be a valid integer."}), 400
    else:
        telegram_chat_id = None
    notify_telegram = bool(data.get("notify_telegram", True))

    if len(origin) != 3 or not origin.isalpha():
        return jsonify({"error": "Origin must be a 3-letter airport code."}), 400
    if len(destination) != 3 or not destination.isalpha():
        return jsonify({"error": "Destination must be a 3-letter airport code."}), 400
    if not departure_date:
        return jsonify({"error": "Departure date is required."}), 400

    try:
        adults = int(data.get("adults") or 1)
        if adults < 1:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Travellers must be a positive whole number."}), 400

    target_price = data.get("target_price")
    if target_price not in (None, ""):
        try:
            target_price = float(target_price)
        except (TypeError, ValueError):
            return jsonify({"error": "Alert target must be a number."}), 400
    else:
        target_price = None

    db = get_db()
    now = datetime.utcnow().isoformat()
    cur = db.execute(
        """INSERT INTO flights
               (origin, destination, departure_date, return_date, adults, travel_class,
                target_price, notify_email, telegram_chat_id, notify_telegram, current_price, lowest_price, active, created_at, user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1, ?, ?)""",
        (origin, destination, departure_date, return_date, adults, travel_class,
         target_price, notify_email, telegram_chat_id, notify_telegram, now, user["id"]),
    )
    flight_id = cur.lastrowid
    db.commit()

    # Try an initial fare check, but don't fail tracker creation if the
    # provider call fails — surface it as a non-fatal warning instead, same
    # pattern as the wishlist import flow above.
    warning = None
    try:
        price = flights.fetch_cheapest_fare(
            origin, destination, departure_date, return_date, adults, travel_class
        )
        checked_at = datetime.utcnow().isoformat()
        db.execute(
            "UPDATE flights SET current_price = ?, lowest_price = ? WHERE id = ? AND user_id = ?",
            (price, price, flight_id, user["id"]),
        )
        db.execute(
            "INSERT INTO flight_price_history (flight_id, price, checked_at) VALUES (?, ?, ?)",
            (flight_id, price, checked_at),
        )
        db.commit()
    except ValueError as exc:
        warning = str(exc)
    except requests.RequestException:
        warning = "Could not reach the flight pricing provider. You can check the fare manually later."

    row = dict(db.execute("SELECT * FROM flights WHERE id = ? AND user_id = ?", (flight_id, user["id"])).fetchone())
    if warning:
        row["warning"] = warning
    return jsonify(row), 201


@app.route("/api/flights/check-by-details", methods=["POST"])
def check_flight_fare_by_details():
    user = require_user()
    """Resolve a saved tracker by route/date details, keeping its internal id hidden from Sara's user-facing flow."""
    data = request.get_json(force=True)
    origin = (data.get("origin") or "").strip().upper()
    destination = (data.get("destination") or "").strip().upper()
    departure_date = (data.get("departure_date") or "").strip()
    return_date = (data.get("return_date") or "").strip() or None

    if len(origin) != 3 or not origin.isalpha():
        return jsonify({"error": "Origin must be a 3-letter airport code."}), 400
    if len(destination) != 3 or not destination.isalpha():
        return jsonify({"error": "Destination must be a 3-letter airport code."}), 400
    if not departure_date:
        return jsonify({"error": "Departure date is required."}), 400

    db = get_db()
    if return_date:
        row = db.execute(
            "SELECT * FROM flights WHERE origin = ? AND destination = ? "
            "AND departure_date = ? AND (return_date = ? OR (return_date IS NULL AND ? = '')) "
            "AND active = 1 AND user_id = ? ORDER BY created_at DESC LIMIT 1",
            (origin, destination, departure_date, return_date, return_date, user["id"]),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT * FROM flights WHERE origin = ? AND destination = ? "
            "AND departure_date = ? AND active = 1 AND user_id = ? ORDER BY created_at DESC LIMIT 1",
            (origin, destination, departure_date, user["id"]),
        ).fetchone()

    if not row:
        return jsonify({"error": "No active flight tracker matches that route and date."}), 404

    return check_flight_fare(row["id"])


@app.route("/api/flights/<int:flight_id>/check", methods=["POST"])
def check_flight_fare(flight_id):
    user = require_user()
    db = get_db()
    flight = db.execute("SELECT * FROM flights WHERE id = ? AND user_id = ?", (flight_id, user["id"])).fetchone()
    if not flight:
        return jsonify({"error": "Flight tracker not found."}), 404

    try:
        # One SerpApi search does double duty here: fetch_all_fares() gives
        # us everything, and the cheapest entry (results are sorted
        # ascending) is what gets saved as current_price/lowest_price.
        # Previously this route called fetch_cheapest_fare() and a separate
        # "view all fares" button called fetch_all_fares() again for the
        # same route/date — two SerpApi calls for data that comes back in
        # one response. Merging them here halves that to one call.
        fares = flights.fetch_all_fares(
            flight["origin"], flight["destination"], flight["departure_date"],
            flight["return_date"], flight["adults"], flight["travel_class"],
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException:
        return jsonify({"error": "Could not reach the flight pricing provider. Try again shortly."}), 502

    price = fares[0]["price"]  # fetch_all_fares returns results sorted ascending
    lowest = flight["lowest_price"]
    lowest = price if lowest is None else min(lowest, price)
    now = datetime.utcnow().isoformat()
    db.execute(
        "UPDATE flights SET current_price = ?, lowest_price = ? WHERE id = ? AND user_id = ?",
        (price, lowest, flight_id, user["id"]),
    )
    db.execute(
        "INSERT INTO flight_price_history (flight_id, price, checked_at) VALUES (?, ?, ?)",
        (flight_id, price, now),
    )
    db.commit()
    return jsonify({"id": flight_id, "price": price, "lowest_price": lowest, "checked_at": now, "fares": fares})


@app.route("/api/flights/<int:flight_id>/history", methods=["GET"])
def flight_price_history(flight_id):
    user = require_user()
    db = get_db()
    flight = db.execute("SELECT * FROM flights WHERE id = ? AND user_id = ?", (flight_id, user["id"])).fetchone()
    if not flight:
        return jsonify({"error": "Flight tracker not found."}), 404
    rows = db.execute(
        "SELECT price, checked_at FROM flight_price_history WHERE flight_id = ? ORDER BY checked_at ASC",
        (flight_id,),
    ).fetchall()
    return jsonify({"flight": dict(flight), "history": [dict(r) for r in rows]})


@app.route("/api/flights/<int:flight_id>", methods=["DELETE"])
def delete_flight(flight_id):
    user = require_user()
    db = get_db()
    cur = db.execute("DELETE FROM flights WHERE id = ? AND user_id = ?", (flight_id, user["id"]))
    if cur.rowcount == 0:
        return jsonify({"error": "Flight tracker not found."}), 404
    db.commit()
    return jsonify({"deleted": flight_id})


def _expense_budget_snapshot(db, expense_date=None, user_id=None):
    """Return the current discretionary budget and category-spend snapshot."""
    expense_date = expense_date or date.today().isoformat()
    try:
        expense_day = datetime.strptime(expense_date, "%Y-%m-%d").date()
    except ValueError:
        expense_day = date.today()

    if not user_id:
        raise ValueError("user_id is required for budget snapshots")

    salary_row = db.execute("SELECT value FROM settings WHERE key = 'monthly_salary' AND user_id = ?", (user_id,)).fetchone()
    salary = float(salary_row["value"]) if salary_row else None
    savings_row = db.execute("SELECT value FROM settings WHERE key = 'savings_percent' AND user_id = ?", (user_id,)).fetchone()
    savings_pct = float(savings_row["value"]) if savings_row else agent.DEFAULT_SAVINGS_PERCENT

    month_prefix = f"{expense_day.year:04d}-{expense_day.month:02d}"
    purchases_total = db.execute(
        "SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE purchased_on LIKE ? AND user_id = ?",
        (f"{month_prefix}%", user_id),
    ).fetchone()["s"]
    fixed_total = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM monthly_expenses WHERE year = ? AND month = ? AND user_id = ?",
        (expense_day.year, expense_day.month, user_id),
    ).fetchone()["s"]
    day_spend_total = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM day_expenses WHERE date LIKE ? AND user_id = ?",
        (f"{month_prefix}%", user_id),
    ).fetchone()["s"]

    discretionary_spent = float(purchases_total or 0) + float(day_spend_total or 0)

    _, total_days = calendar.monthrange(expense_day.year, expense_day.month)
    days_remaining = max(1, total_days - expense_day.day + 1)

    snapshot = {
        "configured": bool(salary),
        "currency": "INR",
        "today": expense_day.isoformat(),
        "monthly_spent": round(discretionary_spent, 2),
    }

    if salary:
        savings_target = float(salary) * (savings_pct / 100.0)
        monthly_pool = max(0.0, float(salary) - float(fixed_total or 0) - savings_target)
        monthly_left = round(monthly_pool - discretionary_spent, 2)

        today_purchases = db.execute(
            "SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE purchased_on = ? AND user_id = ?",
            (expense_day.isoformat(), user_id),
        ).fetchone()["s"]
        today_day_expenses = db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM day_expenses WHERE date = ? AND user_id = ?",
            (expense_day.isoformat(), user_id),
        ).fetchone()["s"]
        spent_today = float(today_purchases or 0) + float(today_day_expenses or 0)

        daily_budget = round(max(0.0, monthly_left) / days_remaining, 2)
        snapshot.update({
            "daily_safe_budget": daily_budget,
            "spent_today": round(spent_today, 2),
            "today_left": round(daily_budget - spent_today, 2),
            "monthly_left": monthly_left,
            "days_remaining": days_remaining,
        })

    categories = db.execute(
        "SELECT COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized') AS category, "
        "COALESCE(SUM(amount), 0) AS amount "
        "FROM day_expenses WHERE date LIKE ? AND user_id = ? "
        "GROUP BY COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized') "
        "ORDER BY amount DESC",
        (f"{month_prefix}%", user_id),
    ).fetchall()
    category_totals = [
        {"category": row["category"], "amount": round(float(row["amount"] or 0), 2)}
        for row in categories
    ]
    snapshot["top_category"] = category_totals[0] if category_totals else None
    snapshot["category_breakdown"] = category_totals[:5]
    return snapshot


# --------------------------------------------------------------------------- #
# Routes — API: day-wise expenses (manual entry + Telegram sync)
# --------------------------------------------------------------------------- #

@app.route("/api/day-expenses", methods=["POST"])
def save_day_expenses():
    user = require_user()
    """
    Body: { "items": [{"date": "2026-08-14", "merchant": "Blinkit", "category": "Food", "amount": 342, "source": "vision"}, ...] }
    Saves the (possibly user-edited) parsed screenshot items. Items without
    a valid YYYY-MM-DD date are skipped rather than guessed at, since a
    missing/unresolved date from OCR/vision is different from "this
    happened today" — the frontend should have the user fill it in first.
    """
    data = request.get_json(force=True)
    items = data.get("items") or []
    if not items:
        return jsonify({"error": "No expense items to save."}), 400

    db = get_db()
    saved = []
    skipped = 0
    for item in items:
        date_str = str(item.get("date") or "").strip()
        merchant = str(item.get("merchant") or "").strip()
        category = str(item.get("category") or "Uncategorized").strip() or "Uncategorized"
        source = str(item.get("source") or "manual").strip()

        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            skipped += 1
            continue
        try:
            amount = float(item.get("amount"))
            if amount <= 0:
                raise ValueError
        except (TypeError, ValueError):
            skipped += 1
            continue

        cur = db.execute(
            "INSERT INTO day_expenses (date, merchant, category, amount, source, created_at, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date_str, merchant, category, amount, source, datetime.utcnow().isoformat(), user["id"]),
        )
        row = db.execute("SELECT * FROM day_expenses WHERE id = ? AND user_id = ?", (cur.lastrowid, user["id"])).fetchone()
        saved.append(dict(row))
    db.commit()

    if not saved:
        return jsonify({"error": "No valid items with a resolved date (YYYY-MM-DD) and amount were provided."}), 400

    snapshot = _expense_budget_snapshot(db, saved[-1]["date"], user["id"])
    return jsonify({"items": saved, "skipped": skipped, "budget": snapshot}), 201


@app.route("/api/day-expenses/categories", methods=["GET"])
def day_expense_categories():
    user = require_user()
    year = request.args.get("year", type=int, default=date.today().year)
    month = request.args.get("month", type=int, default=date.today().month)
    month_prefix = f"{year:04d}-{month:02d}"

    db = get_db()
    rows = db.execute(
        "SELECT category, COALESCE(SUM(amount), 0) AS amount "
        "FROM day_expenses WHERE date LIKE ? AND user_id = ? "
        "GROUP BY category ORDER BY amount DESC",
        (f"{month_prefix}%", user["id"]),
    ).fetchall()
    totals = {row["category"]: round(float(row["amount"] or 0), 2) for row in rows}
    categories = getattr(agent, "DAY_EXPENSE_CATEGORIES", ["Other"])
    return jsonify({
        "year": year,
        "month": month,
        "categories": [{"category": cat, "amount": totals.get(cat, 0)} for cat in categories],
        "top_category": next(
            ({"category": cat, "amount": totals[cat]} for cat in categories if totals.get(cat, 0) > 0),
            None,
        ),
    })


@app.route("/api/day-expenses", methods=["GET"])
def list_day_expenses():
    user = require_user()
    year = request.args.get("year", type=int, default=date.today().year)
    month = request.args.get("month", type=int, default=date.today().month)
    month_prefix = f"{year:04d}-{month:02d}"

    db = get_db()
    rows = db.execute(
        "SELECT * FROM day_expenses WHERE date LIKE ? AND user_id = ? ORDER BY date DESC, id DESC",
        (f"{month_prefix}%", user["id"]),
    ).fetchall()
    items = [dict(r) for r in rows]

    totals_by_day = {}
    for item in items:
        totals_by_day[item["date"]] = round(totals_by_day.get(item["date"], 0) + item["amount"], 2)
    # Oldest first for charting.
    day_totals = [{"date": d, "total": t} for d, t in sorted(totals_by_day.items())]

    return jsonify({"items": items, "day_totals": day_totals})


@app.route("/api/day-expenses/<int:item_id>", methods=["DELETE"])
def delete_day_expense(item_id):
    user = require_user()
    db = get_db()
    cur = db.execute("DELETE FROM day_expenses WHERE id = ? AND user_id = ?", (item_id, user["id"]))
    if cur.rowcount == 0:
        return jsonify({"error": "Day expense not found."}), 404
    db.commit()
    return jsonify({"deleted": item_id})


@app.route("/api/telegram/sync", methods=["POST"])
def sync_telegram_expenses():
    """
    Pull every unprocessed message out of the Supabase inbox (written by
    the Cloudflare Worker the moment a Telegram message arrives — see
    telegram_sync.py for the full picture), parse each with
    agent.parse_day_expense(), and insert the results into day_expenses.

    "today"/"yesterday" in a message resolve against sent_at (when the
    message was actually sent), not against whenever this sync happens to
    run — those can be days apart, since this only runs when Ledger is
    launched.
    """
    user = require_user()
    db = get_db()
    try:
        pending = telegram_sync.fetch_pending(db, user["id"])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:  # noqa: BLE001 - covers psycopg2 connection/network failures too
        return jsonify({"error": "Could not reach the Telegram inbox database. Try again shortly."}), 502

    if not pending:
        return jsonify({"items": [], "skipped": 0, "synced": 0})

    saved = []
    skipped = 0
    processed_ids = []

    for row in pending:
        parsed = agent.parse_day_expense(row.get("raw_text") or "", reference_date=row.get("sent_at"))
        processed_ids.append(row["id"])  # mark as handled either way, so a bad message doesn't jam the queue forever
        if not parsed:
            skipped += 1
            continue

        cur = db.execute(
            "INSERT INTO day_expenses (date, merchant, category, amount, source, created_at, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (parsed["date"], parsed.get("merchant") or "", parsed["category"],
             parsed["amount"], "telegram", datetime.utcnow().isoformat(), user["id"]),
        )
        saved_row = db.execute("SELECT * FROM day_expenses WHERE id = ? AND user_id = ?", (cur.lastrowid, user["id"])).fetchone()
        saved.append(dict(saved_row))

    db.commit()

    budget = _expense_budget_snapshot(db, saved[-1]["date"], user["id"]) if saved else _expense_budget_snapshot(db, user_id=user["id"])

    try:
        telegram_sync.mark_processed(db, processed_ids)
    except Exception:  # noqa: BLE001 - covers psycopg2 failures too
        # The local inserts above already succeeded and are committed, so
        # nothing's lost — but if this fails, the same messages will be
        # re-fetched (and re-inserted as duplicates) on the next sync.
        # Surface it rather than pretending everything's clean.
        return jsonify({
            "items": saved, "skipped": skipped, "synced": len(saved), "budget": budget,
            "warning": "Saved locally, but couldn't mark messages as processed in the inbox — "
                       "they may be synced again next time.",
        })

    return jsonify({"items": saved, "skipped": skipped, "synced": len(saved), "budget": budget})


# --------------------------------------------------------------------------- #
# Routes — API: monthly expenses (natural language)
# --------------------------------------------------------------------------- #

@app.route("/api/monthly-expenses/parse", methods=["POST"])
def parse_monthly_expenses():
    """Preview-parse text without saving, so the UI can show a confirm step."""
    require_user()
    data = request.get_json(force=True)
    text = data.get("text", "")
    parsed, source = agent.parse_expense(text)
    return jsonify({"parsed": parsed, "source": source})


@app.route("/api/status", methods=["GET"])
def status():
    """Lets the frontend show whether Groq parsing is actually configured."""
    return jsonify({"groq_configured": agent.is_llm_configured()})


@app.route("/api/monthly-expenses", methods=["POST"])
def save_monthly_expenses():
    user = require_user()
    """
    Body: { "text": "...", "year": 2026, "month": 8, "items": [optional edited list] }
    If "items" is provided (user edited the parsed preview), use it directly;
    otherwise parse "text" server-side.
    """
    data = request.get_json(force=True)
    text = data.get("text", "")
    # This endpoint is exclusively for recurring/fixed monthly expenses.
    # One-off spending must use /api/day-expenses instead.
    if data.get("expense_type") == "daily":
        return jsonify({"error": "Daily expenses must be saved through the day-wise expense flow."}), 400
    year = int(data.get("year") or date.today().year)
    month = int(data.get("month") or date.today().month)
    items = data.get("items")

    if items:
        parsed, source = items, "manual"
    else:
        parsed, source = agent.parse_expense(text)

    if not parsed:
        return jsonify({"error": "Could not find any '<amount> on <category>' patterns."}), 400

    db = get_db()
    # Replace existing entries for that month so re-submitting doesn't duplicate.
    db.execute("DELETE FROM monthly_expenses WHERE year = ? AND month = ? AND user_id = ?", (year, month, user["id"]))
    for item in parsed:
        db.execute(
            "INSERT INTO monthly_expenses (year, month, category, amount, raw_text, created_at, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (year, month, item["category"], float(item["amount"]), text, datetime.utcnow().isoformat(), user["id"]),
        )
    db.commit()

    rows = db.execute(
        "SELECT * FROM monthly_expenses WHERE year = ? AND month = ? AND user_id = ? ORDER BY amount DESC",
        (year, month, user["id"]),
    ).fetchall()
    return jsonify({"items": [dict(r) for r in rows], "source": source}), 201


@app.route("/api/monthly-expenses", methods=["GET"])
def get_monthly_expenses():
    user = require_user()
    year = request.args.get("year", type=int, default=date.today().year)
    month = request.args.get("month", type=int, default=date.today().month)
    db = get_db()
    rows = db.execute(
        "SELECT * FROM monthly_expenses WHERE year = ? AND month = ? AND user_id = ? ORDER BY amount DESC",
        (year, month, user["id"]),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# --------------------------------------------------------------------------- #
# Routes — API: dashboard summary
# --------------------------------------------------------------------------- #

@app.route("/api/summary", methods=["GET"])
def summary():
    user = require_user()
    """
    Aggregates everything the dashboard needs:
      - total spend on tracked purchases (all time)
      - this month's purchase spend
      - this month's fixed/monthly expenses total (rent, food, etc.)
      - monthly salary, and % of salary going to purchases vs fixed expenses
      - year-to-date purchase spend and % of (salary * months elapsed)
    """
    year = request.args.get("year", type=int, default=date.today().year)
    month = request.args.get("month", type=int, default=date.today().month)

    db = get_db()

    total_purchases = db.execute("SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE user_id = ?", (user["id"],)).fetchone()["s"]

    month_prefix = f"{year:04d}-{month:02d}"
    month_purchases = db.execute(
        "SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE purchased_on LIKE ? AND user_id = ?",
        (f"{month_prefix}%", user["id"]),
    ).fetchone()["s"]

    ytd_purchases = db.execute(
        "SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE purchased_on LIKE ? AND user_id = ?",
        (f"{year:04d}-%", user["id"]),
    ).fetchone()["s"]

    fixed_rows = db.execute(
        "SELECT category, amount FROM monthly_expenses WHERE year = ? AND month = ? AND user_id = ? ORDER BY amount DESC",
        (year, month, user["id"]),
    ).fetchall()
    fixed_total = sum(r["amount"] for r in fixed_rows)

    salary_row = db.execute("SELECT value FROM settings WHERE key = 'monthly_salary' AND user_id = ?", (user["id"],)).fetchone()
    salary = float(salary_row["value"]) if salary_row else None

    def pct(part, whole):
        if not whole:
            return None
        return round((part / whole) * 100, 1)

    months_elapsed = month if year == date.today().year else 12
    ytd_salary = salary * months_elapsed if salary else None

    return jsonify(
        {
            "total_purchases_all_time": round(total_purchases, 2),
            "month_purchases": round(month_purchases, 2),
            "ytd_purchases": round(ytd_purchases, 2),
            "fixed_expenses": [dict(r) for r in fixed_rows],
            "fixed_total": round(fixed_total, 2),
            "monthly_salary": salary,
            "pct_purchases_of_salary_month": pct(month_purchases, salary),
            "pct_fixed_of_salary_month": pct(fixed_total, salary),
            "pct_purchases_of_salary_ytd": pct(ytd_purchases, ytd_salary),
        }
    )
import calendar
from datetime import datetime, date

# --------------------------------------------------------------------------- #
# Routes — API: Safe-To-Spend Daily Meter
# --------------------------------------------------------------------------- #

@app.route("/api/budget/safe-to-spend", methods=["GET"])
def safe_to_spend():
    user = require_user()
    """
    Calculates remaining discretionary allowance for the month and splits
    it across remaining days (today included).
    """
    db = get_db()
    today = date.today()
    
    # 1. Fetch salary & savings target
    salary_row = db.execute("SELECT value FROM settings WHERE key = 'monthly_salary' AND user_id = ?", (user["id"],)).fetchone()
    salary = float(salary_row["value"]) if salary_row else None

    savings_row = db.execute("SELECT value FROM settings WHERE key = 'savings_percent' AND user_id = ?", (user["id"],)).fetchone()
    savings_pct = float(savings_row["value"]) if savings_row else agent.DEFAULT_SAVINGS_PERCENT

    # 2. Total days & days left in month (including today)
    _, total_days = calendar.monthrange(today.year, today.month)
    days_remaining = max(1, total_days - today.day + 1)

    # 3. Monthly expenses and purchases
    month_prefix = f"{today.year:04d}-{today.month:02d}"
    purchases_total = db.execute(
        "SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE purchased_on LIKE ? AND user_id = ?",
        (f"{month_prefix}%", user["id"]),
    ).fetchone()["s"]

    fixed_total = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM monthly_expenses WHERE year = ? AND month = ? AND user_id = ?",
        (today.year, today.month, user["id"]),
    ).fetchone()["s"]

    # Also count day_expenses (manual entries + Telegram sync) for this month
    day_spend_total = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM day_expenses WHERE date LIKE ? AND user_id = ?",
        (f"{month_prefix}%", user["id"]),
    ).fetchone()["s"]

    discretionary_spent = purchases_total + day_spend_total

    if not salary:
        return jsonify({
            "configured": False,
            "message": "Add your monthly salary in Overview to enable the Safe-to-Spend meter."
        })

    # Target savings amount
    savings_target_amount = round(salary * (savings_pct / 100.0), 2)
    
    # Total monthly allowance for discretionary spending after fixed costs & planned savings
    total_discretionary_pool = max(0.0, salary - fixed_total - savings_target_amount)
    remaining_pool = round(total_discretionary_pool - discretionary_spent, 2)
    daily_safe_budget = round(max(0.0, remaining_pool) / days_remaining, 2)

    # What's already gone out today specifically, so "today's left" reflects
    # actual spending today rather than a flat unadjusted daily average.
    today_str = today.isoformat()
    purchases_today = db.execute(
        "SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE purchased_on = ? AND user_id = ?",
        (today_str, user["id"]),
    ).fetchone()["s"]
    day_expenses_today = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM day_expenses WHERE date = ? AND user_id = ?",
        (today_str, user["id"]),
    ).fetchone()["s"]
    spent_today = round(purchases_today + day_expenses_today, 2)
    today_left = round(daily_safe_budget - spent_today, 2)  # can go negative if today's overspent — that's meaningful, not clamped

    return jsonify({
        "configured": True,
        "daily_safe_budget": daily_safe_budget,
        "spent_today": spent_today,
        "today_left": today_left,
        "remaining_pool": remaining_pool,
        "total_discretionary_pool": total_discretionary_pool,
        "discretionary_spent": discretionary_spent,
        "days_remaining": days_remaining,
        "total_days": total_days,
        "current_day": today.day,
        "savings_target_amount": savings_target_amount,
        "savings_pct": savings_pct
    })

# --------------------------------------------------------------------------- #
# Routes — API: Autonomous Agents
# --------------------------------------------------------------------------- #

@app.route("/api/agents/spend-cutter/plan", methods=["GET"])
def spend_cutter_plan():
    user = require_user()
    db = get_db()
    today = date.today()
    month_purchases, fixed_total, fixed_expenses = _month_spend(db, today.year, today.month, user["id"])
    
    salary_row = db.execute("SELECT value FROM settings WHERE key = 'monthly_salary' AND user_id = ?", (user["id"],)).fetchone()
    salary = float(salary_row["value"]) if salary_row else None
    
    savings_row = db.execute("SELECT value FROM settings WHERE key = 'savings_percent' AND user_id = ?", (user["id"],)).fetchone()
    savings_pct = float(savings_row["value"]) if savings_row else agent.DEFAULT_SAVINGS_PERCENT
    
    current_savings = max(0.0, (salary - fixed_total - month_purchases)) if salary else 0.0

    context = {
        "monthly_salary": salary,
        "savings_target_percent": savings_pct,
        "fixed_expenses": fixed_expenses,
        "fixed_total": fixed_total,
        "month_purchases": month_purchases,
        "current_savings": current_savings
    }
    
    plan, source = agent.generate_spend_cut_plan(context)
    return jsonify({**plan, "source": source})


@app.route("/api/agents/spend-cutter/apply", methods=["POST"])
def spend_cutter_apply():
    user = require_user()
    """Applies the agent's rebalanced fixed expense targets into monthly_expenses."""
    data = request.get_json(force=True)
    rebalanced = data.get("rebalanced_fixed_expenses") or []
    if not rebalanced:
        return jsonify({"error": "No rebalancing items supplied."}), 400

    db = get_db()
    today = date.today()

    for item in rebalanced:
        cat = item.get("category")
        new_amt = float(item.get("proposed_amount", 0))
        if cat and new_amt > 0:
            db.execute(
                "UPDATE monthly_expenses SET amount = ? WHERE year = ? AND month = ? AND LOWER(category) = LOWER(?) AND user_id = ?",
                (new_amt, today.year, today.month, cat, user["id"])
            )
    db.commit()
    return jsonify({"status": "applied", "updated_count": len(rebalanced)}), 200


@app.route("/api/agents/deal-evaluator", methods=["POST"])
def evaluate_wishlist_deal():
    user = require_user()
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    price = float(data.get("price") or 0)
    
    if not name or price <= 0:
        return jsonify({"error": "Valid item name and price required."}), 400

    db = get_db()
    today = date.today()
    month_purchases, fixed_total, _ = _month_spend(db, today.year, today.month, user["id"])
    salary_row = db.execute("SELECT value FROM settings WHERE key = 'monthly_salary' AND user_id = ?", (user["id"],)).fetchone()
    salary = float(salary_row["value"]) if salary_row else 0
    monthly_savings = max(0.0, salary - fixed_total - month_purchases)

    deal_info, source = agent.evaluate_deal(name, price, monthly_savings)
    return jsonify({**deal_info, "source": source})

# --------------------------------------------------------------------------- #
# Routes — API: Natural Language Financial Chatbot
# --------------------------------------------------------------------------- #

def _scope_financial_sql(sql, user_id):
    """Restrict chatbot SELECTs to the authenticated user's financial rows."""
    clean = (sql or "").strip()
    if not clean or ";" in clean or "--" in clean or "/*" in clean:
        return None

    lowered = clean.lower()
    if not lowered.startswith("select"):
        return None

    # Keep the generated query simple enough that the ownership barrier can
    # be applied deterministically. Joins/subqueries are rejected rather than
    # risking a query that escapes the per-user predicate.
    if any(token in lowered for token in (" join ", " union ", " intersect ", " except ")):
        return None
    if len(re.findall(r"\bselect\b", lowered)) != 1:
        return None

    tables = ("purchases", "monthly_expenses", "day_expenses", "wishlist", "settings", "flights")
    matched = [table for table in tables if re.search(rf"\bfrom\s+{table}\b", lowered)]
    if not matched:
        return None

    # Never trust a user_id predicate generated by the model. Reject queries
    # containing one and inject the authenticated ID ourselves.
    if re.search(r"\buser_id\b", lowered):
        return None

    if re.search(r"\bwhere\b", lowered):
        clean = re.sub(r"\bwhere\b", f"WHERE user_id = '{user_id}' AND ", clean, count=1, flags=re.IGNORECASE)
    else:
        # Insert before GROUP/ORDER/LIMIT/OFFSET when present.
        boundary = re.search(r"\b(group\s+by|order\s+by|limit|offset)\b", clean, flags=re.IGNORECASE)
        if boundary:
            clean = clean[:boundary.start()] + f"WHERE user_id = '{user_id}' " + clean[boundary.start():]
        else:
            clean = clean.rstrip() + f" WHERE user_id = '{user_id}'"

    return clean


@app.route("/api/chat", methods=["POST"])
def financial_chat():
    user = require_user()
    data = request.get_json(force=True)
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Empty message."}), 400

    parsed = agent.generate_chat_sql(message)
    
    if not parsed:
        # Fallback simple search across purchases and expenses
        db = get_db()
        rows = db.execute("SELECT * FROM purchases WHERE user_id = ? ORDER BY purchased_on DESC LIMIT 5", (user["id"],)).fetchall()
        return jsonify({
            "reply": "I couldn't run a deep SQL search, but here are your 5 most recent purchases.",
            "data": [dict(r) for r in rows]
        })

    if parsed.get("direct_reply"):
        return jsonify({"reply": parsed["direct_reply"], "sql": None, "data": []})

    sql = parsed.get("sql")
    sql = _scope_financial_sql(sql, user["id"])
    # Security barrier: enforce read-only SELECT statements
    clean_sql = sql.strip().lower() if sql else ""
    if not clean_sql.startswith("select") or any(bad in clean_sql for bad in ["insert", "update", "delete", "drop", "alter", "attach", "exec"]):
        return jsonify({"reply": "I can only run read-only analytical queries over your financial records.", "sql": None, "data": []}), 400

    db = get_db()
    try:
        rows = db.execute(sql).fetchall()
        results = [dict(r) for r in rows]
        reply_text = agent.answer_financial_query(message, results, sql)
        return jsonify({"reply": reply_text, "sql": sql, "data": results})
    except Exception as e:
        return jsonify({"reply": f"Encountered an issue running query: {str(e)}", "sql": sql, "data": []}), 500

@app.route("/api/sara/financial-context", methods=["GET"])
def sara_financial_context():
    """Return a compact, user-scoped financial snapshot for Sara."""
    user = require_user()
    db = get_db()
    today = date.today()

    salary_row = db.execute(
        "SELECT value FROM settings WHERE key = 'monthly_salary' AND user_id = ?",
        (user["id"],),
    ).fetchone()
    savings_row = db.execute(
        "SELECT value FROM settings WHERE key = 'savings_percent' AND user_id = ?",
        (user["id"],),
    ).fetchone()

    salary = float(salary_row["value"]) if salary_row else None
    savings_percent = (
        float(savings_row["value"])
        if savings_row
        else agent.DEFAULT_SAVINGS_PERCENT
    )

    month_purchases, fixed_total, fixed_expenses = _month_spend(
        db, today.year, today.month, user["id"]
    )
    budget = _expense_budget_snapshot(db, today.isoformat(), user["id"])

    wishlist = db.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(price), 0) AS total "
        "FROM wishlist WHERE user_id = ?",
        (user["id"],),
    ).fetchone()
    flights_count = db.execute(
        "SELECT COUNT(*) AS count FROM flights WHERE user_id = ? AND active = 1",
        (user["id"],),
    ).fetchone()

    # Deterministic health signals for Sara. These are computed from the
    # authenticated user's data so the LLM reasons over facts rather than
    # inventing budget numbers.
    discretionary_spending = float(budget.get("monthly_spent") or 0)
    monthly_left = budget.get("monthly_left")
    savings_target_amount = (
        round(float(salary) * savings_percent / 100.0, 2) if salary else None
    )
    total_discretionary_pool = (
        round(discretionary_spending + float(monthly_left), 2)
        if monthly_left is not None else None
    )
    spending_utilization = (
        round((discretionary_spending / total_discretionary_pool) * 100, 1)
        if total_discretionary_pool and total_discretionary_pool > 0 else None
    )
    day_of_month = today.day
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    projected_monthly_spending = (
        round(discretionary_spending / day_of_month * days_in_month, 2)
        if discretionary_spending > 0 and day_of_month > 0 else 0
    )
    budget_status = None
    if monthly_left is not None:
        if monthly_left < 0:
            budget_status = "over_budget"
        elif spending_utilization is not None and spending_utilization >= 80:
            budget_status = "at_risk"
        else:
            budget_status = "on_track"

    return jsonify({
        "today": today.isoformat(),
        "currency": "INR",
        "monthly_salary": salary,
        "savings_percent": savings_percent,
        "month": {
            "purchases": round(float(month_purchases or 0), 2),
            "fixed_expenses": round(float(fixed_total or 0), 2),
            "fixed_expense_items": fixed_expenses,
            "discretionary_spending": round(float(budget.get("monthly_spent") or 0), 2),
        },
        "budget": budget,
        "financial_health": {
            "budget_status": budget_status,
            "savings_target_amount": savings_target_amount,
            "total_discretionary_pool": total_discretionary_pool,
            "spending_utilization_percent": spending_utilization,
            "projected_monthly_discretionary_spending": projected_monthly_spending,
        },
        "wishlist": {
            "count": int(wishlist["count"] or 0),
            "total_value": round(float(wishlist["total"] or 0), 2),
        },
        "active_flight_trackers": int(flights_count["count"] or 0),
    })


@app.route("/api/sara/spending-trends", methods=["GET"])
def sara_spending_trends():
    """Return deterministic, user-scoped spending trends for Sara."""
    user = require_user()
    db = get_db()
    today = date.today()

    def month_shift(year, month, delta):
        index = year * 12 + (month - 1) + delta
        return index // 12, index % 12 + 1

    def month_snapshot(year, month):
        prefix = f"{year:04d}-{month:02d}"
        purchases = db.execute(
            "SELECT COALESCE(SUM(price), 0) AS total, COUNT(*) AS count "
            "FROM purchases WHERE purchased_on LIKE ? AND user_id = ?",
            (f"{prefix}%", user["id"]),
        ).fetchone()
        daily = db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count "
            "FROM day_expenses WHERE date LIKE ? AND user_id = ?",
            (f"{prefix}%", user["id"]),
        ).fetchone()
        fixed = db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total "
            "FROM monthly_expenses WHERE year = ? AND month = ? AND user_id = ?",
            (year, month, user["id"]),
        ).fetchone()
        categories = db.execute(
            "SELECT COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized') AS category, "
            "COALESCE(SUM(amount), 0) AS amount "
            "FROM day_expenses WHERE date LIKE ? AND user_id = ? "
            "GROUP BY COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized') "
            "ORDER BY amount DESC",
            (f"{prefix}%", user["id"]),
        ).fetchall()
        discretionary = float(purchases["total"] or 0) + float(daily["total"] or 0)
        return {
            "year": year,
            "month": month,
            "purchases": round(float(purchases["total"] or 0), 2),
            "day_expenses": round(float(daily["total"] or 0), 2),
            "discretionary_spending": round(discretionary, 2),
            "fixed_expenses": round(float(fixed["total"] or 0), 2),
            "transaction_count": int(purchases["count"] or 0) + int(daily["count"] or 0),
            "categories": [
                {"category": row["category"], "amount": round(float(row["amount"] or 0), 2)}
                for row in categories
            ],
        }

    current = month_snapshot(today.year, today.month)
    previous_year, previous_month = month_shift(today.year, today.month, -1)
    previous = month_snapshot(previous_year, previous_month)

    change = round(
        current["discretionary_spending"] - previous["discretionary_spending"], 2
    )
    pct_change = (
        round((change / previous["discretionary_spending"]) * 100, 1)
        if previous["discretionary_spending"] else None
    )

    previous_categories = {
        item["category"]: item["amount"] for item in previous["categories"]
    }
    category_changes = []
    for item in current["categories"]:
        old = previous_categories.get(item["category"], 0)
        category_changes.append({
            "category": item["category"],
            "current": item["amount"],
            "previous": round(old, 2),
            "change": round(item["amount"] - old, 2),
            "pct_change": round(((item["amount"] - old) / old) * 100, 1) if old else None,
        })
    category_changes.sort(key=lambda item: item["change"], reverse=True)

    # Flag meaningful anomalies using a conservative deterministic rule:
    # current category spend is >= 50% above last month and at least ₹500 higher.
    anomalies = []
    for item in category_changes:
        if item["change"] >= 500 and item["pct_change"] is not None and item["pct_change"] >= 50:
            anomalies.append({
                **item,
                "severity": "high" if item["pct_change"] >= 100 else "medium",
                "reason": "category_spending_increased_significantly",
            })

    return jsonify({
        "current_month": current,
        "previous_month": previous,
        "month_over_month": {
            "change": change,
            "percent_change": pct_change,
            "direction": "up" if change > 0 else "down" if change < 0 else "flat",
        },
        "category_changes": category_changes[:10],
        "anomalies": anomalies[:5],
    })


@app.route("/api/sara/affordability", methods=["POST"])
def sara_affordability():
    """Deterministically assess a proposed purchase against the user's budget."""
    user = require_user()
    data = request.get_json(force=True) or {}
    try:
        price = float(data.get("price") or 0)
    except (TypeError, ValueError):
        price = 0
    name = str(data.get("name") or "purchase").strip() or "purchase"

    if price <= 0:
        return jsonify({"error": "A positive purchase price is required."}), 400

    db = get_db()
    today = date.today()
    budget = _expense_budget_snapshot(db, today.isoformat(), user["id"])

    salary_row = db.execute(
        "SELECT value FROM settings WHERE key = 'monthly_salary' AND user_id = ?",
        (user["id"],),
    ).fetchone()
    savings_row = db.execute(
        "SELECT value FROM settings WHERE key = 'savings_percent' AND user_id = ?",
        (user["id"],),
    ).fetchone()

    salary = float(salary_row["value"]) if salary_row else None
    savings_percent = float(savings_row["value"]) if savings_row else agent.DEFAULT_SAVINGS_PERCENT

    monthly_left = budget.get("monthly_left")
    today_left = budget.get("today_left")
    savings_target = round(salary * savings_percent / 100.0, 2) if salary else None

    if monthly_left is None:
        status = "insufficient_data"
        after_monthly = None
        savings_impact = None
    else:
        after_monthly = round(float(monthly_left) - price, 2)
        if after_monthly < 0:
            status = "not_affordable_from_budget"
        elif after_monthly < max(0.0, float(monthly_left) * 0.2):
            status = "affordable_but_tight"
        else:
            status = "affordable"

        savings_impact = round(price / salary * 100, 1) if salary else None

    today_impact = (
        round(float(today_left) - price, 2)
        if today_left is not None else None
    )

    return jsonify({
        "name": name,
        "price": round(price, 2),
        "status": status,
        "monthly_budget": {
            "remaining_before": monthly_left,
            "remaining_after": after_monthly,
        },
        "today_budget": {
            "remaining_before": today_left,
            "remaining_after": today_impact,
        },
        "salary": salary,
        "savings_target_percent": savings_percent,
        "savings_target_amount": savings_target,
        "purchase_as_percent_of_salary": savings_impact,
    })


@app.route("/api/sara/goals", methods=["GET", "POST"])
def sara_goals():
    """List or create user-scoped financial goals for Sara."""
    user = require_user()
    db = get_db()

    if request.method == "POST":
        data = request.get_json(force=True) or {}
        name = str(data.get("name") or "").strip()
        target_amount = float(data.get("target_amount") or 0)
        current_amount = float(data.get("current_amount") or 0)
        target_date = str(data.get("target_date") or "").strip()

        if not name or target_amount <= 0 or current_amount < 0 or not target_date:
            return jsonify({"error": "name, positive target_amount, non-negative current_amount, and target_date are required."}), 400
        try:
            date.fromisoformat(target_date)
        except ValueError:
            return jsonify({"error": "target_date must be YYYY-MM-DD."}), 400
        if current_amount > target_amount:
            return jsonify({"error": "current_amount cannot exceed target_amount."}), 400

        now = datetime.utcnow().isoformat()
        db.execute(
            "INSERT INTO financial_goals "
            "(user_id, name, target_amount, current_amount, target_date, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, name) DO UPDATE SET "
            "target_amount = excluded.target_amount, current_amount = excluded.current_amount, "
            "target_date = excluded.target_date, updated_at = excluded.updated_at",
            (user["id"], name, target_amount, current_amount, target_date, now, now),
        )
        db.commit()

    rows = db.execute(
        "SELECT id, name, target_amount, current_amount, target_date, created_at, updated_at "
        "FROM financial_goals WHERE user_id = ? ORDER BY target_date ASC, id ASC",
        (user["id"],),
    ).fetchall()

    today = date.today()
    goals = []
    for row in rows:
        target = float(row["target_amount"])
        current = float(row["current_amount"])
        target_date = date.fromisoformat(row["target_date"])
        remaining = max(0.0, target - current)
        days_left = max(0, (target_date - today).days)
        months_left = max(days_left / 30.4375, 0.0)
        required_monthly = round(remaining / months_left, 2) if months_left > 0 else (remaining if remaining > 0 else 0)
        progress = round((current / target) * 100, 1)

        goals.append({
            "id": row["id"],
            "name": row["name"],
            "target_amount": round(target, 2),
            "current_amount": round(current, 2),
            "remaining_amount": round(remaining, 2),
            "target_date": target_date.isoformat(),
            "days_left": days_left,
            "progress_percent": progress,
            "required_monthly_saving": required_monthly,
            "status": "complete" if remaining <= 0 else "overdue" if days_left == 0 else "in_progress",
        })

    return jsonify({"goals": goals})


@app.route("/api/sara/goal-plan", methods=["GET"])
def sara_goal_plan():
    """Compare active savings goals with the user's actual budget capacity."""
    user = require_user()
    db = get_db()
    today = date.today()
    budget = _expense_budget_snapshot(db, today.isoformat(), user["id"])

    salary_row = db.execute(
        "SELECT value FROM settings WHERE key = 'monthly_salary' AND user_id = ?",
        (user["id"],),
    ).fetchone()
    savings_row = db.execute(
        "SELECT value FROM settings WHERE key = 'savings_percent' AND user_id = ?",
        (user["id"],),
    ).fetchone()

    salary = float(salary_row["value"]) if salary_row else None
    savings_percent = float(savings_row["value"]) if savings_row else agent.DEFAULT_SAVINGS_PERCENT
    planned_savings = round(salary * savings_percent / 100.0, 2) if salary else None

    rows = db.execute(
        "SELECT id, name, target_amount, current_amount, target_date "
        "FROM financial_goals WHERE user_id = ? ORDER BY target_date ASC, id ASC",
        (user["id"],),
    ).fetchall()

    goals = []
    for row in rows:
        target = float(row["target_amount"])
        current = float(row["current_amount"])
        target_date = date.fromisoformat(row["target_date"])
        remaining = max(0.0, target - current)
        days_left = (target_date - today).days
        months_left = max(days_left / 30.4375, 0.0)
        required = round(remaining / months_left, 2) if months_left > 0 else remaining

        # The user's current remaining monthly discretionary budget is the
        # safest available capacity signal; it does not assume unrecorded income.
        budget_left = budget.get("monthly_left")
        capacity = max(0.0, float(budget_left)) if budget_left is not None else None
        gap = round(required - capacity, 2) if capacity is not None else None

        if remaining <= 0:
            status = "complete"
        elif days_left < 0:
            status = "overdue"
        elif gap is None:
            status = "insufficient_data"
        elif gap <= 0:
            status = "on_track"
        elif capacity > 0 and gap <= capacity * 0.25:
            status = "at_risk"
        else:
            status = "behind"

        goals.append({
            "id": row["id"],
            "name": row["name"],
            "remaining_amount": round(remaining, 2),
            "target_date": target_date.isoformat(),
            "days_left": max(0, days_left),
            "required_monthly_saving": required,
            "available_monthly_capacity": round(capacity, 2) if capacity is not None else None,
            "monthly_gap": gap,
            "status": status,
        })

    return jsonify({
        "today": today.isoformat(),
        "monthly_salary": salary,
        "planned_savings_percent": savings_percent,
        "planned_savings_amount": planned_savings,
        "budget_monthly_left": budget.get("monthly_left"),
        "goals": goals,
    })


# --------------------------------------------------------------------------- #
# Routes — API: Sara (supervisor agent)
# --------------------------------------------------------------------------- #
# Sara doesn't contain any business logic of her own — she calls the routes
# above internally via app.test_client(), so every page's existing
# validation and provider fallbacks are reused as-is. See sara.py for the
# tool schema and orchestration loop.

@app.route("/api/sara/chat", methods=["POST"])
def sara_chat():
    data = request.get_json(force=True)
    message = (data.get("message") or "").strip()
    history = data.get("history") or []
    if not message:
        return jsonify({"error": "Empty message."}), 400

    result = sara.run_sara(message, app.test_client(), history, access_token=auth.get_bearer_token(request))
    return jsonify(result)


@app.route("/api/telegram/link", methods=["POST"])
def telegram_link():
    user = require_user()
    data = request.get_json(force=True)
    chat_id = data.get("chat_id")
    if chat_id is None:
        return jsonify({"error": "chat_id is required."}), 400
    db = get_db()
    db.execute(
        "INSERT INTO telegram_user_links (chat_id, user_id) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET user_id = excluded.user_id",
        (int(chat_id), user["id"]),
    )
    db.commit()
    return jsonify({"linked": True}), 200


@app.route("/api/telegram/sara", methods=["POST"])
def telegram_sara():
    """
    Telegram-facing Sara endpoint.

    The Cloudflare Worker authenticates itself with TELEGRAM_SARA_SECRET,
    passes the Telegram chat id + message, and Sara runs with persistent
    per-chat history stored in Postgres. This keeps Telegram as just another
    interface to the same Sara supervisor used by the web app.
    """
    expected_secret = os.environ.get("TELEGRAM_SARA_SECRET")
    if not expected_secret or request.headers.get("X-Telegram-Sara-Secret") != expected_secret:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True)
    message = (data.get("message") or "").strip()
    chat_id = data.get("chat_id")
    telegram_message_id = data.get("telegram_message_id")

    if not message or chat_id is None:
        return jsonify({"error": "message and chat_id are required."}), 400

    db = get_db()
    chat_id = int(chat_id)
    link = db.execute("SELECT user_id FROM telegram_user_links WHERE chat_id = ?", (chat_id,)).fetchone()
    if not link:
        return jsonify({"error": "This Telegram chat is not linked to a Ledger account."}), 403
    linked_user_id = link["user_id"]

    # Reuse the same conversation format as the web client: user/assistant
    # messages only. Keep the window bounded so prompts do not grow forever.
    rows = db.execute(
        "SELECT role, content FROM telegram_sara_messages "
        "WHERE chat_id = ? ORDER BY id DESC LIMIT 12",
        (chat_id,),
    ).fetchall()
    history = [dict(r) for r in reversed(rows)]

    result = sara.run_sara(message, app.test_client(), history, channel="telegram", telegram_chat_id=chat_id, internal_user_id=linked_user_id)

    # Persist the turn after Sara has generated its response. The Telegram
    # message id is retained for traceability/deduplication diagnostics.
    reply = result.get("reply") or "Done."
    db.execute(
        "INSERT INTO telegram_sara_messages "
        "(chat_id, telegram_message_id, role, content) VALUES (?, ?, ?, ?)",
        (chat_id, telegram_message_id, "user", message),
    )
    db.execute(
        "INSERT INTO telegram_sara_messages "
        "(chat_id, telegram_message_id, role, content) VALUES (?, ?, ?, ?)",
        (chat_id, telegram_message_id, "assistant", reply),
    )
    db.commit()

    return jsonify({
        "reply": reply,
        "actions": result.get("actions", []),
        "source": result.get("source"),
    })


if __name__ == "__main__":
    app.run(debug=True, port=8000)