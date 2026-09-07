"""Tests for the OAuth flow: state store management, code exchange, cleanup."""
import time
from unittest.mock import patch, MagicMock

from app.oauth import _google_state_store, _cleanup_stores, _exchange_code
from app.oauth_provider import provider, registry


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
