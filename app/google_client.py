"""Centralized Google Credentials management.

In the MCP OAuth flow, the MCP SDK's ``AuthContextMiddleware`` sets the
authenticated user (with subject=Google email) in a contextvar.
``get_google_client()`` reads this contextvar, looks up the correct
Google credentials in the registry, and returns a ``GoogleClient``.

Token auto-refresh is handled transparently — refreshed tokens are written
back to the registry.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

from app.config import settings
from app.registry import Registry

# Import the MCP auth context accessor
try:
    from mcp.server.auth.middleware.auth_context import get_access_token
except ImportError:
    # Fallback: no auth available (should not happen in MCP context)
    def get_access_token():
        return None

log = logging.getLogger("google-workspace-mcp")


def _to_local_iso(dt: datetime | None) -> str | None:
    """Convert a datetime to Asia/Kolkata ISO string (uses settings.tz)."""
    if dt is None:
        return None
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(settings.tz)
    if dt.tzinfo is None:
        # Assume UTC if no tzinfo (Google API returns UTC)
        from datetime import timezone as dt_tz
        dt = dt.replace(tzinfo=dt_tz.utc)
    return dt.astimezone(tz).isoformat()


class GoogleClient:
    """Per-user credential manager + service factory.

    Constructed from token data stored in the registry (keyed by Google email).
    """

    def __init__(self, email: str, token_data: dict[str, Any], scopes: list[str]):
        self._email = email
        self._token_data = token_data
        self._scopes = scopes
        self._creds: Credentials | None = None

    # ── credential lifecycle ──

    def get_credentials(self) -> Credentials:
        """Return valid Google credentials, refreshing if necessary."""
        if self._creds and self._creds.valid:
            return self._creds

        # client_id/client_secret are fixed (from client_secret.json) and not
        # stored per-user. Load from token_data (legacy) or from the file.
        client_id = self._token_data.get("client_id")
        client_secret = self._token_data.get("client_secret")
        if not client_id or not client_secret:
            client_id, client_secret = _load_client_creds_from_file()

        self._creds = Credentials(
            token=self._token_data.get("token"),
            refresh_token=self._token_data.get("refresh_token"),
            token_uri=self._token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=client_id,
            client_secret=client_secret,
            scopes=self._scopes,
        )

        if self._creds.expired:
            log.info("Refreshing expired Google access token for %s", self._email)
            self._creds.refresh(GoogleRequest())
            # Persist updated token back to registry (only token fields, not client creds)
            _persist_updated_token(self._email, self._creds)

        return self._creds

    # ── service factory ──

    def get_service(self, api: str, version: str = "v1") -> Any:
        """Build a Google API service object.

        Usage:
            gmail  = client.get_service("gmail")
            drive  = client.get_service("drive", "v3")
            docs   = client.get_service("docs", "v1")
            sheets = client.get_service("sheets", "v4")
            cal    = client.get_service("calendar", "v3")
            slides = client.get_service("slides", "v1")
        """
        creds = self.get_credentials()
        return build(api, version, credentials=creds)

    def has_token(self) -> bool:
        """Check whether the user has stored Google credentials."""
        return bool(self._token_data.get("token"))


# ── Per-request client factory ──────────────────────────────────────────────

# Module-level registry singleton (same instance as oauth_provider.py)
_registry = Registry(settings.registry_file)


def _load_client_creds_from_file() -> tuple[str | None, str | None]:
    """Load client_id and client_secret from client_secret.json.

    These are fixed per-deployment (not per-user), so we read them from the
    Google OAuth credentials file at runtime instead of storing them in the
    registry per user.
    """
    try:
        import json
        from pathlib import Path
        path = Path(settings.google_client_secret_file)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            conf = data.get("web") or data.get("installed") or data
            return conf.get("client_id"), conf.get("client_secret")
    except Exception:
        pass
    return None, None


def _persist_updated_token(email: str, creds: Credentials) -> None:
    """Write refreshed credentials back to the registry.

    Only stores token-specific fields (token, refresh_token, expiry).
    client_id/client_secret are NOT stored per-user (loaded from file).
    scopes are NOT stored in token_data (registry top-level scopes is authoritative).
    """
    _registry.save(email, {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "expiry": _to_local_iso(creds.expiry) if creds.expiry else None,
    }, _registry.get_scopes(email) or settings.default_scopes)


def get_google_client() -> GoogleClient:
    """Return the GoogleClient for the current request's authenticated user.

    Reads the MCP access token from the auth contextvar (set by
    ``AuthContextMiddleware``). The token's ``subject`` is the user's
    Google email, which is used to look up credentials in the registry.

    Raises RuntimeError if not authenticated or user not found in registry.
    """
    access_token = get_access_token()
    if access_token is None:
        raise RuntimeError(
            "Not authenticated. The MCP client must complete the OAuth flow first."
        )

    subject = access_token.subject
    if not subject:
        raise RuntimeError("Access token has no subject (Google email)")

    token_data = _registry.get_token(subject)
    if token_data is None:
        raise RuntimeError(
            f"No Google credentials found for {subject}. "
            "Complete OAuth via your MCP client"
        )

    scopes = _registry.get_scopes(subject) or settings.default_scopes
    return GoogleClient(email=subject, token_data=token_data, scopes=scopes)


def get_registry() -> Registry:
    """Access the shared Registry singleton."""
    return _registry
