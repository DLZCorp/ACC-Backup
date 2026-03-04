"""
Token utility — generates a 3LO access token via SSA JWT assertion.

Based on the official APS SSA walkthrough:
https://developer.doc.autodesk.com/.../task3-generate-3-legged-access-token.html

Reads credentials from .env file via python-dotenv.
Caches token in memory and regenerates when expired.
"""

import sys, os, time, pathlib
from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).parent / ".env")

# ── APS credentials from .env ───────────────────────────────────────────────

CLIENT_ID = os.environ.get("APS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("APS_CLIENT_SECRET", "")
SERVICE_ACCOUNT_ID = os.environ.get("APS_SERVICE_ACCOUNT_ID", "")
KEY_ID = os.environ.get("APS_KEY_ID", "")
PRIVATE_KEY = os.environ.get("APS_PRIVATE_KEY", "")
SCOPES = os.environ.get("APS_SCOPES", "data:read").split()

TOKEN_URL = "https://developer.api.autodesk.com/authentication/v2/token"

_cached_token = None
_cached_expires_at = 0


# ── SSA JWT flow (from APS walkthrough) ─────────────────────────────────────

def _generate_jwt_assertion():
    """Generate a JWT assertion string (Step 1 of SSA walkthrough)."""
    import jwt  # PyJWT

    claims = {
        "iss": CLIENT_ID,
        "sub": SERVICE_ACCOUNT_ID,
        "aud": TOKEN_URL,
        "exp": int(time.time()) + 300,
        "scope": SCOPES,
    }
    headers = {"alg": "RS256", "kid": KEY_ID}
    return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers=headers)


def _exchange_jwt_for_token(jwt_assertion):
    """Exchange JWT assertion for a 3LO access token (Step 2 of SSA walkthrough)."""
    import requests

    response = requests.post(TOKEN_URL, headers={
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt_assertion,
        "scope": " ".join(SCOPES),
    }, auth=(CLIENT_ID, CLIENT_SECRET), timeout=30)

    if not response.ok:
        raise RuntimeError(
            f"Token exchange failed ({response.status_code}): {response.text[:300]}")
    return response.json()


# ── Public API ───────────────────────────────────────────────────────────────

def load_token():
    """Return a valid access token. Generates via SSA JWT if expired."""
    global _cached_token, _cached_expires_at

    # CLI arg override
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        return sys.argv[1]

    # Return cached if still valid (5-min safety margin)
    if _cached_token and time.time() < _cached_expires_at - 300:
        return _cached_token

    # Generate new token via SSA
    jwt_assertion = _generate_jwt_assertion()
    token_response = _exchange_jwt_for_token(jwt_assertion)

    _cached_token = token_response["access_token"]
    expires_in = token_response.get("expires_in", 3600)
    _cached_expires_at = int(time.time()) + expires_in
    print(f"  SSA token generated, expires in {expires_in}s")
    return _cached_token
