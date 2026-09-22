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
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import jwt
from pydantic import AnyUrl


from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    RefreshToken,
    TokenError,
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

    All read-modify-write cycles are guarded by an internal lock so concurrent
    token exchanges/refreshes in one process can't interleave and duplicate
    entries or resurrect deleted JTIs.
    """

    def __init__(self, token_file: str | Path | None = None):
        self._path = Path(token_file) if token_file else None
        self._lock = threading.Lock()

        # Migration: rename old file name (mcp_tokens.json) to current (refresh_tokens.json)
        if self._path and self._path.name == "refresh_tokens.json":
            old_path = self._path.parent / "mcp_tokens.json"
            if old_path.exists() and not self._path.exists():
                shutil.move(str(old_path), str(self._path))
                log.info("Migrated MCP token store: %s → %s", old_path, self._path)

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
        try:
            return dt.astimezone(ZoneInfo(settings.tz)).isoformat()
        except Exception:
            # Invalid TZ config — store as UTC rather than crashing store().
            log.warning("Invalid timezone %r, storing MCP token expiry as UTC", settings.tz)
            return dt.isoformat()

    @staticmethod
    def _expiry_to_timestamp(exp: Any) -> float | None:
        """Normalize a stored expiry (Unix seconds or ISO string) to epoch seconds.

        Naive ISO strings are interpreted as UTC (matching
        ``_expires_at_to_iso``, which serializes aware UTC datetimes).
        Returns None when the value is missing or unparseable.
        """
        if isinstance(exp, (int, float)):
            return float(exp)
        if isinstance(exp, str):
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(exp)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except (ValueError, TypeError):
                return None
        return None

    @classmethod
    def _is_expired(cls, exp: Any, now: float) -> bool:
        """Fail closed: a missing or unparseable expiry counts as expired."""
        ts = cls._expiry_to_timestamp(exp)
        if ts is None:
            return True
        return ts < now

    def _cleanup_expired(self) -> None:
        """Remove entries whose expiry is in the past (or unparseable).

        Handles both the old Unix-timestamp format (int/float) and the
        ISO-string format that ``store()`` writes.
        """
        now = time.time()
        with self._lock:
            expired = [
                k for k, v in self._tokens.items()
                if self._is_expired(v.get("expires_at"), now)
            ]
            if expired:
                for k in expired:
                    del self._tokens[k]
                log.info("Cleaned up %d expired MCP token(s)", len(expired))
                self._save()

    def store(self, token_id: str, subject: str, client_id: str, scopes: list[str], expires_at: int) -> None:
        with self._lock:
            self._tokens[token_id] = {
                "subject": subject,
                "client_id": client_id,
                "scopes": scopes,
                "expires_at": self._expires_at_to_iso(expires_at),
            }
            self._save()

    def rotate(
        self,
        old_jti: str | None,
        new_jti: str,
        subject: str,
        client_id: str,
        scopes: list[str],
        expires_at: int,
    ) -> None:
        """Atomically store a rotated refresh token and drop the old JTI.

        Single lock + single disk write, so a crash or a concurrent exchange
        can't leave old and new tokens valid at the same time (replay) or
        lose the new token while the old one is gone.
        """
        with self._lock:
            self._tokens[new_jti] = {
                "subject": subject,
                "client_id": client_id,
                "scopes": scopes,
                "expires_at": self._expires_at_to_iso(expires_at),
            }
            if old_jti:
                self._tokens.pop(old_jti, None)
            self._save()

    def delete_by_subject_and_client(self, subject: str, client_id: str) -> int:
        """Delete all tokens for a given subject AND client_id.

        More targeted than a subject-wide delete — only removes tokens
        issued to the same user+client combination, preserving tokens
        that other MCP clients (e.g. Claude Desktop vs QwenPaw) may
        have for the same user.

        Returns the number of deleted entries.
        """
        with self._lock:
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

    def get_valid_by_subject(self, subject: str, client_id: str | None = None) -> dict[str, Any] | None:
        """Find a valid (non-expired) refresh token entry for a subject.

        Used by server-side access-token auto-refresh: when an MCP access
        token is expired, we decode it (without verifying expiry) to get the
        ``sub``, then look up a still-valid MCP refresh token for that user.
        If found, a new access token is issued transparently — no 401 to the
        MCP client, no manual re-authorization.

        Prefers an entry issued to ``client_id``; falls back to any valid
        entry for the subject so clients that rotate client_ids (dynamic
        client registration) keep working. Expired entries encountered are
        purged (lazy cleanup — the store no longer grows unbounded).

        Returns the stored token dict plus its ``jti``, or None.
        """
        now = time.time()
        with self._lock:
            match: dict[str, Any] | None = None
            fallback: dict[str, Any] | None = None
            expired: list[str] = []
            for jti, v in self._tokens.items():
                if v.get("subject") != subject:
                    continue
                if self._is_expired(v.get("expires_at"), now):
                    expired.append(jti)
                    continue
                entry = {**v, "jti": jti}
                if client_id is not None and v.get("client_id") == client_id:
                    match = entry
                    break
                if fallback is None:
                    fallback = entry
            if expired:
                for k in expired:
                    self._tokens.pop(k, None)
                self._save()
                log.info("Purged %d expired MCP token(s) for subject %s", len(expired), subject)
            return match if match is not None else fallback

    def get(self, token_id: str) -> dict[str, Any] | None:
        with self._lock:
            v = self._tokens.get(token_id)
            return dict(v) if v is not None else None

    def delete(self, token_id: str) -> None:
        with self._lock:
            if self._tokens.pop(token_id, None) is not None:
                self._save()


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


def _decode_jwt_no_exp(token: str) -> dict[str, Any] | None:
    """Decode an MCP JWT without enforcing expiry (signature + issuer still verified).

    Used when claims (e.g. ``jti``) are needed from a token that may already
    be expired — revocation and rotation must work on expired tokens too.
    """
    try:
        return jwt.decode(
            token,
            settings.mcp_jwt_secret,
            algorithms=["HS256"],
            options={"verify_exp": False, "verify_iat": True, "verify_aud": False},
            issuer=settings.external_url,
        )
    except jwt.PyJWTError:
        return None


# ── Permissive client (for unregistered MCP clients) ─────────────────────────

from mcp.shared.auth import OAuthClientInformationFull

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
            "jti": secrets.token_urlsafe(16),
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

        # Belt-and-braces: the store's own expiry must also hold. Normally it
        # mirrors the JWT exp, but a hand-edited store file or clock skew can
        # diverge them — fail closed and drop the stale entry.
        if _McpTokenStore._is_expired(stored.get("expires_at"), time.time()):
            log.info("Dropping stale MCP refresh token entry (jti=%s)", jti)
            self._mcp_tokens.delete(jti)
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
        # Defense-in-depth: the SDK's TokenHandler already rejects out-of-range
        # scopes, but direct callers bypass it — never escalate scopes here.
        granted = set(refresh_token.scopes or [])
        if not set(scopes) <= granted:
            raise TokenError(
                error="invalid_scope",
                error_description="cannot request scope not provided by refresh token",
            )

        now = int(time.time())
        subject = refresh_token.subject

        access_token = _sign_jwt({
            "sub": subject,
            "client_id": client.client_id,
            "jti": secrets.token_urlsafe(16),
            "scope": " ".join(scopes),
            "exp": now + settings.mcp_access_token_ttl,
            "token_type": "access",
        })

        # Rotate refresh token — atomically store the new JTI and drop the old
        # one (single lock + single write; see _McpTokenStore.rotate). The old
        # JTI is decoded WITHOUT enforcing expiry so rotation invalidates the
        # predecessor even if it just expired.
        new_jti = secrets.token_urlsafe(16)
        new_refresh = _sign_jwt({
            "sub": subject,
            "aud": client.client_id,
            "jti": new_jti,
            "scope": " ".join(scopes),
            "exp": now + settings.mcp_refresh_token_ttl,
            "token_type": "refresh",
        })

        old_payload = _decode_jwt_no_exp(refresh_token.token)
        old_jti = old_payload.get("jti") if old_payload else None
        self._mcp_tokens.rotate(
            old_jti,
            new_jti,
            subject=subject,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=now + settings.mcp_refresh_token_ttl,
        )
        if old_jti is None:
            log.warning("Rotated MCP refresh token for %s but could not decode old JTI", subject)

        return OAuthToken(
            access_token=access_token,
            refresh_token=new_refresh,
            expires_in=settings.mcp_access_token_ttl,
            scope=" ".join(scopes),
        )

    # ── Token verification ──

    def _try_auto_refresh_access_token(self, expired_token: str) -> AccessToken | None:
        """Server-side auto-refresh for expired MCP access tokens.

        When ``_verify_jwt`` returns None because the access token is expired,
        decode it without verifying expiry to extract the subject. Then check
        if a valid (non-expired) MCP refresh token exists for that user in the
        token store. If found, issue a new access token and return it.

        This is a server-side workaround for MCP clients that don't implement
        automatic token refresh on 401 (e.g., QwenPaw's HttpStatelessClient),
        eliminating the need for manual re-authorization every 8 hours.

        The STORED refresh entry's scopes/client_id are authoritative — the
        expired token's may predate a scope change. The user must also still
        exist in the Google registry (a de-authorized user gets a 401 and
        re-authorizes instead of fresh tokens).
        """
        payload = _decode_jwt_no_exp(expired_token)
        if payload is None:
            return None

        # Only auto-refresh access tokens (not refresh tokens)
        if payload.get("token_type") != "access":
            return None

        subject = payload.get("sub", "")
        if not subject:
            return None

        if not self._registry.user_exists(subject):
            return None

        # Check if a valid (non-expired) refresh token exists for this subject,
        # preferring one issued to the same client.
        stored = self._mcp_tokens.get_valid_by_subject(
            subject, payload.get("client_id") or None
        )
        if stored is None:
            return None

        scopes = stored.get("scopes") or []
        client_id = stored.get("client_id") or ""

        # Issue a new access token (same claim schema as the exchange paths:
        # no aud on access tokens, jti always present).
        now = int(time.time())
        new_token = _sign_jwt({
            "sub": subject,
            "client_id": client_id,
            "jti": secrets.token_urlsafe(16),
            "scope": " ".join(scopes),
            "exp": now + settings.mcp_access_token_ttl,
            "token_type": "access",
        })
        log.info("Server-side auto-refreshed access token for subject %s", subject)

        return AccessToken(
            token=new_token,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + settings.mcp_access_token_ttl,
            resource=None,
            subject=subject,
            claims={"refreshed": True},
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Verify an MCP access token (JWT). Returns AccessToken with subject=Google email.

        If the token is expired, attempts server-side auto-refresh using the
        stored MCP refresh token — no 401 to the client, no manual re-auth needed.
        """
        payload = _verify_jwt(token)
        if payload is None:
            # Token is expired or invalid — try server-side auto-refresh
            return self._try_auto_refresh_access_token(token)
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
        """Revoke an MCP token.

        Refresh tokens are deleted by JTI — decoded without enforcing expiry
        so already-expired tokens are actually removed instead of lingering.
        Revoking an access token also drops that subject+client's refresh
        entries, so no new access tokens can be auto-refreshed afterwards.
        """
        # For refresh tokens, we need to invalidate in the store
        if isinstance(token, RefreshToken):
            # The token string is a JWT; extract jti (expiry not enforced —
            # an expired-but-stored token must still be deletable)
            payload = _decode_jwt_no_exp(token.token)
            if payload and payload.get("jti"):
                self._mcp_tokens.delete(payload["jti"])
            log.info("MCP refresh token revoked for subject %s", token.subject or "unknown")
        elif isinstance(token, AccessToken):
            if token.subject:
                removed = self._mcp_tokens.delete_by_subject_and_client(
                    token.subject, token.client_id or ""
                )
                log.info("MCP access token revoked for subject=%s (dropped %d refresh entries)",
                         token.subject, removed)
            else:
                log.info("MCP access token revocation ignored (no subject)")

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
            "jti": secrets.token_urlsafe(16),
            "scope": " ".join(scopes),
            "exp": int(time.time()) + settings.mcp_access_token_ttl,
            "token_type": "access",
        })


# ── Module-level singletons ─────────────────────────────────────────────────
# Initialized at import time. oauth.py and main.py import these.

_registry_file = settings.registry_file
_mcp_tokens_file = str(Path(_registry_file).with_name("refresh_tokens.json"))
registry = Registry(_registry_file)
provider = GoogleOAuthProvider(registry, _mcp_tokens_file)
