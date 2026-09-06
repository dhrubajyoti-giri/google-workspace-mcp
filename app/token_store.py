"""Secure persistence of Google OAuth refresh tokens."""
from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path
from typing import Any


class TokenStore:
    """File-based token store with restrictive permissions (0600).

    The token JSON contains the refresh_token, client_id, client_secret,
    token_uri, etc. — treat it as a credential.
    """

    def __init__(self, token_file: str):
        self._path = Path(token_file)

    # ── public API ──

    def exists(self) -> bool:
        return self._path.exists()

    def load(self) -> dict[str, Any] | None:
        if not self._path.exists():
            return None
        with open(self._path, "r") as f:
            return json.load(f)

    def save(self, token_data: dict[str, Any]) -> None:
        """Save (or overwrite) the token file with 0600 permissions."""
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Write atomically to avoid partial reads.
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(token_data, f)
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600 — owner only
        tmp.replace(self._path)

    def delete(self) -> None:
        if self._path.exists():
            self._path.unlink()

    # ── convenience ──

    def is_expired(self) -> bool:
        """Return True if the stored token is expired (or absent)."""
        data = self.load()
        if data is None:
            return True
        # Credentials object stores expiry as ISO string in 'expiry'.
        expiry_str = data.get("expiry")
        if not expiry_str:
            return True
        from datetime import datetime, timezone
        try:
            expiry = datetime.fromisoformat(
                expiry_str.replace("Z", "+00:00")
            )
            return expiry <= datetime.now(timezone.utc)
        except (ValueError, TypeError):
            return True
