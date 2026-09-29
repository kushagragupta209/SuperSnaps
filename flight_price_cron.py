"""
Daily flight fare checker for Ledger.

Run this as a Render Cron Job once per day. It checks every active tracker
whose departure date has not passed, stores the new fare in the existing
flight_price_history table, and sends a concise Telegram update when the
tracker is linked to Telegram notifications.
"""

import os
import requests
from datetime import date, datetime

import flights
from app import app, get_db


def send_telegram(chat_id, text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    response.raise_for_status()


def money(value):
    return f"₹{float(value):,.0f}"


def check_tracker(db, row):
    tracker_id = row["id"]
    try:
        fares = flights.fetch_all_fares(
            row["origin"],
            row["destination"],
            row["departure_date"],
            row["return_date"],
            row["adults"],
            row["travel_class"],
        )
    except Exception as exc:  # provider/network errors should not stop other trackers
        print(f"[flight-cron] tracker {tracker_id} failed: {exc}")
        return False

    price = float(fares[0]["price"])
    previous = row["current_price"]
    lowest = row["lowest_price"]
    lowest = price if lowest is None else min(float(lowest), price)
    now = datetime.utcnow().isoformat()

    db.execute(
        "UPDATE flights SET current_price = ?, lowest_price = ?, active = 1 WHERE id = ?",
        (price, lowest, tracker_id),
    )
    db.execute(
        "INSERT INTO flight_price_history (flight_id, price, checked_at) VALUES (?, ?, ?)",
        (tracker_id, price, now),
    )
    db.commit()

    if not row["telegram_chat_id"] or not row["notify_telegram"]:
        return True

    route = f"{row['origin']} → {row['destination']}"
    try:
        departure = datetime.strptime(row["departure_date"], "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        departure = row["departure_date"]

    lines = [
        "✈️ <b>Flight Price Update</b>",
        "",
        f"<b>{route}</b>",
        f"📅 {departure}",
        "",
        f"💰 <b>Current fare:</b> {money(price)}",
        f"📉 <b>Lowest tracked:</b> {money(lowest)}",
    ]

    if previous is not None:
        change = price - float(previous)
        if change < 0:
            lines.append(f"⬇️ <b>Since yesterday:</b> {money(abs(change))} cheaper")
        elif change > 0:
            lines.append(f"⬆️ <b>Since yesterday:</b> {money(change)} higher")
        else:
            lines.append("➡️ <b>Since yesterday:</b> No change")

    if price <= lowest:
        lines.append("🔥 <b>New lowest tracked price!</b>")

    lines.append("")
    lines.append("🕐 Checked today.")
    try:
        send_telegram(row["telegram_chat_id"], "\n".join(lines))
    except Exception as exc:
        print(f"[flight-cron] Telegram notification failed for tracker {tracker_id}: {exc}")

    return True


def main():
    today = date.today().isoformat()
    with app.app_context():
        db = get_db()
        rows = db.execute(
            "SELECT * FROM flights WHERE active = 1 AND departure_date >= ? ORDER BY id",
            (today,),
        ).fetchall()

        print(f"[flight-cron] checking {len(rows)} active tracker(s)")
        checked = 0
        for row in rows:
            if check_tracker(db, row):
                checked += 1

        # Automatically stop tracking flights whose departure date has passed.
        db.execute(
            "UPDATE flights SET active = 0 WHERE active = 1 AND departure_date < ?",
            (today,),
        )
        db.commit()
        print(f"[flight-cron] completed: {checked}/{len(rows)} checked")


if __name__ == "__main__":
    main()
