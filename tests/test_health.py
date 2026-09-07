"""Tests for /healthz and / endpoints.

Uses a module-scoped TestClient so the MCP session manager's run()
context (which can only be entered once) is shared across both tests.
"""
from fastapi.testclient import TestClient
from app.main import app
import pytest


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_healthz_returns_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "version" in data
    assert data["status"] in ("ok", "degraded")
    assert data["mcp_enabled"] is True
    assert "oauth_start" in data


def test_root_returns_info(client):
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert "service" in data
    assert data["service"] == "Google Workspace MCP"
    assert "/mcp" in data["mcp_endpoint"]
    assert "/oauth/start" in data["oauth_start"]
