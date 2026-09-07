"""Google OAuth 2.0 flow — start, callback, state validation, PKCE.

The Google Workspace MCP owns all OAuth logic. the MCP client never sees
authorization codes, tokens, or client secrets.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from app.config import settings
from app.context import current_user_id, user_id as user_id_ctx

log = logging.getLogger("google-workspace-mcp")

# ── In-memory state store (transient) ──────────────────────────
# Maps state → {redirect_uri, scopes, user_id, created_at, flow}
_state_store: dict[str, dict[str, Any]] = {}
_STATE_TTL = 300  # 5 minutes


def _state_dir() -> Path:
    """Directory for state files (if using file-based state instead)."""
    return Path(settings.google_credentials_dir) / ".oauth_states"


def _cleanup_states() -> None:
    """Remove expired state entries."""
    now = time.time()
    expired = [k for k, v in _state_store.items()
               if now - v.get("created_at", 0) > _STATE_TTL]
    for k in expired:
        del _state_store[k]


def get_redirect_uri() -> str:
    """The OAuth callback URI that Google will redirect to."""
    return f"{settings.external_url.rstrip('/')}/oauth/callback"


def get_flow(scopes: list[str] | None = None) -> Flow:
    """Create a Google OAuth Flow with PKCE."""
    flow = Flow.from_client_secrets_file(
        settings.google_client_secret_file,
        scopes=scopes or settings.google_scopes,
        redirect_uri=get_redirect_uri(),
    )
    # Enable PKCE automatically (flow.run_flow uses it internally)
    return flow


def get_authorization_url(scopes: list[str] | None = None, user_id: str | None = None) -> tuple[str, str]:
    """Return (auth_url, state) for the given scopes.

    In multi-user mode, ``user_id`` is stored in the state so the callback
    can route the token to the correct per-user file.
    In single-user mode, user_id is None → treated as "default".
    """
    _cleanup_states()
    effective_user = user_id or "default"
    flow = get_flow(scopes)
    auth_url, state = flow.authorization_url(
        access_type="offline",       # need refresh_token for auto-refresh
        prompt="consent",            # force consent screen → guarantees refresh_token
                                     # (without this, Google uses prompt=none on re-auth
                                     #  and skips the refresh_token for returning users)
    )
    _state_store[state] = {
        "created_at": time.time(),
        "redirect_uri": get_redirect_uri(),
        "scopes": scopes or settings.google_scopes,
        "user_id": effective_user,
        "flow": flow,  # Keep the Flow (incl. PKCE code_verifier) for token exchange
    }
    log.info("OAuth flow started — state %s..., user=%s, scopes=%d", state[:8], effective_user, len(scopes or settings.google_scopes))
    return auth_url, state


def exchange_code(code: str, state: str) -> dict[str, Any]:
    """Exchange authorization code for credentials, validate state.

    Returns token dict that can be stored by TokenStore.
    Raises HTTPException on invalid state or token errors.
    """
    _cleanup_states()

    # ── Validate state ──
    stored = _state_store.pop(state, None)
    if stored is None:
        # Also check if it was created >5 min ago or just doesn't match
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired OAuth state. Please restart the flow.",
        )

    # ── Reuse the original Flow (it holds the PKCE code_verifier from
    #    get_authorization_url). Creating a new Flow would lose the
    #    verifier and Google rejects the token exchange with
    #    "invalid_grant: Missing code verifier." ──
    flow = stored["flow"]
    creds = None
    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
    except Warning as w:
        # oauthlib raises a Warning when Google returns scopes that differ
        # from what the Flow requested (e.g. if the OAuth client was
        # previously used by another app with different scopes).
        # The token IS still fetched — oauthlib parses it before the
        # Warning, but doesn't return it from fetch_token().
        log.warning("Scope mismatch during token exchange (token still valid): %s", w)
        # Extract the token from the OAuth2 session that oauthlib populated
        token = flow.oauth2session.token
        if not token:
            raise HTTPException(status_code=500, detail="Token exchange failed — no token received from Google")
        # Build Credentials from the raw token + the Flow's client config
        config = flow.client_config
        client_info = config.get("web") or config.get("installed") or config
        scopes_str = token.get("scope", "")
        creds = Credentials(
            token=token.get("access_token"),
            refresh_token=token.get("refresh_token"),
            token_uri=client_info.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=client_info.get("client_id"),
            client_secret=client_info.get("client_secret"),
            scopes=scopes_str.split() if isinstance(scopes_str, str) else scopes_str,
        )

    if creds is None or creds.refresh_token is None:
        raise HTTPException(
            status_code=400,
            detail="OAuth exchange succeeded but no refresh token was returned. "
                   "Ensure 'access_type=offline' is set and the user hasn't "
                   "pre-authorized the app before.",
        )

    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
        "scopes": list(creds.scopes) if creds.scopes else [],
    }
    log.info("OAuth token stored — scopes: %s", ", ".join(token_data["scopes"][:3]))
    return token_data, stored.get("user_id", "default")


# ── FastAPI router ─────────────────────────────────────────────

router = APIRouter(prefix="/oauth")


@router.get("/start")
def start_oauth():
    """Start the Google OAuth 2.0 flow.

    Requires bearer token (to identify the user). In multi-user mode,
    uses the requesting user's configured scopes. The user_id is embedded
    in the OAuth state so /callback can store the token to the correct
    per-user file.
    """
    uid = current_user_id()
    if uid is None:
        uid = "default"

    # In multi-user mode, use the user's specific scopes if defined;
    # otherwise fall back to the global default scopes.
    user_scopes = None
    if settings.is_multi_user:
        user_scopes = settings.user_scopes(uid)

    auth_url, state = get_authorization_url(scopes=user_scopes, user_id=uid)
    return RedirectResponse(url=auth_url)


@router.get("/callback")
def callback(code: str, state: str):
    """Handle Google's OAuth callback.

    Public endpoint — Google redirects here with ?code=...&state=...
    The user_id is recovered from the stored state (set during /oauth/start)
    and used to route the token to the correct per-user file.
    """
    try:
        token_data, uid = exchange_code(code, state)
    except HTTPException:
        raise
    except RefreshError as e:
        raise HTTPException(status_code=400, detail=f"OAuth refresh error: {e}")
    except Exception as e:
        log.error("OAuth callback error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="OAuth exchange failed")

    # Set the user context so get_google_client() returns the right client
    from app.google_client import get_google_client
    ctx = user_id_ctx.set(uid)
    try:
        client = get_google_client()
        creds = Credentials(
            token=token_data["token"],
            refresh_token=token_data["refresh_token"],
            token_uri=token_data["token_uri"],
            client_id=token_data["client_id"],
            client_secret=token_data["client_secret"],
            scopes=token_data["scopes"],
        )
        client.store_new_credentials(creds)
    finally:
        user_id_ctx.reset(ctx)

    return JSONResponse({
        "status": "success",
        "message": "Google OAuth completed. Token stored securely.",
        "user_id": uid,
        "scopes": token_data["scopes"],
    })


@router.get("/status")
def oauth_status():
    """Report OAuth status without exposing tokens."""
    uid = current_user_id() or "default"
    from app.google_client import get_google_client
    ctx = user_id_ctx.set(uid)
    try:
        client = get_google_client()
        has_token = client.has_token()
    finally:
        user_id_ctx.reset(ctx)
    return {"authenticated": has_token, "credentials_dir": settings.google_credentials_dir, "user_id": uid}


@router.post("/revoke")
def revoke_token():
    """Clear the stored Google OAuth token for the current user.

    Requires bearer token. Deletes the per-user token file. The user
    will need to re-authorize at /oauth/start afterward.
    """
    uid = current_user_id()
    if uid is None:
        uid = "default"
    from app.google_client import get_google_client
    ctx = user_id_ctx.set(uid)
    try:
        client = get_google_client()
        token_store = client.token_store
        token_exists = token_store.exists()
        if token_exists:
            token_store.delete()
            log.info("Token revoked for user %s", uid)
            return JSONResponse({
                "status": "success",
                "user_id": uid,
                "message": f"Token for user {uid} revoked. Re-authorize at /oauth/start.",
            })
        return JSONResponse({
            "status": "success",
            "user_id": uid,
            "message": f"No token found for user {uid} — nothing to revoke.",
        })
    finally:
        user_id_ctx.reset(ctx)
