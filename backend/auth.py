"""OpenAI / ChatGPT OAuth (OIDC authorization-code + PKCE) for the backend.

Flow
----
* GET /api/oauth/start builds the authorize URL with a PKCE challenge and a
  random state (both stashed in the signed Flask session cookie).
* After the user consents, OpenAI redirects to /api/auth/callback with `code`.
* The server exchanges the code + code_verifier at the token endpoint, then
  verifies the RS256-signed id_token against the JWKS before trusting it.
* Tokens are stored per-user in SQLite; access tokens are lazily refreshed.

The OAuth access token is a ChatGPT-scoped token. If the registered OAuth app
is also enabled for "API on behalf of users", that exact token can be used as
a Bearer token against the OpenAI platform API (see config.USE_OAUTH_ACCESS_KEY).
"""
import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse

import jwt
import requests

from config import config
from db import get_db

# The codex CLI / ChatGPT-simplified flow flags. When set, ChatGPT subscribers
# see the expected consent screen. They are optional for regular OAuth apps.
OPTIONAL_QUERY = {
    "id_token_add_organizations": "true",
    "codex_cli_simplified_flow": "true",
}

_jwks_cache: dict = {}
_jwks_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# PKCE helpers
# --------------------------------------------------------------------------- #
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge)."""
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def build_authorize_url(state: str, challenge: str) -> str:
    params = {
        "client_id": config.OPENAI_CLIENT_ID,
        "redirect_uri": config.OPENAI_REDIRECT_URI,
        "response_type": "code",
        "scope": config.OPENAI_OAUTH_SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    params.update(OPTIONAL_QUERY)
    return f"{config.OPENAI_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str, code_verifier: str) -> dict:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.OPENAI_REDIRECT_URI,
        "client_id": config.OPENAI_CLIENT_ID,
        "code_verifier": code_verifier,
    }
    if config.OPENAI_CLIENT_SECRET:
        data["client_secret"] = config.OPENAI_CLIENT_SECRET
    return _token_request(data)


def refresh_access_token(refresh_token: str) -> dict:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": config.OPENAI_CLIENT_ID,
    }
    if config.OPENAI_CLIENT_SECRET:
        data["client_secret"] = config.OPENAI_CLIENT_SECRET
    return _token_request(data)


def _token_request(data: dict) -> dict:
    resp = requests.post(
        config.OPENAI_TOKEN_URL,
        data=data,
        headers={"Accept": "application/json"},
        timeout=30,
    )
    if resp.status_code >= 300:
        raise OAuthError(
            f"Token exchange failed: HTTP {resp.status_code}: {resp.text[:300]}"
        )
    return resp.json()


class OAuthError(Exception):
    pass


# --------------------------------------------------------------------------- #
# id_token verification (RS256 against OpenAI JWKS)
# --------------------------------------------------------------------------- #
def _fetch_jwks() -> dict:
    global _jwks_cache
    with _jwks_lock:
        if _jwks_cache and _jwks_cache.get("_fetched", 0) > time.time() - 3600:
            return _jwks_cache
        resp = requests.get(config.OPENAI_JWKS_URL, timeout=30)
        resp.raise_for_status()
        keys = resp.json()
        keys = {k: v for k, v in keys.items() if k != "keys"}
        if "keys" in keys:
            keys = keys["keys"]
        _jwks_cache = {"keys": keys, "_fetched": time.time()}
        return _jwks_cache


def verify_id_token(id_token: str) -> dict:
    """Verify signature, exp, audience and issuer of the OpenAI id_token."""
    jwks = _fetch_jwks()
    unverified = jwt.decode(id_token, options={"verify_signature": False})
    kid = unverified.get("kid") or unverified.get("alg")
    signer_keys = [k for k in jwks["keys"] if k.get("kid") == kid] or jwks["keys"]
    for key in signer_keys:
        try:
            payload = jwt.decode(
                id_token,
                key=jwt.algorithms.RSAAlgorithm.from_jwk(key),
                algorithms=["RS256"],
                audience=config.OPENAI_CLIENT_ID,
                issuer=config.OPENAI_ISSUER,
                options={"require": ["exp", "sub", "iss", "aud"]},
            )
            return payload
        except jwt.PyJWTError:
            continue
    raise OAuthError("Could not verify id_token signature.")


def userinfo(access_token: str) -> dict:
    resp = requests.get(
        "https://auth.openai.com/api/accounts/oauth/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if resp.status_code >= 300:
        raise OAuthError(f"userinfo failed: HTTP {resp.status_code}")
    return resp.json()


# --------------------------------------------------------------------------- #
# Per-user token handling
# --------------------------------------------------------------------------- #
def save_tokens(user_id: str, tokens: dict, scopes: str):
    tokens = dict(tokens)
    tokens.setdefault("expires_at", time.time() + int(tokens.get("expires_in", 3600)))
    get_db().update_user_tokens(user_id, tokens, scopes)


def get_valid_token(user_id: str) -> dict:
    """Return a non-expired token bundle for a user, refreshing if needed."""
    tokens = get_db().get_user_tokens(user_id)
    if not tokens:
        raise OAuthError("No OAuth tokens stored for user.")
    now = time.time()
    expires = tokens.get("expires_at", 0)
    # Refresh with a 60s safety margin if close to expiry or already expired.
    if not tokens.get("refresh_token"):
        return tokens
    if expires - now < 60:
        try:
            fresh = refresh_access_token(tokens["refresh_token"])
        except Exception:
            raise OAuthError("Stale session - please sign in again.")
        fresh.setdefault("expires_at", time.time() + int(fresh.get("expires_in", 3600)))
        get_db().update_user_tokens(user_id, fresh, config.OPENAI_OAUTH_SCOPE)
        tokens = fresh
    return tokens


def bearer_for_api(user_id: str) -> str | None:
    """Bearer token to call the OpenAI platform API for this user.

    Priority:
      1. OAuth access token when the app is configured for on-behalf API use.
      2. Server-side API key (requires a CONFIG-rediscovered restart note: the
         key is resolved at request time so live rotation works).
    """
    if config.USE_OAUTH_ACCESS_KEY:
        try:
            tok = get_valid_token(user_id)
            if tok.get("access_token"):
                return tok["access_token"]
        except OAuthError:
            pass
    if config.OPENAI_API_KEY:
        return config.OPENAI_API_KEY
    return None


def verify_subscription(user_id: str) -> bool:
    """Best-effort check that the user's OAuth token is a live ChatGPT token."""
    if not config.USE_OAUTH_ACCESS_KEY:
        return True  # we are using a server-side key anyway
    try:
        tok = get_valid_token(user_id)
        return bool(tok.get("access_token"))
    except OAuthError:
        return False


def is_subscription_access(user_id: str) -> bool:
    """True when API calls are made with the user's OAuth token."""
    return config.USE_OAUTH_ACCESS_KEY and bool(bearer_for_api(user_id))


def normalize_claims(claims: dict) -> dict:
    user_info = userinfo if False else None  # placeholder guard
    return {
        "sub": claims.get("sub", ""),
        "name": claims.get("name", "") or claims.get("preferred_username", ""),
        "email": claims.get("email", ""),
        "picture": claims.get("picture", ""),
    }


class OAuthSession:
    """PKCE + state + redirect intent kept in the signed Flask session."""

    PREFIX = "apex_oauth"  # map key prefix, JSON-encoded under session key

    @staticmethod
    def start(session: dict) -> dict:
        state = _b64url(secrets.token_bytes(32))
        verifier, challenge = generate_pkce()
        session[OAuthSession.PREFIX] = {
            "state": state,
            "verifier": verifier,
            "created": time.time(),
        }
        return {"state": state, "verifier": verifier, "challenge": challenge}

    @staticmethod
    def verify_and_consume(session: dict, state: str) -> str | None:
        data = session.get(OAuthSession.PREFIX)
        if not data:
            return None
        if not secrets.compare_digest(data.get("state", ""), state or ""):
            return None
        verifier = data.get("verifier")
        session.pop(OAuthSession.PREFIX, None)
        return verifier


def summary(user: dict) -> dict:
    """API-facing, safe representation of the signed-in user."""
    scopes = (user.get("token_scopes") or "").split()
    return {
        "id": user["id"],
        "name": user.get("name") or "Apex user",
        "email": user.get("email") or "",
        "picture": user.get("picture") or "",
        "oauth": config.oauth_configured,
        "uses_oauth_token": is_subscription_access(user["id"]),
        "scopes": scopes,
        "refresh": bool(
            (user.get("tokens") or {}).get("refresh_token")
        ) if isinstance(user.get("tokens"), str) else bool(
            json.loads(user.get("tokens") or "{}").get("refresh_token")
        ),
    }