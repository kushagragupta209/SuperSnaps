"""
Ledger — a local-first personal purchase timeline + monthly expense tracker.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000

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

from flask import Flask, g, jsonify, render_template, request

import agent
import flights
import telegram_sync
import sara
import db_pg

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
        CREATE TABLE IF NOT EXISTS purchases (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            price       REAL NOT NULL,
            purchased_on TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
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
    db = get_db()
    rows = db.execute(
        "SELECT * FROM purchases ORDER BY purchased_on DESC, id DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/purchases", methods=["POST"])
def add_purchase():
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
        "INSERT INTO purchases (name, price, purchased_on, created_at) VALUES (?, ?, ?, ?)",
        (name, price, purchased_on, datetime.utcnow().isoformat()),
    )
    db.commit()
    new_row = db.execute("SELECT * FROM purchases WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(new_row)), 201


@app.route("/api/purchases/<int:purchase_id>", methods=["DELETE"])
def delete_purchase(purchase_id):
    db = get_db()
    db.execute("DELETE FROM purchases WHERE id = ?", (purchase_id,))
    db.commit()
    return jsonify({"deleted": purchase_id})


# --------------------------------------------------------------------------- #
# Routes — API: settings (monthly salary)
# --------------------------------------------------------------------------- #

@app.route("/api/settings/salary", methods=["GET"])
def get_salary():
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = 'monthly_salary'").fetchone()
    return jsonify({"monthly_salary": float(row["value"]) if row else None})


@app.route("/api/settings/salary", methods=["POST"])
def set_salary():
    data = request.get_json(force=True)
    try:
        salary = float(data.get("monthly_salary"))
        if salary < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Salary must be a positive number."}), 400

    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('monthly_salary', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(salary),),
    )
    db.commit()
    return jsonify({"monthly_salary": salary})


@app.route("/api/settings/savings-percent", methods=["GET"])
def get_savings_percent():
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = 'savings_percent'").fetchone()
    return jsonify({"savings_percent": float(row["value"]) if row else None})


@app.route("/api/settings/savings-percent", methods=["POST"])
def set_savings_percent():
    """Blank/null clears the goal so insights fall back to the 50/30/20 default."""
    data = request.get_json(force=True)
    raw = data.get("savings_percent")

    db = get_db()
    if raw is None or raw == "":
        db.execute("DELETE FROM settings WHERE key = 'savings_percent'")
        db.commit()
        return jsonify({"savings_percent": None})

    try:
        pct = float(raw)
        if not (0 <= pct <= 100):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Savings goal must be a percentage between 0 and 100."}), 400

    db.execute(
        "INSERT INTO settings (key, value) VALUES ('savings_percent', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(pct),),
    )
    db.commit()
    return jsonify({"savings_percent": pct})


# --------------------------------------------------------------------------- #
# Routes — API: spending insights (LLM/rule-based monthly review + budget)
# --------------------------------------------------------------------------- #

def _month_spend(db, year, month):
    """Returns (purchases_total, fixed_total, fixed_rows) for one (year, month)."""
    month_prefix = f"{year:04d}-{month:02d}"
    purchases_total = db.execute(
        "SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE purchased_on LIKE ?",
        (f"{month_prefix}%",),
    ).fetchone()["s"]
    fixed_rows = db.execute(
        "SELECT category, amount FROM monthly_expenses WHERE year = ? AND month = ? ORDER BY amount DESC",
        (year, month),
    ).fetchall()
    fixed_total = sum(r["amount"] for r in fixed_rows)
    return purchases_total, fixed_total, [dict(r) for r in fixed_rows]


@app.route("/api/insights", methods=["GET"])
def insights():
    """
    Judges the current month's spending against salary/savings goal and
    recent history, and proposes a next-month budget. See agent.py's
    analyze_spending() / INSIGHTS_SYSTEM_PROMPT for the full response shape
    and how the next-month budget is derived.
    """
    db = get_db()
    today = date.today()

    month_purchases, fixed_total, fixed_expenses = _month_spend(db, today.year, today.month)

    ytd_purchases = db.execute(
        "SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE purchased_on LIKE ?",
        (f"{today.year:04d}-%",),
    ).fetchone()["s"]

    # Last 6 months including the current one, oldest first.
    history = []
    for i in range(5, -1, -1):
        y, m = today.year, today.month - i
        while m <= 0:
            m += 12
            y -= 1
        p, f, _ = _month_spend(db, y, m)
        history.append({"year": y, "month": m, "purchases": round(p, 2), "fixed_expenses": round(f, 2)})

    salary_row = db.execute("SELECT value FROM settings WHERE key = 'monthly_salary'").fetchone()
    salary = float(salary_row["value"]) if salary_row else None

    savings_row = db.execute("SELECT value FROM settings WHERE key = 'savings_percent'").fetchone()
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
    db = get_db()
    rows = db.execute(
        "SELECT key, value FROM settings WHERE key IN ('profession', 'interests')"
    ).fetchall()
    values = {r["key"]: r["value"] for r in rows}
    return jsonify({
        "profession": values.get("profession", ""),
        "interests": values.get("interests", ""),
    })


@app.route("/api/settings/profile", methods=["POST"])
def set_profile():
    data = request.get_json(force=True)
    profession = (data.get("profession") or "").strip()
    interests = (data.get("interests") or "").strip()

    db = get_db()
    for key, value in (("profession", profession), ("interests", interests)):
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    db.commit()
    return jsonify({"profession": profession, "interests": interests})


# --------------------------------------------------------------------------- #
# Routes — API: product recommendations
# --------------------------------------------------------------------------- #

@app.route("/api/recommendations", methods=["GET"])
def recommendations():
    db = get_db()
    purchase_rows = db.execute(
        "SELECT name, price FROM purchases ORDER BY purchased_on DESC, id DESC LIMIT 20"
    ).fetchall()
    purchases = [dict(r) for r in purchase_rows]

    profile_rows = db.execute(
        "SELECT key, value FROM settings WHERE key IN ('profession', 'interests')"
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
    """
    Returns each wishlist item alongside a savings projection: how much is
    being saved per month right now (salary minus this month's fixed
    expenses and purchases), and how many months at that rate it'd take to
    afford each item.
    """
    db = get_db()
    rows = db.execute(
        "SELECT * FROM wishlist ORDER BY price ASC, id ASC"
    ).fetchall()
    items = [dict(r) for r in rows]

    salary_row = db.execute("SELECT value FROM settings WHERE key = 'monthly_salary'").fetchone()
    salary = float(salary_row["value"]) if salary_row else None

    today = date.today()
    month_prefix = f"{today.year:04d}-{today.month:02d}"
    month_purchases = db.execute(
        "SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE purchased_on LIKE ?",
        (f"{month_prefix}%",),
    ).fetchone()["s"]
    fixed_total = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM monthly_expenses WHERE year = ? AND month = ?",
        (today.year, today.month),
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
        "INSERT INTO wishlist (name, price, created_at, product_url, platform) VALUES (?, ?, ?, ?, ?)",
        (name, price, datetime.utcnow().isoformat(), product_url, platform),
    )
    item_id = cur.lastrowid
    db.execute(
        "INSERT INTO wishlist_price_history (wishlist_id, price, checked_at) VALUES (?, ?, ?)",
        (item_id, price, datetime.utcnow().isoformat()),
    )
    db.commit()
    new_row = db.execute("SELECT * FROM wishlist WHERE id = ?", (item_id,)).fetchone()
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
    existing = db.execute("SELECT id FROM wishlist WHERE product_url = ?", (url,)).fetchone()
    if existing:
        item_id = existing["id"]
        db.execute("UPDATE wishlist SET name = ?, price = ?, platform = ? WHERE id = ?", (product["name"], product["price"], product["platform"], item_id))
    else:
        cur = db.execute(
            "INSERT INTO wishlist (name, price, created_at, product_url, platform) VALUES (?, ?, ?, ?, ?)",
            (product["name"], product["price"], datetime.utcnow().isoformat(), url, product["platform"]),
        )
        item_id = cur.lastrowid
    db.execute("INSERT INTO wishlist_price_history (wishlist_id, price, checked_at) VALUES (?, ?, ?)", (item_id, product["price"], datetime.utcnow().isoformat()))
    db.commit()
    row = db.execute("SELECT * FROM wishlist WHERE id = ?", (item_id,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/wishlist/<int:item_id>/history", methods=["GET"])
def wishlist_price_history(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM wishlist WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return jsonify({"error": "Wishlist item not found."}), 404
    rows = db.execute("SELECT price, checked_at FROM wishlist_price_history WHERE wishlist_id = ? ORDER BY checked_at ASC", (item_id,)).fetchall()
    return jsonify({"item": dict(item), "history": [dict(r) for r in rows]})


@app.route("/api/wishlist/<int:item_id>/refresh", methods=["POST"])
def refresh_wishlist_price(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM wishlist WHERE id = ?", (item_id,)).fetchone()
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
    db.execute("UPDATE wishlist SET name = ?, price = ?, platform = ? WHERE id = ?", (product["name"], product["price"], product["platform"], item_id))
    db.execute("INSERT INTO wishlist_price_history (wishlist_id, price, checked_at) VALUES (?, ?, ?)", (item_id, product["price"], now))
    db.commit()
    return jsonify({"name": product["name"], "price": product["price"], "platform": product["platform"]})


@app.route("/api/wishlist/<int:item_id>/manual-price", methods=["POST"])
def manual_wishlist_price(item_id):
    data = request.get_json(force=True)
    try:
        price = float(data.get("price"))
        if price <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Price must be a positive number."}), 400
    db = get_db()
    item = db.execute("SELECT * FROM wishlist WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return jsonify({"error": "Wishlist item not found."}), 404
    now = datetime.utcnow().isoformat()
    db.execute("UPDATE wishlist SET price = ? WHERE id = ?", (price, item_id))
    db.execute("INSERT INTO wishlist_price_history (wishlist_id, price, checked_at) VALUES (?, ?, ?)", (item_id, price, now))
    db.commit()
    return jsonify({"id": item_id, "price": price, "checked_at": now})


@app.route("/api/wishlist/<int:item_id>", methods=["DELETE"])
def delete_wishlist(item_id):
    db = get_db()
    db.execute("DELETE FROM wishlist WHERE id = ?", (item_id,))
    db.commit()
    return jsonify({"deleted": item_id})


# --------------------------------------------------------------------------- #
# Routes — API: flight fare tracking
# --------------------------------------------------------------------------- #
# Fare lookups go through flights.py (SerpApi's Google Flights engine). See
# that file for provider notes and how to set SERPAPI_API_KEY.

@app.route("/api/flights/search", methods=["POST"])
def search_flights():
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
    db = get_db()
    rows = db.execute("SELECT * FROM flights ORDER BY created_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/flights", methods=["POST"])
def add_flight():
    data = request.get_json(force=True)
    origin = (data.get("origin") or "").strip().upper()
    destination = (data.get("destination") or "").strip().upper()
    departure_date = (data.get("departure_date") or "").strip()
    return_date = (data.get("return_date") or "").strip() or None
    travel_class = (data.get("travel_class") or "ECONOMY").strip().upper()
    notify_email = (data.get("notify_email") or "").strip() or None

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
                target_price, notify_email, current_price, lowest_price, active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1, ?)""",
        (origin, destination, departure_date, return_date, adults, travel_class,
         target_price, notify_email, now),
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
            "UPDATE flights SET current_price = ?, lowest_price = ? WHERE id = ?",
            (price, price, flight_id),
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

    row = dict(db.execute("SELECT * FROM flights WHERE id = ?", (flight_id,)).fetchone())
    if warning:
        row["warning"] = warning
    return jsonify(row), 201


@app.route("/api/flights/<int:flight_id>/check", methods=["POST"])
def check_flight_fare(flight_id):
    db = get_db()
    flight = db.execute("SELECT * FROM flights WHERE id = ?", (flight_id,)).fetchone()
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
        "UPDATE flights SET current_price = ?, lowest_price = ? WHERE id = ?",
        (price, lowest, flight_id),
    )
    db.execute(
        "INSERT INTO flight_price_history (flight_id, price, checked_at) VALUES (?, ?, ?)",
        (flight_id, price, now),
    )
    db.commit()
    return jsonify({"id": flight_id, "price": price, "lowest_price": lowest, "checked_at": now, "fares": fares})


@app.route("/api/flights/<int:flight_id>/history", methods=["GET"])
def flight_price_history(flight_id):
    db = get_db()
    flight = db.execute("SELECT * FROM flights WHERE id = ?", (flight_id,)).fetchone()
    if not flight:
        return jsonify({"error": "Flight tracker not found."}), 404
    rows = db.execute(
        "SELECT price, checked_at FROM flight_price_history WHERE flight_id = ? ORDER BY checked_at ASC",
        (flight_id,),
    ).fetchall()
    return jsonify({"flight": dict(flight), "history": [dict(r) for r in rows]})


@app.route("/api/flights/<int:flight_id>", methods=["DELETE"])
def delete_flight(flight_id):
    db = get_db()
    db.execute("DELETE FROM flights WHERE id = ?", (flight_id,))
    db.commit()
    return jsonify({"deleted": flight_id})


# --------------------------------------------------------------------------- #
# Routes — API: day-wise expenses (manual entry + Telegram sync)
# --------------------------------------------------------------------------- #

@app.route("/api/day-expenses", methods=["POST"])
def save_day_expenses():
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
            "INSERT INTO day_expenses (date, merchant, category, amount, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date_str, merchant, category, amount, source, datetime.utcnow().isoformat()),
        )
        row = db.execute("SELECT * FROM day_expenses WHERE id = ?", (cur.lastrowid,)).fetchone()
        saved.append(dict(row))
    db.commit()

    if not saved:
        return jsonify({"error": "No valid items with a resolved date (YYYY-MM-DD) and amount were provided."}), 400

    return jsonify({"items": saved, "skipped": skipped}), 201


@app.route("/api/day-expenses", methods=["GET"])
def list_day_expenses():
    year = request.args.get("year", type=int, default=date.today().year)
    month = request.args.get("month", type=int, default=date.today().month)
    month_prefix = f"{year:04d}-{month:02d}"

    db = get_db()
    rows = db.execute(
        "SELECT * FROM day_expenses WHERE date LIKE ? ORDER BY date DESC, id DESC",
        (f"{month_prefix}%",),
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
    db = get_db()
    db.execute("DELETE FROM day_expenses WHERE id = ?", (item_id,))
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
    db = get_db()
    try:
        pending = telegram_sync.fetch_pending(db)
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
            "INSERT INTO day_expenses (date, merchant, category, amount, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (parsed["date"], parsed.get("merchant") or "", parsed["category"],
             parsed["amount"], "telegram", datetime.utcnow().isoformat()),
        )
        saved_row = db.execute("SELECT * FROM day_expenses WHERE id = ?", (cur.lastrowid,)).fetchone()
        saved.append(dict(saved_row))

    db.commit()

    try:
        telegram_sync.mark_processed(db, processed_ids)
    except Exception:  # noqa: BLE001 - covers psycopg2 failures too
        # The local inserts above already succeeded and are committed, so
        # nothing's lost — but if this fails, the same messages will be
        # re-fetched (and re-inserted as duplicates) on the next sync.
        # Surface it rather than pretending everything's clean.
        return jsonify({
            "items": saved, "skipped": skipped, "synced": len(saved),
            "warning": "Saved locally, but couldn't mark messages as processed in the inbox — "
                       "they may be synced again next time.",
        })

    return jsonify({"items": saved, "skipped": skipped, "synced": len(saved)})


# --------------------------------------------------------------------------- #
# Routes — API: monthly expenses (natural language)
# --------------------------------------------------------------------------- #

@app.route("/api/monthly-expenses/parse", methods=["POST"])
def parse_monthly_expenses():
    """Preview-parse text without saving, so the UI can show a confirm step."""
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
    """
    Body: { "text": "...", "year": 2026, "month": 8, "items": [optional edited list] }
    If "items" is provided (user edited the parsed preview), use it directly;
    otherwise parse "text" server-side.
    """
    data = request.get_json(force=True)
    text = data.get("text", "")
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
    db.execute("DELETE FROM monthly_expenses WHERE year = ? AND month = ?", (year, month))
    for item in parsed:
        db.execute(
            "INSERT INTO monthly_expenses (year, month, category, amount, raw_text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (year, month, item["category"], float(item["amount"]), text, datetime.utcnow().isoformat()),
        )
    db.commit()

    rows = db.execute(
        "SELECT * FROM monthly_expenses WHERE year = ? AND month = ? ORDER BY amount DESC",
        (year, month),
    ).fetchall()
    return jsonify({"items": [dict(r) for r in rows], "source": source}), 201


@app.route("/api/monthly-expenses", methods=["GET"])
def get_monthly_expenses():
    year = request.args.get("year", type=int, default=date.today().year)
    month = request.args.get("month", type=int, default=date.today().month)
    db = get_db()
    rows = db.execute(
        "SELECT * FROM monthly_expenses WHERE year = ? AND month = ? ORDER BY amount DESC",
        (year, month),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# --------------------------------------------------------------------------- #
# Routes — API: dashboard summary
# --------------------------------------------------------------------------- #

@app.route("/api/summary", methods=["GET"])
def summary():
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

    total_purchases = db.execute("SELECT COALESCE(SUM(price), 0) AS s FROM purchases").fetchone()["s"]

    month_prefix = f"{year:04d}-{month:02d}"
    month_purchases = db.execute(
        "SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE purchased_on LIKE ?",
        (f"{month_prefix}%",),
    ).fetchone()["s"]

    ytd_purchases = db.execute(
        "SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE purchased_on LIKE ?",
        (f"{year:04d}-%",),
    ).fetchone()["s"]

    fixed_rows = db.execute(
        "SELECT category, amount FROM monthly_expenses WHERE year = ? AND month = ? ORDER BY amount DESC",
        (year, month),
    ).fetchall()
    fixed_total = sum(r["amount"] for r in fixed_rows)

    salary_row = db.execute("SELECT value FROM settings WHERE key = 'monthly_salary'").fetchone()
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
    """
    Calculates remaining discretionary allowance for the month and splits
    it across remaining days (today included).
    """
    db = get_db()
    today = date.today()
    
    # 1. Fetch salary & savings target
    salary_row = db.execute("SELECT value FROM settings WHERE key = 'monthly_salary'").fetchone()
    salary = float(salary_row["value"]) if salary_row else None

    savings_row = db.execute("SELECT value FROM settings WHERE key = 'savings_percent'").fetchone()
    savings_pct = float(savings_row["value"]) if savings_row else agent.DEFAULT_SAVINGS_PERCENT

    # 2. Total days & days left in month (including today)
    _, total_days = calendar.monthrange(today.year, today.month)
    days_remaining = max(1, total_days - today.day + 1)

    # 3. Monthly expenses and purchases
    month_prefix = f"{today.year:04d}-{today.month:02d}"
    purchases_total = db.execute(
        "SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE purchased_on LIKE ?",
        (f"{month_prefix}%",),
    ).fetchone()["s"]

    fixed_total = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM monthly_expenses WHERE year = ? AND month = ?",
        (today.year, today.month),
    ).fetchone()["s"]

    # Also count day_expenses (manual entries + Telegram sync) for this month
    day_spend_total = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM day_expenses WHERE date LIKE ?",
        (f"{month_prefix}%",),
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
        "SELECT COALESCE(SUM(price), 0) AS s FROM purchases WHERE purchased_on = ?",
        (today_str,),
    ).fetchone()["s"]
    day_expenses_today = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM day_expenses WHERE date = ?",
        (today_str,),
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
    db = get_db()
    today = date.today()
    month_purchases, fixed_total, fixed_expenses = _month_spend(db, today.year, today.month)
    
    salary_row = db.execute("SELECT value FROM settings WHERE key = 'monthly_salary'").fetchone()
    salary = float(salary_row["value"]) if salary_row else None
    
    savings_row = db.execute("SELECT value FROM settings WHERE key = 'savings_percent'").fetchone()
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
                "UPDATE monthly_expenses SET amount = ? WHERE year = ? AND month = ? AND LOWER(category) = LOWER(?)",
                (new_amt, today.year, today.month, cat)
            )
    db.commit()
    return jsonify({"status": "applied", "updated_count": len(rebalanced)}), 200


@app.route("/api/agents/deal-evaluator", methods=["POST"])
def evaluate_wishlist_deal():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    price = float(data.get("price") or 0)
    
    if not name or price <= 0:
        return jsonify({"error": "Valid item name and price required."}), 400

    db = get_db()
    today = date.today()
    month_purchases, fixed_total, _ = _month_spend(db, today.year, today.month)
    salary_row = db.execute("SELECT value FROM settings WHERE key = 'monthly_salary'").fetchone()
    salary = float(salary_row["value"]) if salary_row else 0
    monthly_savings = max(0.0, salary - fixed_total - month_purchases)

    deal_info, source = agent.evaluate_deal(name, price, monthly_savings)
    return jsonify({**deal_info, "source": source})

# --------------------------------------------------------------------------- #
# Routes — API: Natural Language Financial Chatbot
# --------------------------------------------------------------------------- #

@app.route("/api/chat", methods=["POST"])
def financial_chat():
    data = request.get_json(force=True)
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Empty message."}), 400

    parsed = agent.generate_chat_sql(message)
    
    if not parsed:
        # Fallback simple search across purchases and expenses
        db = get_db()
        rows = db.execute("SELECT * FROM purchases ORDER BY purchased_on DESC LIMIT 5").fetchall()
        return jsonify({
            "reply": "I couldn't run a deep SQL search, but here are your 5 most recent purchases.",
            "data": [dict(r) for r in rows]
        })

    if parsed.get("direct_reply"):
        return jsonify({"reply": parsed["direct_reply"], "sql": None, "data": []})

    sql = parsed.get("sql")
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

    result = sara.run_sara(message, app.test_client(), history)
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True)