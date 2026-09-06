"""Standalone diagnostic - run this in your Ledger environment to see the
*actual* error from SerpApi instead of the generic Flask-side message.

Usage:
    export SERPAPI_API_KEY="..."
    python diagnose_flights.py DEL BOM 2026-12-01
"""
import sys, os, requests

key = ""
print("SERPAPI_API_KEY set:", bool(key), "| length:", len(key) if key else 0)

origin, destination, dep_date = sys.argv[1], sys.argv[2], sys.argv[3]

params = {
    "engine": "google_flights",
    "departure_id": origin,
    "arrival_id": destination,
    "outbound_date": dep_date,
    "adults": 1,
    "travel_class": 1,
    "currency": "INR",
    "hl": "en",
    "type": 2,
    "api_key": key,
}

resp = requests.get("https://serpapi.com/search", params=params, timeout=15)
print("HTTP status:", resp.status_code)
print("Response body (first 1000 chars):")
print(resp.text[:1000])