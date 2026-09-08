"""Configuration for the Google Workspace MCP.

All values are configurable via environment variables — nothing is hardcoded
at runtime.  See ``.env.example`` for the full list.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("google-workspace-mcp")


def _parse_scopes(raw: str) -> list[str]:
    """Parse a comma-separated or space-separated scope list."""
    if not raw:
        return []
    return [s.strip() for s in raw.replace(",", " ").split() if s.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Server ──
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000

    # ── External URL (for OAuth callbacks + metadata discovery) ──
    external_url: str = "https://mcp.example.com"
    google_client_secret_file: str = "/secrets/client_secret.json"
    google_credentials_dir: str = "/secrets"
    registry_file: str = "/secrets/registry.json"

    # ── MCP JWT secret (signs access_tokens + refresh_tokens) ──
    mcp_jwt_secret: str = Field(
        default="change-me-in-production",
        validation_alias=AliasChoices("MCP_JWT_SECRET"),
    )

    # ── Google API scopes ──
    # These are the DEFAULT fallback lists. In production, configure scopes
    # via env vars:
    #   AVAILABLE_SCOPES  — comma-separated list of all scopes to show in selector
    #   READONLY_SCOPES   — comma-separated subset marked as read-only (green in UI)
    #   GOOGLE_SCOPES     — comma-separated default scopes if MCP client specifies none
    _read_scopes: list[str] = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/documents.readonly",
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/presentations.readonly",
        "https://www.googleapis.com/auth/contacts.readonly",
        "https://www.googleapis.com/auth/tasks.readonly",
    ]

    _write_scopes: list[str] = [
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.metadata",
        "https://www.googleapis.com/auth/gmail.settings.basic",
        "https://www.googleapis.com/auth/gmail.labels",
        "https://www.googleapis.com/auth/gmail.insert",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/presentations",
        "https://www.googleapis.com/auth/contacts",
        "https://www.googleapis.com/auth/tasks",
    ]

    # Chat scopes — enable with ENABLE_CHAT_SCOPES=true
    _chat_scopes: list[str] = [
        "https://www.googleapis.com/auth/chat.messages",
        "https://www.googleapis.com/auth/chat.spaces",
    ]

    # ── Env-var-driven scope configuration ──
    # AVAILABLE_SCOPES: all scopes to show in the scope selector (comma-separated)
    #   If not set, falls back to _read_scopes + _write_scopes (+ chat if enabled)
    available_scopes_raw: str = Field(
        default="",
        validation_alias=AliasChoices("AVAILABLE_SCOPES"),
    )
    # READONLY_SCOPES: subset of AVAILABLE_SCOPES that are read-only (green UI)
    #   If not set, falls back to _read_scopes
    readonly_scopes_raw: str = Field(
        default="",
        validation_alias=AliasChoices("READONLY_SCOPES"),
    )
    # Enable Chat API scopes (requires Chat API enabled in GCP console)
    enable_chat_scopes: bool = Field(
        default=False,
        validation_alias=AliasChoices("ENABLE_CHAT_SCOPES"),
    )

    @property
    def all_scopes(self) -> list[str]:
        """All available Google API scopes shown in the scope selector.

        Order:
        1. AVAILABLE_SCOPES env var (if set — user's custom list)
        2. _read_scopes + _write_scopes + optional _chat_scopes (default fallback)
        """
        env_scopes = _parse_scopes(self.available_scopes_raw)
        if env_scopes:
            # Add identity scopes that are always needed
            for s in ("openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile"):
                if s not in env_scopes:
                    env_scopes.append(s)
            return env_scopes
        scopes = self._read_scopes + self._write_scopes
        if self.enable_chat_scopes:
            scopes += self._chat_scopes
        scopes += [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
        ]
        return scopes

    @property
    def read_scopes(self) -> list[str]:
        """Scopes marked as read-only (for UI coloring)."""
        env_read = _parse_scopes(self.readonly_scopes_raw)
        if env_read:
            return env_read
        return self._read_scopes

    @property
    def default_scopes(self) -> list[str]:
        """Scopes used if MCP client doesn't request specific ones."""
        env_scopes = _parse_scopes(self.google_scopes_raw)
        if env_scopes:
            return env_scopes
        return self._read_scopes + [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
        ]

    @property
    def google_scopes(self) -> list[str]:
        """Effective Google scopes (from env or default)."""
        return self.default_scopes

    # ── Human-readable scope descriptions for the scope selector UI ──
    # Maps scope string → (display name, category)
    _scope_labels: dict[str, str] = {
        # Gmail
        "https://www.googleapis.com/auth/gmail.readonly": "Gmail — Read only",
        "https://www.googleapis.com/auth/gmail.modify": "Gmail — Read + modify",
        "https://www.googleapis.com/auth/gmail.send": "Gmail — Send",
        "https://www.googleapis.com/auth/gmail.compose": "Gmail — Compose drafts",
        "https://www.googleapis.com/auth/gmail.metadata": "Gmail — Metadata (headers only)",
        "https://www.googleapis.com/auth/gmail.settings.basic": "Gmail — Settings",
        "https://www.googleapis.com/auth/gmail.labels": "Gmail — Labels",
        "https://www.googleapis.com/auth/gmail.insert": "Gmail — Insert messages",
        # Drive
        "https://www.googleapis.com/auth/drive.readonly": "Drive — Read only",
        "https://www.googleapis.com/auth/drive": "Drive — Full access",
        "https://www.googleapis.com/auth/drive.file": "Drive — Per-file access",
        # Docs
        "https://www.googleapis.com/auth/documents.readonly": "Docs — Read only",
        "https://www.googleapis.com/auth/documents": "Docs — Full access",
        # Sheets
        "https://www.googleapis.com/auth/spreadsheets.readonly": "Sheets — Read only",
        "https://www.googleapis.com/auth/spreadsheets": "Sheets — Full access",
        # Calendar
        "https://www.googleapis.com/auth/calendar.readonly": "Calendar — Read only",
        "https://www.googleapis.com/auth/calendar": "Calendar — Full access",
        # Presentations
        "https://www.googleapis.com/auth/presentations.readonly": "Slides — Read only",
        "https://www.googleapis.com/auth/presentations": "Slides — Full access",
        # Other
        "https://www.googleapis.com/auth/contacts.readonly": "Contacts — Read only",
        "https://www.googleapis.com/auth/contacts": "Contacts — Full access",
        "https://www.googleapis.com/auth/tasks.readonly": "Tasks — Read only",
        "https://www.googleapis.com/auth/tasks": "Tasks — Full access",
        "https://www.googleapis.com/auth/chat.messages": "Chat — Messages",
        "https://www.googleapis.com/auth/chat.spaces": "Chat — Spaces",
    }

    @property
    def scope_labels(self) -> dict[str, str]:
        """Return scope → label mapping for UI display."""
        return self._scope_labels.copy()

    # ── MCP server info ──
    mcp_server_name: str = "google-workspace-mcp"
    mcp_server_version: str = "1.0.0"

    # ── OAuth token lifetimes ──
    mcp_access_token_ttl: int = 8 * 3600       # 8 hours
    mcp_refresh_token_ttl: int = 30 * 86400     # 30 days
    mcp_auth_code_ttl: int = 600                # 10 minutes

    # ── Misc ──
    tz: str = Field(default="Asia/Kolkata", validation_alias=AliasChoices("TZ", "TZ_VALUE"))
    log_level: str = "INFO"

    # ── Additional allowed hosts for DNS rebinding protection ──
    # Comma-separated list of Host header values to accept (in addition to the
    # external URL domain and localhost). Useful for internal Docker hostnames
    # when the MCP client connects via the container's service name.
    # Example: MCP_ALLOWED_HOSTS=gws-mcp,gws-mcp:*
    allowed_hosts_raw: str = Field(
        default="",
        validation_alias=AliasChoices("MCP_ALLOWED_HOSTS"),
    )

    # ── Scope selector UI mode ──
    # "all"       → show every available Google scope in the selector (user picks freely)
    # "requested" → show only scopes requested by the MCP client (plus existing grants)
    scope_selector_mode: str = Field(
        default="all",
        validation_alias=AliasChoices("SCOPE_SELECTOR_MODE"),
    )

settings = Settings()
