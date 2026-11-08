"""Kick webhook 受信 (公式ドキュメントの RSA 署名検証)。"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

import aiohttp
from aiohttp import web
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .pipeline import LRUSet

log = logging.getLogger(__name__)
PUBLIC_KEY_URL = "https://api.kick.com/public/v1/public-key"
MAX_BODY = 64 * 1024


async def fetch_public_key(session: aiohttp.ClientSession) -> rsa.RSAPublicKey:
    async with session.get(PUBLIC_KEY_URL) as r:
        r.raise_for_status()
        data = await r.json()
    pem = data["data"]["public_key"].encode()
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("RSA 公開鍵ではありません")
    return key


def verify_signature(key: rsa.RSAPublicKey, message_id: str, timestamp: str, body: bytes, signature_b64: str) -> bool:
    try:
        sig = base64.b64decode(signature_b64, validate=True)
        key.verify(sig, f"{message_id}.{timestamp}.".encode() + body, padding.PKCS1v15(), hashes.SHA256())
        return True
    except (InvalidSignature, ValueError):
        return False


def parse_ts(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


Handler = Callable[[str, dict], Awaitable[None]]


class WebhookVerifier:
    def __init__(self, key_provider: Callable[[], Awaitable[rsa.RSAPublicKey]], max_age_sec: int = 300):
        self._key_provider = key_provider
        self._key: rsa.RSAPublicKey | None = None
        self.max_age = max_age_sec
        self.seen = LRUSet(5000)

    async def check(self, headers, body: bytes, now: datetime | None = None) -> tuple[bool, str]:
        mid = headers.get("Kick-Event-Message-Id", "")
        ts = headers.get("Kick-Event-Message-Timestamp", "")
        sig = headers.get("Kick-Event-Signature", "")
        if not (mid and ts and sig):
            return False, "ヘッダー不足"
        dt = parse_ts(ts)
        now = now or datetime.now(timezone.utc)
        if dt is None or abs((now - dt).total_seconds()) > self.max_age:
            return False, "タイムスタンプが古い/不正"
        if self._key is None:
            self._key = await self._key_provider()
        ok = verify_signature(self._key, mid, ts, body, sig)
        if not ok:  # 鍵ローテーションに備えて一度だけ再取得
            self._key = await self._key_provider()
            ok = verify_signature(self._key, mid, ts, body, sig)
        if not ok:
            return False, "署名不一致"
        if not self.seen.add_if_new(mid):
            return False, "再送/重複"
        return True, "ok"


def make_app(verifier: WebhookVerifier, path: str, handler: Handler) -> web.Application:
    async def receive(request: web.Request) -> web.Response:
        body = await request.content.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            return web.Response(status=413)
        ok, reason = await verifier.check(request.headers, body)
        if not ok:
            log.warning("webhook 拒否: %s", reason)
            # 重複は 200 を返して再送を止める
            return web.Response(status=200 if reason == "再送/重複" else 401)
        etype = request.headers.get("Kick-Event-Type", "")
        try:
            import json
            payload = json.loads(body)
        except ValueError:
            return web.Response(status=400)
        await handler(etype, payload)
        return web.Response(status=200)

    app = web.Application(client_max_size=MAX_BODY)
    app.router.add_post(path, receive)
    return app
