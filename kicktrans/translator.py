"""翻訳バックエンド。

注意: GoogleWebTranslator は Google 翻訳の非公式 Web エンドポイント
(translate.googleapis.com/translate_a/single, client=gtx と、失敗時に
clients5.google.com/translate_a/t) を使います。無料ですが、
仕様変更・レート制限・利用規約上の扱いが保証されません。安定運用には有料の
Google Cloud Translation API への置き換えを推奨します。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import aiohttp

from .textutil import looks_japanese


@dataclass
class Translation:
    text: str
    source_lang: str
    target_lang: str


class Translator(Protocol):
    async def translate(self, text: str, target: str) -> Translation: ...
    async def close(self) -> None: ...


def choose_target(text: str, detected: str) -> str:
    """日本語 → 英語、それ以外 → 日本語。"""
    if detected.lower().startswith("ja") or looks_japanese(text):
        return "en"
    return "ja"


class GoogleWebTranslator:
    URL = "https://translate.googleapis.com/translate_a/single"
    FALLBACK_URL = "https://clients5.google.com/translate_a/t"

    def __init__(self, timeout: float = 10.0):
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _get(self, text: str, target: str) -> tuple[str, str]:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        try:
            return await self._get_gtx(text, target)
        except (aiohttp.ClientError, ValueError, IndexError, TypeError):
            return await self._get_dict(text, target)

    async def _get_dict(self, text: str, target: str) -> tuple[str, str]:
        params = {"client": "dict-chrome-ex", "sl": "auto", "tl": target, "q": text}
        async with self._session.get(self.FALLBACK_URL, params=params) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
        item = data[0]
        if isinstance(item, list):
            return str(item[0]), str(item[1]) if len(item) > 1 else "und"
        return str(item), "und"

    async def _get_gtx(self, text: str, target: str) -> tuple[str, str]:
        params = {"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text}
        async with self._session.get(self.URL, params=params) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
        translated = "".join(part[0] for part in (data[0] or []) if part and part[0])
        src = data[2] if len(data) > 2 and isinstance(data[2], str) else "und"
        return translated, src

    async def translate(self, text: str, target: str | None = None) -> Translation:
        # まず日本語向けに訳し、原文が日本語と判定されたら英語で訳し直す
        first_target = target or ("en" if looks_japanese(text) else "ja")
        out, src = await self._get(text, first_target)
        want = target or choose_target(text, src)
        if want != first_target:
            out, src = await self._get(text, want)
        return Translation(out, src, want)

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None


class MockTranslator:
    """テスト・デモ用。ネットワークを使わない。出力は明示的に [MOCK] 付き。"""

    async def translate(self, text: str, target: str | None = None) -> Translation:
        src = "ja" if looks_japanese(text) else ("ko" if any("\uac00" <= c <= "\ud7a3" for c in text) else "en")
        tgt = target or choose_target(text, src)
        return Translation(f"[MOCK {src}>{tgt}] {text}", src, tgt)

    async def close(self) -> None:
        return None
