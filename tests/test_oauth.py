"""Tests for the OAuth flow: state generation, validation, PKCE."""
import json
from unittest.mock import patch, MagicMock

from app.oauth import get_authorization_url, exchange_code, _state_store, _cleanup_states


def test_get_authorization_url_returns_url_and_state():
    with patch("app.oauth.settings") as mock_settings:
        mock_settings.google_client_secret_file = "/dev/null"
        mock_settings.external_url = "https://test.example.com"
        mock_settings.google_scopes = ["openid", "email"]

        with patch("app.oauth.Flow") as mock_flow_cls:
            mock_flow = MagicMock()
            mock_flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth?...", "test_state_123")
            mock_flow_cls.from_client_secrets_file.return_value = mock_flow

            auth_url, state = get_authorization_url()

            assert auth_url.startswith("https://accounts.google.com")
            assert state != ""
            assert state in _state_store
            assert _state_store[state]["scopes"] == ["openid", "email"]
            # Flow object must be stored for PKCE code_verifier reuse
            assert "flow" in _state_store[state]
            assert _state_store[state]["flow"] is mock_flow


def test_exchange_code_invalid_state():
    _state_store.clear()
    try:
        exchange_code("fake_code", "invalid_state")
        assert False, "Should have raised"
    except Exception as e:
        assert "Invalid or expired" in str(e) or "400" in str(e)


def test_state_cleanup():
    # Add an expired state
    _state_store["expired"] = {"created_at": 0, "scopes": []}
    _cleanup_states()
    assert "expired" not in _state_store
