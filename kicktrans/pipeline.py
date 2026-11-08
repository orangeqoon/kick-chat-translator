"""受信メッセージ → 翻訳 → 投稿文 の処理。チャット本文は信頼しない入力として扱う。"""
from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass

from .config import Config
from .textutil import clean, has_letters, is_command, truncate
from .translator import Translator

log = logging.getLogger(__name__)


class LRUSet:
    def __init__(self, maxlen: int = 2000):
        self._d: OrderedDict[str, None] = OrderedDict()
        self._max = maxlen

    def add_if_new(self, key: str) -> bool:
        if key in self._d:
            self._d.move_to_end(key)
            return False
        self._d[key] = None
        if len(self._d) > self._max:
            self._d.popitem(last=False)
        return True

    def add(self, key: str) -> None:
        self.add_if_new(key)

    def __contains__(self, key: str) -> bool:
        return key in self._d


@dataclass
class ChatIn:
    message_id: str
    sender_id: int | None
    sender_name: str
    content: str

    @classmethod
    def from_event(cls, payload: dict) -> "ChatIn":
        sender = payload.get("sender") or {}
        return cls(
            message_id=str(payload.get("message_id", "")),
            sender_id=sender.get("user_id"),
            sender_name=str(sender.get("username", "?"))[:50],
            content=str(payload.get("content", ""))[:2000],
        )


@dataclass(frozen=True)
class PipelineResult:
    """A translated row plus the posting text kept for the legacy API."""

    output: str = ""
    original: str = ""
    translated: str = ""
    source_lang: str = ""
    target_lang: str = ""
    status: str = "skipped"
    detail: str = ""


def _language_base(value: str) -> str:
    return str(value or "").strip().lower().replace("_", "-").split("-", 1)[0]


def _same_language(left: str, right: str) -> bool:
    left_base, right_base = _language_base(left), _language_base(right)
    return bool(left_base and right_base and left_base == right_base)


class Pipeline:
    def __init__(self, cfg: Config, translator: Translator):
        self.cfg = cfg
        self.translator = translator
        self.own_user_ids: set[int] = set()  # トークン所有者 / bot の user_id
        self.sent_message_ids = LRUSet()  # 自分が投稿した message_id
        self.seen_message_ids = LRUSet()

    def skip_reason(self, msg: ChatIn) -> str | None:
        if not msg.message_id:
            return "message_id なし"
        if not self.seen_message_ids.add_if_new(msg.message_id):
            return "重複"
        if msg.message_id in self.sent_message_ids:
            return "自分の投稿 (message_id)"
        if msg.sender_id is not None and msg.sender_id in self.own_user_ids:
            return "自分の投稿 (user_id)"
        if msg.content.lstrip().startswith(self.cfg.output_prefix):
            return "翻訳出力プレフィックス"
        if msg.sender_name.lower() in {u.lower() for u in self.cfg.skip_users}:
            return "無視ユーザー"
        if self.cfg.skip_commands and is_command(msg.content):
            return "コマンド"
        return None

    async def process_detailed(self, msg: ChatIn) -> PipelineResult:
        """Process one message while retaining metadata for the UI row.

        ``process`` remains the original string-returning API used by existing
        callers.  This method is intentionally separate so the Engine can
        publish the raw translation without parsing its posting decoration.
        """

        reason = self.skip_reason(msg)
        if reason:
            log.info("skip %s: %s", msg.message_id, reason)
            return PipelineResult(original=msg.content, detail=reason)
        text = clean(msg.content)
        if not has_letters(text):
            log.info("skip %s: 翻訳対象の文字なし", msg.message_id)
            return PipelineResult(original=msg.content, detail="翻訳対象の文字なし")

        home_lang = getattr(self.cfg, "home_lang", "ja")
        foreign_lang = getattr(self.cfg, "foreign_lang", "en")
        trans_to_home_only = bool(getattr(self.cfg, "trans_to_home_only", False))

        # Ask for the home language first.  The translator returns its detected
        # source language, allowing arbitrary home/foreign language pairs while
        # keeping the existing two-way default (foreign -> home, home -> foreign).
        tr = await self.translator.translate(text, target=home_lang)
        source_lang = str(tr.source_lang or "und")
        if any(_same_language(source_lang, skipped) for skipped in getattr(self.cfg, "skip_languages", [])):
            log.info("skip %s: 無視言語 %s", msg.message_id, tr.source_lang)
            return PipelineResult(
                original=msg.content,
                source_lang=source_lang,
                target_lang=str(tr.target_lang or home_lang),
                detail=f"無視言語 {source_lang}",
            )

        if _same_language(source_lang, home_lang):
            if trans_to_home_only:
                reason = "母語メッセージ (母語のみ設定)"
                log.info("skip %s: %s", msg.message_id, reason)
                return PipelineResult(
                    original=msg.content,
                    source_lang=source_lang,
                    target_lang=str(tr.target_lang or home_lang),
                    detail=reason,
                )
            # The first call was only for detection; request the actual foreign
            # translation explicitly so non-Japanese home languages work too.
            tr = await self.translator.translate(text, target=foreign_lang)

        translated = str(tr.text or "")
        target_lang = str(tr.target_lang or (foreign_lang if _same_language(source_lang, home_lang) else home_lang))
        if not translated or translated.strip().lower() == text.strip().lower():
            reason = "翻訳結果なし"
            log.info("skip %s: %s", msg.message_id, reason)
            return PipelineResult(
                original=msg.content,
                source_lang=source_lang,
                target_lang=target_lang,
                detail=reason,
            )

        author = f" [by {msg.sender_name}]" if getattr(self.cfg, "show_author", True) else ""
        out = f"{getattr(self.cfg, 'output_prefix', '[TR]')} {translated}{author} ({source_lang}>{target_lang})"
        return PipelineResult(
            output=truncate(out),
            original=msg.content,
            translated=translated,
            source_lang=source_lang,
            target_lang=target_lang,
            status="translated",
        )

    async def process(self, msg: ChatIn) -> str | None:
        result = await self.process_detailed(msg)
        return result.output or None
