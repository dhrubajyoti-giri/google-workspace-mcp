"""MCP OAuth Authorization Server Provider — proxies Google OAuth.

Implements the ``OAuthAuthorizationServerProvider`` protocol from the MCP SDK.
When mounted via ``create_auth_routes()``, the MCP SDK exposes:

  GET  /.well-known/oauth-authorization-server  — server metadata
  GET  /.well-known/oauth-protected-resource/mcp — resource metadata
  GET  /authorize  — start auth (redirect to scope selector → Google)
  POST /token      — exchange MCP auth code / refresh token
  POST /register   — dynamic client registration (if enabled)
  POST /revoke     — revoke MCP token

The provider maps MCP tokens to Google accounts in the registry. Each MCP
access token is a JWT signed with ``MCP_JWT_SECRET``; its ``sub`` claim is the
Google email, which is used to look up the user's Google credentials.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

import jwt
from pydantic import AnyUrl


from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from app.config import settings
from app.registry import Registry

log = logging.getLogger("google-workspace-mcp")


# ── In-memory stores (mcp auth codes + refresh token IDs) ─────────────────

class _AuthRequestStore:
    """Stores MCP auth requests pending completion (across Google redirect chain)."""

    def __init__(self):
        self._requests: dict[str, dict[str, Any]] = {}

    def store(self, req: dict[str, Any]) -> str:
        """Store an MCP auth request. Returns the request ID."""
        req_id = secrets.token_urlsafe(16)
        req["request_id"] = req_id
        self._requests[req_id] = req
        return req_id

    def get(self, req_id: str) -> dict[str, Any] | None:
        r = self._requests.get(req_id)
        if r is None:
            return None
        if r.get("expires_at", 0) < time.time():
            del self._requests[req_id]
            return None
        return r

    def delete(self, req_id: str) -> None:
        self._requests.pop(req_id, None)


class _McpTokenStore:
    """Stores MCP refresh tokens (mapped to Google email) for revocation + refresh.

    Persists to disk so refresh tokens survive server restarts.
    File format: JSON mapping refresh_token_jti → {subject, client_id, scopes, expires_at}
    ``expires_at`` is stored as an ISO-8601 string in the configured local timezone
    (``settings.tz``, e.g. Asia/Kolkata).
    """

    def __init__(self, token_file: str | Path | None = None):
        self._path = Path(token_file) if token_file else None
        self._tokens: dict[str, dict[str, Any]] = self._load()
        # Purge expired entries on load so stale tokens don't accumulate
        self._cleanup_expired()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._path and self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError) as e:
                log.warning("MCP token store corrupted, starting fresh: %s", e)
        return {}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._tokens, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)

    @staticmethod
    def _expires_at_to_iso(expires_at: int) -> str:
        """Convert a Unix-seconds expiry to a human-readable local-time ISO string."""
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(expires_at, tz=timezone.utc)
        return dt.astimezone(ZoneInfo(settings.tz)).isoformat()

    def _cleanup_expired(self) -> None:
        """Remove entries whose expiry is in the past.

        Handles both the old Unix-timestamp format (int/float) and the new
        ISO-string format that ``store()`` writes.
        """
        now = time.time()
        expired = []
        for k, v in self._tokens.items():
            exp = v.get("expires_at")
            if isinstance(exp, (int, float)):
                if exp < now:
                    expired.append(k)
            elif isinstance(exp, str):
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(exp)
                    if dt.timestamp() < now:
                        expired.append(k)
                except (ValueError, TypeError):
                    pass
        if expired:
            for k in expired:
                del self._tokens[k]
            log.info("Cleaned up %d expired MCP token(s)", len(expired))
            self._save()

    def store(self, token_id: str, subject: str, client_id: str, scopes: list[str], expires_at: int) -> None:
        self._tokens[token_id] = {
            "subject": subject,
            "client_id": client_id,
            "scopes": scopes,
            "expires_at": self._expires_at_to_iso(expires_at),
        }
        self._save()

    def delete_by_subject(self, subject: str) -> int:
        """Delete all tokens for a given subject (user email).

        Called during re-authorization to prevent duplicate JTI entries
        in ``mcp_tokens.json``. When a user re-authorizes with a specific
        MCP client, the old refresh token JTI for that user+client combo
        is removed so it can't accumulate indefinitely.

        Returns the number of deleted entries.
        """
        to_delete = [
            k for k, v in self._tokens.items()
            if v.get("subject") == subject
        ]
        for k in to_delete:
            del self._tokens[k]
        if to_delete:
            log.info("Deleted %d old MCP token(s) for subject %s (re-authorization)",
                     len(to_delete), subject)
            self._save()
        return len(to_delete)

    def delete_by_subject_and_client(self, subject: str, client_id: str) -> int:
        """Delete all tokens for a given subject AND client_id.

        More targeted than ``delete_by_subject`` — only removes tokens
        issued to the same user+client combination, preserving tokens
        that other MCP clients (e.g. Claude Desktop vs QwenPaw) may
        have for the same user.

        Returns the number of deleted entries.
        """
        to_delete = [
            k for k, v in self._tokens.items()
            if v.get("subject") == subject and v.get("client_id") == client_id
        ]
        for k in to_delete:
            del self._tokens[k]
        if to_delete:
            log.info("Deleted %d old MCP token(s) for subject %s / client %s (re-authorization)",
                     len(to_delete), subject, client_id)
            self._save()
        return len(to_delete)

    def get(self, token_id: str) -> dict[str, Any] | None:
        return self._tokens.get(token_id)

    def delete(self, token_id: str) -> None:
        self._tokens.pop(token_id, None)
        self._save()


# ── Request-scoped state (for Google OAuth callback coordination) ─────────

# Maps Google OAuth state → {request_id, mcp_state, step, selected_scopes}
_google_oauth_state: dict[str, dict[str, Any]] = {}


# ── MCP JWT helpers ────────────────────────────────────────────────────────

def _sign_jwt(payload: dict[str, Any]) -> str:
    """Sign a JWT with MCP_JWT_SECRET (HS256)."""
    now = int(time.time())
    payload = {
        **payload,
        "iss": settings.external_url,
        "iat": now,
    }
    return jwt.encode(payload, settings.mcp_jwt_secret, algorithm="HS256")


def _verify_jwt(token: str) -> dict[str, Any] | None:
    """Verify an MCP JWT. Returns payload if valid, None if invalid/expired.

    Audience verification is DISABLED (verify_aud=False) because refresh tokens
    carry an ``aud`` claim bound to the client_id at issuance time. In PyJWT 2.x,
    ``audience=None`` actually REJECTS tokens that HAVE an ``aud`` claim (treats
    it as "no audience expected") — this silently broke every refresh-token
    verification, causing daily 401s. We verify issuer + exp + iat in PyJWT and
    check the ``aud`` claim manually in load_refresh_token() if needed.
    """
    try:
        return jwt.decode(
            token,
            settings.mcp_jwt_secret,
            algorithms=["HS256"],
            options={"verify_exp": True, "verify_iat": True, "verify_aud": False},
            issuer=settings.external_url,
        )
    except jwt.PyJWTError:
        return None


# ── Permissive client (for unregistered MCP clients) ─────────────────────────

from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata

class PermissiveOAuthClient(OAuthClientInformationFull):
    """Client that accepts any redirect_uri and any scope.

    Used for MCP clients that call /authorize without prior registration.
    Registered clients (from /register) get their full metadata back instead.
    """

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl | None:
        """Accept any redirect_uri — the bridge does not restrict where
        MCP clients receive the authorization code."""
        if redirect_uri is not None:
            return redirect_uri
        if self.redirect_uris is not None and len(self.redirect_uris) == 1:
            return self.redirect_uris[0]
        return AnyUrl("https://localhost")

    def validate_scope(self, requested_scope: str | None) -> list[str] | None:
        """Accept any requested scope."""
        if requested_scope is None:
            return None
        return requested_scope.split(" ")


# ── Provider ────────────────────────────────────────────────────────────────

class GoogleOAuthProvider:
    """OAuthAuthorizationServerProvider that proxies to Google OAuth.

    The MCP client gets an MCP token (JWT) whose ``sub`` claim is the
    Google user's email.  The bridge uses this to look up the correct
    Google credentials in the registry.
    """

    def __init__(self, registry: Registry, token_file: str | Path | None = None):
        self._registry = registry
        self._auth_requests = _AuthRequestStore()
        self._mcp_tokens = _McpTokenStore(token_file)
        # In-memory auth codes: code_string → AuthorizationCode
        self._auth_codes: dict[str, AuthorizationCode] = {}
        # Registered MCP clients: client_id → OAuthClientInformationFull
        self._clients: dict[str, OAuthClientInformationFull] = {}

    # ── Client management ──

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Return client info if the client_id is known.

        Returns the stored client from /register if found. Falls back to
        a permissive inline client (any redirect_uri accepted) for clients
        that skip registration.
        """
        # Return stored client from registration if available
        if client_id in self._clients:
            return self._clients[client_id]
        # Fallback: permissive client for unregistered MCP clients
        if client_id:
            return PermissiveOAuthClient(
                client_id=client_id,
                client_secret=None,
                token_endpoint_auth_method="none",
                redirect_uris=None,
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope=None,
            )
        return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Register a new MCP client.

        For simplicity, we accept all registrations. The client_id is
        generated by the client or provided by the MCP client implementation.
        """
        log.info("New MCP client registered: %s (redirect_uris=%s)", client_info.client_id, client_info.redirect_uris)
        self._clients[client_info.client_id] = client_info

    # ── Authorization ──

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: Any,  # AuthorizationParams
    ) -> str:
        """Called when MCP client hits /authorize.

        Stores the MCP auth request and returns a URL to our scope-selection
        page, which will then redirect to Google.
        """
        # Store the MCP client's auth request
        req_id = self._auth_requests.store({
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "code_challenge": params.code_challenge,
            "scopes": params.scopes or [],
            "mcp_state": params.state,  # original state from MCP client
            "resource": params.resource,
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "expires_at": time.time() + settings.mcp_auth_code_ttl,
        })

        # The client's requested scopes are used as defaults in the scope selector
        client_scopes = params.scopes or []

        # Redirect to our scope selection page.
        # client_scopes are already stored in the auth request; scope_selector
        # reads them from there — no need to pass them in the URL (avoid
        # unencoded spaces in query strings).
        return f"{settings.external_url}/oauth/scale?rid={req_id}"

    # ── Authorization code ──

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        """Look up an MCP authorization code we generated."""
        code = self._auth_codes.get(authorization_code)
        if code is None:
            return None
        if code.expires_at < time.time():
            del self._auth_codes[authorization_code]
            return None
        return code

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        """Exchange MCP auth code for MCP access + refresh tokens (JWTs)."""
        now = int(time.time())
        subject = authorization_code.subject

        # Generate MCP access token (JWT)
        access_token = _sign_jwt({
            "sub": subject,
            "client_id": client.client_id,
            "scope": " ".join(authorization_code.scopes),
            "exp": now + settings.mcp_access_token_ttl,
            "token_type": "access",
        })

        # Generate MCP refresh token (JWT with unique jti)
        refresh_jti = secrets.token_urlsafe(16)
        refresh_token = _sign_jwt({
            "sub": subject,
            "aud": client.client_id,
            "jti": refresh_jti,
            "scope": " ".join(authorization_code.scopes),
            "exp": now + settings.mcp_refresh_token_ttl,
            "token_type": "refresh",
        })

        # Store refresh token for verification/revocation
        # Clean up old refresh tokens for this user+client first (prevents
        # duplicate JTI accumulation during re-authorization)
        self._mcp_tokens.delete_by_subject_and_client(subject, client.client_id)
        self._mcp_tokens.store(
            refresh_jti,
            subject=subject,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=now + settings.mcp_refresh_token_ttl,
        )

        # Clean up the used auth code
        self._auth_codes.pop(authorization_code.code, None)

        log.info("Issued MCP tokens for user %s (client=%s, scopes=%d)",
                 subject, client.client_id, len(authorization_code.scopes))

        return OAuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.mcp_access_token_ttl,
            scope=" ".join(authorization_code.scopes),
        )

    # ── Refresh token ──

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        """Look up an MCP refresh token.

        Validates the JWT signature, expiry, issuer, token type, and JTI
        existence in the token store. Does NOT bind the refresh token to a
        specific client_id — this allows MCP clients that use dynamic client
        registration (which may rotate client_ids across sessions) to still
        refresh tokens. The subject (Google email) in the JWT is the binding
        identity, and the JTI check prevents use of revoked tokens.
        """
        payload = _verify_jwt(refresh_token)
        if payload is None:
            return None
        if payload.get("token_type") != "refresh":
            return None
        if payload.get("iss") != settings.external_url:
            return None

        jti = payload.get("jti")
        stored = self._mcp_tokens.get(jti) if jti else None
        if stored is None:
            return None

        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=stored["scopes"],
            expires_at=payload.get("exp"),
            subject=payload.get("sub"),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Exchange MCP refresh token for a new access + refresh token."""
        now = int(time.time())
        subject = refresh_token.subject

        access_token = _sign_jwt({
            "sub": subject,
            "client_id": client.client_id,
            "scope": " ".join(scopes),
            "exp": now + settings.mcp_access_token_ttl,
            "token_type": "access",
        })

        # Rotate refresh token
        new_jti = secrets.token_urlsafe(16)
        new_refresh = _sign_jwt({
            "sub": subject,
            "aud": client.client_id,
            "jti": new_jti,
            "scope": " ".join(scopes),
            "exp": now + settings.mcp_refresh_token_ttl,
            "token_type": "refresh",
        })

        self._mcp_tokens.store(
            new_jti,
            subject=subject,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=now + settings.mcp_refresh_token_ttl,
        )
        # Revoke old refresh token (extract JTI from JWT, not the JWT string)
        old_payload = _verify_jwt(refresh_token.token)
        if old_payload and old_payload.get("jti"):
            self._mcp_tokens.delete(old_payload["jti"])

        return OAuthToken(
            access_token=access_token,
            refresh_token=new_refresh,
            expires_in=settings.mcp_access_token_ttl,
            scope=" ".join(scopes),
        )

    # ── Token verification ──

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Verify an MCP access token (JWT). Returns AccessToken with subject=Google email."""
        payload = _verify_jwt(token)
        if payload is None:
            return None
        if payload.get("token_type") != "access":
            return None
        if payload.get("iss") != settings.external_url:
            return None

        subject = payload.get("sub")
        if not subject:
            return None

        scopes = payload.get("scope", "").split() if payload.get("scope") else []
        client_id = payload.get("client_id", "")

        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=payload.get("exp"),
            resource=None,
            subject=subject,
            claims=payload,
        )

    # ── Revocation ──

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke an MCP token."""
        # For refresh tokens, we need to invalidate in the store
        if isinstance(token, RefreshToken):
            # The token string is a JWT; extract jti
            payload = _verify_jwt(token.token)
            if payload and payload.get("jti"):
                self._mcp_tokens.delete(payload["jti"])
            log.info("MCP refresh token revoked for subject %s", token.subject or "unknown")
        elif isinstance(token, AccessToken):
            log.info("MCP access token verified for revocation (subject=%s)", token.subject or "unknown")

    def _generate_mcp_auth_code(
        self,
        client_id: str,
        code_challenge: str,
        redirect_uri: str,
        scopes: list[str],
        subject: str,
    ) -> str:
        """Generate a random MCP authorization code and store it.

        Called by the OAuth callback flow after Google OAuth completes.
        The code is later exchanged at /token via exchange_authorization_code().
        """
        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=scopes,
            expires_at=time.time() + settings.mcp_auth_code_ttl,
            client_id=client_id,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri if redirect_uri else None,
            redirect_uri_provided_explicitly=True,
            resource=None,
            subject=subject,
        )
        log.info("MCP auth code generated for subject=%s, client=%s", subject, client_id)
        return code

    def _issue_access_token(self, subject: str, scopes: list[str]) -> str:
        """Generate an MCP JWT access token (for manual testing)."""
        return _sign_jwt({
            "sub": subject,
            "scope": " ".join(scopes),
            "exp": int(time.time()) + settings.mcp_access_token_ttl,
            "token_type": "access",
        })


# ── Module-level singletons ─────────────────────────────────────────────────
# Initialized at import time. oauth.py and main.py import these.

_registry_file = settings.registry_file
_mcp_tokens_file = str(Path(_registry_file).with_name("mcp_tokens.json"))
registry = Registry(_registry_file)
provider = GoogleOAuthProvider(registry, _mcp_tokens_file)
