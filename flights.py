"""
flights.py — Ledger's flight-fare tracking integration.

Kept separate from app.py on purpose, same reasoning as agent.py: this is
where the external flight-pricing call lives, so app.py's routes stay thin
and this file can be swapped out later (e.g. for real-time flight *status*
via AviationStack/FlightAware, which is a different kind of data than fare
search) without touching Flask routes.

Provider: SerpApi's Google Flights engine.
  https://serpapi.com/google-flights-api — free tier includes 100 searches/month,
  self-serve signup, no partner agreement required. This replaces the earlier
  Amadeus integration (and, before that, Kiwi) which required a partner
  agreement to move past sandbox/test data.

Public interface used by app.py:
    fetch_cheapest_fare(origin, destination, departure_date, return_date,
                         adults, travel_class) -> float (price in INR)
    fetch_all_fares(origin, destination, departure_date, return_date,
                     adults, travel_class) -> list[dict], sorted by price
                     ascending, each item: {"airline", "price", "stops",
                     "duration_minutes"}
    is_flights_configured() -> bool

Everything else is an implementation detail of talking to SerpApi.
"""

import os

try:
    import requests
except ImportError:  # requests is in requirements.txt; guard just in case
    requests = None


# --------------------------------------------------------------------------- #
# Config — SerpApi (https://serpapi.com/google-flights-api)
# --------------------------------------------------------------------------- #
#
# Set this in your shell before running the app:
#   export SERPAPI_API_KEY="..."
# Get a free key (100 searches/month) at
#   https://serpapi.com/users/sign_up
#
# SerpApi proxies live Google Flights results, so unlike Amadeus's sandbox
# there's no separate "test" vs "production" base URL — the free tier just
# caps you at 100 searches/month before you need a paid plan.

SERPAPI_API_KEY = os.environ.get("SERPAPI_API_KEY")
SERPAPI_BASE_URL = os.environ.get("SERPAPI_BASE_URL", "https://serpapi.com/search")

# Ledger's `travel_class` values (ECONOMY / PREMIUM_ECONOMY / BUSINESS / FIRST)
# map onto SerpApi's Google Flights `travel_class` integer codes.
_TRAVEL_CLASS_MAP = {
    "ECONOMY": 1,
    "PREMIUM_ECONOMY": 2,
    "BUSINESS": 3,
    "FIRST": 4,
}


def is_flights_configured() -> bool:
    """Whether a SerpApi key is present (doesn't guarantee it's valid)."""
    return bool(SERPAPI_API_KEY and requests is not None)


def _airline_label(offer):
    """
    Best-effort display name for an offer. Most offers are a single leg
    (one airline); connecting itineraries can mix carriers, so fall back
    to joining the distinct airline names on the itinerary.
    """
    legs = offer.get("flights") or []
    airlines = []
    for leg in legs:
        name = leg.get("airline")
        if name and name not in airlines:
            airlines.append(name)
    if not airlines:
        return "Unknown airline"
    return " + ".join(airlines)


def _search_offers(origin, destination, departure_date, return_date=None,
                    adults=1, travel_class="ECONOMY"):
    """
    Shared SerpApi call used by both fetch_cheapest_fare and fetch_all_fares.
    Returns the raw list of offer dicts (best_flights + other_flights) as
    SerpApi returns them — unsorted, unfiltered.

    Raises ValueError for configuration/data problems (no API key, no
    offers found, SerpApi-reported errors) and requests.RequestException
    for genuine network-level failures, so callers (app.py) can decide how
    to surface each case.
    """
    if not is_flights_configured():
        raise ValueError(
            "Flight pricing isn't configured yet — set SERPAPI_API_KEY "
            "(see flights.py for how to get a free key)."
        )

    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": departure_date,
        "adults": adults,
        "travel_class": _TRAVEL_CLASS_MAP.get(travel_class.upper(), 1),
        "currency": "INR",
        "hl": "en",
        "api_key": SERPAPI_API_KEY,
    }
    if return_date:
        params["return_date"] = return_date
        params["type"] = 1  # round trip
    else:
        params["type"] = 2  # one way

    resp = requests.get(SERPAPI_BASE_URL, params=params, timeout=15)
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError:
        # Surface SerpApi's actual error message (bad key, bad params, quota
        # exhausted, etc.) instead of letting app.py's generic "could not
        # reach the provider" RequestException handler swallow it.
        detail = None
        try:
            detail = resp.json().get("error")
        except ValueError:
            pass
        raise ValueError(
            f"SerpApi request failed ({resp.status_code}): {detail or resp.text[:200]}"
        )

    payload = resp.json()
    if payload.get("error"):
        raise ValueError(f"SerpApi error: {payload['error']}")

    offers = (payload.get("best_flights") or []) + (payload.get("other_flights") or [])
    if not offers:
        raise ValueError("No fares found for that route and date — try different dates or airports.")
    return offers


def fetch_cheapest_fare(origin, destination, departure_date, return_date=None,
                         adults=1, travel_class="ECONOMY"):
    """
    Return the single cheapest total fare found, in INR, as a float.
    """
    offers = _search_offers(origin, destination, departure_date, return_date,
                             adults, travel_class)
    prices = [float(o["price"]) for o in offers if o.get("price")]
    if not prices:
        raise ValueError("No fares found for that route and date — try different dates or airports.")
    return min(prices)


def fetch_all_fares(origin, destination, departure_date, return_date=None,
                     adults=1, travel_class="ECONOMY"):
    """
    Return every offer SerpApi found for the route/date, sorted by price
    ascending. Each item:
        {"airline": str, "price": float, "stops": int, "duration_minutes": int|None}

    Used for the "preview fares while filling out the tracker form" flow —
    unlike fetch_cheapest_fare, this doesn't collapse the results down to
    a single number.
    """
    offers = _search_offers(origin, destination, departure_date, return_date,
                             adults, travel_class)

    results = []
    for offer in offers:
        price = offer.get("price")
        if not price:
            continue
        legs = offer.get("flights") or []
        results.append({
            "airline": _airline_label(offer),
            "price": float(price),
            "stops": max(0, len(legs) - 1),
            "duration_minutes": offer.get("total_duration"),
        })

    if not results:
        raise ValueError("No fares found for that route and date — try different dates or airports.")

    results.sort(key=lambda r: r["price"])
    return results