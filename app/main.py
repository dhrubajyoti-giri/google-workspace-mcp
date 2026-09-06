"""Google API Bridge — main FastAPI application.

Exposes:
  GET  /healthz        — health check
  GET  /oauth/start     — start OAuth flow (redirect to Google)
  GET  /oauth/callback  — OAuth callback
  GET  /oauth/status    — oauth status (no secrets exposed)
  POST /mcp/*           — MCP streamable HTTP endpoint
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("google-api-bridge")

# ── MCP server ────────────────────────────────────────────────
# Imported at module level so the session manager exists before the
# lifespan context enters run().  The ASGI app is created once below,
# after the FastAPI app and bearer-guard middleware are defined, so
# the mount wraps the real transport app.
from app.mcp_server import mcp, create_mcp_asgi


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks — including MCP session manager lifecycle."""
    log.info("Google API Bridge starting up (v%s)", settings.mcp_server_version)
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
    log.info("Google API Bridge shutting down")


app = FastAPI(
    title="Google API Bridge",
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
        "service": "Google API Bridge",
        "mcp_endpoint": "/mcp",
        "oauth_start": "/oauth/start",
        "healthz": "/healthz",
    })
