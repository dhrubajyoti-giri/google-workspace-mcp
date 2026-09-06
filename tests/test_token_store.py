"""Tests for the TokenStore file persistence."""
import json
import os
import stat
import tempfile
from pathlib import Path

from app.token_store import TokenStore


def test_save_and_load():
    with tempfile.TemporaryDirectory() as tmp:
        token_file = os.path.join(tmp, "token.json")
        store = TokenStore(token_file)
        data = {"refresh_token": "abc123", "token": "xyz", "client_id": "test"}
        store.save(data)
        loaded = store.load()
        assert loaded == data


def test_exists_and_delete():
    with tempfile.TemporaryDirectory() as tmp:
        token_file = os.path.join(tmp, "token.json")
        store = TokenStore(token_file)
        assert not store.exists()
        store.save({"refresh_token": "test"})
        assert store.exists()
        store.delete()
        assert not store.exists()


def test_file_permissions_0600():
    with tempfile.TemporaryDirectory() as tmp:
        token_file = os.path.join(tmp, "token.json")
        store = TokenStore(token_file)
        store.save({"refresh_token": "secret"})
        file_stat = os.stat(token_file)
        mode = stat.S_IMODE(file_stat.st_mode)
        assert mode == 0o600, f"Expected 0600, got {oct(mode)}"


def test_load_missing_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        token_file = os.path.join(tmp, "missing.json")
        store = TokenStore(token_file)
        assert store.load() is None
        assert not store.exists()


def test_overwrite():
    with tempfile.TemporaryDirectory() as tmp:
        token_file = os.path.join(tmp, "token.json")
        store = TokenStore(token_file)
        store.save({"refresh_token": "old"})
        store.save({"refresh_token": "new"})
        loaded = store.load()
        assert loaded["refresh_token"] == "new"
