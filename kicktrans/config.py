"""Public runtime configuration; secrets belong in the encrypted token store."""
from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .paths import SERVICES_DIR

APP_DIR = SERVICES_DIR / "kick-translator"
CONFIG_PATH = APP_DIR / "config.json"
LEGACY_CONFIG_PATH = Path(os.environ.get("APPDATA") or Path.home() / ".config") / "kick-translator" / "config.json"
SHARED_CONFIG_PATH = SERVICES_DIR / "kick-config.json"
DEFAULT_REDIRECT_URI = "http://localhost:3000/oauth2callback"


def _read_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError):
        raise ValueError("設定JSONを読み取れません。内容と文字コードを確認してください") from None
    if not isinstance(data, dict):
        raise ValueError("設定JSONはオブジェクトにしてください")
    return data


def _public_data(data: dict) -> dict:
    if any(key in data for key in ("client_secret", "access_token", "refresh_token")):
        raise ValueError("翻訳ボット設定に秘密情報を書かないでください")
    return {key: value for key, value in data.items() if key in Config.__dataclass_fields__}


@dataclass
class Config:
    channel_slug: str = "your-channel"
    client_id: str = ""
    redirect_uri: str = DEFAULT_REDIRECT_URI
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8081
    webhook_path: str = "/kick/webhook"
    live_posting: bool = False
    receive_events: bool = False
    receive_method: str = "websocket"
    home_lang: str = "ja"
    foreign_lang: str = "en"
    trans_to_home_only: bool = False
    output_prefix: str = "[TR]"
    show_author: bool = True
    skip_users: list[str] = field(default_factory=list)
    skip_languages: list[str] = field(default_factory=list)
    skip_commands: bool = True
    min_post_interval_sec: float = 1.5
    max_queue: int = 50
    max_event_age_sec: int = 300

    def __post_init__(self) -> None:
        if self.receive_method not in ("websocket", "webhook"):
            raise ValueError("接続方式を選択してください")
        if type(self.trans_to_home_only) is not bool:
            raise ValueError("母語のみの設定は true または false にしてください")
        for language in (self.home_lang, self.foreign_lang):
            if not isinstance(language, str) or not language.strip() or len(language) > 16 or not all(c.isascii() and (c.isalpha() or c == "-") for c in language):
                raise ValueError("言語コードを確認してください")
        if not isinstance(self.output_prefix, str) or not self.output_prefix.strip() or len(self.output_prefix) > 32:
            raise ValueError("翻訳出力の目印は1〜32文字で設定してください")
        if not isinstance(self.channel_slug, str) or not self.channel_slug.strip() or not all(c.isascii() and (c.isalnum() or c in "_-") for c in self.channel_slug):
            raise ValueError("Kickチャンネル名を確認してください")
        if type(self.webhook_port) is not int or not 1 <= self.webhook_port <= 65535:
            raise ValueError("受信ポートを確認してください")
        for values in (self.skip_users, self.skip_languages):
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError("除外リストは文字列の一覧にしてください")
        if type(self.show_author) is not bool or type(self.skip_commands) is not bool:
            raise ValueError("表示・除外の設定は true または false にしてください")
        if isinstance(self.min_post_interval_sec, bool) or not isinstance(self.min_post_interval_sec, (int, float)) or not math.isfinite(self.min_post_interval_sec) or not 0 <= self.min_post_interval_sec <= 300:
            raise ValueError("投稿間隔は0〜300秒にしてください")
        if type(self.max_queue) is not int or not 1 <= self.max_queue <= 5000:
            raise ValueError("送信待ちは1〜5000件にしてください")
        if type(self.max_event_age_sec) is not int or not 1 <= self.max_event_age_sec <= 3600:
            raise ValueError("イベント期限は1〜3600秒にしてください")
        if not isinstance(self.webhook_host, str) or not self.webhook_host.strip():
            raise ValueError("受信ホストを確認してください")
        if not isinstance(self.webhook_path, str) or not self.webhook_path.startswith("/") or any(c in self.webhook_path for c in " ?#"):
            raise ValueError("受信パスを確認してください")
        if not isinstance(self.client_id, str) or not isinstance(self.redirect_uri, str):
            raise ValueError("client_id と redirect_uri は文字列にしてください")
        if type(self.receive_events) is not bool:
            raise ValueError("receive_events は true または false にしてください")
        if type(self.live_posting) is not bool:
            raise ValueError("live_posting は true または false にしてください")
        try:
            url = urlparse(self.redirect_uri)
            port = url.port
            valid = (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1")
                     and port is not None and 1 <= port <= 65535 and not url.username and not url.password
                     and not url.query and not url.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("redirect_uri はポート付きの http://localhost または http://127.0.0.1 にしてください")

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        if path is not None:
            path = Path(path)
            return cls(**_public_data(_read_object(path))) if path.exists() else cls()
        if CONFIG_PATH.exists():
            return cls(**_public_data(_read_object(CONFIG_PATH)))
        if LEGACY_CONFIG_PATH.exists():
            data = _public_data(_read_object(LEGACY_CONFIG_PATH))
            # Reading an old installation must never silently enable posting.
            data["live_posting"] = False
            data["receive_events"] = False
            return cls(**data)
        # Reuse only public app identity; never copy the shared secret to config.
        if SHARED_CONFIG_PATH.exists():
            shared = _read_object(SHARED_CONFIG_PATH)
            data = {key: shared[key] for key in ("client_id", "redirect_uri") if key in shared}
            return cls(**data)
        return cls()

    def save(self, path: Path | None = None, *, overwrite: bool = True) -> None:
        self.__post_init__()
        path = Path(path) if path is not None else CONFIG_PATH
        payload = json.dumps(asdict(self), ensure_ascii=False, indent=2).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".config-", delete=False) as temp:
                temp_path = Path(temp.name)
                temp.write(payload)
                temp.flush()
                os.fsync(temp.fileno())
            if overwrite and path.exists():
                suffix = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                backup = path.with_name(path.name + ".bak-" + suffix)
                with backup.open("xb") as dest, path.open("rb") as source:
                    shutil.copyfileobj(source, dest)
            if overwrite:
                os.replace(temp_path, path)
            elif os.name == "nt":
                # Windows rename refuses an existing destination atomically.
                os.rename(temp_path, path)
            else:
                # Same-directory linking provides no-replace publication on POSIX.
                os.link(temp_path, path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
