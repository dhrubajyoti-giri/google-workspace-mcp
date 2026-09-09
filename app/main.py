"""Google Workspace MCP — main FastAPI application.

Exposes:
  GET  /healthz                              — health check (public)
  GET  /                                     — service info (public)
  GET  /.well-known/oauth-authorization-server — OAuth server metadata
  GET  /.well-known/oauth-protected-resource/mcp — protected resource metadata
  GET  /authorize                             — MCP OAuth entry (redirect to scope selector)
  POST /token                                 — exchange MCP auth code / refresh token
  POST /register                              — dynamic client registration
  POST /revoke                                — revoke MCP token
  GET  /oauth/scale                           — scope selection page (public)
  POST /oauth/scale                           — submit scopes (public)
  GET  /oauth/callback                        — Google OAuth callback (public)
  POST /mcp/*                                 — MCP streamable HTTP (requires MCP bearer token,
                                                EXCEPT server/discover which is pre-auth)

Auth model:
  - MCP OAuth endpoints (/authorize, /token, /register, /revoke) and
    protected-resource metadata are served by the MCP SDK at the root level.
  - /mcp/* is wrapped with MCPAuthMiddleware — returns 401 with
    WWW-Authenticate (resource_metadata) if no valid MCP bearer token,
    EXCEPT for server/discover POSTs which are allowed through pre-auth
    (MCP 2026-07-28 spec: discover is a pre-authentication negotiation step).
  - AuthenticationMiddleware validates the bearer token and sets scope["user"].
  - AuthContextMiddleware sets a contextvar so MCP tools can call
    get_access_token().subject to identify the Google user.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.mcp_server import mcp, create_mcp_asgi
from app.oauth import router as oauth_router
from app.oauth_provider import provider, registry

# ── MCP SDK auth components ────────────────────────────────────
from mcp.server.auth.middleware.bearer_auth import (
    AuthenticatedUser,
    BearerAuthBackend,
)
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.auth.routes import (
    create_auth_routes,
    create_protected_resource_routes,
    build_resource_metadata_url,
)
from starlette.authentication import AuthCredentials
from starlette.middleware.authentication import AuthenticationMiddleware

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("google-workspace-mcp")


# ── MCP OAuth settings ─────────────────────────────────────────
# These are used to:
# 1. Build the AuthSettings for the MCP SDK
# 2. Mount auth routes at the root level via create_auth_routes()
# 3. Wrap /mcp with MCPAuthMiddleware
auth_settings = AuthSettings(
    issuer_url=str(settings.external_url).rstrip("/"),
    resource_server_url=f"{str(settings.external_url).rstrip('/')}/mcp",
    required_scopes=None,  # no MCP-level scope gating — any authenticated user can call tools
    client_registration_options=ClientRegistrationOptions(
        enabled=True,  # allow dynamic client registration (QwenPaw, Claude Desktop, etc.)
        valid_scopes=settings.all_scopes + ["openid", "email", "profile"],
    ),
    revocation_options=RevocationOptions(enabled=True),
)

# Token verifier — used by BearerAuthBackend to validate MCP bearer tokens
token_verifier = ProviderTokenVerifier(provider)

# Auth routes (mount at root level on FastAPI)
_auth_routes = create_auth_routes(
    provider=provider,
    issuer_url=auth_settings.issuer_url,
    client_registration_options=auth_settings.client_registration_options,
    revocation_options=auth_settings.revocation_options,
)

# Protected resource metadata routes (RFC 9728)
_protected_routes = create_protected_resource_routes(
    resource_url=auth_settings.resource_server_url,
    authorization_servers=[auth_settings.issuer_url],
    scopes_supported=settings.all_scopes,
)

# Resource metadata URL for the 401 WWW-Authenticate header
_resource_metadata_url = build_resource_metadata_url(auth_settings.resource_server_url)


# ── ServerDiscoverMiddleware (QwenPaw protocol fix) ────────────
def _get_supported_protocol_versions(client_version: str | None = None) -> list[str]:
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
    versions = list(SUPPORTED_PROTOCOL_VERSIONS)
    if client_version and client_version not in versions:
        versions.append(client_version)
    return versions


def _add_protocol_version(client_version: str) -> None:
    if not client_version:
        return
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
    if client_version not in SUPPORTED_PROTOCOL_VERSIONS:
        SUPPORTED_PROTOCOL_VERSIONS.append(client_version)


def _get_client_protocol_version(scope: dict) -> str | None:
    for key, value in scope.get("headers", []):
        if key == b"mcp-protocol-version":
            return value.decode()
    return None


class ServerDiscoverMiddleware:
    """Intercept QwenPaw's non-standard ``server/discover`` JSON-RPC method.

    QwenPaw sends ``server/discover`` before ``initialize``. The MCP SDK
    does not implement this method. This middleware intercepts it and
    responds with the supported protocol versions.

    Dynamically appends the client's protocol version to
    SUPPORTED_PROTOCOL_VERSIONS so any future version is accepted.
    """

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any):
        if scope["type"] != "http" or scope.get("method", "") != "POST":
            await self.app(scope, receive, send)
            return

        # Dynamic version patching (forward-compatible)
        client_version = _get_client_protocol_version(scope)
        if client_version:
            _add_protocol_version(client_version)

        # Read the full request body
        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.request":
                body += message.get("body", b"")
                more = message.get("more_body", False)
            elif message["type"] == "http.disconnect":
                break

        # Check if this is a server/discover request
        try:
            data = json.loads(body)
            if isinstance(data, dict) and data.get("method") == "server/discover":
                supported = _get_supported_protocol_versions(client_version)
                response_body = json.dumps({
                    "jsonrpc": "2.0",
                    "id": data.get("id"),
                    "result": {"supportedVersions": supported},
                })
                response_headers = [
                    (b"content-type", b"application/json"),
                ]
                if client_version:
                    response_headers.append(
                        (b"mcp-protocol-version", client_version.encode())
                    )
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": response_headers,
                })
                await send({
                    "type": "http.response.body",
                    "body": response_body.encode(),
                })
                return
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
            pass

        # Re-inject body for the MCP ASGI app
        async def _receive():
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, _receive, send)


# ── Custom BearerAuthBackend (fixes server/discover 503) ──────────
import time as _time


class MCPBearerAuthBackend(BearerAuthBackend):
    """BearerAuthBackend that does not raise on invalid tokens.

    The stock BearerAuthBackend.authenticate() calls
    token_verifier.verify_token(token) which may raise for an
    expired / malformed token. Starlette's AuthenticationMiddleware
    catches that and fires on_error -> 401 **before** the mounted
    /mcp app (with MCPAuthMiddleware) ever runs. That makes
    server/discover (which should be pre-auth) fail with 401,
    breaking QwenPaw's init() -> connect() -> 503 "inactive".

    Fix: return None for server/discover POSTs (skip auth entirely),
    and catch verify_token exceptions (return None instead of raising).
    """

    async def authenticate(self, conn):
        # server/discover is a pre-auth negotiation step — skip bearer check
        for key, value in conn.headers.items():
            if key.lower() == "mcp-method":
                method = value if isinstance(value, str) else value.decode("ascii", "replace")
                if method == "server/discover":
                    return None

        auth_header = conn.headers.get("authorization")
        if not auth_header or not auth_header.lower().startswith("bearer "):
            return None

        token = auth_header[7:]
        try:
            auth_info = await self.token_verifier.verify_token(token)
        except Exception:
            return None
        if not auth_info:
            return None
        if auth_info.expires_at and auth_info.expires_at < int(_time.time()):
            return None
        return AuthCredentials(auth_info.scopes), AuthenticatedUser(auth_info)


# ── MCP Auth Middleware (allows server/discover without bearer token) ──
class MCPAuthMiddleware:
    """Auth middleware for /mcp that allows ``server/discover`` without a bearer token.

    The MCP 2026-07-28 spec defines ``server/discover`` as a pre-authentication
    negotiation step — the client sends it *before* obtaining or presenting an
    MCP bearer token. The SDK's ``RequireAuthMiddleware`` rejects all requests
    without a valid bearer token (401), which causes the MCP client's
    ``connect()`` → ``_negotiate()`` → ``server/discover`` to fail, leaving the
    QwenPaw driver handler stuck in "inactive" state (503 on all subsequent
    tool calls).

    This middleware is a drop-in replacement for ``RequireAuthMiddleware`` that
    additionally lets ``server/discover`` POSTs through when no authenticated
    user is present. All other requests require a valid bearer token.
    """

    def __init__(self, app: Any, required_scopes: list[str], resource_metadata_url: Any | None = None):
        self.app = app
        self.required_scopes = required_scopes
        self.resource_metadata_url = resource_metadata_url

    def _is_discover(self, scope: dict) -> bool:
        """Check if the request is a server/discover POST (identified by the mcp-method header)."""
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return False
        for raw_key, raw_value in scope.get("headers", []):
            key = raw_key.decode("ascii", errors="replace").lower()
            if key == "mcp-method":
                return raw_value.decode("ascii", errors="replace") == "server/discover"
        return False

    async def _send_auth_error(self, send: Any, status_code: int, error: str, description: str) -> None:
        www_auth_parts = [f'error="{error}"', f'error_description="{description}"']
        if self.resource_metadata_url:
            www_auth_parts.append(f'resource_metadata="{self.resource_metadata_url}"')
        www_authenticate = f"Bearer {', '.join(www_auth_parts)}"
        body = json.dumps({"error": error, "error_description": description}).encode()
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", www_authenticate.encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        auth_user = scope.get("user")

        # server/discover is a pre-auth negotiation step — allow without bearer token
        if self._is_discover(scope) and not isinstance(auth_user, AuthenticatedUser):
            await self.app(scope, receive, send)
            return

        if not isinstance(auth_user, AuthenticatedUser):
            await self._send_auth_error(send, 401, "invalid_token", "Authentication required")
            return

        auth_credentials = scope.get("auth")
        for required_scope in self.required_scopes:
            if auth_credentials is None or required_scope not in auth_credentials.scopes:
                await self._send_auth_error(send, 403, "insufficient_scope", f"Required scope: {required_scope}")
                return

        await self.app(scope, receive, send)


# ── Lifespan ───────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Google Workspace MCP starting up (v%s)", settings.mcp_server_version)
    log.info("External URL: %s", settings.external_url)
    log.info("OAuth callback: %s/oauth/callback", settings.external_url)
    log.info("MCP endpoint: %s/mcp", settings.external_url)
    log.info("OAuth flow: %s/authorize (MCP OAuth) — client discovers endpoints automatically", settings.external_url)

    if not os.path.exists(settings.google_client_secret_file):
        log.warning(
            "Google client_secret.json not found at %s — OAuth will not work until placed there",
            settings.google_client_secret_file,
        )

    registry_users = registry.list_users()
    if registry_users:
        log.info("Registry: %d authorized user(s): %s", len(registry_users), [u["email"] for u in registry_users])
    else:
        log.info("Registry: no authorized users — authorize via your MCP client")

    async with mcp.session_manager.run():
        log.info("MCP session manager started")
        yield

    log.info("MCP session manager stopped")
    log.info("Google Workspace MCP shutting down")


app = FastAPI(
    title="Google Workspace MCP",
    version=settings.mcp_server_version,
    lifespan=lifespan,
)

# ── Auth middleware (runs for ALL HTTP requests) ─────────────
# Order matters: AuthenticationMiddleware (outer, validates token) must run
# BEFORE AuthContextMiddleware (inner, reads scope["user"] → sets contextvar).
# Starlette add_middleware() stacks in reverse — the last added is outermost,
# so AuthContextMiddleware is added first (inner), then AuthenticationMiddleware (outer).
app.add_middleware(AuthContextMiddleware)
app.add_middleware(
    AuthenticationMiddleware,
    backend=MCPBearerAuthBackend(token_verifier),
    # on_error only fires if authenticate() raises; MCPBearerAuthBackend never
    # raises (returns None instead), so this is a safe fallback.
    on_error=lambda conn, exc: JSONResponse({"detail": str(exc)}, status_code=401),
)

# ── Auth routes at root level (public endpoints) ─────────────
# These are served by the MCP SDK — mounted directly on the FastAPI app.
for route in _auth_routes:
    app.routes.append(route)

# Protected resource metadata (RFC 9728)
for route in _protected_routes:
    app.routes.append(route)

# ── OAuth web UI router (public endpoints) ───────────────────
app.include_router(oauth_router)

# ── MCP mount (protected — requires MCP bearer token) ────────
# Middleware order — ServerDiscoverMiddleware MUST be outermost so it can
# intercept QwenPaw's server/discover JSON-RPC calls (sent as standard
# JSON-RPC in the body, NOT as an mcp-method header) and respond with 200
# before MCPAuthMiddleware rejects them with 401.
#
# Flow: ServerDiscoverMiddleware → MCPAuthMiddleware → MCP ASGI app
mcp_asgi = create_mcp_asgi()
protected_mcp = MCPAuthMiddleware(
    mcp_asgi,
    required_scopes=[],  # no MCP-level scope requirements — token required only
    resource_metadata_url=_resource_metadata_url,
)
# ServerDiscoverMiddleware intercepts server/discover POSTs (pre-auth
# protocol negotiation) so QwenPaw's connect() succeeds without a bearer token.
mcp_asgi = ServerDiscoverMiddleware(protected_mcp)
app.mount("/mcp", mcp_asgi)


# ── Health + root (public) ───────────────────────────────────

@app.get("/healthz")
def healthz():
    """Health check — reports service, MCP, and OAuth status.

    Public endpoint (no bearer token required).
    """
    users = registry.list_users()
    return JSONResponse({
        "status": "ok" if users else "degraded",
        "version": settings.mcp_server_version,
        "mcp_enabled": True,
        "oauth_configured": bool(users),
        "registered_users": [u["email"] for u in users],
        "mcp_auth": f"{settings.external_url}/oauth/scale",
    })


@app.get("/")
def root():
    return JSONResponse({
        "service": "Google Workspace MCP",
        "mcp_endpoint": "/mcp",
        "oauth_metadata": "/.well-known/oauth-authorization-server",
        "healthz": "/healthz",
    })