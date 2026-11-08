"""投稿キュー: 上限付き・送信間隔・429/5xx の指数バックオフ。"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


class RateLimited(Exception):
    def __init__(self, retry_after: float | None = None):
        super().__init__("rate limited")
        self.retry_after = retry_after


class TransientError(Exception):
    pass


PostFn = Callable[[str], Awaitable[str | None]]  # 戻り値: 投稿した message_id


class SendQueue:
    def __init__(self, post: PostFn, min_interval: float = 1.5, maxsize: int = 50,
                 max_retries: int = 4, base_backoff: float = 2.0,
                 on_sent: Callable[[str, str | None], None] | None = None,
                 sleep=asyncio.sleep, on_failed=None):
        self.on_failed = on_failed
        self._post = post
        self._q: asyncio.Queue[str] = asyncio.Queue(maxsize=maxsize)
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.on_sent = on_sent
        self._sleep = sleep
        self.dropped = 0

    def submit(self, text: str) -> bool:
        try:
            self._q.put_nowait(text)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            log.warning("送信キュー満杯のため破棄 (計 %d)", self.dropped)
            return False

    async def _send_one(self, text: str) -> None:
        for attempt in range(self.max_retries + 1):
            try:
                mid = await self._post(text)
                if self.on_sent:
                    self.on_sent(text, mid)
                return
            except RateLimited as e:
                wait = e.retry_after or self.base_backoff * (2 ** attempt)
                log.warning("429: %.1f 秒待機", wait)
            except TransientError as e:
                wait = self.base_backoff * (2 ** attempt)
                log.warning("一時エラー %s: %.1f 秒待機", e, wait)
            if attempt < self.max_retries:
                await self._sleep(wait)
        log.error("投稿を諦めました: %s", text[:40])
        if self.on_failed:
            self.on_failed(text)

    async def run(self) -> None:
        while True:
            text = await self._q.get()
            try:
                await self._send_one(text)
            except Exception:  # noqa: BLE001 - 1件の失敗で止めない
                log.error("投稿に失敗しました")
                if self.on_failed:
                    self.on_failed(text)
            finally:
                self._q.task_done()
            await self._sleep(self.min_interval)
