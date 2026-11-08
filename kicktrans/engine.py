"""バックグラウンドスレッドで asyncio を回すエンジン。GUI とは queue でやりとり。"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import uuid

import aiohttp
from aiohttp import web

from .config import Config
from .events import EngineEvent
from .pipeline import ChatIn, Pipeline
from .sender import SendQueue
from .translator import GoogleWebTranslator, MockTranslator

log = logging.getLogger(__name__)

DEMO_MESSAGES = [
    ("viewer_en", "Hello! Great stream today"),
    ("viewer_ko", "안녕하세요 반가워요"),
    ("viewer_es", "¡Hola! ¿De dónde eres?"),
    ("orangeqoon", "みんな来てくれてありがとう"),
    ("viewer_en", "!discord"),
    ("viewer_en", "[emote:37226:KEKW] [emote:37226:KEKW]"),
    ("translat_echo", "[TR] これはループ防止テスト"),
]


class _QueueHandler(logging.Handler):
    """kicktrans のログを GUI キューへ (トークン等はログに出さない設計)。"""

    def __init__(self, q):
        super().__init__(logging.INFO)
        self.q = q

    def emit(self, record):
        self.q.put(f"{record.levelname}: {record.getMessage()}")


class Engine:
    def __init__(self, cfg: Config, events: "queue.Queue[str]", demo: bool = False, mock: bool = False, updates=None):
        self.cfg = cfg
        self.updates = updates
        self.events = events  # GUI へのログ行
        self.demo = demo
        self.mock = mock
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._stop_latch = threading.Event()

    def emit(self, line: str) -> None:
        self.events.put(line)

    def update(self, **values):
        if self.updates is not None:
            self.updates.put(EngineEvent(**values))

    # --- スレッド制御 ---
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_latch.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_latch.set()
        loop, stop = self._loop, self._stop
        if loop is None or stop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(stop.set)
        except RuntimeError:
            # The worker can close the loop between the check and scheduling.
            if not loop.is_closed():
                raise

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:  # noqa: BLE001
            self.emit(f"エラーで停止: {type(e).__name__}")
            self.update(kind="state", state="error", detail=type(e).__name__)
        finally:
            self._loop.close()
            self.emit("停止しました")
            self.update(kind="state", state="stopped")

    # --- 本体 ---
    async def _main(self) -> None:
        self._stop = asyncio.Event()
        if self._stop_latch.is_set():
            self._stop.set()
        handler = _QueueHandler(self.events)
        logging.getLogger("kicktrans").addHandler(handler)
        logging.getLogger("kicktrans").setLevel(logging.INFO)
        logging.getLogger("kicktrans").propagate = False
        try:
            await self._main_inner()
        finally:
            logging.getLogger("kicktrans").removeHandler(handler)

    def _stop_pending(self) -> bool:
        return self._stop_latch.is_set() or (self._stop is not None and self._stop.is_set())

    async def _main_inner(self) -> None:
        translator = MockTranslator() if self.mock else GoogleWebTranslator()
        try:
            if self._stop_pending():
                return
            pipe = Pipeline(self.cfg, translator)
            live = self.cfg.live_posting and not self.demo
            receive_events = getattr(self.cfg, "receive_events", False) and not self.demo
            mode = "【ライブ投稿】Kick に投稿します" if live else "【ドライラン】Kick には投稿しません"
            self.emit(mode + (" / デモ" if self.demo else "") + (" / MOCK翻訳" if self.mock else ""))

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                if self._stop_pending():
                    return
                client = None
                sub_ids: list[str] = []
                if live or (receive_events and getattr(self.cfg, "receive_method", "webhook") == "webhook"):
                    from .kick_api import KickClient, load_tokens
                    if self._stop_pending():
                        return
                    tokens = load_tokens()
                    if self._stop_pending():
                        return
                    if not tokens:
                        raise RuntimeError("未ログイン: `python -m kicktrans login` を先に実行")
                    client = KickClient(self.cfg.client_id, tokens, session)
                    if self._stop_pending():
                        return
                    me = await client.me()
                    if self._stop_pending():
                        return
                    # Owner manual chat must remain eligible for translation.
                    ch = await client.channel(self.cfg.channel_slug)
                    if self._stop_pending():
                        return
                    if int(ch["broadcaster_user_id"]) != int(me["user_id"]):
                        raise RuntimeError("ログイン中のユーザーと投稿先チャンネルが一致しません")

                if self._stop_pending():
                    return

                pending = {}
                async def post(text: str) -> str | None:
                    if not live:
                        mid = "dry-" + uuid.uuid4().hex
                        self.emit(f"(未投稿・ドライラン) {text}")
                        return mid
                    return await client.post_chat(text)

                def on_sent(text: str, mid: str | None) -> None:
                    if live and mid:
                        pipe.sent_message_ids.add(mid)
                    if live:
                        self.emit(f"投稿済み: {text}")
                    mids = pending.get(text, [])
                    if mids:
                        self.update(kind="message", message_id=mids.pop(0), status="posted" if live else "dry_run")

                def on_failed(text):
                    mids = pending.get(text, [])
                    if mids:
                        self.update(kind="message", message_id=mids.pop(0), status="failed", detail="投稿失敗")
                sq = SendQueue(post, self.cfg.min_post_interval_sec, self.cfg.max_queue,
                               on_sent=on_sent, on_failed=on_failed)
                sender_task = asyncio.create_task(sq.run())

                async def handle(msg: ChatIn) -> None:
                    self.emit(f"受信 {msg.sender_name}: {msg.content}")
                    self.update(kind="message", message_id=msg.message_id, sender=msg.sender_name,
                                original=msg.content, status="received")
                    try:
                        result = await pipe.process_detailed(msg)
                    except Exception as error:
                        self.emit(f"翻訳失敗: {type(error).__name__}")
                        self.update(kind="message", message_id=msg.message_id, status="failed", detail=type(error).__name__)
                        return
                    self.update(kind="message", message_id=msg.message_id, translated=result.translated,
                                source_lang=result.source_lang, target_lang=result.target_lang,
                                status=result.status, detail=result.detail)
                    if result.output:
                        pending.setdefault(result.output, []).append(msg.message_id)
                        if not sq.submit(result.output):
                            pending[result.output].pop()
                            self.update(kind="message", message_id=msg.message_id, status="failed", detail="送信待ち上限")

                runner = None
                try:
                    if self.demo:
                        for i, (user, text) in enumerate(DEMO_MESSAGES):
                            await handle(ChatIn(f"demo-{i}", 1000 + i, user, text))
                        await handle(ChatIn("demo-0", 1000, "viewer_en", "Hello! Great stream today"))  # 重複テスト
                        while sq._q._unfinished_tasks and not self._stop_pending():
                            await asyncio.sleep(0.1)
                        await asyncio.sleep(0.2)
                        self.emit("デモ完了")
                    else:
                        if self._stop_pending():
                            return
                        if receive_events and getattr(self.cfg, "receive_method", "webhook") == "websocket":
                            from .kick_socket import receive_chat
                            def socket_state(state, detail):
                                self.emit(detail)
                                self.update(kind="state", state=state, detail=detail)
                            await receive_chat(session, self.cfg.channel_slug, handle, self._stop, socket_state)
                            return
                        runner = await self._start_webhook(session, pipe, client, handle, sub_ids)
                        if self._stop_pending():
                            return
                        await self._stop.wait()
                finally:
                    if client is not None and sub_ids:
                        try:
                            await client.unsubscribe(sub_ids)
                            self.emit("イベント購読を解除しました")
                        except Exception as e:  # noqa: BLE001
                            self.emit(f"購読解除失敗: {e}")
                    if runner:
                        await runner.cleanup()
                    sender_task.cancel()
                    try:
                        await sender_task
                    except asyncio.CancelledError:
                        pass
        finally:
            await translator.close()

    async def _start_webhook(self, session, pipe, client, handle, sub_ids):
        from .webhook import WebhookVerifier, fetch_public_key, make_app

        tasks: set[asyncio.Task] = set()

        async def on_event(etype: str, payload: dict) -> None:
            if etype != "chat.message.sent":
                return
            t = asyncio.create_task(handle(ChatIn.from_event(payload)))  # 応答を遅らせない
            tasks.add(t)
            t.add_done_callback(tasks.discard)

        verifier = WebhookVerifier(lambda: fetch_public_key(session), self.cfg.max_event_age_sec)
        app = make_app(verifier, self.cfg.webhook_path, on_event)
        if self._stop_pending():
            return None
        runner = web.AppRunner(app)
        try:
            await runner.setup()
            if self._stop_pending():
                return runner
            await web.TCPSite(runner, self.cfg.webhook_host, self.cfg.webhook_port).start()
            if self._stop_pending():
                return runner
            self.emit(f"webhook 待受: http://{self.cfg.webhook_host}:{self.cfg.webhook_port}{self.cfg.webhook_path}"
                      " (トンネルの公開URLを Kick 開発者設定に登録してください)")
            if client is not None:
                if self._stop_pending():
                    return runner
                me = await client.me()
                if self._stop_pending():
                    return runner
                ids = await client.subscribe_chat(int(me["user_id"]))
                sub_ids.extend(ids)
                self.emit(f"chat.message.sent を購読しました ({len(sub_ids)})")
            else:
                self.emit("ドライランのため購読は作成しません (既存購読があれば受信は可能)")
            return runner
        except BaseException:
            await runner.cleanup()
            raise
