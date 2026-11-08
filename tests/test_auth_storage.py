"""Focused offline tests for the shared encrypted auth store."""
from __future__ import annotations

import importlib.util
import json
import sys
import types
import urllib.error
from pathlib import Path

import pytest

from kicktrans import storage


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if sys.platform != "win32":
        pytest.skip("Windows DPAPI storage")
    canonical = tmp_path / "state" / "tokens" / "kick-translator"
    legacy = tmp_path / "state" / "services" / "kick-translator-auth"
    values = {
        "CANONICAL_DIR": canonical,
        "STORE_PATH": canonical / "credentials.dpapi",
        "LEGACY_HELPER_DIR": legacy,
        "LEGACY_TOKENS_PATH": legacy / "kick-tokens.dpapi",
        "LEGACY_CLIENT_SECRET_PATH": legacy / "client-secret.dpapi",
    }
    for name, value in values.items():
        monkeypatch.setattr(storage, name, value)
    monkeypatch.delenv("KICK_CLIENT_SECRET", raising=False)
    return canonical, legacy


def test_save_load_and_secret_are_shared(isolated_store):
    data = {
        "access_token": "synthetic-access",
        "refresh_token": "synthetic-refresh",
        "expires_at": 1900000000.0,
        "scope": "user:read chat:write",
    }
    storage.save_token_data(data)
    storage.save_client_secret("synthetic-secret")
    assert storage.load_token_data() == data
    assert storage.get_client_secret() == "synthetic-secret"
    assert b"synthetic-access" not in storage.STORE_PATH.read_bytes()
    assert b"synthetic-secret" not in storage.STORE_PATH.read_bytes()


def test_missing_and_corrupt_values_are_unavailable(isolated_store):
    assert storage.load_token_data() is None
    assert storage.get_client_secret() is None
    storage.STORE_PATH.parent.mkdir(parents=True)
    storage.STORE_PATH.write_bytes(b"not-dpapi")
    assert storage.load_token_data() is None
    assert storage.get_client_secret() is None
    with pytest.raises(ValueError):
        storage.save_client_secret("synthetic-secret")
    assert storage.STORE_PATH.read_bytes() == b"not-dpapi"


def test_update_backs_up_encrypted_file_and_preserves_it_on_replace_failure(isolated_store, monkeypatch):
    first = {"access_token": "first", "refresh_token": "r1", "expires_at": 1900000000}
    second = {"access_token": "second", "refresh_token": "r2", "expires_at": 1900000001}
    storage.save_token_data(first)
    original = storage.STORE_PATH.read_bytes()

    def fail_replace(*_args, **_kwargs):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with pytest.raises(OSError):
        storage.save_token_data(second)
    assert storage.STORE_PATH.read_bytes() == original
    backups = list(storage.STORE_PATH.parent.glob("credentials.dpapi.bak-*"))
    assert backups and any(path.read_bytes() == original for path in backups)
    assert storage.load_token_data() == first


def test_explicit_helper_migration_converts_expiry_and_keeps_legacy(isolated_store):
    _canonical, legacy = isolated_store
    legacy.mkdir(parents=True)
    old = {
        "access_token": "legacy-access",
        "refresh_token": "legacy-refresh",
        "expires_in": 3600,
        "obtained_at": 1000,
        "scope": "user:read",
    }
    old_token = storage.dpapi_protect(json.dumps(old).encode("utf-8"))
    old_secret = storage.dpapi_protect(b"legacy-secret")
    storage.LEGACY_TOKENS_PATH.write_bytes(old_token)
    storage.LEGACY_CLIENT_SECRET_PATH.write_bytes(old_secret)

    assert storage.migrate_legacy("helper") == ["tokens", "client_secret"]
    assert storage.load_token_data() == {
        "access_token": "legacy-access",
        "refresh_token": "legacy-refresh",
        "expires_at": 4540.0,
        "scope": "user:read",
    }
    assert storage.get_client_secret() == "legacy-secret"
    assert storage.LEGACY_TOKENS_PATH.read_bytes() == old_token
    assert storage.LEGACY_CLIENT_SECRET_PATH.read_bytes() == old_secret
    with pytest.raises(FileExistsError):
        storage.migrate_legacy("helper")


def test_explicit_keyring_migration_and_canonical_rejection(isolated_store, monkeypatch):
    old = {"access_token": "keyring-access", "refresh_token": "keyring-refresh", "expires_at": 1900000000}
    fake = types.SimpleNamespace(
        get_password=lambda service, key: json.dumps(old) if key == "tokens" else "keyring-secret"
    )
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert storage.migrate_legacy("keyring") == ["tokens", "client_secret"]
    with pytest.raises(FileExistsError):
        storage.migrate_legacy("keyring")
    assert storage.load_token_data()["access_token"] == "keyring-access"
    assert storage.get_client_secret() == "keyring-secret"


def test_helper_does_not_retain_provider_error_text():
    helper_path = Path(__file__).parents[1] / "auth" / "kick_oauth_helper.py"
    spec = importlib.util.spec_from_file_location("kick_oauth_helper_test", helper_path)
    helper = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(helper)
    secret = "synthetic-provider-error-description"
    error = urllib.error.HTTPError(
        "https://id.kick.com/oauth/token",
        403,
        "forbidden",
        {"Content-Type": "application/json"},
        __import__("io").BytesIO(json.dumps({"error_description": secret}).encode()),
    )
    info = helper.classify_error(error)
    assert secret not in repr(info)
    assert "error_description" not in info
