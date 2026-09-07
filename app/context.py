"""Request-scoped user context via ContextVar.

The bearer-token middleware (app/main.py) extracts the user_id from the
bearer token on every HTTP request and sets it here.  Downstream code —
including the MCP SDK's tool dispatch — reads it via ``current_user_id()``
to route to the correct Google credentials.

ContextVars propagate through asyncio.create_task() and async context
boundaries, so the MCP SDK's internal task scheduling preserves the user
context set in the middleware.
"""
from __future__ import annotations

from contextvars import ContextVar

#: The authenticated user's ID (set by the bearer-token middleware).
#: ``None`` when no user context is active (public endpoints, or
#: single-user fallback mode).
user_id: ContextVar[str | None] = ContextVar("user_id", default=None)


def current_user_id() -> str | None:
    """Return the current request's user_id, or None if not set."""
    return user_id.get()
