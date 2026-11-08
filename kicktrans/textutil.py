"""チャット本文の前処理と Kick の文字数制限。"""
from __future__ import annotations

import re

import regex  # \X (書記素クラスタ) のため

EMOTE_RE = re.compile(r"\[emote:\d+:[^\]]*\]")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
KANA_RE = re.compile(r"[\u3040-\u30ff]")
MAX_GRAPHEMES = 500
MAX_BYTES = 2048


def clean(text: str) -> str:
    """エモート・URL・制御文字を除去。結果は翻訳にのみ使う (実行はしない)。"""
    text = EMOTE_RE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    text = "".join(ch for ch in text if ch.isprintable() or ch == " ")
    return re.sub(r"\s+", " ", text).strip()


def is_command(text: str) -> bool:
    return text.lstrip().startswith("!")


def has_letters(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


def looks_japanese(text: str) -> bool:
    return bool(KANA_RE.search(text))


def grapheme_len(text: str) -> int:
    return len(regex.findall(r"\X", text))


def truncate(text: str, max_graphemes: int = MAX_GRAPHEMES, max_bytes: int = MAX_BYTES) -> str:
    """書記素 500 個かつ UTF-8 2048 バイト以内に収める (Kick 公式ドキュメント準拠)。"""
    clusters = regex.findall(r"\X", text)
    if len(clusters) <= max_graphemes and len(text.encode("utf-8")) <= max_bytes:
        return text
    ell = "…"
    out: list[str] = []
    size = len(ell.encode("utf-8"))
    for c in clusters[: max_graphemes - 1]:
        b = len(c.encode("utf-8"))
        if size + b > max_bytes:
            break
        out.append(c)
        size += b
    return "".join(out) + ell
