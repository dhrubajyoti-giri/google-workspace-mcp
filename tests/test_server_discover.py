"""Tests for the ServerDiscoverMiddleware (QwenPaw protocol negotiation).

These tests exercise the ASGI middleware directly (no TestClient /
session-manager lifecycle) to avoid the 'run() called twice' error.
"""
import json
import pytest

from app.main import ServerDiscoverMiddleware, _get_supported_protocol_versions


def test_get_supported_protocol_versions_includes_2026_07_28():
    """The patched SUPPORTED_PROTOCOL_VERSIONS must include 2026-07-28."""
    versions = _get_supported_protocol_versions()
    assert isinstance(versions, list)
    assert len(versions) > 0
    assert "2026-07-28" in versions
    assert "2024-11-05" in versions


@pytest.mark.asyncio
async def test_server_discover_intercepts_request():
    """server/discover requests must be intercepted before reaching MCP SDK."""
    intercepted = False

    async def mock_app(scope, receive, send):
        nonlocal intercepted
        intercepted = True

    middleware = ServerDiscoverMiddleware(mock_app)

    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 42,
        "method": "server/discover",
        "params": {},
    }).encode()

    scope = {"type": "http", "method": "POST", "path": "/"}
    receive_calls = 0

    async def receive():
        nonlocal receive_calls
        if receive_calls == 0:
            receive_calls += 1
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)

    assert not intercepted  # mock_app should NOT be called
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200
    assert sent[1]["type"] == "http.response.body"
    data = json.loads(sent[1]["body"])
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == 42
    assert "supportedVersions" in data["result"]
    assert "2026-07-28" in data["result"]["supportedVersions"]


@pytest.mark.asyncio
async def test_non_discover_request_passes_through():
    """Non-server/discover requests must pass through with body re-injected."""
    passthrough = False

    async def mock_app(scope, receive, send):
        nonlocal passthrough
        passthrough = True
        # Read the re-injected body
        msg = await receive()
        assert msg["type"] == "http.request"
        assert msg["body"] == body
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [],
        })
        await send({
            "type": "http.response.body",
            "body": b"mcp-sdk-response",
        })

    middleware = ServerDiscoverMiddleware(mock_app)

    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 99,
        "method": "tools/list",
        "params": {},
    }).encode()

    scope = {"type": "http", "method": "POST", "path": "/"}

    receive_calls = 0

    async def receive():
        nonlocal receive_calls
        if receive_calls == 0:
            receive_calls += 1
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)

    assert passthrough
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"mcp-sdk-response"


@pytest.mark.asyncio
async def test_get_request_passes_through():
    """Non-POST requests should pass through immediately."""
    intercepted = False

    async def mock_app(scope, receive, send):
        nonlocal intercepted
        intercepted = True

    middleware = ServerDiscoverMiddleware(mock_app)
    scope = {"type": "http", "method": "GET", "path": "/"}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    await middleware(scope, receive, send)
    assert intercepted
