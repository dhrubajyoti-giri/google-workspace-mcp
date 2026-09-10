"""Tests for the OAuth flow: state store management, code exchange, cleanup."""
import asyncio
import time
import pytest
from unittest.mock import patch, MagicMock

from app.oauth import _google_state_store, _cleanup_stores, _exchange_code, _build_token_data
from app.oauth_provider import (
    provider,
    registry,
    _sign_jwt,
    _McpTokenStore,
    GoogleOAuthProvider,
    RefreshToken,
)


def test_cleanup_stores_removes_expired_entries():
    """Expired entries in both stores should be removed by _cleanup_stores."""
    _google_state_store.clear()

    # Add an expired entry (expires_at in the past)
    _google_state_store["expired"] = {
        "flow": MagicMock(),
        "rid": "expired_rid",
        "step": "identify",
        "created_at": 0,
        "expires_at": 0,
    }
    # Add a valid entry (expires_at in the future)
    _google_state_store["valid"] = {
        "flow": MagicMock(),
        "rid": "valid_rid",
        "step": "identify",
        "created_at": time.time(),
        "expires_at": time.time() + 600,
    }

    _cleanup_stores()

    assert "expired" not in _google_state_store
    assert "valid" in _google_state_store
    _google_state_store.clear()


def test_exchange_code_invalid_state():
    """Exchange with an unknown state should raise an HTTPException (400)."""
    _google_state_store.clear()
    try:
        _exchange_code("fake_code", "invalid_state")
        assert False, "Should have raised"
    except Exception as e:
        assert "Invalid" in str(e) or "expired" in str(e).lower() or "400" in str(e)
    _google_state_store.clear()


def test_google_state_store_uses_expires_at():
    """All entries in _google_state_store must have an expires_at field."""
    _google_state_store.clear()
    _google_state_store["test"] = {
        "flow": MagicMock(),
        "rid": "test",
        "step": "identify",
        "created_at": time.time(),
        "expires_at": time.time() + 600,
    }
    assert _google_state_store["test"]["expires_at"] > time.time()
    _google_state_store.clear()


def test_registry_roundtrip():
    """Registry should save and retrieve user credentials and scopes."""
    test_email = "test@example.com"
    test_creds = {
        "token": "test_token",
        "refresh_token": "test_refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "test_client",
        "client_secret": "test_secret",
        "expiry": "2030-01-01T00:00:00",
        "scopes": ["openid", "email"],
    }
    registry.save(test_email, test_creds, test_creds["scopes"])
    user = registry.get(test_email)
    assert user is not None
    assert user["token"]["token"] == "test_token"
    assert "openid" in user["scopes"]

    fetched_scopes = registry.get_scopes(test_email)
    assert "openid" in fetched_scopes

    registry.delete(test_email)
    assert registry.get(test_email) is None


def test_build_token_data_deduplicates_credentials():
    """_build_token_data should NOT store client_id/client_secret per-user.

    These are fixed per-deployment (from client_secret.json) and loaded at
    runtime by GoogleClient. Storing them per-user was redundant.
    """
    token_response = {
        "access_token": "test_token",
        "refresh_token": "test_refresh",
        "scope": "openid email",
        "id_token": "test_id_token",
    }
    mock_creds = MagicMock()
    mock_creds.token = "test_token"
    mock_creds.refresh_token = "test_refresh"
    mock_creds.token_uri = "https://oauth2.googleapis.com/token"
    mock_creds.client_id = "test_client_id"
    mock_creds.client_secret = "test_secret"
    mock_creds.expiry = None

    with patch("app.oauth._safe_get_credentials", return_value=mock_creds):
        creds_data = _build_token_data(token_response, MagicMock())

    # Only token-specific fields should be stored, no client creds or scopes
    assert "client_id" not in creds_data
    assert "client_secret" not in creds_data
    assert "scopes" not in creds_data
    assert creds_data["token"] == "test_token"
    assert creds_data["refresh_token"] == "test_refresh"


# ── Refresh token flow ───────────────────────────────────────────────────────

def test_load_refresh_token_works_across_client_id_rotation():
    """A refresh token should be loadable even if the MCP client_id changed
    since issuance (e.g., QwenPaw dynamic client registration rotates
    client_ids across sessions). Previously, load_refresh_token() rejected
    refresh tokens when client_id didn't match — causing daily 401s.
    """
    from app.config import settings

    # Create a test provider with an in-memory token store (no disk write)
    test_store = _McpTokenStore(token_file=None)  # None → in-memory only
    test_provider = GoogleOAuthProvider(registry, None)
    test_provider._mcp_tokens = test_store

    # Simulate initial issuance with client_id "original_client"
    now = int(time.time())
    jti = "test_jti_123"
    subject = "user@example.com"
    original_client_id = "original_client"
    new_client_id = "rotated_client_456"

    refresh_jwt = _sign_jwt({
        "sub": subject,
        "aud": original_client_id,
        "jti": jti,
        "scope": "openid email",
        "exp": now + settings.mcp_refresh_token_ttl,
        "token_type": "refresh",
    })

    test_store.store(jti, subject=subject, client_id=original_client_id,
                     scopes=["openid", "email"], expires_at=now + settings.mcp_refresh_token_ttl)

    # Create a client with a DIFFERENT client_id (simulating client rotation)
    rotated_client = MagicMock()
    rotated_client.client_id = new_client_id

    # The refresh token should still be valid — client_id should NOT block it
    result = asyncio.run(test_provider.load_refresh_token(rotated_client, refresh_jwt))
    assert result is not None, "Refresh token rejected due to client_id mismatch — this was the bug!"
    assert result.subject == subject
    assert result.token == refresh_jwt
