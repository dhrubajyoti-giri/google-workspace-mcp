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

# ── Token Store Re-authorization Cleanup ────────────────────────────────────

def test_re_authorization_does_not_create_duplicate_jtis():
    """When a user re-authorizes, exchange_authorization_code should remove
    old MCP refresh token JTIs for the same subject+client before storing
    the new one. Without this cleanup, duplicate entries accumulate in
    mcp_tokens.json on every re-auth.
    """
    import asyncio
    from app.config import settings
    from unittest.mock import MagicMock

    test_store = _McpTokenStore(token_file=None)  # in-memory only
    test_provider = GoogleOAuthProvider(registry, None)
    test_provider._mcp_tokens = test_store

    subject = "user@example.com"
    client_id = "mcp_client_1"
    now = int(time.time())
    ttl = settings.mcp_refresh_token_ttl

    # Simulate an existing token (from prior authorization)
    old_jti = "old_jti"
    test_store.store(old_jti, subject=subject, client_id=client_id,
                     scopes=["openid", "email"], expires_at=now + ttl)
    assert old_jti in test_store._tokens

    # Create an auth code for re-authorization
    auth_code = MagicMock()
    auth_code.subject = subject
    auth_code.scopes = ["openid", "email"]
    auth_code.code = "test_code"
    auth_code.client_id = client_id
    auth_code.code_challenge = ""
    auth_code.redirect_uri = None
    auth_code.redirect_uri_provided_explicitly = True
    auth_code.resource = None
    auth_code.expires_at = now + 600

    mock_client = MagicMock()
    mock_client.client_id = client_id

    result = asyncio.run(test_provider.exchange_authorization_code(mock_client, auth_code))

    # Old JTI should be gone
    assert old_jti not in test_store._tokens, "Old JTI not cleaned up on re-authorization!"
    # New JTI should exist
    new_jtis = [k for k, v in test_store._tokens.items() if v.get("subject") == subject]
    assert len(new_jtis) == 1, "Expected exactly 1 active token after re-auth, got %d" % len(new_jtis)
    # New token should have a different JTI
    assert new_jtis[0] != old_jti


def test_re_authorization_preserves_other_clients_tokens():
    """Re-authorizing with client A should NOT delete client B's tokens
    for the same user.
    """
    import asyncio
    from app.config import settings
    from unittest.mock import MagicMock

    test_store = _McpTokenStore(token_file=None)
    test_provider = GoogleOAuthProvider(registry, None)
    test_provider._mcp_tokens = test_store

    subject = "user@example.com"
    now = int(time.time())
    ttl = settings.mcp_refresh_token_ttl

    # Token for client A (the one being re-authorized)
    test_store.store("jti_a", subject=subject, client_id="client_a",
                     scopes=["openid"], expires_at=now + ttl)
    # Token for client B (different client, same user — should be preserved)
    test_store.store("jti_b", subject=subject, client_id="client_b",
                     scopes=["openid"], expires_at=now + ttl)

    auth_code = MagicMock()
    auth_code.subject = subject
    auth_code.scopes = ["openid"]
    auth_code.code = "test_code"
    auth_code.client_id = "client_a"
    auth_code.code_challenge = ""
    auth_code.redirect_uri = None
    auth_code.redirect_uri_provided_explicitly = True
    auth_code.resource = None
    auth_code.expires_at = now + 600

    mock_client = MagicMock()
    mock_client.client_id = "client_a"

    asyncio.run(test_provider.exchange_authorization_code(mock_client, auth_code))

    # Client A's old token should be replaced
    assert "jti_a" not in test_store._tokens
    a_tokens = [k for k, v in test_store._tokens.items() if v.get("client_id") == "client_a"]
    assert len(a_tokens) == 1
    # Client B's token should still be there
    assert "jti_b" in test_store._tokens


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
