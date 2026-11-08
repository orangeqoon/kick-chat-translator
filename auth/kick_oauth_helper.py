"""kick_oauth_helper v0.3.1 — Kick OAuth 2.1 (PKCE S256) helper.

The helper intentionally uses only the Python standard library itself.  The
repository package is added to ``sys.path`` relative to this file so the same
DPAPI storage and public configuration loader are used when this file is
launched directly.

Legacy credentials are never read at import time.  ``--secret existing`` is a
compatibility mode for the old shared ``kick-config.json`` only; explicit
``kicktrans migrate-auth`` is the migration path for old encrypted helper or
keyring credentials.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path


# Direct script execution does not otherwise put the package root on sys.path.
PACKAGE_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, PACKAGE_ROOT)

from kicktrans.config import Config, SHARED_CONFIG_PATH  # noqa: E402
from kicktrans.paths import SERVICES_DIR  # noqa: E402
from kicktrans.storage import (  # noqa: E402
    STORE_PATH,
    get_client_secret,
    save_credentials,
)


__version__ = "0.3.0"
OUTDIR = Path(SERVICES_DIR) / "kick-translator-auth"
STATUS = OUTDIR / "status.json"
TOKENS = Path(STORE_PATH)
CONFIG = Path(SHARED_CONFIG_PATH)
SCOPES = "user:read channel:read chat:write events:subscribe"
TIMEOUT = 300
TOKEN_SAFETY_MARGIN_SEC = 60


def status(**kw: object) -> None:
    """Write non-secret helper state under the legacy status directory."""
    payload = dict(kw)
    payload["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    payload["pid"] = os.getpid()
    OUTDIR.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=".status-", dir=str(OUTDIR))
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, STATUS)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_shared_config() -> dict:
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        raise ValueError("共有Kick設定を読み取れません") from None
    if not isinstance(data, dict):
        raise ValueError("共有Kick設定が不正です")
    return data


def _config_value(cfg: Config | dict, key: str) -> object:
    if isinstance(cfg, dict):
        return cfg.get(key)
    return getattr(cfg, key, None)


def get_secret(cfg: Config | dict, mode: str) -> str:
    """Resolve a secret using the helper's compatible three modes."""
    if mode == "existing":
        # This mode is deliberately the only path that reads the old plaintext
        # shared config.  It never saves that value into the canonical store.
        shared = _read_shared_config()
        shared_client_id = shared.get("client_id")
        configured_client_id = _config_value(cfg, "client_id")
        if not isinstance(shared_client_id, str) or shared_client_id != configured_client_id:
            raise ValueError("共有Kick設定のclient_idが現在の設定と一致しません")
        secret = shared.get("client_secret")
        if not isinstance(secret, str) or not secret:
            raise ValueError("共有Kick設定にclient_secretがありません")
        return secret
    if mode == "stored":
        secret = get_client_secret()
        if not secret:
            raise ValueError("暗号ストアにclient_secretがありません")
        return secret
    if mode == "prompt":
        import getpass

        secret = getpass.getpass("Kick client_secret: ").strip()
        if not secret:
            raise ValueError("client_secretが空です")
        return secret
    raise ValueError("secret mode")


def classify_error(error: urllib.error.HTTPError) -> dict[str, object]:
    """Classify an HTTP response without retaining provider error text."""
    headers = error.headers or {}
    content_type = (headers.get("Content-Type") or "")[:60]
    try:
        raw = error.read(4096) if hasattr(error, "read") else b""
    except OSError:
        raw = b""
    if not isinstance(raw, bytes):
        raw = bytes(raw)
    info: dict[str, object] = {
        "http_status": error.code,
        "content_type": content_type,
        "server": (headers.get("Server") or "")[:40],
        "has_cf_ray": bool(headers.get("CF-RAY")),
        "body_len": len(raw),
    }
    lower = raw.lower()
    if "json" in content_type.lower():
        info["kind"] = "oauth_json"
        # Retain only known protocol codes, never arbitrary provider text.
        allowed_errors = {
            "invalid_client", "invalid_grant", "invalid_request",
            "unauthorized_client", "unsupported_grant_type", "invalid_scope",
            "access_denied", "server_error", "temporarily_unavailable",
        }
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            body = None
        code = body.get("error") if isinstance(body, dict) else None
        if isinstance(code, str) and code in allowed_errors:
            info["oauth_error"] = code
        else:
            info["oauth_error"] = "unrecognized"
    elif b"<html" in lower:
        info["kind"] = "html_block_page" if b"cloudflare" in lower or b"attention required" in lower or b"just a moment" in lower else "html"
    else:
        info["kind"] = "other"
    return info


def _canonical_token_data(token_response: object, obtained_at: float) -> dict:
    if not isinstance(token_response, dict):
        raise ValueError("OAuth応答が不正です")
    access_token = token_response.get("access_token")
    refresh_token = token_response.get("refresh_token", "")
    expires_in = token_response.get("expires_in")
    if not isinstance(access_token, str) or not access_token or not isinstance(refresh_token, str):
        raise ValueError("OAuth応答が不正です")
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
        raise ValueError("OAuth応答の有効期限が不正です")
    if expires_in < 0:
        raise ValueError("OAuth応答の有効期限が不正です")
    result = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": float(obtained_at) + float(expires_in) - TOKEN_SAFETY_MARGIN_SEC,
    }
    if isinstance(token_response.get("scope"), str):
        result["scope"] = token_response["scope"]
    return result


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kick_oauth_helper.py")
    parser.add_argument("--secret", choices=("existing", "prompt", "stored"), default="prompt")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = Config.load()
    # Resolve the secret before starting the listener or opening a browser.
    secret = get_secret(cfg, args.secret)
    client_id = cfg.client_id
    redirect = cfg.redirect_uri
    if not client_id:
        status(state="error", detail="client_id_missing")
        return 2
    parsed = urllib.parse.urlparse(redirect)
    if parsed.hostname not in ("localhost", "127.0.0.1") or parsed.scheme != "http" or not parsed.port:
        status(state="error", detail="redirect_uri_invalid")
        return 2

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state_value = secrets.token_urlsafe(24)
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def _reply(self, code: int, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            request = urllib.parse.urlparse(self.path)
            if request.path != (parsed.path or "/"):
                self._reply(404, "not found")
                return
            query = urllib.parse.parse_qs(request.query)
            if query.get("state", [""])[0] != state_value:
                self._reply(400, "state が一致しません。もう一度やり直してください。")
                return
            if "error" in query:
                # Provider-controlled text is neither returned nor stored.
                result["error"] = "oauth_error"
                self._reply(400, "認可されませんでした。もう一度お試しください。")
            elif "code" in query and query["code"][0]:
                result["code"] = query["code"][0]
                self._reply(200, "受け付けました。このタブを閉じてください。結果は status.json に記録されます。")
            else:
                self._reply(400, "code がありません")
                return
            done.set()

    server: http.server.HTTPServer | None = None
    try:
        try:
            server = http.server.HTTPServer(("127.0.0.1", parsed.port), Handler)
        except OSError as error:
            status(state="error", detail=f"port_unavailable:{error.__class__.__name__}")
            return 3
        threading.Thread(target=server.serve_forever, daemon=True).start()
        query = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "scope": SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state_value,
        })
        status(state="waiting_for_consent", redirect_uri=redirect, scopes=SCOPES, timeout_sec=TIMEOUT)
        print("ブラウザでKickアプリへの権限を承認してください。", flush=True)
        webbrowser.open("https://id.kick.com/oauth/authorize?" + query)
        if not done.wait(TIMEOUT):
            status(state="timeout")
            return 4
        if "error" in result:
            status(state="denied_or_error")
            return 5

        form = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": secret,
            "redirect_uri": redirect,
            "code_verifier": verifier,
            "code": result["code"],
        }).encode()
        request = urllib.request.Request(
            "https://id.kick.com/oauth/token",
            data=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "kick-translator-auth/0.3.1 (+local OAuth helper)",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                token_response = json.load(response)
        except urllib.error.HTTPError as error:
            info = classify_error(error)
            status(state="token_exchange_failed", **info)
            code = info.get("oauth_error", info["kind"])
            print(f"トークン交換失敗: HTTP {info['http_status']} / {code}", flush=True)
            if code == "invalid_client":
                print("Kick開発者設定で、このアプリのClient IDとClient Secretの組み合わせを確認してください。", flush=True)
            elif code == "invalid_grant":
                print("認可コードが使えません。ツールを起動し直して新しい認可を行ってください。", flush=True)
            return 6
        except (OSError, ValueError, json.JSONDecodeError):
            status(state="token_exchange_failed", kind="network_or_bad_json")
            return 6

        obtained_at = time.time()
        canonical = _canonical_token_data(token_response, obtained_at)
        save_credentials(canonical, secret)
        status(
            state="success",
            scope=canonical.get("scope"),
            expires_in=token_response.get("expires_in") if isinstance(token_response, dict) else None,
            token_file=str(TOKENS),
        )
        print("Kick認証成功。本体と共通の暗号ストアに保存しました。", flush=True)
        return 0
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # noqa: BLE001
        # Only the exception class is safe to publish; provider messages may
        # contain secrets or external error text.
        try:
            status(state="error", detail=error.__class__.__name__)
        finally:
            sys.exit(1)
