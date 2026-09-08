"""JSON-backed registry for Google OAuth tokens (multi-user).

Replaces the single ``token.json`` file.  Each authorized Google account
gets its own entry keyed by the user's email address:

```json
{
  "users": {
    "alice@example.com": {
      "user_id": "alice",
      "token": {
        "token": "ya29...",           // Google access token
        "refresh_token": "1//...",    // Google refresh token
        "token_uri": "https://oauth2.googleapis.com/token",
        "expiry": "2024-06-01T12:00:00+00:00"
      },
      "scopes": ["gmail.readonly", "drive.readonly"],  // user-selected scopes
      "authorized_at": "2024-06-01T12:00:00Z",
      "refreshed_at": "2024-06-01T12:00:00Z"
    }
  }
}
```

Note: ``client_id`` and ``client_secret`` are NOT stored per-user — they
are fixed per-deployment (from ``client_secret.json``) and loaded at
runtime by ``GoogleClient``.  ``scopes`` is stored once per user (the
user-selected scopes), not duplicated in ``token``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("google-workspace-mcp")


class Registry:
    """Thread-safe JSON registry for Google OAuth credentials.

    One entry per authorized Google account, keyed by email.
    """

    def __init__(self, registry_file: str | Path):
        self._path = Path(registry_file)
        self._lock = threading.RLock()
        self._cache: dict[str, Any] | None = None

    @property
    def path(self) -> Path:
        return self._path

    # ── internal ──

    def _load(self) -> dict[str, Any]:
        """Load registry from disk (or initialize empty)."""
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or "users" not in data:
                    return {"users": {}}
                return data
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Registry file corrupted, starting fresh: %s", e)
                return {"users": {}}
        return {"users": {}}

    def _save(self, data: dict[str, Any]) -> None:
        """Save registry to disk with 0600 permissions."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)

    # ── public API ──

    def get(self, email: str) -> dict[str, Any] | None:
        """Get a user's entry by Google email."""
        with self._lock:
            data = self._load()
            return data["users"].get(email)

    def save(self, email: str, token_data: dict[str, Any], scopes: list[str], user_id: str | None = None) -> None:
        """Save (or update) a user's entry in the registry."""
        with self._lock:
            data = self._load()
            data["users"][email] = {
                "user_id": user_id or email.split("@")[0],
                "token": token_data,
                "scopes": scopes,
                "authorized_at": data["users"].get(email, {}).get("authorized_at") or _now_iso(),
                "refreshed_at": _now_iso(),
            }
            self._save(data)
            log.info("Registry updated for user %s (scopes: %d)", email, len(scopes))

    def delete(self, email: str) -> bool:
        """Delete a user's entry. Returns True if existed."""
        with self._lock:
            data = self._load()
            if email in data["users"]:
                del data["users"][email]
                self._save(data)
                log.info("User %s removed from registry", email)
                return True
            return False

    def list_users(self) -> list[dict[str, Any]]:
        """List all authorized users (without token data)."""
        with self._lock:
            data = self._load()
            result = []
            for email, info in data["users"].items():
                result.append({
                    "email": email,
                    "user_id": info.get("user_id", email.split("@")[0]),
                    "scopes": info.get("scopes", []),
                    "authorized_at": info.get("authorized_at", ""),
                    "refreshed_at": info.get("refreshed_at", ""),
                })
            return result

    def user_exists(self, email: str) -> bool:
        """Check if a user is in the registry."""
        with self._lock:
            data = self._load()
            return email in data["users"]

    def update_scopes(self, email: str, scopes: list[str]) -> bool:
        """Update a user's scopes. Returns True if user exists."""
        with self._lock:
            data = self._load()
            if email in data["users"]:
                data["users"][email]["scopes"] = scopes
                data["users"][email]["refreshed_at"] = _now_iso()
                self._save(data)
                return True
            return False

    def get_token(self, email: str) -> dict[str, Any] | None:
        """Get a user's Google token data (for GoogleClient)."""
        with self._lock:
            data = self._load()
            user = data["users"].get(email)
            return user["token"] if user else None

    def get_scopes(self, email: str) -> list[str] | None:
        """Get a user's granted scopes."""
        with self._lock:
            data = self._load()
            user = data["users"].get(email)
            return user.get("scopes") if user else None


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
