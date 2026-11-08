"""Encrypted credential storage shared by the bot and OAuth helper.

The canonical store is one DPAPI (CurrentUser) encrypted JSON bundle.  The
module only computes paths at import time; directories, lock files, and legacy
credentials are touched only by an explicit read/write operation.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import hashlib
import json
import math
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .paths import SERVICES_DIR, TOKENS_DIR


KEYRING_SERVICE = "kick-translator"
CANONICAL_DIR = TOKENS_DIR / "kick-translator"
STORE_PATH = CANONICAL_DIR / "credentials.dpapi"

# Kept as compatibility aliases for callers that imported the old names.  All
# writes and reads use STORE_PATH, so there is never a second canonical file.
TOKENS_PATH = STORE_PATH
CLIENT_SECRET_PATH = STORE_PATH

LEGACY_HELPER_DIR = SERVICES_DIR / "kick-translator-auth"
LEGACY_TOKENS_PATH = LEGACY_HELPER_DIR / "kick-tokens.dpapi"
LEGACY_CLIENT_SECRET_PATH = LEGACY_HELPER_DIR / "client-secret.dpapi"

CRYPTPROTECT_UI_FORBIDDEN = 0x1
LOCK_TIMEOUT_SEC = 5.0
LOCK_POLL_SEC = 0.05
_TOKEN_KEYS = frozenset({"access_token", "refresh_token", "expires_at", "scope"})
_REQUIRED_TOKEN_KEYS = frozenset({"access_token", "refresh_token", "expires_at"})
_BUNDLE_KEYS = frozenset({"tokens", "client_secret"})


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]


def _dpapi_functions() -> tuple[Any, Any, Any]:
    """Load DPAPI lazily and configure every pointer prototype explicitly."""
    if os.name != "nt":
        raise OSError("Windows DPAPI is unavailable")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    protect = crypt32.CryptProtectData
    protect.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    protect.restype = wintypes.BOOL

    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    unprotect.restype = wintypes.BOOL

    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    return protect, unprotect, local_free


def _input_blob(data: bytes) -> tuple[DATA_BLOB, Any]:
    buffer = ctypes.create_string_buffer(data, max(1, len(data)))
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.c_void_p)), buffer


def dpapi_protect(data: bytes) -> bytes:
    """Encrypt bytes for the current Windows user without UI prompts."""
    if not isinstance(data, bytes):
        raise TypeError("DPAPI入力はbytesにしてください")
    protect, _unprotect, local_free = _dpapi_functions()
    blob, _buffer = _input_blob(data)
    out = DATA_BLOB()
    if not protect(
        ctypes.byref(blob),
        "kick-translator",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    ):
        raise OSError("DPAPI暗号化に失敗しました")
    try:
        if not out.pbData or not out.cbData:
            raise OSError("DPAPI暗号化結果が空です")
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        local_free(out.pbData)


def dpapi_unprotect(data: bytes) -> bytes:
    """Decrypt bytes for the current Windows user without UI prompts."""
    if not isinstance(data, bytes):
        raise TypeError("DPAPI入力はbytesにしてください")
    _protect, unprotect, local_free = _dpapi_functions()
    blob, _buffer = _input_blob(data)
    out = DATA_BLOB()
    if not unprotect(
        ctypes.byref(blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    ):
        raise OSError("DPAPI復号に失敗しました")
    try:
        if not out.pbData or not out.cbData:
            raise OSError("DPAPI復号結果が空です")
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        local_free(out.pbData)


def _validate_token_data(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("トークン形式が不正です")
    if set(data) - _TOKEN_KEYS or not _REQUIRED_TOKEN_KEYS.issubset(data):
        raise ValueError("トークン形式が不正です")
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_at = data.get("expires_at")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("トークン形式が不正です")
    if not isinstance(refresh_token, str):
        raise ValueError("トークン形式が不正です")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise ValueError("トークン形式が不正です")
    if not math.isfinite(float(expires_at)):
        raise ValueError("トークン形式が不正です")
    if "scope" in data and data["scope"] is not None and not isinstance(data["scope"], str):
        raise ValueError("トークン形式が不正です")
    normalized: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
    }
    if data.get("scope") is not None:
        normalized["scope"] = data["scope"]
    return normalized


def _validate_bundle(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) - _BUNDLE_KEYS:
        raise ValueError("認証ストア形式が不正です")
    normalized: dict[str, Any] = {}
    if "tokens" in data:
        normalized["tokens"] = _validate_token_data(data["tokens"])
    if "client_secret" in data:
        secret = data["client_secret"]
        if not isinstance(secret, str) or not secret:
            raise ValueError("認証ストア形式が不正です")
        normalized["client_secret"] = secret
    return normalized


def _read_bundle_unlocked() -> dict[str, Any] | None:
    if not STORE_PATH.exists():
        return None
    try:
        raw = dpapi_unprotect(STORE_PATH.read_bytes())
        return _validate_bundle(json.loads(raw.decode("utf-8")))
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise ValueError("暗号ストアを読み取れません") from None


def _lock_path() -> Path:
    return STORE_PATH.with_name(STORE_PATH.name + ".lock")


def _acquire_lock(handle: Any) -> None:
    deadline = time.monotonic() + LOCK_TIMEOUT_SEC
    while True:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except (OSError, BlockingIOError):
            if time.monotonic() >= deadline:
                raise TimeoutError("認証ストアのロック取得がタイムアウトしました") from None
            time.sleep(LOCK_POLL_SEC)


def _release_lock(handle: Any) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def _store_lock(*, create: bool) -> Iterator[None]:
    parent = STORE_PATH.parent
    if create:
        parent.mkdir(parents=True, exist_ok=True)
    elif not parent.exists():
        # A missing store cannot race with a writer unless that writer creates
        # the parent; getters can return an empty snapshot without creating it.
        yield
        return
    handle = None
    try:
        handle = _lock_path().open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        _acquire_lock(handle)
        yield
    finally:
        if handle is not None:
            _release_lock(handle)
            handle.close()


def _backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{index}")
        index += 1
    return candidate


def _backup_existing(path: Path) -> None:
    """Copy an encrypted store to a collision-safe dated backup."""
    for _attempt in range(100):
        backup = _backup_path(path)
        created = False
        try:
            with path.open("rb") as source, backup.open("xb") as destination:
                created = True
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
            return
        except FileExistsError:
            continue
        except Exception:
            if created:
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
    raise OSError("暗号ファイルのバックアップ名を確保できません")


def _atomic_write(encrypted: bytes, *, overwrite: bool = True) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{STORE_PATH.name}-", suffix=".tmp", dir=str(STORE_PATH.parent))
        temporary_path = Path(name)
        with os.fdopen(fd, "wb") as temporary:
            temporary.write(encrypted)
            temporary.flush()
            os.fsync(temporary.fileno())
        if overwrite:
            if STORE_PATH.exists():
                _backup_existing(STORE_PATH)
            os.replace(temporary_path, STORE_PATH)
            temporary_path = None
        else:
            # Same-directory hard-link creation is atomic and refuses an
            # already existing destination on Windows and POSIX.
            os.link(temporary_path, STORE_PATH)
            temporary_path.unlink()
            temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _encrypted_bundle(bundle: dict[str, Any]) -> bytes:
    try:
        plain = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return dpapi_protect(plain)
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("認証データを保存できません") from None


def _read_for_update() -> dict[str, Any]:
    bundle = _read_bundle_unlocked()
    return {} if bundle is None else bundle


def load_token_data() -> dict[str, Any] | None:
    """Return the token snapshot, or ``None`` when absent/corrupt."""
    try:
        with _store_lock(create=False):
            bundle = _read_bundle_unlocked()
    except (OSError, ValueError, TimeoutError):
        return None
    return bundle.get("tokens") if bundle else None


def get_client_secret() -> str | None:
    """Return the environment secret first, then the encrypted bundle value."""
    value = os.environ.get("KICK_CLIENT_SECRET")
    if value:
        return value
    try:
        with _store_lock(create=False):
            bundle = _read_bundle_unlocked()
    except (OSError, ValueError, TimeoutError):
        return None
    secret = bundle.get("client_secret") if bundle else None
    return secret if isinstance(secret, str) else None


def save_token_data(data: dict[str, Any]) -> None:
    """Merge validated token data into the encrypted credential bundle."""
    normalized = _validate_token_data(data)
    with _store_lock(create=True):
        bundle = _read_for_update()
        bundle["tokens"] = normalized
        _atomic_write(_encrypted_bundle(bundle))


def save_client_secret(secret: str) -> None:
    """Merge a validated secret into the encrypted credential bundle."""
    if not isinstance(secret, str) or not secret:
        raise ValueError("client_secret が空です")
    with _store_lock(create=True):
        bundle = _read_for_update()
        bundle["client_secret"] = secret
        _atomic_write(_encrypted_bundle(bundle))


def save_credentials(token_data: dict[str, Any], secret: str) -> None:
    """Atomically commit token and client secret as one encrypted snapshot."""
    normalized = _validate_token_data(token_data)
    if not isinstance(secret, str) or not secret:
        raise ValueError("client_secret が空です")
    with _store_lock(create=True):
        bundle = _read_for_update()
        bundle["tokens"] = normalized
        bundle["client_secret"] = secret
        _atomic_write(_encrypted_bundle(bundle))



def prepare_token_refresh(expected_tokens: dict[str, Any]) -> tuple[str, bytes]:
    """Capture the current identity and revision before a network refresh."""
    expected = _validate_token_data(expected_tokens)
    with _store_lock(create=False):
        bundle = _read_bundle_unlocked()
        if not bundle or bundle.get("tokens") != expected:
            raise RuntimeError("認証情報が変更されました。ログイン状態を確認して再起動してください")
        secret = os.environ.get("KICK_CLIENT_SECRET") or bundle.get("client_secret")
        if not isinstance(secret, str) or not secret:
            raise RuntimeError("client_secret が未設定です")
        revision = hashlib.sha256(STORE_PATH.read_bytes()).digest()
        return secret, revision


def commit_token_refresh(
    token_data: dict[str, Any], expected_tokens: dict[str, Any],
    secret: str, revision: bytes,
) -> bool:
    """Commit only if no login, secret change, or other refresh superseded us."""
    normalized = _validate_token_data(token_data)
    expected = _validate_token_data(expected_tokens)
    with _store_lock(create=False):
        if not STORE_PATH.exists() or hashlib.sha256(STORE_PATH.read_bytes()).digest() != revision:
            return False
        bundle = _read_bundle_unlocked()
        if not bundle or bundle.get("tokens") != expected:
            return False
        if (os.environ.get("KICK_CLIENT_SECRET") or bundle.get("client_secret")) != secret:
            return False
        # Persist the secret that actually authenticated this successful refresh.
        bundle["tokens"] = normalized
        bundle["client_secret"] = secret
        _atomic_write(_encrypted_bundle(bundle))
        return True

def _read_legacy_json(path: Path) -> Any | None:
    try:
        return json.loads(dpapi_unprotect(path.read_bytes()).decode("utf-8"))
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None


def _legacy_token_data(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("旧トークン形式が不正です")
    access_token = raw.get("access_token")
    refresh_token = raw.get("refresh_token", "")
    if not isinstance(access_token, str) or not access_token or not isinstance(refresh_token, str):
        raise ValueError("旧トークン形式が不正です")
    expires_at = raw.get("expires_at")
    if expires_at is None:
        obtained_at = raw.get("obtained_at")
        expires_in = raw.get("expires_in")
        if isinstance(obtained_at, bool) or not isinstance(obtained_at, (int, float)):
            raise ValueError("旧トークン形式が不正です")
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
            raise ValueError("旧トークン形式が不正です")
        expires_at = float(obtained_at) + float(expires_in) - 60
    result: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
    }
    if raw.get("scope") is not None:
        result["scope"] = raw["scope"]
    return _validate_token_data(result)


def _load_legacy_helper() -> tuple[dict[str, Any] | None, str | None]:
    token_data = None
    token_raw = _read_legacy_json(LEGACY_TOKENS_PATH)
    if token_raw is not None:
        try:
            token_data = _legacy_token_data(token_raw)
        except (TypeError, ValueError):
            token_data = None
    secret = None
    try:
        secret = dpapi_unprotect(LEGACY_CLIENT_SECRET_PATH.read_bytes()).decode("utf-8")
    except (OSError, UnicodeError, TypeError, ValueError):
        pass
    return token_data, secret or None


def _load_legacy_keyring() -> tuple[dict[str, Any] | None, str | None]:
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError("keyringを読み込めません") from exc
    token_data = None
    raw_tokens = keyring.get_password(KEYRING_SERVICE, "tokens")
    if raw_tokens:
        try:
            token_data = _legacy_token_data(json.loads(raw_tokens))
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            token_data = None
    secret = keyring.get_password(KEYRING_SERVICE, "client_secret") or None
    return token_data, secret


def migrate_legacy(source: str) -> list[str]:
    """Explicitly migrate old helper/keyring values into one bundle."""
    if source not in {"helper", "keyring"}:
        raise ValueError("移行元はhelperまたはkeyringにしてください")
    with _store_lock(create=True):
        # Refuse before reading the legacy source and never mix an old pair
        # into an existing canonical identity.
        if STORE_PATH.exists():
            raise FileExistsError("暗号ストアに既存の認証データがあります")
        if source == "helper":
            token_data, secret = _load_legacy_helper()
        else:
            token_data, secret = _load_legacy_keyring()
        bundle: dict[str, Any] = {}
        migrated: list[str] = []
        if token_data is not None:
            bundle["tokens"] = token_data
            migrated.append("tokens")
        if secret is not None:
            bundle["client_secret"] = secret
            migrated.append("client_secret")
        if not bundle:
            return []
        _atomic_write(_encrypted_bundle(bundle), overwrite=False)
        return migrated


__all__ = [
    "CANONICAL_DIR",
    "prepare_token_refresh",
    "commit_token_refresh",
    "CLIENT_SECRET_PATH",
    "CRYPTPROTECT_UI_FORBIDDEN",
    "KEYRING_SERVICE",
    "LEGACY_CLIENT_SECRET_PATH",
    "LEGACY_HELPER_DIR",
    "LEGACY_TOKENS_PATH",
    "STORE_PATH",
    "TOKENS_PATH",
    "dpapi_protect",
    "dpapi_unprotect",
    "get_client_secret",
    "load_token_data",
    "migrate_legacy",
    "save_client_secret",
    "save_credentials",
    "save_token_data",
]
