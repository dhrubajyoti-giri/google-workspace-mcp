"""Configuration for the Google API Bridge.

All values are configurable via environment variables — nothing is hardcoded
at runtime.  See ``.env.example`` for the full list.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # ── External URL (for OAuth callbacks) ──
    external_url: str = "https://google-api.mcp.dg.linkpc.net"

    # ── Google OAuth credentials ──
    google_client_secret_file: str = "/config/client_secret.json"
    google_token_file: str = "/config/token.json"
    google_credentials_dir: str = "/config"

    # ── Google API scopes ──
    # Full-access scopes (read + write) so a single OAuth consent covers all tools.
    google_scopes_raw: str = Field(
        # ── DEFAULT: read-only scopes (safe, no data modification) ──
        # Override with GOOGLE_SCOPES=<full-access> in .env for write tools.
        default=(
            "https://www.googleapis.com/auth/gmail.readonly,"
            "https://www.googleapis.com/auth/drive.readonly,"
            "https://www.googleapis.com/auth/documents.readonly,"
            "https://www.googleapis.com/auth/spreadsheets.readonly,"
            "https://www.googleapis.com/auth/calendar.readonly,"
            "https://www.googleapis.com/auth/presentations.readonly,"
            "https://www.googleapis.com/auth/contacts.readonly,"
            "https://www.googleapis.com/auth/tasks.readonly,"
            "openid,"
            "https://www.googleapis.com/auth/userinfo.email,"
            "https://www.googleapis.com/auth/userinfo.profile"
        ),
        validation_alias=AliasChoices("GOOGLE_SCOPES", "GOOGLE_SCOPES_RAW"),
    )
    # ── Full-access scopes (uncomment to enable read+write tools) ──
    # _FULL_ACCESS_SCOPES = (
    #     "https://www.googleapis.com/auth/gmail.modify,"
    #     "https://www.googleapis.com/auth/gmail.send,"
    #     "https://www.googleapis.com/auth/gmail.compose,"
    #     "https://www.googleapis.com/auth/gmail.metadata,"
    #     "https://www.googleapis.com/auth/gmail.settings.basic,"
    #     "https://www.googleapis.com/auth/gmail.labels,"
    #     "https://www.googleapis.com/auth/gmail.insert,"
    #     "https://www.googleapis.com/auth/drive,"
    #     "https://www.googleapis.com/auth/drive.file,"
    #     "https://www.googleapis.com/auth/drive.readonly,"
    #     "https://www.googleapis.com/auth/documents,"
    #     "https://www.googleapis.com/auth/spreadsheets,"
    #     "https://www.googleapis.com/auth/calendar,"
    #     "https://www.googleapis.com/auth/presentations,"
    #     "https://www.googleapis.com/auth/contacts,"
    #     "https://www.googleapis.com/auth/forms.body,"
    #     "https://www.googleapis.com/auth/forms.responses,"
    #     "https://www.googleapis.com/auth/tasks,"
    #     "https://www.googleapis.com/auth/chat.bot,"
    #     "https://www.googleapis.com/auth/chat.messages,"
    #     "https://www.googleapis.com/auth/chat.spaces,"
    #     "openid,"
    #     "https://www.googleapis.com/auth/userinfo.email,"
    #     "https://www.googleapis.com/auth/userinfo.profile"
    # )

    @property
    def google_scopes(self) -> list[str]:
        return _parse_scopes(self.google_scopes_raw)

    # ── MCP server info ──
    mcp_server_name: str = "google-api-bridge"
    mcp_server_version: str = "1.0.0"

    # ── MCP bearer token (security: only QwenPaw knows this) ──
    mcp_bearer_token: str = Field(
        default="",
        validation_alias=AliasChoices("AUTH_TOKEN", "MCP_BEARER_TOKEN", "MCP_AUTH_TOKEN"),
    )

    # ── Misc ──
    tz: str = Field(default="Asia/Kolkata", validation_alias=AliasChoices("TZ", "TZ_VALUE"))
    log_level: str = "INFO"


settings = Settings()
