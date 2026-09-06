# Ledger — Purchase Timeline & Expense Tracker

## Run it
```
pip install -r requirements.txt
python app.py
```
Then open http://127.0.0.1:5000

## What's inside
- `app.py` — Flask backend: routes, SQLite storage, and the dashboard/summary
  calculations. Delegates all natural-language parsing to `agent.py`, all
  flight-fare lookups to `flights.py`, and Telegram inbox sync to
  `telegram_sync.py`.
- `agent.py` — the "agent" module, kept separate on purpose so it can grow
  independently of the Flask routes. It does three jobs: turn a sentence
  like "I spent 12k on rent, 5k on food and 3k on fuel" into structured
  monthly `{category, amount}` rows (`parse_expense`, trying **Groq's API**
  first and falling back to a local regex parser); turn a short message
  like "paid 180 today" or "spent 200 yesterday" into a single day-wise
  `{date, category, amount, merchant}` line item (`parse_day_expense`,
  regex-only — see "Day-wise spending & Telegram sync" below); and generate
  product recommendations / spending insights (see their own sections
  below). This is where future agent features should live — e.g.
  auto-categorizing purchases — without touching `app.py`.
- `flights.py` — flight-fare lookups via SerpApi's Google Flights engine.
  Kept separate from `app.py` for the same reason as `agent.py`: it can be
  swapped for a different provider without touching Flask routes. See
  "Flight fare tracking" below.
- `telegram_sync.py` — talks to a Supabase table that acts as a durable
  inbox for Telegram messages (written by a small Cloudflare Worker, not by
  this app). See "Day-wise spending & Telegram sync" below for why this
  exists and how the pieces fit together.
- `templates/index.html` — single-page dashboard: salary + savings goal
  (with a show/hide toggle) + monthly expense input, a Safe-to-Spend Daily
  Meter, a profile card (job/interests), summary stats (luxury purchases,
  fixed expenses with a per-category breakdown), a donut chart + legend for
  fixed expenses, a "Spending insights" panel, a "What you might want next"
  recommendations panel, a wishlist with savings projections, flight-fare
  tracking, a day-wise spending list (fed by Telegram — see below), a
  vertical purchase timeline with a "+" button to log new purchases, and a
  floating chat assistant (💬 button, bottom-right, available from any tab).
  Badges throughout show whether "Groq" or the "Local rules"/"rules"
  fallback actually produced a given result.
- `ledger.db` — created automatically on first run (SQLite).

## Using the Groq parser
1. Create a free account and API key at https://console.groq.com/keys
   (no credit card required for the free tier; it's rate-limited — roughly
   30 requests/minute and several thousand/day on Llama models as of this
   writing — check https://console.groq.com for current limits).
2. Set the key as an environment variable before starting the app:
   ```
   export GROQ_API_KEY="gsk_yourkeyhere"
   python app.py
   ```
3. Optionally override the model used (default is `llama-3.3-70b-versatile`):
   ```
   export GROQ_MODEL="llama-3.3-70b-versatile"
   ```
   Groq's model lineup changes over time — check
   https://console.groq.com/docs/models for the current text-generation
   models before relying on this in production, and just update
   `GROQ_MODEL` if the default gets deprecated.
4. If the key is missing, invalid, or the API call fails for any reason
   (network issue, rate limit), `agent.parse_expense()` automatically and
   silently falls back to the built-in regex parser — it will never crash
   the request, it just won't be as flexible about phrasing.

Never commit your API key to source control or hardcode it in `agent.py` —
always pass it as an environment variable.

## Product recommendations
The "What you might want next" panel calls `GET /api/recommendations`, which
sends your last 20 purchases plus your job/field of work and interests
(`GET`/`POST /api/settings/profile`) to Groq and asks for up to 5 concrete
next-purchase suggestions with a reason, category, and estimated price.

If Groq isn't configured or the call fails, `agent.recommend_products()`
falls back to a small rule-based recommender: keyword rules match your job
title/interests (e.g. "software engineer" → keyboard, monitor, ergonomic
chair) and complementary-item rules match your past purchases (bought
headphones → suggests a stand; bought a laptop → suggests a sleeve).
Already-bought items are filtered out either way. The `source` field
(`"groq"` or `"rules"`) tells you which one ran.

## Flight fare tracking
The "Track a Flight" panel (`GET`/`POST /api/flights`,
`POST /api/flights/<id>/check`, `POST /api/flights/search`,
`GET /api/flights/<id>/history`, `DELETE /api/flights/<id>`) watches a
route/date you specify and shows the cheapest fare found, plus the full
list of available fares (airline, price, stops, duration) sorted cheapest
first.

Fares come from **SerpApi's Google Flights engine**
(https://serpapi.com/google-flights-api):
1. Create a free account at https://serpapi.com (free tier: 100 searches/
   month).
2. Set the key as an environment variable before starting the app:
   ```
   export SERPAPI_API_KEY="your_key_here"
   python app.py
   ```
3. If the key is missing or invalid, or the SerpApi call fails, the routes
   return a clear error (surfacing SerpApi's own error message where
   possible) rather than crashing — flight tracking is the one feature
   here with no local-rules fallback, since there's no sensible way to
   guess a real-world flight price offline.

Each SerpApi call (creating a tracker, "Check fare now," "Search Fares," or
"View all fares" while filling out the form) counts against your monthly
quota — a handful of tracked routes checked daily can use up the free
tier's 100/month fairly quickly, so check deliberately rather than
repeatedly while experimenting with dates.

Trackers only refresh when something calls `/check` — there's no built-in
scheduler. `check_flights_cron.py` (run separately, e.g. via a daily cron
job) drives that: it calls `/api/flights` for active trackers and posts to
each one's `/check` endpoint, so scheduling stays outside the Flask
process and survives app restarts. See the script's own docstring for
setup.

## Day-wise spending & Telegram sync
The "Day-wise Spending" panel (`GET`/`POST /api/day-expenses`,
`DELETE /api/day-expenses/<id>`) tracks spend by *day* rather than by
month — the only feature in the app that does. Entries land here two ways:

1. **Messaging a Telegram bot** — e.g. "paid 180 today" or "spent 200 on
   food yesterday" — then clicking **"🔄 Sync Telegram"** in the app.
2. **Directly via the API** (`POST /api/day-expenses`) — there's currently
   no manual-entry form in the UI (it was removed along with the old
   screenshot-upload feature), so this path only exists for anyone
   scripting against the API directly.

### Why Telegram sync needs a few extra pieces
A Telegram bot needs to be listening on an always-on machine to catch
messages at any time, but this app only runs on-demand (e.g. on a laptop).
Telegram itself only holds undelivered messages for 24 hours if you simply
poll for them, which risks silently losing entries — so instead:

```
Telegram message → Telegram's servers → instant webhook push
  → a small Cloudflare Worker (always-on, free — see
    telegram-webhook-worker.js) verifies it's really you and writes the
    raw message into a `pending_expenses` table in a free-tier Supabase
    Postgres project (the durable inbox — see supabase_setup.sql)
  → whenever Ledger is launched, clicking "Sync Telegram" calls
    telegram_sync.fetch_pending(), parses each message with
    agent.parse_day_expense(), inserts the result into the local
    day_expenses table, and calls telegram_sync.mark_processed() so the
    same message is never synced twice
```

`agent.parse_day_expense(text, reference_date)` resolves "today"/
"yesterday" against `reference_date` — the date the message was actually
**sent** (from Telegram's own timestamp) — not whenever the sync happens
to run, since those can be days apart. It also does simple keyword-based
category guessing (food/shopping/travel/fuel/rent/entertainment,
defaulting to "Uncategorized") and merchant extraction (an "at X" pattern),
and returns `None` if it can't find a usable amount, so a message with no
parseable amount is safely skipped rather than saved wrong.

To wire this up you need three things running, none of which live in this
repo's Python code:
1. A Telegram bot (create one via **@BotFather**, get its token and your
   own chat_id).
2. A free Supabase project — run `supabase_setup.sql` once in its SQL
   editor to create the `pending_expenses` table.
3. A Cloudflare Worker (free tier) running `telegram-webhook-worker.js`,
   registered as your bot's webhook via Telegram's `setWebhook` API.

Then set these two environment variables before starting the app:
```
export SUPABASE_URL="https://xxxx.supabase.co"
export SUPABASE_SERVICE_KEY="your_service_role_or_secret_key"
python app.py
```
If they're not set, `/api/telegram/sync` returns a clear error rather than
crashing. Use the `service_role`/`secret` key here, never the `anon`/
`publishable` one — the table has Row Level Security enabled with no
public policies, so only the private key can read/write it.

## Safe-to-Spend Daily Meter
`GET /api/budget/safe-to-spend` powers the dashboard's daily-allowance
meter. It takes this month's salary, subtracts fixed expenses and the
savings target (same savings-rate logic as "Spending insights" below),
then subtracts what's already been spent this month (purchases +
day-wise expenses combined) to get a remaining discretionary pool, split
across the days left in the month:

```
daily_safe_budget = remaining_pool / days_remaining_in_month
today_left        = daily_safe_budget − amount spent today
```

`today_left` is the headline number shown on the dashboard — it's more
actionable than a flat daily average since it accounts for what's already
gone out *today* specifically, and can go negative (shown in red) if
today's spending has already exceeded the daily allowance; it's never
silently clamped to zero. If salary isn't set, the endpoint returns
`configured: false` with a message pointing you to set it.

## Wishlist
The "Wishlist" panel (`GET`/`POST /api/wishlist`,
`DELETE /api/wishlist/<id>`) lets you save products you're saving up for.
Each item shows a projected number of months to afford it and a rough
target date, based on:

```
monthly_savings = monthly_salary − this month's fixed expenses − this month's purchases
months_to_afford = ceil(item_price / monthly_savings)
```

This is a live snapshot of the current month, not a rolling average, so the
estimate will move around as you log more expenses/purchases during the
month. If salary isn't set, or `monthly_savings` is zero or negative, the
item shows an explanatory note instead of a number.

## Spending insights
The "Spending insights" panel (button on the dashboard) calls
`GET /api/insights`, which gathers the current month's salary, savings
goal, purchases, and fixed-expense breakdown plus the last 6 months of
history, and asks Groq to judge the month and return:
- a **verdict** (`good` / `watch` / `overspending`),
- a short plain-language **summary**,
- 2-4 concrete **suggestions**,
- any **flagged categories** that look unusually large or are trending up,
- **current vs. target savings rate** (`current_savings_percent` /
  `savings_target_percent`),
- a **next-month budget** (separate purchases / fixed-expenses numbers).

### How the next-month budget is chosen
The budget isn't just "tighter if this month ran high" guesswork — it's
anchored to a savings *rate*:
1. **Your own savings goal**, set via the "Savings goal (% of salary)"
   field next to salary (`GET`/`POST /api/settings/savings-percent`). Leave
   it blank to clear it.
2. If you haven't set one, it falls back to the **50/30/20 rule** — a
   widely-cited budgeting guideline (50% needs, 30% wants, 20% savings) —
   using its 20% savings share as the default target.

Either way, that percentage of your salary is reserved for savings first;
the remainder is split between fixed expenses and purchases using the same
rule's 50:30 needs-to-wants proportion.

Like your salary, the savings goal is a single standing setting — once you
save it, it applies to every month until you change it again; there's no
separate value per month.

If Groq isn't configured or the call fails, `agent.analyze_spending()` falls
back to a rule-based analyzer that applies the same savings-rate logic
(spend vs. salary and savings goal, spend vs. recent average, and the
largest fixed-expense category), so the panel always returns something —
the response's `source` field (`"groq"` or `"rules"`) tells you which one
ran, same pattern as the expense parser's `"groq"` / `"regex"` badge.

## Data model
- `purchases(id, name, price, purchased_on, created_at)`
- `settings(key, value)` — `monthly_salary`, `savings_percent`, `profession`,
  and `interests`; each a single standing value that applies until updated
- `monthly_expenses(id, year, month, category, amount, raw_text, created_at)`
- `wishlist(id, name, price, created_at)`
- `day_expenses(id, date, merchant, category, amount, source, created_at)` —
  day-wise line items; `source` is `"telegram"` for entries synced from the
  Telegram inbox, or whatever a direct API caller sets otherwise
- `flights(id, origin, destination, departure_date, return_date, adults,
  travel_class, target_price, notify_email, current_price, lowest_price,
  active, created_at)` — one row per tracked route/date
- `flight_price_history(id, flight_id, price, checked_at)` — every fare
  check's result over time for a tracker, deleted automatically if the
  tracker itself is deleted

Not part of `ledger.db`: the Telegram sync feature's `pending_expenses`
table lives in a separate Supabase Postgres project (see "Day-wise
spending & Telegram sync" above) — it's a durable inbox, not part of this
app's own data.

## Extending it
- New agent behaviors go in `agent.py` (or new files alongside it, e.g.
  `agent_categorizer.py`) — keep `app.py` as thin routing/DB glue and let
  the agent module(s) own any LLM calls, prompts, and parsing logic.
  `flights.py` and `telegram_sync.py` follow the same pattern for
  non-LLM external services (SerpApi, Supabase) — each owns one external
  integration and exposes a small function-based interface, so `app.py`
  never talks to those APIs directly.
- Swap `GROQ_MODEL` or point `agent.GROQ_API_URL` at a different
  OpenAI-compatible provider if you want to try another model later.
- Swap SQLite for Postgres by changing the `sqlite3` calls in `app.py`;
  the schema is intentionally simple.