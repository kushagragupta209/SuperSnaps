"""
sara.py — Ledger's supervisor agent.

Sara is the single conversational entry point that sits in front of every
other page in Ledger. Instead of duplicating each page's logic, every
existing Flask route in app.py is exposed to Sara as a "tool" — she calls
them internally through app.test_client(), so whatever validation,
database writes, or provider fallbacks those routes already do (SerpApi
failures, scrape failures, read-only SQL guard, etc.) are reused exactly
as-is. Adding a new page later just means adding one entry to TOOL_SPECS
and TOOLS below; no route needs to change.

Public interface used by app.py:
    run_sara(message, client, history=None) -> dict
        {"reply": str, "actions": [...], "source": "groq" | "unavailable" | "error"}

Requires GROQ_API_KEY (see agent.py) — reliable multi-tool intent
extraction isn't something a regex fallback can do safely with money and
delete operations involved, so Sara tells the user plainly if it's
unavailable rather than guessing.
"""

import json
import os
import re
from datetime import date

import agent  # reuse GROQ_API_URL / GROQ_MODEL / is_llm_configured()

try:
    import requests
except ImportError:
    requests = None


# --------------------------------------------------------------------------- #
# Tool routing table — maps a tool name straight onto an existing app.py route.
# --------------------------------------------------------------------------- #
# method:  HTTP verb used against the Flask test client
# path:    route path; "{param}" segments are filled from the tool's own
#          arguments and stripped out of the body/query before the call
# confirm: destructive tools only execute once the model passes
#          confirmed=true (see _execute_tool) — otherwise Sara is told to
#          ask the user first
# wrap:    some routes expect {"items": [...]} — wrap the flat args into
#          a single-item list under this key

TOOL_SPECS = {
    "add_purchase":            {"method": "POST",   "path": "/api/purchases"},
    "delete_purchase":         {"method": "DELETE", "path": "/api/purchases/{purchase_id}", "confirm": True},
    "get_summary":             {"method": "GET",    "path": "/api/summary"},
    "set_salary":              {"method": "POST",   "path": "/api/settings/salary"},
    "set_savings_percent":     {"method": "POST",   "path": "/api/settings/savings-percent"},
    "log_monthly_expenses":    {"method": "POST",   "path": "/api/monthly-expenses"},
    "get_monthly_expenses":    {"method": "GET",    "path": "/api/monthly-expenses"},
    "add_day_expense":         {"method": "POST",   "path": "/api/day-expenses", "wrap": "items"},
    "sync_telegram_expenses":  {"method": "POST",   "path": "/api/telegram/sync"},
    "import_wishlist_item":    {"method": "POST",   "path": "/api/wishlist/import"},
    "list_wishlist":           {"method": "GET",    "path": "/api/wishlist"},
    "delete_wishlist_item":    {"method": "DELETE", "path": "/api/wishlist/{item_id}", "confirm": True},
    "search_flights":          {"method": "POST",   "path": "/api/flights/search"},
    "add_flight_tracker":      {"method": "POST",   "path": "/api/flights"},
    "check_flight_fare":       {"method": "POST",   "path": "/api/flights/{flight_id}/check"},
    "delete_flight_tracker":   {"method": "DELETE", "path": "/api/flights/{flight_id}", "confirm": True},
    "get_insights":            {"method": "GET",    "path": "/api/insights"},
    "get_safe_to_spend":       {"method": "GET",    "path": "/api/budget/safe-to-spend"},
    "run_spend_cutter_plan":   {"method": "GET",    "path": "/api/agents/spend-cutter/plan"},
    "evaluate_deal":           {"method": "POST",   "path": "/api/agents/deal-evaluator"},
    "ask_financial_question":  {"method": "POST",   "path": "/api/chat"},
}


# --------------------------------------------------------------------------- #
# Tool schemas (OpenAI/Groq function-calling format)
# --------------------------------------------------------------------------- #

TOOLS = [
    {"type": "function", "function": {
        "name": "add_purchase",
        "description": "Log a one-off discretionary purchase on the Transactions timeline.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "What was bought."},
            "price": {"type": "number", "description": "Price paid, in INR."},
            "purchased_on": {"type": "string", "description": "YYYY-MM-DD. Defaults to today if omitted."},
        }, "required": ["name", "price"]},
    }},
    {"type": "function", "function": {
        "name": "delete_purchase",
        "description": "Delete a purchase by its id. Destructive — only pass confirmed=true after the user explicitly agrees.",
        "parameters": {"type": "object", "properties": {
            "purchase_id": {"type": "integer"},
            "confirmed": {"type": "boolean", "description": "Set true only once the user has explicitly confirmed this deletion."},
        }, "required": ["purchase_id"]},
    }},
    {"type": "function", "function": {
        "name": "get_summary",
        "description": "Dashboard totals: all-time/month/YTD purchase spend, fixed expenses, salary percentages.",
        "parameters": {"type": "object", "properties": {
            "year": {"type": "integer"}, "month": {"type": "integer"},
        }},
    }},
    {"type": "function", "function": {
        "name": "set_salary",
        "description": "Set the user's monthly salary used for budget calculations.",
        "parameters": {"type": "object", "properties": {
            "monthly_salary": {"type": "number"},
        }, "required": ["monthly_salary"]},
    }},
    {"type": "function", "function": {
        "name": "set_savings_percent",
        "description": "Set the target percentage of salary to save each month.",
        "parameters": {"type": "object", "properties": {
            "savings_percent": {"type": "number", "description": "0-100."},
        }, "required": ["savings_percent"]},
    }},
    {"type": "function", "function": {
        "name": "log_monthly_expenses",
        "description": "Record this month's recurring/fixed expenses from a free-form sentence, e.g. '12k rent, 5k food, 3000 fuel'. Replaces any existing entries for that month.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
            "year": {"type": "integer"}, "month": {"type": "integer"},
        }, "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "get_monthly_expenses",
        "description": "Read back the fixed/recurring expense line items already saved for a month.",
        "parameters": {"type": "object", "properties": {
            "year": {"type": "integer"}, "month": {"type": "integer"},
        }},
    }},
    {"type": "function", "function": {
        "name": "add_day_expense",
        "description": "Add a single day-wise expense (e.g. a Blinkit or UPI transaction) directly, without going through Telegram.",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD."},
            "merchant": {"type": "string"},
            "category": {"type": "string"},
            "amount": {"type": "number"},
        }, "required": ["date", "category", "amount"]},
    }},
    {"type": "function", "function": {
        "name": "sync_telegram_expenses",
        "description": "Pull and parse any pending expense messages sent over Telegram into day-wise expenses.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "import_wishlist_item",
        "description": "Add a product to the wishlist by pasting its Amazon or Flipkart URL — fetches the live price automatically.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
        }, "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "list_wishlist",
        "description": "List wishlist items with their savings-projection (months until affordable).",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "delete_wishlist_item",
        "description": "Remove a wishlist item by id. Destructive — only pass confirmed=true after the user explicitly agrees.",
        "parameters": {"type": "object", "properties": {
            "item_id": {"type": "integer"},
            "confirmed": {"type": "boolean"},
        }, "required": ["item_id"]},
    }},
    {"type": "function", "function": {
        "name": "search_flights",
        "description": "Preview flight fares for a route/date without saving a tracker.",
        "parameters": {"type": "object", "properties": {
            "origin": {"type": "string", "description": "3-letter airport code."},
            "destination": {"type": "string", "description": "3-letter airport code."},
            "departure_date": {"type": "string", "description": "YYYY-MM-DD."},
            "return_date": {"type": "string"},
            "adults": {"type": "integer"},
            "travel_class": {"type": "string", "enum": ["ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"]},
        }, "required": ["origin", "destination", "departure_date"]},
    }},
    {"type": "function", "function": {
        "name": "add_flight_tracker",
        "description": "Start tracking a route/date's fare over time, with an optional price-drop alert target.",
        "parameters": {"type": "object", "properties": {
            "origin": {"type": "string"}, "destination": {"type": "string"},
            "departure_date": {"type": "string"}, "return_date": {"type": "string"},
            "adults": {"type": "integer"},
            "travel_class": {"type": "string", "enum": ["ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"]},
            "target_price": {"type": "number"}, "notify_email": {"type": "string"},
        }, "required": ["origin", "destination", "departure_date"]},
    }},
    {"type": "function", "function": {
        "name": "check_flight_fare",
        "description": "Re-check the current fare for an existing flight tracker by id.",
        "parameters": {"type": "object", "properties": {
            "flight_id": {"type": "integer"},
        }, "required": ["flight_id"]},
    }},
    {"type": "function", "function": {
        "name": "delete_flight_tracker",
        "description": "Stop tracking a flight by id. Destructive — only pass confirmed=true after the user explicitly agrees.",
        "parameters": {"type": "object", "properties": {
            "flight_id": {"type": "integer"},
            "confirmed": {"type": "boolean"},
        }, "required": ["flight_id"]},
    }},
    {"type": "function", "function": {
        "name": "get_insights",
        "description": "Get the AI monthly spending review and next-month budget suggestion.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_safe_to_spend",
        "description": "Get today's safe-to-spend allowance and how much of this month's discretionary budget is left.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "run_spend_cutter_plan",
        "description": "Ask the spend-cutter agent for a proposed rebalance of fixed expenses to hit the savings target.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "evaluate_deal",
        "description": "Ask whether a specific wishlist-style purchase is a good deal given current savings rate.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "price": {"type": "number"},
        }, "required": ["name", "price"]},
    }},
    {"type": "function", "function": {
        "name": "ask_financial_question",
        "description": "Delegate an analytical/historical question about the user's own spending (totals, averages, biggest purchase, etc.) to the read-only financial-query engine, instead of computing it yourself.",
        "parameters": {"type": "object", "properties": {
            "message": {"type": "string"},
        }, "required": ["message"]},
    }},
]


SARA_SYSTEM_PROMPT = """You are Sara, the supervisor agent for Ledger, a personal finance app.
Today's date is {today}. Currency is INR.

You have tools that map directly onto Ledger's pages (purchases, salary/savings
settings, monthly fixed expenses, day-wise expenses, wishlist, flight tracking,
insights/budget agents). The user talks to you instead of visiting each page
themselves — figure out their intent and call the right tool(s) with the right
arguments.

Rules:
- Resolve relative dates ("today", "yesterday", "next Friday") to YYYY-MM-DD
  yourself using today's date above. Never pass a relative date to a tool.
- If a required detail is genuinely missing or ambiguous (e.g. no amount given,
  an airport code you're not sure of), ask the user instead of guessing —
  this is real money and real data.
- Tools named delete_* are destructive. Never pass confirmed=true unless the
  user has explicitly agreed to that specific deletion earlier in this
  conversation. If they haven't, call the tool with confirmed=false (or omit
  it) to see what it targets, then ask the user to confirm in plain language
  before trying again with confirmed=true.
- For questions about past spending, totals, or trends, use
  ask_financial_question rather than trying to compute it yourself.
- After calling tools, reply in plain, friendly language. Never mention SQL,
  JSON, tool names, status codes, or other internal details.
- If a tool result contains an "error" or a non-2xx status, explain the
  problem in plain language and suggest a fix; don't retry the same call
  with the same arguments.
"""


# --------------------------------------------------------------------------- #
# Tool execution
# --------------------------------------------------------------------------- #

def _execute_tool(client, name, args):
    """Run one tool call against the Flask app's own routes via test_client()."""
    spec = TOOL_SPECS.get(name)
    if not spec:
        return {"error": f"Unknown tool '{name}'."}

    args = dict(args or {})

    if spec.get("confirm") and not args.pop("confirmed", False):
        return {
            "pending_confirmation": True,
            "message": "Not executed. Ask the user to explicitly confirm this "
                        "specific action, then call this tool again with confirmed=true.",
        }
    args.pop("confirmed", None)

    path = spec["path"]
    try:
        for key in re.findall(r"\{(\w+)\}", path):
            if key not in args:
                return {"error": f"Missing required '{key}' for {name}."}
            path = path.replace("{" + key + "}", str(args.pop(key)))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    method = spec["method"]
    body = {spec["wrap"]: [args]} if spec.get("wrap") else args

    try:
        if method == "GET":
            resp = client.get(path, query_string={k: str(v) for k, v in args.items()})
        elif method == "DELETE":
            resp = client.delete(path)
        else:
            resp = client.post(path, json=body)
        return {"status": resp.status_code, "data": resp.get_json(silent=True)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Tool call failed: {exc}"}


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def run_sara(message: str, client, history=None, max_iterations: int = 4):
    """
    Orchestrate one turn of the supervisor agent: send the message (plus any
    prior user/assistant turns in `history`) to Groq with the tool schema,
    execute whatever it calls via `client` (a Flask app.test_client()), feed
    results back, and repeat until it produces a plain-language reply.
    """
    if not agent.is_llm_configured() or requests is None:
        return {
            "reply": "Sara needs a Groq API key to understand requests — set "
                     "GROQ_API_KEY (see agent.py for how to get a free one), "
                     "then try again.",
            "actions": [],
            "source": "unavailable",
        }

    api_key = os.environ["GROQ_API_KEY"]
    messages = [{"role": "system", "content": SARA_SYSTEM_PROMPT.format(today=date.today().isoformat())}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": message})

    actions = []
    try:
        for _ in range(max_iterations):
            response = requests.post(
                agent.GROQ_API_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": agent.GROQ_MODEL,
                    "temperature": 0,
                    "messages": messages,
                    "tools": TOOLS,
                    "tool_choice": "auto",
                },
                timeout=20,
            )
            response.raise_for_status()
            choice = response.json()["choices"][0]["message"]
            messages.append(choice)

            tool_calls = choice.get("tool_calls")
            if not tool_calls:
                return {"reply": choice.get("content") or "Done.", "actions": actions, "source": "groq"}

            for call in tool_calls:
                fn_name = call["function"]["name"]
                try:
                    fn_args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    fn_args = {}

                result = _execute_tool(client, fn_name, fn_args)
                actions.append({
                    "tool": fn_name,
                    "args": {k: v for k, v in fn_args.items() if k != "confirmed"},
                    "result": result,
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result)[:4000],
                })

        return {
            "reply": "That request needed more steps than I could complete in one go — "
                     "could you break it into smaller asks?",
            "actions": actions,
            "source": "groq",
        }

    except requests.exceptions.HTTPError as exc:
        detail = None
        if exc.response is not None:
            try:
                detail = exc.response.json().get("error", {}).get("message")
            except Exception:  # noqa: BLE001
                detail = exc.response.text[:300]
        print(f"[sara] Groq API error ({exc.response.status_code if exc.response is not None else '?'}): {detail}")
        return {
            "reply": "The AI service rejected that request. Check the app's terminal "
                     "output for the exact error (e.g. an invalid key, a decommissioned "
                     "model, or a rate limit).",
            "actions": actions,
            "source": "error",
        }
    except Exception as exc:  # noqa: BLE001 - network/timeout/parse failure
        print(f"[sara] Unexpected error: {exc!r}")
        return {
            "reply": "I couldn't reach the AI service just now. Try again shortly.",
            "actions": actions,
            "source": "error",
        }