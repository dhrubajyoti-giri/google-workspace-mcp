"""Centralized Google Credentials management.

Owns the OAuth refresh-token and auto-refreshes access tokens.
All service modules call ``get_service()`` instead of doing their own auth.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from app.config import settings
from app.token_store import TokenStore

log = logging.getLogger("google-workspace-mcp")


class GoogleClient:
    """Singleton-style credential manager + service factory."""

    _instance: "GoogleClient | None" = None

    def __init__(self):
        self._token_store = TokenStore(settings.google_token_file)
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
                "No Google token stored. Complete OAuth first via /oauth/start"
            )

        self._creds = Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
            scopes=settings.google_scopes,
        )

        if self._creds.expired:
            log.info("Refreshing expired Google access token")
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


def get_google_client() -> GoogleClient:
    """Module-level singleton accessor."""
    if GoogleClient._instance is None:
        GoogleClient._instance = GoogleClient()
    return GoogleClient._instance
