"""Tests for the OAuth flow: state store management, code exchange, cleanup."""
import asyncio
import time
import pytest
from unittest.mock import patch, MagicMock

from app.oauth import _google_state_store, _cleanup_stores, _exchange_code, _build_token_data, _merge_token_data
from app.google_client import _parse_expiry
from app.oauth_provider import (
    provider,
    registry,
    _sign_jwt,
    _verify_jwt,
    _McpTokenStore,
    GoogleOAuthProvider,
    RefreshToken,
    TokenError,
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


# ── Server-Side Auto-Refresh Tests ─────────────────────────────────────────

def test_auto_refresh_renews_expired_access_token():
    """When an MCP access token is expired, _try_auto_refresh_access_token
    should decode it (without verifying expiry), find a valid refresh token
    for the subject, and issue a NEW access token. This prevents the MCP
    client (QwenPaw's HttpStatelessClient) from seeing a 401 and requiring
    manual re-authorization.
    """
    import time as _time
    from app.config import settings

    test_store = _McpTokenStore(token_file=None)
    test_provider = GoogleOAuthProvider(registry, None)
    test_provider._mcp_tokens = test_store

    subject = "user@example.com"

    # Create an expired access token (exp in the past)
    now = int(_time.time())
    expired_token = _sign_jwt({
        "iss": settings.external_url,
        "sub": subject,
        "aud": "mcp",
        "exp": now - 3600,  # expired 1 hour ago
        "iat": now - 7200,
        "scope": "openid email profile",
        "token_type": "access",
        "jti": "expired_jti",
    })

    # Manually insert a valid refresh token for this subject into the store
    test_store.store(
        "refresh_jti",
        subject=subject,
        client_id="test_client",
        scopes=["openid", "email"],
        expires_at=now + settings.mcp_refresh_token_ttl,
    )

    # Auto-refresh requires the user to still exist in the Google registry
    # (a de-authorized user must re-authorize instead of getting fresh tokens)
    registry.save(subject, {
        "token": "google_access",
        "refresh_token": "google_refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "expiry": None,
    }, ["openid", "email"])
    try:
        # The expired access token should NOT pass _verify_jwt
        assert _verify_jwt(expired_token) is None, "Expired token should fail verification"

        # But _try_auto_refresh_access_token should succeed and return a new token
        new_access = test_provider._try_auto_refresh_access_token(expired_token)
        assert new_access is not None, "Auto-refresh should succeed when a valid refresh token exists"
        assert new_access.subject == subject
        assert new_access.token != expired_token, "Should be a NEW token, not the expired one"
        assert new_access.expires_at > _time.time(), "New token should not be expired"
        # The STORED refresh entry's scopes win over the expired token's stale ones
        assert sorted(new_access.scopes) == ["email", "openid"]

        # The new token should pass _verify_jwt
        assert _verify_jwt(new_access.token) is not None, "New token should pass verification"
    finally:
        registry.delete(subject)


def test_auto_refresh_rejects_expired_refresh_token():
    """If the refresh token in the store is also expired, auto-refresh
    should NOT work — no valid token to issue from.
    """
    import time as _time
    from app.config import settings

    test_store = _McpTokenStore(token_file=None)
    test_provider = GoogleOAuthProvider(registry, None)
    test_provider._mcp_tokens = test_store

    subject = "user@example.com"
    now = int(_time.time())

    expired_token = _sign_jwt({
        "iss": settings.external_url,
        "sub": subject,
        "aud": "mcp",
        "exp": now - 3600,
        "iat": now - 7200,
        "scope": "openid email",
        "token_type": "access",
        "jti": "expired_jti",
    })

    # Insert an EXPIRED refresh token
    test_store.store(
        "expired_refresh_jti",
        subject=subject,
        client_id="test_client",
        scopes=["openid", "email"],
        expires_at=now - 100,  # already expired
    )

    # Seed the registry so the failure comes from the expired refresh token
    # (not from the de-authorized-user gate)
    registry.save(subject, {
        "token": "google_access",
        "refresh_token": "google_refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "expiry": None,
    }, ["openid", "email"])
    try:
        result = test_provider._try_auto_refresh_access_token(expired_token)
        assert result is None, "Auto-refresh should fail when the refresh token is also expired"
    finally:
        registry.delete(subject)


def test_auto_refresh_no_valid_subject_returns_none():
    """If no refresh token exists for the subject at all, auto-refresh returns None.
    This triggers the normal 401 flow so the client knows to re-authorize.
    """
    import time as _time
    from app.config import settings

    test_store = _McpTokenStore(token_file=None)
    test_provider = GoogleOAuthProvider(registry, None)
    test_provider._mcp_tokens = test_store

    now = int(_time.time())
    expired_token = _sign_jwt({
        "iss": settings.external_url,
        "sub": "user@example.com",
        "aud": "mcp",
        "exp": now - 3600,
        "iat": now - 7200,
        "scope": "openid email",
        "token_type": "access",
        "jti": "no_refresh_jti",
    })

    # Store is empty — no refresh token for this subject
    assert test_provider._try_auto_refresh_access_token(expired_token) is None


def test_auto_refresh_rejects_deauthorized_user():
    """A user removed from the Google registry must NOT get fresh MCP access
    tokens, even with a valid refresh token in the store — they must
    re-authorize instead.
    """
    import time as _time
    from app.config import settings

    test_store = _McpTokenStore(token_file=None)
    test_provider = GoogleOAuthProvider(registry, None)
    test_provider._mcp_tokens = test_store

    subject = "gone@example.com"
    now = int(_time.time())

    expired_token = _sign_jwt({
        "sub": subject,
        "client_id": "test_client",
        "exp": now - 60,
        "scope": "openid email",
        "token_type": "access",
        "jti": "expired_jti_2",
    })

    test_store.store(
        "valid_refresh_jti",
        subject=subject,
        client_id="test_client",
        scopes=["openid", "email"],
        expires_at=now + settings.mcp_refresh_token_ttl,
    )

    # No registry entry for subject → gate must refuse
    assert registry.get(subject) is None
    assert test_provider._try_auto_refresh_access_token(expired_token) is None


def test_exchange_refresh_token_rejects_scope_escalation():
    """exchange_refresh_token must not mint tokens with scopes beyond what the
    refresh token was granted (defense-in-depth; the SDK TokenHandler also
    enforces this, but direct callers bypass it).
    """
    import asyncio
    import time as _time
    from app.config import settings

    test_store = _McpTokenStore(token_file=None)
    test_provider = GoogleOAuthProvider(registry, None)
    test_provider._mcp_tokens = test_store

    subject = "user@example.com"
    now = int(_time.time())
    jti = "scoped_jti"

    refresh_jwt = _sign_jwt({
        "sub": subject,
        "aud": "client_a",
        "jti": jti,
        "scope": "openid email",
        "exp": now + settings.mcp_refresh_token_ttl,
        "token_type": "refresh",
    })
    test_store.store(jti, subject=subject, client_id="client_a",
                     scopes=["openid", "email"],
                     expires_at=now + settings.mcp_refresh_token_ttl)

    mock_client = MagicMock()
    mock_client.client_id = "client_a"

    granted = RefreshToken(
        token=refresh_jwt,
        client_id="client_a",
        scopes=["openid", "email"],
        expires_at=now + settings.mcp_refresh_token_ttl,
        subject=subject,
    )

    # Escalation attempt → TokenError invalid_scope
    try:
        asyncio.run(test_provider.exchange_refresh_token(
            mock_client, granted, ["openid", "email", "admin"]))
        assert False, "Should have raised TokenError"
    except TokenError as e:
        assert e.error == "invalid_scope"

    # Subset still works and rotates exactly one JTI
    result = asyncio.run(test_provider.exchange_refresh_token(
        mock_client, granted, ["openid"]))
    assert result is not None
    assert result.scope == "openid"
    assert jti not in test_store._tokens
    assert len(test_store._tokens) == 1


def test_revoke_expired_refresh_token_removes_jti():
    """Revoking an already-expired refresh token must still delete its store
    entry (previously _verify_jwt's exp check made this a silent no-op).
    """
    import asyncio
    import time as _time
    from app.config import settings

    test_store = _McpTokenStore(token_file=None)
    test_provider = GoogleOAuthProvider(registry, None)
    test_provider._mcp_tokens = test_store

    subject = "user@example.com"
    now = int(_time.time())
    jti = "stale_jti"

    expired_jwt = _sign_jwt({
        "sub": subject,
        "aud": "client_a",
        "jti": jti,
        "scope": "openid",
        "exp": now - 3600,  # already expired
        "token_type": "refresh",
    })
    test_store.store(jti, subject=subject, client_id="client_a",
                     scopes=["openid"], expires_at=now - 3600)

    stale = RefreshToken(
        token=expired_jwt,
        client_id="client_a",
        scopes=["openid"],
        expires_at=now - 3600,
        subject=subject,
    )
    asyncio.run(test_provider.revoke_token(stale))
    assert jti not in test_store._tokens


def test_merge_token_data_preserves_refresh_token():
    """Re-authorization responses that omit refresh_token must not wipe the
    stored one — _merge_token_data falls back to the existing entry.
    """
    existing = {
        "token": "old_access",
        "refresh_token": "long_lived_refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "expiry": "2030-01-01T00:00:00+00:00",
    }
    new = {
        "token": "new_access",
        "refresh_token": None,  # Google omits this on re-auth
        "token_uri": "https://oauth2.googleapis.com/token",
        "expiry": None,
    }
    merged = _merge_token_data(existing, new)
    assert merged["token"] == "new_access"
    assert merged["refresh_token"] == "long_lived_refresh"
    assert merged["expiry"] == "2030-01-01T00:00:00+00:00"

    # No existing entry → new data passes through untouched
    assert _merge_token_data(None, new)["refresh_token"] is None


def test_parse_expiry_handles_formats():
    """_parse_expiry must accept naive/aware ISO strings and reject garbage
    without raising."""
    from datetime import datetime, timezone

    aware = _parse_expiry("2030-01-01T00:00:00+00:00")
    assert aware is not None and aware.tzinfo is not None

    naive = _parse_expiry("2030-01-01T00:00:00")
    assert naive is not None and naive.tzinfo == timezone.utc

    assert _parse_expiry(None) is None
    assert _parse_expiry("not-a-date") is None

    dt = datetime(2030, 1, 1)
    assert _parse_expiry(dt) is dt
