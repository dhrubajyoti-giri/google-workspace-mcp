"""Tests for the ServerDiscoverMiddleware (QwenPaw protocol negotiation).

These tests exercise the ASGI middleware directly (no TestClient /
session-manager lifecycle) to avoid the 'run() called twice' error.

Verifies:
  1. server/discover returns supported versions including the client's version
  2. Future protocol versions (not just 2026-07-28) are accepted dynamically
  3. Non-discovery requests pass through with body re-injected
  4. Non-POST requests pass through immediately
"""
import json
import pytest

from app.main import (
    ServerDiscoverMiddleware,
    _get_supported_protocol_versions,
    _add_protocol_version,
    _get_client_protocol_version,
)


def test_get_supported_protocol_versions_includes_current():
    """The SDK's supported versions plus any dynamically added ones."""
    versions = _get_supported_protocol_versions()
    assert isinstance(versions, list)
    assert len(versions) > 0
    assert "2024-11-05" in versions  # base SDK version


def test_dynamic_version_add():
    """_add_protocol_version should add unknown versions dynamically."""
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
    initial_count = len(SUPPORTED_PROTOCOL_VERSIONS)

    # Add a hypothetical future version
    _add_protocol_version("9999-12-31")
    assert "9999-12-31" in SUPPORTED_PROTOCOL_VERSIONS
    assert len(SUPPORTED_PROTOCOL_VERSIONS) == initial_count + 1

    # Adding again is a no-op (idempotent)
    _add_protocol_version("9999-12-31")
    assert len(SUPPORTED_PROTOCOL_VERSIONS) == initial_count + 1

    # None / empty string is a no-op
    _add_protocol_version(None)
    _add_protocol_version("")
    assert len(SUPPORTED_PROTOCOL_VERSIONS) == initial_count + 1


def test_get_client_protocol_version_from_headers():
    """_get_client_protocol_version extracts from ASGI scope headers."""
    assert _get_client_protocol_version({"headers": []}) is None
    assert _get_client_protocol_version({"headers": []}) is None
    result = _get_client_protocol_version({
        "headers": [(b"mcp-protocol-version", b"2026-07-28")]
    })
    assert result == "2026-07-28"


def test_future_version_is_in_supported_list():
    """get_supported_protocol_versions should include dynamically-added versions."""
    _add_protocol_version("2099-01-01")
    versions = _get_supported_protocol_versions()
    assert "2099-01-01" in versions


@pytest.mark.asyncio
async def test_server_discover_returns_client_version():
    """server/discover must echo back the client's protocol version."""
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

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"mcp-protocol-version", b"2099-12-31")],
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)

    assert not intercepted  # mock_app should NOT be called
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200
    # Response header should echo the client's version
    header_dict = dict(sent[0]["headers"])
    assert header_dict[(b"mcp-protocol-version")] == b"2099-12-31"
    # Result should include the client's version in supportedVersions
    data = json.loads(sent[1]["body"])
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == 42
    assert "supportedVersions" in data["result"]
    assert "2099-12-31" in data["result"]["supportedVersions"]


@pytest.mark.asyncio
async def test_non_discover_request_passes_through():
    """Non-server/discover requests must pass through with body re-injected."""
    passthrough = False

    async def mock_app(scope, receive, send):
        nonlocal passthrough
        passthrough = True
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

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

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
