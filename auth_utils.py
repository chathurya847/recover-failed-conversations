"""Shared token helpers for download_attachments.py and run_conversation.py."""

import base64
import json
import time
from urllib.parse import unquote, urlparse


def clean_token(raw):
    """A token copied from a browser cookie is often URL-encoded and quoted (%22eyJ...%22)."""
    token = unquote(raw or "").strip().strip("'\"")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def token_claims(token):
    """Decode the (unverified) JWT payload. Returns {} if the token is not a readable JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


def check_token(token, accounts_url, label):
    """Return an error message if the token is expired or was issued by a different system, else None.

    accounts_url is the accounts (login) service of the system the token will be used on, for example
    https://accounts-dev.layernext.ai. When it is empty the system check is skipped.
    """
    claims = token_claims(token)
    expires = claims.get("exp")
    if expires and expires < time.time():
        return f"{label} has expired ({time.strftime('%Y-%m-%d %H:%M', time.localtime(expires))}). Copy a fresh one into .env."

    if not accounts_url:
        return None
    issuer = claims.get("iss")
    if not issuer:
        return None  # not a JWT we can read, let the server decide
    expected_host = urlparse(accounts_url if "://" in accounts_url else f"https://{accounts_url}").netloc.lower()
    issuer_host = urlparse(issuer).netloc.lower()
    if expected_host and issuer_host != expected_host:
        return (
            f"{label} was issued by {issuer_host}, but the accounts URL in .env is {expected_host}. "
            "It belongs to a different system (for example a dev token used on production). "
            "Copy the token from the same system, or fix the accounts URL."
        )
    return None
