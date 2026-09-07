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
  POST /mcp/*                                 — MCP streamable HTTP (requires MCP bearer token)

Auth model:
  - MCP OAuth endpoints (/authorize, /token, /register, /revoke) and
    protected-resource metadata are served by the MCP SDK at the root level.
  - /mcp/* is wrapped with RequireAuthMiddleware — returns 401 with
    WWW-Authenticate (resource_metadata) if no valid MCP bearer token.
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
    BearerAuthBackend,
    RequireAuthMiddleware,
)
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.auth.routes import (
    create_auth_routes,
    create_protected_resource_routes,
    build_resource_metadata_url,
)
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
# 3. Wrap /mcp with RequireAuthMiddleware
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
    backend=BearerAuthBackend(token_verifier),
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
# RequireAuthMiddleware checks scope["user"] (set by AuthenticationMiddleware).
# If not authenticated → 401 with WWW-Authenticate (resource_metadata URL).
mcp_asgi = create_mcp_asgi()
mcp_asgi = ServerDiscoverMiddleware(mcp_asgi)  # handle QwenPaw's server/discover
protected_mcp = RequireAuthMiddleware(
    mcp_asgi,
    required_scopes=[],  # no MCP-level scope requirements — see Point 3: token required only
    resource_metadata_url=_resource_metadata_url,
)
app.mount("/mcp", protected_mcp)


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
