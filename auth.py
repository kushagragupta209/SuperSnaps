"""Supabase Auth helpers for the multi-user foundation.

The browser owns the Supabase session. Flask receives the access token and
asks Supabase Auth's /auth/v1/user endpoint who the token belongs to.
This avoids depending on a particular JWT signing-key configuration.
"""

import os
import requests

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY") or ""


def is_configured():
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)


def get_user_from_token(access_token):
    if not access_token or not is_configured():
        return None

    try:
        response = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {access_token}",
            },
            timeout=10,
        )
        if not response.ok:
            return None
        user = response.json()
        return {
            "id": user.get("id"),
            "email": user.get("email"),
        } if user.get("id") else None
    except requests.RequestException:
        return None


def get_bearer_token(request):
    value = request.headers.get("Authorization", "")
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return None
