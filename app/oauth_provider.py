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

import logging
import secrets
import time
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
    """Stores MCP refresh tokens (mapped to Google email) for revocation + refresh."""

    def __init__(self):
        # refresh_token_id → {subject, client_id, scopes, expires_at}
        self._tokens: dict[str, dict[str, Any]] = {}

    def store(self, token_id: str, subject: str, client_id: str, scopes: list[str], expires_at: int) -> None:
        self._tokens[token_id] = {
            "subject": subject,
            "client_id": client_id,
            "scopes": scopes,
            "expires_at": expires_at,
        }

    def get(self, token_id: str) -> dict[str, Any] | None:
        return self._tokens.get(token_id)

    def delete(self, token_id: str) -> None:
        self._tokens.pop(token_id, None)


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
    """Verify an MCP JWT. Returns payload if valid, None if invalid/expired."""
    try:
        return jwt.decode(
            token,
            settings.mcp_jwt_secret,
            algorithms=["HS256"],
            audience=None,  # we validate issuer + subject manually
            options={"verify_exp": True, "verify_iat": True},
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

    def __init__(self, registry: Registry):
        self._registry = registry
        self._auth_requests = _AuthRequestStore()
        self._mcp_tokens = _McpTokenStore()
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
        """Look up an MCP refresh token."""
        payload = _verify_jwt(refresh_token)
        if payload is None:
            return None
        if payload.get("token_type") != "refresh":
            return None
        if payload.get("aud") != client.client_id:
            return None
        if payload.get("iss") != settings.external_url:
            return None

        jti = payload.get("jti")
        stored = self._mcp_tokens.get(jti) if jti else None
        if stored is None:
            return None
        if stored["client_id"] != client.client_id:
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
        self._mcp_tokens.delete(refresh_token.token)  # revoke old

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

registry = Registry(settings.registry_file)
provider = GoogleOAuthProvider(registry)
