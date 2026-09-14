"""
telegram_sync.py — Ledger's Telegram inbox sync.

Kept separate from app.py on purpose, same reasoning as flights.py and
agent.py: this is where the Telegram-specific logic lives, so app.py's
routes stay thin.

Why this exists: a Telegram bot needs an always-on machine to catch
messages at any time, and Telegram itself only holds undelivered updates
for 24 hours (see core.telegram.org/bots/api, getUpdates). A small
Cloudflare Worker (always-on, free — see telegram-webhook-worker.js)
receives Telegram's webhook the instant a message arrives and writes the
raw text into a `pending_expenses` table. That table is the durable
"global inbox": messages sit there indefinitely (no 24-hour risk) until
Ledger fetches them via fetch_pending(), parses each one with
agent.parse_day_expense(), inserts into day_expenses, and calls
mark_processed() so the same message isn't synced twice.

fetch_pending()/mark_processed() run as plain SQL against `db` — the SAME
Postgres connection app.py's routes already use (see db_pg.py) — rather
than a separate call to Supabase's REST API. That's possible because once
DATABASE_URL points Ledger at Postgres (e.g. for Render deployment,
pointed at your existing Supabase project), `pending_expenses` just lives
in that same database as another table: written into by the Cloudflare
Worker via Supabase's REST API (unchanged on that side), read here
directly (no second REST layer needed on this side).

Public interface used by app.py:
    fetch_pending(db) -> list[dict]      # unprocessed inbox rows, oldest first
    mark_processed(db, ids: list[int])   -> None
    is_telegram_sync_configured() -> bool
    send_message(chat_id, text) -> bool
    is_telegram_reply_configured() -> bool
"""

import os

import db_pg

try:
    import requests
except ImportError:  # requests is in requirements.txt; guard just in case
    requests = None

# Only needed for send_message() below — the actual bot token from
# BotFather, used to call Telegram's own sendMessage API so Ledger can
# reply in the same chat after a sync. Separate from the Cloudflare
# Worker's webhook secret, and separate from DATABASE_URL.
#
# NEVER hardcode this (or DATABASE_URL, or any other secret) directly in
# this file — always pass it as an environment variable. A hardcoded
# secret gets committed to git history the moment this file is pushed,
# and stays recoverable there even after you edit it back out later.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


def is_telegram_sync_configured() -> bool:
    """Whether Ledger is running against Postgres (where pending_expenses lives)."""
    return db_pg.is_postgres_configured()


def fetch_pending(db):
    """
    Return every unprocessed row from the pending_expenses inbox table,
    oldest first (so entries land in day_expenses in the order they were
    actually sent). Each row: {id, chat_id, raw_text, telegram_message_id,
    sent_at, processed, created_at}.

    Raises ValueError if Ledger isn't running against Postgres (local
    SQLite mode has no pending_expenses table — Telegram sync needs the
    cloud database).
    """
    if not is_telegram_sync_configured():
        raise ValueError(
            "Telegram sync isn't available in local SQLite mode — set "
            "DATABASE_URL to your Postgres connection string (see "
            "telegram_sync.py and README's Cloud Deployment section)."
        )
    rows = db.execute(
        "SELECT * FROM pending_expenses WHERE processed = false ORDER BY sent_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_processed(db, ids):
    """
    Mark the given pending_expenses row ids as processed so fetch_pending()
    doesn't return them again on the next sync. No-op if ids is empty.
    """
    if not ids:
        return
    if not is_telegram_sync_configured():
        raise ValueError("Telegram sync isn't available in local SQLite mode.")

    placeholders = ",".join("?" for _ in ids)
    db.execute(
        f"UPDATE pending_expenses SET processed = true WHERE id IN ({placeholders})",
        tuple(int(i) for i in ids),
    )
    db.commit()


def is_telegram_reply_configured() -> bool:
    """Whether a bot token is present for send_message() to use."""
    return bool(TELEGRAM_BOT_TOKEN and requests is not None)


def send_message(chat_id, text):
    """
    Send a plain-text reply back into a Telegram chat via the Bot API.
    Used to confirm a synced expense right in the same conversation.

    Never raises — a failed reply shouldn't undo an expense that's
    already been saved locally by the time this is called. Returns True/
    False for whether the send actually succeeded, so callers can decide
    whether to surface a warning.
    """
    if not is_telegram_reply_configured():
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        return resp.ok
    except requests.RequestException:
        return False