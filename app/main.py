"""Google Workspace MCP — main FastAPI application.

Exposes:
  GET  /healthz        — health check
  GET  /oauth/start     — start OAuth flow (redirect to Google)
  GET  /oauth/callback  — OAuth callback
  GET  /oauth/status    — oauth status (no secrets exposed)
  POST /mcp/*           — MCP streamable HTTP endpoint
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

# ── Protocol version helper ────────────────────────────────────
def _get_supported_protocol_versions(client_version: str | None = None) -> list[str]:
    """Return supported MCP protocol versions.

    Reads from the (patched) mcp SDK's SUPPORTED_PROTOCOL_VERSIONS list.
    If ``client_version`` is provided and not already in the list, it is
    dynamically appended — this makes the fix forward-compatible: any
    future QwenPaw protocol version is accepted automatically.
    """
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
    versions = list(SUPPORTED_PROTOCOL_VERSIONS)
    if client_version and client_version not in versions:
        versions.append(client_version)
    return versions


def _add_protocol_version(client_version: str) -> None:
    """Dynamically add a protocol version to SUPPORTED_PROTOCOL_VERSIONS.

    Called by ServerDiscoverMiddleware on every request so that the
    MCP SDK's _validate_protocol_version() also accepts the version.
    """
    if not client_version:
        return
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
    if client_version not in SUPPORTED_PROTOCOL_VERSIONS:
        SUPPORTED_PROTOCOL_VERSIONS.append(client_version)


def _get_client_protocol_version(scope: dict) -> str | None:
    """Extract mcp-protocol-version from ASGI request headers."""
    for key, value in scope.get("headers", []):
        if key == b"mcp-protocol-version":
            return value.decode()
    return None


class ServerDiscoverMiddleware:
    """Intercept QwenPaw's non-standard ``server/discover`` JSON-RPC method.

    QwenPaw's MCP client sends a ``server/discover`` JSON-RPC request before
    calling ``initialize``, to discover the server's supported protocol versions.
    The MCP SDK does **not** implement this method — it returns an error for
    unknown methods, which prevents the modern stateless transport from being
    used (QwenPaw sees the error and does not fall back to legacy).

    This middleware intercepts ``server/discover`` requests **before** they
    reach the MCP SDK and returns the supported protocol versions.

    ── Forward compatibility ──
    Rather than hardcoding a specific version, the middleware dynamically
    adds the client's advertised protocol version (from the
    ``mcp-protocol-version`` request header) to the SDK's
    ``SUPPORTED_PROTOCOL_VERSIONS`` list.  This means any future QwenPaw
    protocol version — not just ``2026-07-28`` — is accepted automatically,
    with no code changes needed.

    For all other requests, the body is re-injected and the request is
    passed through to the MCP ASGI app unchanged.
    """

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any):
        if scope["type"] != "http" or scope.get("method", "") != "POST":
            await self.app(scope, receive, send)
            return

        # ── Dynamic version patching (forward-compatible) ──
        # Add the client's protocol version to SUPPORTED_PROTOCOL_VERSIONS
        # so the MCP SDK's _validate_protocol_version() accepts it.
        client_version = _get_client_protocol_version(scope)
        if client_version:
            _add_protocol_version(client_version)

        # ── Read the full request body ──
        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.request":
                body += message.get("body", b"")
                more = message.get("more_body", False)
            elif message["type"] == "http.disconnect":
                # Client disconnected before sending body — pass through
                break

        # ── Check if this is a server/discover request ──
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

        # ── Re-inject body for the MCP ASGI app ──
        async def _receive():
            return {
                "type": "http.request",
                "body": body,
                "more_body": False,
            }

        await self.app(scope, _receive, send)

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("google-workspace-mcp")

# ── MCP server ────────────────────────────────────────────────
# Imported at module level so the session manager exists before the
# lifespan context enters run().  The ASGI app is created once below,
# after the FastAPI app and bearer-guard middleware are defined, so
# the mount wraps the real transport app.
from app.mcp_server import mcp, create_mcp_asgi


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks — including MCP session manager lifecycle."""
    log.info("Google Workspace MCP starting up (v%s)", settings.mcp_server_version)
    log.info("External URL: %s", settings.external_url)
    log.info("OAuth callback: %s/oauth/callback", settings.external_url)
    log.info("MCP endpoint: %s/mcp", settings.external_url)

    if not os.path.exists(settings.google_client_secret_file):
        log.warning("Google client_secret.json not found at %s — OAuth will not work until placed there", settings.google_client_secret_file)

    # ── Start MCP streamable HTTP session manager ──
    # The session manager's run() creates the async task group required
    # by the streamable HTTP protocol.  When mounted in FastAPI, the
    # Starlette app's own lifespan is not triggered, so we must enter
    # run() ourselves.
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

# ── OAuth routes ─────────────────────────────────────────────
from app.oauth import router as oauth_router

app.include_router(oauth_router)

# ── MCP mount ────────────────────────────────────────────────
# Bearer-token middleware: protects /mcp from unauthenticated access.
# Anyone with the token can invoke MCP tools; without it, 401.
mcp_asgi = create_mcp_asgi()

# Wrap with ServerDiscoverMiddleware to handle QwenPaw's non-standard
# server/discover JSON-RPC method (the MCP SDK doesn't implement it).
mcp_asgi = ServerDiscoverMiddleware(mcp_asgi)


@app.middleware("http")
async def _mcp_bearer_guard(request: Request, call_next):
    """Require Bearer token on /mcp/*; leave /oauth/* and /healthz open."""
    if request.url.path.startswith("/mcp"):
        expected = settings.mcp_bearer_token
        if not expected:
            # No token configured — log once and allow (dev mode)
            log.warning("MCP_BEARER_TOKEN not set — /mcp is open!")
            # fall through
        else:
            auth = request.headers.get("Authorization", "")
            if not auth or auth != f"Bearer {expected}":
                return JSONResponse(
                    {"detail": "Unauthorized — valid Bearer token required"},
                    status_code=401,
                )
    return await call_next(request)


app.mount("/mcp", mcp_asgi)


# ── Health ───────────────────────────────────────────────────

@app.get("/healthz")
def healthz():
    """Health check — reports service, MCP, and OAuth status."""
    from app.google_client import get_google_client
    client = get_google_client()
    has_token = client.has_token()

    return JSONResponse({
        "status": "ok" if has_token else "degraded",
        "version": settings.mcp_server_version,
        "mcp_enabled": True,
        "oauth_configured": has_token,
        "oauth_start": f"{os.environ.get('EXTERNAL_URL', settings.external_url)}/oauth/start",
    })


@app.get("/")
def root():
    return JSONResponse({
        "service": "Google Workspace MCP",
        "mcp_endpoint": "/mcp",
        "oauth_start": "/oauth/start",
        "healthz": "/healthz",
    })
