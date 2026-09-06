"""
telegram_sync.py — Ledger's Telegram inbox sync.

Kept separate from app.py on purpose, same reasoning as flights.py and
agent.py: this is where the external call to Supabase lives, so app.py's
routes stay thin.

Why this exists: a Telegram bot needs an always-on machine to catch
messages at any time, and Telegram itself only holds undelivered updates
for 24 hours (see core.telegram.org/bots/api, getUpdates). Since Ledger's
Flask app only runs on-demand on a laptop, neither fits directly — so a
small Cloudflare Worker (always-on, free, see telegram-webhook-worker.js)
receives Telegram's webhook the instant a message arrives and writes the
raw text into a `pending_expenses` table in a free-tier Supabase Postgres
project. That table is the durable "global inbox": messages sit there
indefinitely (no 24-hour risk) until Ledger is launched and calls
fetch_pending() to pull them in. app.py then parses each one with
agent.parse_day_expense(), inserts into the local day_expenses table, and
calls mark_processed() so the same message isn't synced twice.

Public interface used by app.py:
    fetch_pending() -> list[dict]      # unprocessed inbox rows, oldest first
    mark_processed(ids: list[int])     -> None
    is_telegram_sync_configured() -> bool
"""

import os

try:
    import requests
except ImportError:  # requests is in requirements.txt; guard just in case
    requests = None


# --------------------------------------------------------------------------- #
# Config — Supabase (https://supabase.com), free tier
# --------------------------------------------------------------------------- #
#
# Set these in your shell before running the app:
#   export SUPABASE_URL="https://xxxx.supabase.co"
#   export SUPABASE_SERVICE_KEY="..."   # Settings -> API -> service_role key
#
# Use the service_role key here, never the anon/public key — this table
# has Row Level Security enabled with no public policies, so only the
# service_role key (meant for trusted server-side code) can read/write it.

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")


def is_telegram_sync_configured() -> bool:
    """Whether Supabase credentials are present (doesn't guarantee they're valid)."""
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY and requests is not None)


def _headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def fetch_pending():
    """
    Return every unprocessed row from the pending_expenses inbox table,
    oldest first (so entries land in day_expenses in the order they were
    actually sent). Each row: {id, chat_id, raw_text, telegram_message_id,
    sent_at, processed, created_at}.

    Raises ValueError if Supabase isn't configured, and
    requests.RequestException for network/HTTP failures — same pattern
    as flights.py, so app.py can handle each case consistently.
    """
    if not is_telegram_sync_configured():
        raise ValueError(
            "Telegram sync isn't configured yet — set SUPABASE_URL and "
            "SUPABASE_SERVICE_KEY (see telegram_sync.py for setup notes)."
        )

    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/pending_expenses",
        headers=_headers(),
        params={"processed": "eq.false", "order": "sent_at.asc", "select": "*"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def mark_processed(ids):
    """
    Mark the given pending_expenses row ids as processed so fetch_pending()
    doesn't return them again on the next sync. No-op if ids is empty.
    """
    if not ids:
        return
    if not is_telegram_sync_configured():
        raise ValueError("Telegram sync isn't configured yet.")

    id_list = ",".join(str(int(i)) for i in ids)
    resp = requests.patch(
        f"{SUPABASE_URL}/rest/v1/pending_expenses",
        headers=_headers(),
        params={"id": f"in.({id_list})"},
        json={"processed": True},
        timeout=15,
    )
    resp.raise_for_status()