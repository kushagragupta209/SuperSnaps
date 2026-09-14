"""
agent.py — Ledger's natural-language expense-parsing agent.

Kept separate from app.py on purpose: this is where LLM-backed "agent"
behavior lives (today: parsing free-form monthly-expense text into
{category, amount} line items), so future agent features — e.g. auto-
categorizing purchases, spending-pattern nudges, a "cut my spending"
suggestion agent — can be added here without touching Flask routes.

Public interface used by app.py:
    parse_expense(text)      -> (items: list[dict], source: str)
    is_llm_configured()      -> bool

Everything else is an implementation detail of how a given item gets
{category, amount} pairs out of a sentence.
"""

import json
import os
import re

try:
    import requests
except ImportError:  # requests is in requirements.txt; guard just in case
    requests = None


# --------------------------------------------------------------------------- #
# Config — Groq (https://console.groq.com), OpenAI-compatible API, free tier
# available (rate-limited, no credit card required as of this writing).
# --------------------------------------------------------------------------- #
#
# Set this in your shell before running the app to enable LLM-based parsing:
#   export GROQ_API_KEY="gsk_..."
# Get a free key at https://console.groq.com/keys
#
# If the key isn't set, or the call fails for any reason (bad key, rate
# limit, network issue), parse_expense() automatically falls back to the
# local regex parser below, so callers never have to handle an LLM failure.

GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


def is_llm_configured() -> bool:
    """Whether a Groq API key is present (doesn't guarantee it's valid)."""
    return bool(os.environ.get("GROQ_API_KEY"))


# --------------------------------------------------------------------------- #
# Fallback: lightweight rule-based parser (no external API required)
# --------------------------------------------------------------------------- #
# Turns a sentence like:
#   "I spent 12k on rent, 5k for food and 3000 on fuel"
# into:
#   [{"category": "Rent", "amount": 12000}, {"category": "Food", "amount": 5000},
#    {"category": "Fuel", "amount": 3000}]

MULTIPLIERS = {
    "k": 1_000,
    "l": 100_000,
    "lac": 100_000,
    "lakh": 100_000,
    "m": 1_000_000,
}

STOPWORDS = {"and", "also", "plus", "then", "i", "spent", "spend", "paid", "on", "for", "towards"}

EXPENSE_PATTERN = re.compile(
    r"""
    (?P<amount>\d[\d,]*\.?\d*)          # 12,000 or 12000 or 12.5
    \s*
    (?P<mult>k|lakh|lac|l|m)?           # optional shorthand multiplier
    \b
    \s*(?:on|for|towards|:|-)?\s*       # connecting word
    (?P<category>[A-Za-z][A-Za-z\s]*?)  # category words
    (?=,|\band\b|\.|$)                  # stop at comma / "and" / period / end
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _parse_expense_regex(text: str):
    """Parse free-form monthly-expense text into [{category, amount}, ...]."""
    results = []
    for match in EXPENSE_PATTERN.finditer(text):
        amount_str = match.group("amount").replace(",", "")
        try:
            amount = float(amount_str)
        except ValueError:
            continue

        mult = (match.group("mult") or "").lower()
        amount *= MULTIPLIERS.get(mult, 1)

        category = match.group("category").strip().lower()
        category = re.sub(r"\s+", " ", category)
        # Strip leading stopwords that slipped in (e.g. "and food" -> "food")
        words = [w for w in category.split(" ") if w not in STOPWORDS]
        category = " ".join(words).strip()

        if not category or amount <= 0:
            continue

        results.append({"category": category.title(), "amount": round(amount, 2)})

    return results


# --------------------------------------------------------------------------- #
# Preferred: Groq-hosted LLM parser
# --------------------------------------------------------------------------- #

LLM_SYSTEM_PROMPT = """You extract monthly expense line items from a sentence about personal spending.

Respond with ONLY a JSON object (no prose, no markdown fences) shaped like:
{"items": [{"category": "Rent", "amount": 12000}, {"category": "Food", "amount": 5000}]}

Rules:
- "category" is a short, title-cased label (e.g. "Rent", "Food", "Fuel", "Internet").
- "amount" is a plain number in the base currency unit (no symbols, no commas).
  Expand shorthand: 12k -> 12000, 1.5 lakh -> 150000, 2m -> 2000000.
- If the same category appears more than once, sum it into one entry.
- If you can't find any clear amount+category pairs, return {"items": []}.
- Never include any text outside the JSON object.
"""


def _parse_expense_llm(text: str):
    """
    Ask Groq to extract {category, amount} pairs from free-form text.
    Returns None (not []) if the LLM path could not be used at all, so the
    caller knows to fall back to the regex parser instead of trusting an
    empty result.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key or requests is None:
        return None

    try:
        response = requests.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": LLM_SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
            },
            timeout=15,
        )
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"].strip()
        # Some models wrap JSON in ```json fences despite instructions — strip them.
        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        items = data.get("items", []) if isinstance(data, dict) else data

        cleaned = []
        for item in items:
            category = str(item.get("category", "")).strip().title()
            try:
                amount = round(float(item.get("amount")), 2)
            except (TypeError, ValueError):
                continue
            if category and amount > 0:
                cleaned.append({"category": category, "amount": amount})
        return cleaned

    except Exception:  # noqa: BLE001 - any network/HTTP/parse failure -> fallback
        return None


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def parse_expense(text: str):
    """
    Try the Groq parser first; fall back to the local regex parser if the
    API key isn't set or the call fails for any reason.

    Returns:
        (items, source) where source is "groq" or "regex".
    """
    llm_result = _parse_expense_llm(text)
    if llm_result is not None:
        return llm_result, "groq"
    return _parse_expense_regex(text), "regex"


# --------------------------------------------------------------------------- #
# Telegram day-expense parsing — "paid 180 today" / "paid 200 yesterday"
# --------------------------------------------------------------------------- #
# Turns a short Telegram message into a single day_expenses line item.
# Unlike parse_expense() above (monthly recurring line items, no date),
# this always resolves a concrete calendar date. Critically, "today" /
# "yesterday" are resolved against the date the message was actually
# SENT (reference_date), not whenever Ledger happens to sync the
# Telegram inbox — those can be days apart, since Ledger only runs
# on-demand. See telegram_sync.py for where reference_date comes from.

from datetime import date as _date, timedelta as _timedelta

DAY_EXPENSE_CATEGORY_KEYWORDS = {
    "Food": ["food", "grocery", "groceries", "restaurant", "dining", "zomato",
             "swiggy", "lunch", "dinner", "breakfast"],
    "Shopping": ["shopping", "amazon", "flipkart", "clothes", "clothing"],
    "Travel": ["travel", "flight", "hotel", "cab", "uber", "ola", "auto"],
    "Fuel": ["fuel", "petrol", "diesel"],
    "Rent": ["rent"],
    "Entertainment": ["entertainment", "movie", "movies", "netflix", "spotify"],
}

_DAY_EXPENSE_AMOUNT_RE = re.compile(
    r"(?P<amount>\d[\d,]*\.?\d*)\s*(?P<mult>k|lakh|lac|l|m)?", re.IGNORECASE
)


def parse_day_expense(text: str, reference_date=None):
    """
    Parse a short free-form message like "paid 180 today" or "spent 200
    on food yesterday" into a single day-expense line item.

    reference_date: the date the message was actually sent — a date
    object, or "YYYY-MM-DD"/full-ISO string (only the first 10 chars are
    read). Defaults to today if not given. "today"/"yesterday" in the
    text resolve relative to THIS date.

    Returns None if no usable amount is found. Otherwise:
        {"amount": float, "date": "YYYY-MM-DD", "category": str, "merchant": str|None}
    """
    if not text or not text.strip():
        return None

    if reference_date is None:
        ref = _date.today()
    elif isinstance(reference_date, str):
        try:
            ref = _date.fromisoformat(reference_date[:10])
        except ValueError:
            ref = _date.today()
    else:
        ref = reference_date

    lowered = text.lower()

    amount = None
    match = _DAY_EXPENSE_AMOUNT_RE.search(lowered)
    if match:
        try:
            amount = float(match.group("amount").replace(",", ""))
        except ValueError:
            amount = None
        if amount is not None:
            mult = (match.group("mult") or "").lower()
            amount *= MULTIPLIERS.get(mult, 1)

    if not amount or amount <= 0:
        return None

    explicit_date = None
    date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if date_match:
        try:
            explicit_date = _date.fromisoformat(date_match.group(1))
        except ValueError:
            explicit_date = None

    if explicit_date:
        resolved_date = explicit_date
    elif "day before yesterday" in lowered:
        resolved_date = ref - _timedelta(days=2)
    elif "yesterday" in lowered:
        resolved_date = ref - _timedelta(days=1)
    else:
        # "today", or no date word at all -> the day the message was sent
        resolved_date = ref

    category = "Uncategorized"
    for label, words in DAY_EXPENSE_CATEGORY_KEYWORDS.items():
        if any(w in lowered for w in words):
            category = label
            break

    merchant = None
    merchant_match = re.search(
        r"\bat\s+([A-Z][A-Za-z0-9&']*(?:\s+[A-Z][A-Za-z0-9&']*)*)", text
    )
    if merchant_match:
        merchant = merchant_match.group(1).strip()

    return {
        "amount": round(amount, 2),
        "date": resolved_date.isoformat(),
        "category": category,
        "merchant": merchant,
    }


# --------------------------------------------------------------------------- #
# Product recommendations
# --------------------------------------------------------------------------- #
# Given someone's purchase history + (optionally) their job/field of work,
# suggest a handful of products they might want to buy next. Same
# Groq-first / local-fallback shape as parse_expense above.
#
# Public interface used by app.py:
#     recommend_products(purchases, profession, interests) -> (items, source)

RECS_SYSTEM_PROMPT = """You are a thoughtful shopping assistant inside a personal finance app.
Given someone's past purchases and (optionally) their job/field of work and stated
interests, suggest products they might genuinely want to buy next.

Respond with ONLY a JSON object (no prose, no markdown fences) shaped like:
{"items": [{"name": "Mechanical keyboard", "reason": "Pairs with your recent monitor purchase and suits software work", "category": "Tech", "est_price": 4500}]}

Rules:
- Suggest at most 5 items, ordered by how relevant/useful they are.
- "reason" is one short sentence tying the suggestion to their purchases, job, or interests.
- "category" is a short label (e.g. "Tech", "Home", "Fitness", "Books", "Kitchen").
- "est_price" is a realistic estimated price as a plain number (base currency unit, no symbols/commas).
- Do not repeat products they've already bought.
- Prefer specific, concrete product names over vague categories.
- If there isn't enough information to make a sensible suggestion, return {"items": []}.
- Never include any text outside the JSON object.
"""


def _recommend_products_llm(purchases, profession, interests):
    """
    Ask Groq for product recommendations. Returns None (not []) if the LLM
    path could not be used at all, so the caller falls back to the local
    heuristic instead of trusting an empty result.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key or requests is None:
        return None

    purchase_lines = [f"- {p['name']} (₹{p['price']})" for p in purchases] or ["(no purchases logged yet)"]
    user_context = (
        f"Job / field of work: {profession or 'not specified'}\n"
        f"Stated interests: {interests or 'not specified'}\n"
        f"Recent purchases:\n" + "\n".join(purchase_lines)
    )

    try:
        response = requests.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "temperature": 0.4,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": RECS_SYSTEM_PROMPT},
                    {"role": "user", "content": user_context},
                ],
            },
            timeout=15,
        )
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"].strip()
        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        items = data.get("items", []) if isinstance(data, dict) else data

        cleaned = []
        for item in items:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            reason = str(item.get("reason", "")).strip()
            category = str(item.get("category", "")).strip().title() or "General"
            try:
                est_price = round(float(item.get("est_price")), 2)
            except (TypeError, ValueError):
                est_price = None
            cleaned.append({
                "name": name, "reason": reason, "category": category, "est_price": est_price,
            })
        return cleaned[:5]

    except Exception:  # noqa: BLE001 - any network/HTTP/parse failure -> fallback
        return None


# Static, dependency-free fallback used when Groq isn't configured. Maps
# keywords found in someone's job title / interests / past purchase names to
# a short list of candidate next-purchases. Deliberately simple — this is a
# "something sensible to show" fallback, not a real recommender.
_PROFESSION_RULES = [
    (("software", "developer", "engineer", "programmer", "coder", "it "),
     [("Mechanical keyboard", "Tech", 4500), ("Second monitor", "Tech", 9000),
      ("Ergonomic chair", "Home", 12000), ("Noise-cancelling headphones", "Tech", 6000)]),
    (("design", "designer", "ui", "ux", "artist", "creative"),
     [("Drawing tablet", "Tech", 8000), ("Color-accurate monitor", "Tech", 18000),
      ("Desk lamp with adjustable tone", "Home", 2500)]),
    (("teacher", "professor", "educator", "academic", "researcher"),
     [("E-reader", "Books", 9000), ("Portable whiteboard", "Home", 1500),
      ("Noise-cancelling headphones", "Tech", 6000)]),
    (("student",),
     [("Laptop stand", "Tech", 1200), ("Backpack", "Fashion", 2500),
      ("E-reader", "Books", 9000)]),
    (("finance", "accountant", "banker", "analyst", "consultant"),
     [("Second monitor", "Tech", 9000), ("Noise-cancelling headphones", "Tech", 6000),
      ("Ergonomic chair", "Home", 12000)]),
    (("doctor", "nurse", "healthcare", "physician", "medical"),
     [("Compression socks", "Health", 800), ("Smartwatch", "Tech", 8000),
      ("Insulated water bottle", "Health", 900)]),
    (("sales", "marketing", "manager", "business"),
     [("Noise-cancelling headphones", "Tech", 6000), ("Carry-on suitcase", "Travel", 7000),
      ("Portable charger", "Tech", 1500)]),
    (("fitness", "gym", "trainer", "athlete", "sports"),
     [("Resistance bands set", "Fitness", 1200), ("Smartwatch", "Tech", 8000),
      ("Foam roller", "Fitness", 900)]),
]

# Complementary-purchase pairs: if a past purchase name contains the key,
# suggest the paired items (skipped if already purchased).
_COMPLEMENT_RULES = [
    ("headphone", [("Headphone stand", "Home", 700), ("Bluetooth adapter", "Tech", 900)]),
    ("laptop", [("Laptop sleeve", "Tech", 1200), ("Laptop stand", "Tech", 1200)]),
    ("monitor", [("Monitor arm mount", "Home", 2500), ("HDMI cable", "Tech", 400)]),
    ("phone", [("Phone case", "Tech", 800), ("Screen protector", "Tech", 300)]),
    ("camera", [("Memory card", "Tech", 1200), ("Camera bag", "Tech", 2500)]),
    ("bicycle", [("Bike helmet", "Fitness", 1500), ("Bike lock", "Fitness", 900)]),
    ("cycle", [("Bike helmet", "Fitness", 1500), ("Bike lock", "Fitness", 900)]),
    ("shoe", [("Insoles", "Fitness", 600), ("Shoe cleaning kit", "Fitness", 400)]),
    ("book", [("Reading lamp", "Home", 1200), ("Bookshelf", "Home", 5000)]),
]


def _recommend_products_fallback(purchases, profession, interests):
    already_bought = {p["name"].strip().lower() for p in purchases}
    profession_l = (profession or "").lower()
    interests_l = (interests or "").lower()
    combined_context = f"{profession_l} {interests_l}"

    candidates = []

    for keywords, suggestions in _PROFESSION_RULES:
        if any(kw in combined_context for kw in keywords):
            candidates.extend(suggestions)

    for p in purchases:
        name_l = p["name"].lower()
        for keyword, suggestions in _COMPLEMENT_RULES:
            if keyword in name_l:
                candidates.extend(suggestions)

    # Generic catch-all if we found nothing specific at all.
    if not candidates:
        candidates = [
            ("Noise-cancelling headphones", "Tech", 6000),
            ("Smartwatch", "Tech", 8000),
            ("Insulated water bottle", "Health", 900),
        ]

    seen = set()
    results = []
    for name, category, est_price in candidates:
        key = name.strip().lower()
        if key in already_bought or key in seen:
            continue
        seen.add(key)
        reason = "Fits your role/interests" if profession or interests else "Commonly useful next purchase"
        results.append({"name": name, "reason": reason, "category": category, "est_price": est_price})
        if len(results) >= 5:
            break

    return results


def recommend_products(purchases, profession="", interests=""):
    """
    Try the Groq recommender first; fall back to the local rule-based
    recommender if the API key isn't set or the call fails for any reason.

    purchases: list of {"name": str, "price": number} (most recent first is fine)

    Returns:
        (items, source) where source is "groq" or "rules".
    """
    llm_result = _recommend_products_llm(purchases, profession, interests)
    if llm_result is not None:
        return llm_result, "groq"
    return _recommend_products_fallback(purchases, profession, interests), "rules"


# --------------------------------------------------------------------------- #
# Spending insights — "is this good, and what should next month look like"
# --------------------------------------------------------------------------- #
# Given a snapshot of the current month's numbers (salary, purchases, fixed
# expenses by category) plus a short recent history, ask the LLM to judge
# whether spending looks healthy and propose a next-month budget. Falls back
# to a simple rule-based judgment on the same shape of data if the LLM path
# isn't available, so callers get a consistent response shape either way:
#
#   {
#     "verdict": "good" | "watch" | "overspending" | "unknown",
#     "summary": str,
#     "suggestions": [str, ...],
#     "flagged_categories": [{"category": str, "note": str}, ...],
#     "next_month_budget": {"purchases": number, "fixed_expenses": number},
#     "savings_target_percent": number,
#     "current_savings_percent": number | None,
#   }
#
# The next-month budget is anchored to a savings *rate*, not to trend alone:
# either the user's own savings-goal setting (app.py's /api/settings/
# savings-percent), or — if they haven't set one — the widely-cited 50/30/20
# budgeting rule (50% needs, 30% wants, 20% savings), used here as the
# "globally recognized" default reference point.

DEFAULT_SAVINGS_PERCENT = 20.0   # the 50/30/20 rule's savings share
NEEDS_RATIO = 50 / 80            # needs:wants is 50:30 in that rule; rescaled
WANTS_RATIO = 30 / 80            # below to whatever % of salary isn't earmarked for savings

INSIGHTS_SYSTEM_PROMPT = f"""You are a plain-spoken personal-finance reviewer looking at one \
person's spending data for a single month, plus their last few months for trend.

You will receive a JSON object with: monthly_salary (may be null — treat it as the \
person's standing monthly salary, unchanged unless they update it), the current \
year/month, month_purchases (discretionary buying), fixed_total and fixed_expenses \
(a breakdown of recurring costs like rent/food/fuel for the month), ytd_purchases, \
history_last_6_months (a list of {{year, month, purchases, fixed_expenses}} for recent \
months, oldest first, current month included), savings_percent (the person's own \
savings-rate goal as a % of salary — may be null if they haven't set one), and \
recommended_savings_percent ({DEFAULT_SAVINGS_PERCENT:.0f}, the savings share from the \
well-known 50/30/20 budgeting rule, to use as the reference when savings_percent is null).

Respond with ONLY a JSON object (no prose, no markdown fences) shaped exactly like:
{{
  "verdict": "good" | "watch" | "overspending" | "unknown",
  "summary": "2-3 plain-language sentences on whether this month's spending looks healthy relative to salary, savings goal, and recent months.",
  "suggestions": ["short, specific, actionable tip", "..."],
  "flagged_categories": [{{"category": "Food", "note": "why this category stands out and what to do about it"}}],
  "next_month_budget": {{"purchases": <number>, "fixed_expenses": <number>}},
  "savings_target_percent": <number>,
  "current_savings_percent": <number or null>
}}

Rules:
- Base every judgment strictly on the numbers given. Never invent categories, amounts, or facts not present in the input.
- Treat a history month with 0 purchases and 0 fixed_expenses as *no data logged for that month*, not as "spent nothing" — don't use such months in an average or a trend comparison, and don't state a percentage change against them. If fewer than 2 prior months have real (non-zero) data, say plainly that there isn't enough history yet instead of computing a trend.
- "savings_target_percent" is savings_percent if it's set, otherwise recommended_savings_percent. Always echo this number back.
- "current_savings_percent" is (monthly_salary - (month_purchases + fixed_total)) / monthly_salary * 100, rounded to one decimal — or null if monthly_salary is null.
- Use "unknown" for verdict only if monthly_salary is null and history is empty, since there's nothing to judge against.
- "good" means current_savings_percent meets or beats savings_target_percent (or, if salary is null, spend looks stable vs. history).
- "watch" means current_savings_percent is a bit below target, or a specific category is creeping up.
- "overspending" means current_savings_percent is well below target (e.g. saving little or nothing, or going negative), or this month's total clearly outpaces recent months.
- flagged_categories should only include categories that are unusually large relative to the rest of fixed_expenses or that jumped compared to history — omit this key or use [] if nothing stands out.
- next_month_budget must be absolute numbers in the same currency unit as the input, not percentages, and should be built by reserving savings_target_percent of monthly_salary for savings first, then splitting the remainder between fixed_expenses and purchases roughly in the 50:30 needs-to-wants proportion the 50/30/20 rule uses (i.e. fixed_expenses gets 5/8 of the non-savings remainder, purchases gets 3/8), adjusted only slightly for what's realistic given the categories actually logged.
  If monthly_salary is null, size next_month_budget from history/this month's own numbers instead, since there's no salary to anchor a savings rate to.
- suggestions should be 2-4 short, concrete, specific items tied to the savings-goal gap (if any) or the flagged categories — not generic "save more" platitudes.
- Never include any text outside the JSON object, and never add keys beyond the ones shown above.
"""


def _analyze_spending_llm(context: dict):
    """
    Ask Groq to judge a month's spending and propose a next-month budget.
    Returns None if the LLM path could not be used at all (missing key,
    network/parsing failure), so the caller falls back to the rule-based
    analyzer instead of trusting a partial/empty result.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key or requests is None:
        return None

    try:
        response = requests.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "temperature": 0.3,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": INSIGHTS_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(context)},
                ],
            },
            timeout=20,
        )
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"].strip()
        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None

        return _clean_insights(data)

    except Exception:  # noqa: BLE001 - any network/HTTP/parse failure -> fallback
        return None


def _clean_insights(data: dict) -> dict:
    """Normalize/sanitize an insights payload, whichever source produced it."""
    verdict = str(data.get("verdict", "unknown")).strip().lower()
    if verdict not in {"good", "watch", "overspending", "unknown"}:
        verdict = "unknown"

    summary = str(data.get("summary", "")).strip()

    suggestions = data.get("suggestions") or []
    suggestions = [str(s).strip() for s in suggestions if str(s).strip()][:6]

    flagged = data.get("flagged_categories") or []
    flagged_clean = []
    for item in flagged:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip()
        note = str(item.get("note", "")).strip()
        if category:
            flagged_clean.append({"category": category, "note": note})

    budget = data.get("next_month_budget") or {}
    next_budget = {}
    for key in ("purchases", "fixed_expenses"):
        try:
            next_budget[key] = round(float(budget.get(key)), 2)
        except (TypeError, ValueError):
            next_budget[key] = None

    try:
        savings_target = round(float(data.get("savings_target_percent")), 1)
    except (TypeError, ValueError):
        savings_target = DEFAULT_SAVINGS_PERCENT

    current_savings = data.get("current_savings_percent")
    try:
        current_savings = round(float(current_savings), 1) if current_savings is not None else None
    except (TypeError, ValueError):
        current_savings = None

    return {
        "verdict": verdict,
        "summary": summary,
        "suggestions": suggestions,
        "flagged_categories": flagged_clean[:6],
        "next_month_budget": next_budget,
        "savings_target_percent": savings_target,
        "current_savings_percent": current_savings,
    }


def _analyze_spending_rules(context: dict) -> dict:
    """
    Lightweight fallback used when Groq isn't configured or fails: no
    natural-language nuance, but the next-month budget is still anchored to
    a savings rate — the user's own goal (settings.savings_percent) if set,
    otherwise the 50/30/20 rule's 20% default — rather than just trend math.
    """
    salary = context.get("monthly_salary")
    month_purchases = float(context.get("month_purchases") or 0)
    fixed_total = float(context.get("fixed_total") or 0)
    fixed_expenses = context.get("fixed_expenses") or []
    history = context.get("history_last_6_months") or []
    total_spend = month_purchases + fixed_total

    savings_target_percent = context.get("savings_percent")
    if savings_target_percent is None:
        savings_target_percent = DEFAULT_SAVINGS_PERCENT
    else:
        savings_target_percent = float(savings_target_percent)

    pct_of_salary = (total_spend / salary * 100) if salary else None
    current_savings_percent = round(100 - pct_of_salary, 1) if pct_of_salary is not None else None

    # Trend vs. prior months (exclude current month, which is last in the list).
    # Only count a prior month as real data if something was actually logged
    # for it — an empty/unlogged month is missing data, not "spent ₹0", and
    # averaging those in would make the trend swing wildly (or blow up to
    # absurd percentages when the average collapses toward zero). Require at
    # least 2 logged prior months before trusting a trend comparison at all.
    prior = history[:-1] if len(history) > 1 else []
    logged_prior = [
        h for h in prior
        if (h.get("purchases", 0) or 0) + (h.get("fixed_expenses", 0) or 0) > 0
    ]
    prior_avg = None
    if len(logged_prior) >= 2:
        prior_avg = sum(
            (h.get("purchases", 0) or 0) + (h.get("fixed_expenses", 0) or 0) for h in logged_prior
        ) / len(logged_prior)

    # Verdict: primarily how current_savings_percent compares to the savings
    # goal (the reliable, salary-anchored signal); a valid trend can only
    # escalate it further, and is ignored entirely without enough history.
    if current_savings_percent is None and prior_avg is None:
        verdict = "unknown"
    else:
        verdict = "good"
        if current_savings_percent is not None:
            gap = savings_target_percent - current_savings_percent
            if gap > 15:
                verdict = "overspending"
            elif gap > 5:
                verdict = "watch"
        if prior_avg:
            if total_spend > prior_avg * 1.5:
                verdict = "overspending"
            elif total_spend > prior_avg * 1.25 and verdict == "good":
                verdict = "watch"

    parts = []
    if current_savings_percent is not None:
        parts.append(
            f"You're on track to save about {current_savings_percent:.0f}% of your salary this "
            f"month, versus a {savings_target_percent:.0f}% goal."
        )
    if prior_avg:
        diff_pct = (total_spend - prior_avg) / prior_avg * 100
        direction = "higher" if diff_pct > 0 else "lower"
        parts.append(f"That's roughly {abs(diff_pct):.0f}% {direction} than your recent monthly average.")
    elif len(logged_prior) < 2:
        parts.append("Not enough consistent monthly history yet to compare against past trends.")
    if not parts:
        parts.append("Not enough salary or history data yet to judge this month confidently.")
    summary = " ".join(parts)

    suggestions = []
    if verdict == "overspending":
        suggestions.append(
            f"You're saving well under your {savings_target_percent:.0f}% goal this month — pause "
            "non-essential purchases for the rest of the month to close the gap."
        )
        suggestions.append("Review the largest fixed-expense category below for anything that can be trimmed or renegotiated.")
    elif verdict == "watch":
        suggestions.append(f"You're a bit short of your {savings_target_percent:.0f}% savings goal — keep an eye on discretionary purchases.")
    else:
        suggestions.append("Spending looks steady and on pace with your savings goal — keep tracking to catch changes early.")
    if salary is None:
        suggestions.append("Add your monthly salary so insights can be measured against your income and savings goal.")
    if context.get("savings_percent") is None and salary is not None:
        suggestions.append(
            f"No savings goal set — using the 50/30/20 rule's {DEFAULT_SAVINGS_PERCENT:.0f}% default. "
            "Set your own target in Settings if you'd like something different."
        )

    flagged = []
    if fixed_expenses and fixed_total > 0:
        for row in fixed_expenses:
            amount = float(row.get("amount") or 0)
            if amount > 0.4 * fixed_total:
                flagged.append({
                    "category": row.get("category", ""),
                    "note": f"Makes up {amount / fixed_total * 100:.0f}% of this month's fixed expenses — the single biggest share.",
                })

    # Next-month budget: reserve the savings target off salary first, then
    # split what's left between fixed expenses and purchases using the
    # 50/30/20 rule's 50:30 (5:8 / 3:8) needs-to-wants proportion. Falls back
    # to sizing off this month/history when salary isn't set, since there's
    # no salary to anchor a savings rate to.
    if salary:
        remaining_pct = max(0.0, 100 - savings_target_percent)
        next_budget = {
            "purchases": round(salary * remaining_pct / 100 * WANTS_RATIO, 2),
            "fixed_expenses": round(salary * remaining_pct / 100 * NEEDS_RATIO, 2),
        }
    else:
        if verdict == "overspending":
            target_total = total_spend * 0.85
        elif verdict == "watch":
            target_total = total_spend * 0.95
        else:
            target_total = total_spend if total_spend > 0 else 0
        purchase_share = (month_purchases / total_spend) if total_spend > 0 else 0.5
        next_budget = {
            "purchases": round(target_total * purchase_share, 2),
            "fixed_expenses": round(target_total * (1 - purchase_share), 2),
        }

    return {
        "verdict": verdict,
        "summary": summary,
        "suggestions": suggestions[:6],
        "flagged_categories": flagged[:6],
        "next_month_budget": next_budget,
        "savings_target_percent": round(savings_target_percent, 1),
        "current_savings_percent": current_savings_percent,
    }


def analyze_spending(context: dict):
    """
    Try the Groq analyzer first; fall back to a rule-based judgment on the
    same data if the API key isn't set or the call fails for any reason.

    `context` is a plain-JSON-serializable dict — see INSIGHTS_SYSTEM_PROMPT
    for the expected shape (built by app.py's /api/insights route).

    Returns:
        (insights, source) where source is "groq" or "rules".
    """
    llm_result = _analyze_spending_llm(context)
    if llm_result is not None:
        return llm_result, "groq"
    return _analyze_spending_rules(context), "rules"

# --------------------------------------------------------------------------- #
# Agent 1: Autonomous "Spend Cutter" & Budget Rebalancing Agent
# --------------------------------------------------------------------------- #

SPEND_CUTTER_SYSTEM_PROMPT = """You are an assertive personal financial optimization agent.
Given a user's salary, savings target, current fixed expenses breakdown, and recent day/timeline purchases, 
formulate an aggressive yet realistic spend-cutting and budget rebalancing plan.

Respond with ONLY a JSON object (no prose, no markdown fences) shaped like:
{
  "monthly_savings_boost": 4500,
  "action_plan": [
    "Trim Food delivery by ₹2,000 by limiting quick commerce orders to 3x weekly",
    "Negotiate or pause unused subscriptions for an extra ₹1,000/month"
  ],
  "rebalanced_fixed_expenses": [
    {"category": "Food", "original_amount": 7000, "proposed_amount": 5000, "cut_amount": 2000, "rationale": "High delivery leak"}
  ],
  "new_projected_savings_rate": 28.5
}
"""

def generate_spend_cut_plan(context: dict):
    """Generate a structured, actionable spend-cutting and budget rebalancing plan."""
    api_key = os.environ.get("GROQ_API_KEY")
    if api_key and requests is not None:
        try:
            response = requests.post(
                GROQ_API_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": GROQ_MODEL,
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": SPEND_CUTTER_SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(context)}
                    ],
                },
                timeout=18,
            )
            raw = re.sub(r"^```(json)?|```$", "", response.json()["choices"][0]["message"]["content"].strip(), flags=re.MULTILINE).strip()
            data = json.loads(raw)
            if isinstance(data, dict):
                return data, "groq"
        except Exception:
            pass

    # Heuristic fallback
    fixed = context.get("fixed_expenses") or []
    rebalanced = []
    total_cut = 0.0
    for row in fixed:
        amt = float(row.get("amount") or 0)
        cat = str(row.get("category", "")).strip()
        if amt > 2000 and cat.lower() in {"food", "shopping", "entertainment", "dining", "groceries"}:
            cut = round(amt * 0.20, 2)
            total_cut += cut
            rebalanced.append({
                "category": cat,
                "original_amount": amt,
                "proposed_amount": round(amt - cut, 2),
                "cut_amount": cut,
                "rationale": f"Reduce {cat} by 20% to free up cash flow."
            })

    salary = float(context.get("monthly_salary") or 0)
    current_savings = float(context.get("current_savings") or 0)
    new_savings = current_savings + total_cut
    new_rate = round((new_savings / salary * 100), 1) if salary > 0 else 20.0

    return {
        "monthly_savings_boost": total_cut,
        "action_plan": [
            f"Trim non-essential category spends to recover ~₹{total_cut:,.0f}/month.",
            "Enforce a zero-impulse purchase window for discretionary items over ₹1,000."
        ],
        "rebalanced_fixed_expenses": rebalanced,
        "new_projected_savings_rate": new_rate
    }, "rules"


# --------------------------------------------------------------------------- #
# Agent 2: Autonomous Purchase Negotiator & Deal Evaluator
# --------------------------------------------------------------------------- #

DEAL_EVALUATOR_SYSTEM_PROMPT = """You are an autonomous shopping deal evaluator and purchase negotiator.
Analyze a wishlist item (name, target price, estimated monthly savings runway).
Provide realistic market intelligence:
1. Target buy price based on typical discount cycles.
2. Timing advice (e.g. wait for upcoming seasonal sales vs buy now).
3. Value alternatives or refurbished / lower-tier models.
4. Negotiation or coupon strategies.

Respond with ONLY a JSON object (no prose, no markdown fences) shaped like:
{
  "item_name": "Sony WH-1000XM5",
  "target_deal_price": 18500,
  "potential_savings": 1500,
  "verdict": "Wait for Sale" | "Buy Now" | "Look for Refurbished",
  "timing_recommendation": "Wait for upcoming festive/Prime sales for an estimated ₹1,500-₹2,500 drop.",
  "smart_alternatives": [
    {"name": "Sony WH-1000XM4", "est_price": 14999, "advantage": "90% of sound quality at 30% lower cost"}
  ],
  "haggling_tip": "Check card instant discount offers (HDFC/ICICI) or Amazon renewed section."
}
"""

def evaluate_deal(item_name: str, target_price: float, monthly_savings: float):
    """Analyze deal viability, alternatives, and price reduction tactics for a wishlist item."""
    api_key = os.environ.get("GROQ_API_KEY")
    if api_key and requests is not None:
        try:
            user_payload = {
                "item_name": item_name,
                "current_target_price": target_price,
                "user_monthly_savings": monthly_savings
            }
            response = requests.post(
                GROQ_API_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": GROQ_MODEL,
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": DEAL_EVALUATOR_SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(user_payload)}
                    ],
                },
                timeout=18,
            )
            raw = re.sub(r"^```(json)?|```$", "", response.json()["choices"][0]["message"]["content"].strip(), flags=re.MULTILINE).strip()
            data = json.loads(raw)
            if isinstance(data, dict):
                return data, "groq"
        except Exception:
            pass

    # Heuristic fallback
    est_discount = round(target_price * 0.12, 2)
    return {
        "item_name": item_name,
        "target_deal_price": round(target_price - est_discount, 2),
        "potential_savings": est_discount,
        "verdict": "Wait for Sale" if target_price > 3000 else "Buy Now",
        "timing_recommendation": "Target major online sales for a typical 10-15% discount window.",
        "smart_alternatives": [
            {"name": f"Certified Pre-Owned {item_name}", "est_price": round(target_price * 0.75, 2), "advantage": "25% savings on certified refurbished units."}
        ],
        "haggling_tip": "Apply bank card reward points or trade-in exchange bonuses."
    }, "rules"

# --------------------------------------------------------------------------- #
# Financial Database Chatbot (Text-to-SQL + Natural Language Reply)
# --------------------------------------------------------------------------- #

CHATBOT_SYSTEM_PROMPT = """You are an intelligent financial data assistant for the Ledger personal finance app.
You have access to a local SQLite database with this schema:
- purchases(id, name, price, purchased_on, created_at)
- settings(key, value) [e.g. 'monthly_salary', 'savings_percent', 'profession', 'interests']
- monthly_expenses(id, year, month, category, amount, raw_text, created_at)
- wishlist(id, name, price, created_at)
- day_expenses(id, date, merchant, category, amount, source, created_at)

When the user asks a question, write a SINGLE valid SQLite SELECT query to fetch the necessary data.
Rules:
- Respond with ONLY a JSON object: {"sql": "SELECT ..."}
- ONLY generate SELECT queries (NO INSERT, UPDATE, DELETE, DROP).
- Use SQLite functions (e.g., SUM, AVG, COUNT, strftime, LIKE).
- If the question does not require database querying, return {"sql": null, "direct_reply": "..."}
- Never include any markdown fences or extra text outside JSON.
"""

def _chat_local_sql(user_message: str):
    """Best-effort local Text-to-SQL fallback when Groq is unavailable."""
    text = re.sub(r"\s+", " ", (user_message or "").strip().lower())
    if not text:
        return None

    category_map = {
        "food": ["food", "grocery", "groceries", "restaurant", "dining", "zomato", "swiggy"],
        "shopping": ["shopping", "amazon", "flipkart", "clothes", "clothing"],
        "travel": ["travel", "flight", "hotel", "cab", "uber", "ola"],
        "fuel": ["fuel", "petrol", "diesel"],
        "rent": ["rent"],
        "entertainment": ["entertainment", "movie", "movies", "netflix", "spotify"],
    }
    category = next((k for k, words in category_map.items() if any(w in text for w in words)), None)

    month_nums = {m: i for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"], 1)}
    month = next((n for name, n in month_nums.items() if name in text), None)
    year_match = re.search(r"\b(20\d{2})\b", text)
    year = int(year_match.group(1)) if year_match else None

    purchase_date = ""
    fixed_date = ""
    day_date = ""
    values = []
    if month and year:
        purchase_date = " AND purchased_on LIKE ?"
        fixed_date = " AND year = ? AND month = ?"
        day_date = " AND date LIKE ?"
        values.extend([f"{year:04d}-{month:02d}-%", year, month, f"{year:04d}-{month:02d}-%"])
    elif month:
        purchase_date = " AND strftime('%m', purchased_on) = ?"
        fixed_date = " AND month = ?"
        day_date = " AND strftime('%m', date) = ?"
        values.extend([f"{month:02d}", month, f"{month:02d}"])
    elif "this month" in text:
        from datetime import date as _date
        today = _date.today()
        purchase_date = " AND purchased_on LIKE ?"
        fixed_date = " AND year = ? AND month = ?"
        day_date = " AND date LIKE ?"
        values.extend([f"{today.year:04d}-{today.month:02d}-%", today.year, today.month, f"{today.year:04d}-{today.month:02d}-%"])

    wants_total = any(w in text for w in ["total", "how much", "spent", "spending", "expense", "expenses"])
    wants_average = any(w in text for w in ["average", "avg", "mean"])
    wants_count = any(w in text for w in ["how many", "count", "number of"])
    wants_top = any(w in text for w in ["highest", "largest", "biggest", "most expensive", "top"])
    wants_recent = any(w in text for w in ["recent", "latest", "last purchases"])

    # Category questions search both purchase names and explicit expense categories.
    cat_purchase = cat_fixed = cat_day = ""
    cat_values = []
    if category:
        cat_purchase = " AND LOWER(name) LIKE ?"
        cat_fixed = " AND LOWER(category) LIKE ?"
        cat_day = " AND LOWER(category) LIKE ?"
        cat_values = [f"%{category}%", f"%{category}%", f"%{category}%"]

    if wants_average:
        sql = f"SELECT ROUND(COALESCE(AVG(price), 0), 2) AS average_spending FROM purchases WHERE 1=1{purchase_date}{cat_purchase}"
        vals = ([values[0]] if purchase_date else []) + ([cat_values[0]] if category else [])
    elif wants_count:
        sql = f"SELECT COUNT(*) AS count FROM purchases WHERE 1=1{purchase_date}{cat_purchase}"
        vals = ([values[0]] if purchase_date else []) + ([cat_values[0]] if category else [])
    elif wants_top or wants_recent:
        order = "price DESC" if wants_top else "purchased_on DESC, id DESC"
        sql = f"SELECT name, price, purchased_on FROM purchases WHERE 1=1{purchase_date}{cat_purchase} ORDER BY {order} LIMIT 5"
        vals = ([values[0]] if purchase_date else []) + ([cat_values[0]] if category else [])
    elif wants_total:
        if category:
            sql = (
                "SELECT ROUND("
                f"COALESCE((SELECT SUM(price) FROM purchases WHERE 1=1{purchase_date}{cat_purchase}),0) + "
                f"COALESCE((SELECT SUM(amount) FROM monthly_expenses WHERE 1=1{fixed_date}{cat_fixed}),0) + "
                f"COALESCE((SELECT SUM(amount) FROM day_expenses WHERE 1=1{day_date}{cat_day}),0)"
                ", 2) AS total_spending"
            )
            # Values occur in the SQL in date/filter order.
            vals = []
            if purchase_date: vals.append(values[0])
            vals.append(cat_values[0])
            if fixed_date: vals.extend(values[1:3])
            vals.append(cat_values[1])
            if day_date: vals.append(values[-1])
            vals.append(cat_values[2])
        else:
            sql = (
                "SELECT ROUND("
                f"COALESCE((SELECT SUM(price) FROM purchases WHERE 1=1{purchase_date}),0) + "
                f"COALESCE((SELECT SUM(amount) FROM monthly_expenses WHERE 1=1{fixed_date}),0) + "
                f"COALESCE((SELECT SUM(amount) FROM day_expenses WHERE 1=1{day_date}),0)"
                ", 2) AS total_spending"
            )
            vals = values.copy()
    else:
        sql = f"SELECT name, price, purchased_on FROM purchases WHERE 1=1{purchase_date}{cat_purchase} ORDER BY purchased_on DESC, id DESC LIMIT 5"
        vals = ([values[0]] if purchase_date else []) + ([cat_values[0]] if category else [])

    for value in vals:
        escaped = str(value).replace("'", "''")
        sql = sql.replace("?", f"'{escaped}'", 1)
    return {"sql": sql}


def generate_chat_sql(user_message: str):
    """Generate a safe read-only SQL query, with a local fallback if Groq is unavailable."""
    api_key = os.environ.get("GROQ_API_KEY")
    if api_key and requests is not None:
        try:
            response = requests.post(
                GROQ_API_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": GROQ_MODEL,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": CHATBOT_SYSTEM_PROMPT},
                        {"role": "user", "content": user_message}
                    ],
                },
                timeout=15,
            )
            response.raise_for_status()
            raw = re.sub(r"^```(json)?|```$", "", response.json()["choices"][0]["message"]["content"].strip(), flags=re.MULTILINE).strip()
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    return _chat_local_sql(user_message)

def answer_financial_query(user_message: str, db_results: list, executed_sql: str = None):
    """Turn database results into a user-facing natural-language answer. Never expose SQL."""
    if not db_results:
        return "I couldn't find any matching records in your financial data."
    first = db_results[0]
    question = (user_message or "").lower()

    if "total_spending" in first:
        return f"You spent ₹{float(first.get('total_spending') or 0):,.2f} in the period you asked about."
    if "average_spending" in first:
        return f"Your average spending was ₹{float(first.get('average_spending') or 0):,.2f}."
    if "count" in first:
        value = int(first.get("count") or 0)
        return f"You have {value:,} matching purchase{'s' if value != 1 else ''}."

    if all(k in first for k in ("name", "price")):
        if any(w in question for w in ("biggest", "largest", "most expensive", "highest")):
            row = max(db_results, key=lambda r: float(r.get("price") or 0))
            name = str(row.get("name") or "Unknown purchase")
            price = float(row.get("price") or 0)
            date = row.get("purchased_on")
            if date:
                try:
                    from datetime import datetime
                    date_text = datetime.strptime(str(date), "%Y-%m-%d").strftime("%B %-d, %Y")
                except Exception:
                    date_text = str(date)
                return f"Your biggest purchase was {name} for ₹{price:,.2f} on {date_text}."
            return f"Your biggest purchase was {name} for ₹{price:,.2f}."
        items = [f"{str(r.get('name') or 'Unknown')} (₹{float(r.get('price') or 0):,.2f})" for r in db_results[:5]]
        return "Here are the matching purchases: " + ", ".join(items) + "."

    api_key = os.environ.get("GROQ_API_KEY")
    if api_key and requests is not None:
        prompt = f"""You are Super Snaps, a personal finance assistant.
User question: {user_message}
Database result: {json.dumps(db_results)}
Answer directly in natural language in 1-3 sentences. Never mention SQL, queries, database rows, JSON, or internal implementation details. Use ₹ for money."""
        try:
            response = requests.post(GROQ_API_URL, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json={"model": GROQ_MODEL, "temperature": 0.2, "messages": [{"role": "user", "content": prompt}]}, timeout=15)
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"].strip()
            if text and "SELECT " not in text.upper() and "SQL:" not in text.upper():
                return text
        except Exception:
            pass
    return "I found the relevant records, but I couldn't turn them into a useful summary right now."