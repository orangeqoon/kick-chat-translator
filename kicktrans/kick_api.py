"""Kick 公式 Public API / OAuth 2.1 (PKCE)。エンドポイントは docs.kick.com 準拠。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass

import aiohttp
from aiohttp import web

from .sender import RateLimited, TransientError
from .storage import (
    prepare_token_refresh,
    commit_token_refresh,
    get_client_secret as _stored_client_secret,
    load_token_data,
    save_credentials,
    save_token_data,
)

log = logging.getLogger(__name__)
API = "https://api.kick.com"
ID = "https://id.kick.com"
SCOPES = "user:read channel:read chat:write events:subscribe"
KEYRING_SERVICE = "kick-translator"


# ---------- トークン保存 (Windows DPAPI CurrentUser) ----------
@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str | None = None

    def to_data(self) -> dict:
        data = asdict(self)
        if data.get("scope") is None:
            data.pop("scope", None)
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_data())

    @classmethod
    def from_data(cls, data: dict) -> "Tokens":
        allowed = {"access_token", "refresh_token", "expires_at", "scope"}
        return cls(**{key: value for key, value in data.items() if key in allowed})


def get_client_secret() -> str:
    s = _stored_client_secret()
    if not s:
        raise RuntimeError("client_secret 未設定: `python -m kicktrans set-secret` を実行してください")
    return s


def save_tokens(t: Tokens) -> None:
    save_token_data(t.to_data())


def load_tokens() -> Tokens | None:
    data = load_token_data()
    return Tokens.from_data(data) if data else None


def _tokens_from(resp: dict) -> Tokens:
    return Tokens(resp["access_token"], resp.get("refresh_token", ""),
                  time.time() + float(resp.get("expires_in") or 3600) - 60,
                  resp.get("scope") if isinstance(resp.get("scope"), str) else None)


# ---------- OAuth ----------
def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
         "scope": SCOPES, "code_challenge": challenge, "code_challenge_method": "S256", "state": state}
    query = urllib.parse.urlencode(q)
    if "127.0.0.1" in redirect_uri:  # 公式ドキュメント記載の NextJS 回避策
        query = "redirect=127.0.0.1&" + query
    return f"{ID}/oauth/authorize?{query}"


async def _token_request(session: aiohttp.ClientSession, form: dict) -> dict:
    async with session.post(f"{ID}/oauth/token", data=form) as r:
        if r.status != 200:
            raise RuntimeError(f"トークン取得失敗 HTTP {r.status}")  # 本文は秘密を含みうるので出さない
        return await r.json()


async def login_interactive(client_id: str, redirect_uri: str, timeout: float = 300) -> Tokens:
    """ブラウザで認可 → localhost で code を受け取り → DPAPI 保存。"""
    u = urllib.parse.urlparse(redirect_uri)
    if u.hostname not in ("localhost", "127.0.0.1") or u.scheme != "http":
        raise ValueError("redirect_uri は http://localhost:<port>/<path> にしてください")
    # Fail before opening a browser so a missing secret cannot consume the
    # short-lived authorization code.
    client_secret = get_client_secret()
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def cb(req: web.Request) -> web.Response:
        if req.query.get("state") != state:
            return web.Response(status=400, text="state mismatch")
        code = req.query.get("code")
        if not code:
            return web.Response(status=400, text="no code")
        if not fut.done():
            fut.set_result(code)
        return web.Response(text="認証が完了しました。このタブを閉じてください。", content_type="text/plain", charset="utf-8")

    app = web.Application()
    app.router.add_get(u.path or "/", cb)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, u.hostname, u.port or 80)
    await site.start()
    try:
        webbrowser.open(authorize_url(client_id, redirect_uri, state, challenge))
        code = await asyncio.wait_for(fut, timeout)
    finally:
        await runner.cleanup()
    async with aiohttp.ClientSession() as s:
        resp = await _token_request(s, {
            "grant_type": "authorization_code", "client_id": client_id,
            "client_secret": client_secret, "redirect_uri": redirect_uri,
            "code_verifier": verifier, "code": code})
    t = _tokens_from(resp)
    save_credentials(t.to_data(), client_secret)
    return t


# ---------- API クライアント ----------
class KickClient:
    def __init__(self, client_id: str, tokens: Tokens, session: aiohttp.ClientSession):
        self.client_id = client_id
        self.tokens = tokens
        self.s = session

    async def _ensure_token(self) -> str:
        if time.time() >= self.tokens.expires_at and self.tokens.refresh_token:
            expected = self.tokens.to_data()
            secret, revision = prepare_token_refresh(expected)
            resp = await _token_request(self.s, {
                "grant_type": "refresh_token", "client_id": self.client_id,
                "client_secret": secret, "refresh_token": self.tokens.refresh_token})
            # Kick may omit refresh_token when rotating is not required.  Keep
            # the still-valid old refresh token instead of replacing it with
            # an empty value.
            if not resp.get("refresh_token"):
                resp = dict(resp)
                resp["refresh_token"] = self.tokens.refresh_token
            refreshed = _tokens_from(resp)
            if not commit_token_refresh(refreshed.to_data(), expected, secret, revision):
                raise RuntimeError("認証情報が変更されました。ログイン状態を確認して再起動してください")
            self.tokens = refreshed
            log.info("アクセストークンを更新しました")
        return self.tokens.access_token

    async def _req(self, method: str, path: str, **kw) -> dict:
        headers = {"Authorization": f"Bearer {await self._ensure_token()}"}
        async with self.s.request(method, API + path, headers=headers, **kw) as r:
            if r.status == 429:
                ra = r.headers.get("Retry-After")
                raise RateLimited(float(ra) if ra and ra.isdigit() else None)
            if r.status >= 500:
                raise TransientError(f"HTTP {r.status}")
            if r.status >= 400:
                raise RuntimeError(f"Kick API {method} {path} HTTP {r.status}")
            return await r.json() if r.status != 204 else {}

    async def me(self) -> dict:
        return (await self._req("GET", "/public/v1/users"))["data"][0]

    async def channel(self, slug: str) -> dict:
        return (await self._req("GET", "/public/v1/channels", params={"slug": slug}))["data"][0]

    async def post_chat(self, content: str) -> str | None:
        # type=bot: トークンに紐づくチャンネルへ投稿 (broadcaster_user_id は無視される)
        d = await self._req("POST", "/public/v1/chat", json={"type": "bot", "content": content})
        return (d.get("data") or {}).get("message_id")

    async def subscribe_chat(self, broadcaster_user_id: int) -> list[str]:
        d = await self._req("POST", "/public/v1/events/subscriptions", json={
            "broadcaster_user_id": broadcaster_user_id, "method": "webhook",
            "events": [{"name": "chat.message.sent", "version": 1}]})
        rows = d.get("data")
        ids = [row["subscription_id"] for row in rows
               if isinstance(row, dict) and not row.get("error")
               and isinstance(row.get("subscription_id"), str) and row["subscription_id"]] if isinstance(rows, list) else []
        if not ids:
            raise RuntimeError("chat.message.sent の購読に成功しませんでした。KickアプリのWebhook設定を確認してください")
        return ids

    async def unsubscribe(self, ids: list[str]) -> None:
        if ids:
            await self._req("DELETE", "/public/v1/events/subscriptions", params=[("id", i) for i in ids])
