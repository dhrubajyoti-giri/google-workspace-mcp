"""Centralized Google Credentials management.

Owns the OAuth refresh-token and auto-refreshes access tokens.
All service modules call ``get_google_client()`` instead of doing their
own auth.  In multi-user mode, the client is selected per-request based
on the ``user_id`` contextvar set by the bearer-token middleware.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from app.config import settings
from app.context import current_user_id
from app.token_store import TokenStore

log = logging.getLogger("google-workspace-mcp")


class GoogleClient:
    """Per-user credential manager + service factory.

    In multi-user mode, a separate instance is created per user_id.
    In single-user mode, one instance is used (user_id="default").
    """

    def __init__(self, user_id: str = "default"):
        self._user_id = user_id
        # Per-user token file: token-default.json (single-user) or
        # token-{user_id}.json (multi-user)
        if user_id == "default":
            token_file = settings.google_token_file
        else:
            token_file = f"{settings.google_token_file.rsplit('.', 1)[0]}-{user_id}.json"
        self._token_store = TokenStore(token_file)
        self._creds: Credentials | None = None

    # ── credential lifecycle ──

    @property
    def token_store(self) -> TokenStore:
        return self._token_store

    def has_token(self) -> bool:
        return self._token_store.exists()

    def get_credentials(self) -> Credentials:
        """Return valid Google credentials, refreshing if necessary."""
        if self._creds and self._creds.valid:
            return self._creds

        token_data = self._token_store.load()
        if token_data is None:
            raise RuntimeError(
                f"No Google token stored for user '{self._user_id}'. "
                "Complete OAuth first via /oauth/start"
            )

        # In multi-user mode, use the user's specific scopes if defined;
        # otherwise fall back to the global default scopes.
        scopes = None
        if settings.is_multi_user:
            user_scopes = settings.user_scopes(self._user_id)
            scopes = user_scopes  # may be None → use scopes from token

        self._creds = Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
            scopes=scopes or settings.google_scopes,
        )

        if self._creds.expired:
            log.info("Refreshing expired Google access token for user '%s'", self._user_id)
            self._creds.refresh(Request())
            self._persist(self._creds)

        return self._creds

    def _persist(self, creds: Credentials) -> None:
        self._token_store.save({
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "expiry": creds.expiry.isoformat() if creds.expiry else None,
            "scopes": list(creds.scopes) if creds.scopes else [],
        })

    def store_new_credentials(self, creds: Credentials) -> None:
        """Persist credentials after a fresh OAuth exchange."""
        self._creds = creds
        self._persist(creds)

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


# ── Per-request client factory ────────────────────────────────
# In multi-user mode, reads user_id from contextvar (set by middleware).
# In single-user mode, returns the "default" client.
_client_cache: dict[str, GoogleClient] = {}


def get_google_client() -> GoogleClient:
    """Return the GoogleClient for the current request's user.

    Reads ``user_id`` from the ContextVar (set by the bearer-token
    middleware). Falls back to "default" when no context is set
    (e.g. healthz endpoint, single-user mode).
    """
    uid = current_user_id()
    if uid is None:
        uid = "default"
    if uid not in _client_cache:
        _client_cache[uid] = GoogleClient(user_id=uid)
    return _client_cache[uid]
