import asyncio
import base64
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kicktrans.config import Config
from kicktrans.kick_api import authorize_url, pkce_pair
from kicktrans.pipeline import ChatIn, Pipeline
from kicktrans.sender import RateLimited, SendQueue
from kicktrans.textutil import MAX_BYTES, clean, grapheme_len, truncate
from kicktrans.translator import MockTranslator, choose_target
from kicktrans.webhook import WebhookVerifier, make_app

# ---- text ----

def test_clean_removes_emotes_and_urls():
    assert clean("Hi [emote:1:KEKW] https://x.y/z ok") == "Hi ok"


def test_truncate_graphemes():
    s = "👨‍👩‍👧" * 600  # 1 書記素 = 複数コードポイント
    t = truncate(s, max_bytes=10**6)
    assert grapheme_len(t) == 500


def test_truncate_bytes():
    t = truncate("あ" * 499)  # 499 書記素だが 1497 バイト -> OK
    assert t == "あ" * 499
    t = truncate("😀" * 499)  # 4 バイト * 499 = 1996 OK
    assert len(t.encode()) <= MAX_BYTES
    t = truncate("👍🏽" * 400)  # 8 バイト/書記素 -> 2048 で切る
    assert len(t.encode()) <= MAX_BYTES and t.endswith("…")


def test_choose_target():
    assert choose_target("こんにちは", "und") == "en"
    assert choose_target("hello", "en") == "ja"
    assert choose_target("漢字", "ja") == "en"
    assert choose_target("你好", "zh-CN") == "ja"

# ---- pipeline ----

def run(c):
    return asyncio.run(c)


def make_pipe(**kw):
    p = Pipeline(Config(**kw), MockTranslator())
    p.own_user_ids.add(42)
    return p


def test_pipeline_translates_and_marks():
    out = run(make_pipe().process(ChatIn("m1", 1, "bob", "hello")))
    assert out.startswith("[TR] [MOCK en>ja] hello") and "[by bob]" in out


def test_pipeline_ja_to_en():
    out = run(make_pipe().process(ChatIn("m1", 1, "o", "ありがとう")))
    assert "(ja>en)" in out


@pytest.mark.parametrize("msg,reason", [
    (ChatIn("a", 42, "me", "hello"), "自分の投稿 (user_id)"),
    (ChatIn("b", 1, "x", "[TR] hi"), "翻訳出力プレフィックス"),
    (ChatIn("c", 1, "x", "!so bob"), "コマンド"),
    (ChatIn("d", 1, "nightbot", "hello"), "無視ユーザー"),
    (ChatIn("", 1, "x", "hello"), "message_id なし"),
])
def test_pipeline_skips(msg, reason):
    p = make_pipe(skip_users=["NightBot"])
    assert p.skip_reason(msg) == reason


def test_pipeline_dedup_and_sent_ids():
    p = make_pipe()
    assert run(p.process(ChatIn("m1", 1, "x", "hello")))
    assert run(p.process(ChatIn("m1", 1, "x", "hello"))) is None
    p.sent_message_ids.add("mine")
    assert p.skip_reason(ChatIn("mine", 7, "bot", "whatever")) == "自分の投稿 (message_id)"


def test_pipeline_skip_language():
    p = make_pipe(skip_languages=["en"])
    assert run(p.process(ChatIn("m", 1, "x", "hello"))) is None


def test_pipeline_untrusted_content_not_executed():
    # 本文はただの文字列として翻訳されるだけ
    out = run(make_pipe().process(ChatIn("m", 1, "x", "$(rm -rf /) ; os.system('x')")))
    assert "rm -rf" in out

# ---- sender ----

def test_sender_retries_429_then_succeeds():
    calls, sleeps, sent = [], [], []

    async def post(t):
        calls.append(t)
        if len(calls) < 3:
            raise RateLimited(None if len(calls) == 1 else 7)
        return "mid"

    async def fake_sleep(s):
        sleeps.append(s)

    async def main():
        q = SendQueue(post, min_interval=1, on_sent=lambda t, m: sent.append(m), sleep=fake_sleep)
        q.submit("x")
        await q._send_one(await q._q.get())

    run(main())
    assert len(calls) == 3 and sleeps == [2.0, 7] and sent == ["mid"]


def test_sender_bounded():
    async def main():
        q = SendQueue(lambda t: None, maxsize=2)
        return [q.submit(str(i)) for i in range(3)], q.dropped
    res, dropped = run(main())
    assert res == [True, True, False] and dropped == 1

# ---- oauth helpers ----

def test_pkce_and_url():
    v, c = pkce_pair()
    assert 43 <= len(v) <= 128 and "=" not in c
    u = authorize_url("cid", "http://localhost:8765/callback", "st", c)
    assert u.startswith("https://id.kick.com/oauth/authorize?") and "code_challenge_method=S256" in u
    u2 = authorize_url("cid", "http://127.0.0.1:8765/cb", "st", c)
    assert u2.index("redirect=127.0.0.1") < u2.index("redirect_uri=")


def test_config_rejects_secrets(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"client_secret": "x"}')
    with pytest.raises(ValueError):
        Config.load(p)

# ---- webhook signature ----
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def signed(mid, ts, body):
    sig = KEY.sign(f"{mid}.{ts}.".encode() + body, padding.PKCS1v15(), hashes.SHA256())
    return {"Kick-Event-Message-Id": mid, "Kick-Event-Message-Timestamp": ts,
            "Kick-Event-Signature": base64.b64encode(sig).decode(), "Kick-Event-Type": "chat.message.sent"}


async def _key():
    return KEY.public_key()


def test_verifier():
    async def main():
        v = WebhookVerifier(_key, 300)
        now = datetime.now(timezone.utc)
        ts = now.isoformat().replace("+00:00", "Z")
        body = b'{"message_id":"x"}'
        h = signed("01A", ts, body)
        r1 = await v.check(h, body)
        r2 = await v.check(h, body)  # 再送
        r3 = await v.check(h | {"Kick-Event-Message-Id": "01B"}, body)  # 署名不一致
        r4 = await v.check(signed("01C", ts, body), body + b" ")  # 改ざん
        old = (now - timedelta(minutes=10)).isoformat()
        r5 = await v.check(signed("01D", old, body), body)
        return r1, r2, r3, r4, r5
    r = run(main())
    assert r[0] == (True, "ok")
    assert r[1] == (False, "再送/重複")
    assert r[2][1] == "署名不一致" and r[3][1] == "署名不一致"
    assert r[4][1].startswith("タイムスタンプ")


async def test_webhook_http(aiohttp_client):
    got = []

    async def handler(t, p):
        got.append((t, p["message_id"]))
    app = make_app(WebhookVerifier(_key), "/hook", handler)
    c = await aiohttp_client(app)
    body = b'{"message_id":"m9","content":"hi"}'
    ts = datetime.now(timezone.utc).isoformat()
    assert (await c.post("/hook", data=body, headers=signed("01Z", ts, body))).status == 200
    assert (await c.post("/hook", data=body, headers=signed("01Z", ts, body))).status == 200  # 重複は200で無視
    assert (await c.post("/hook", data=body, headers={})).status == 401
    assert got == [("chat.message.sent", "m9")]

# ---- engine lifecycle ----

def test_engine_stop_after_mock_demo_completion():
    import queue
    from kicktrans.engine import Engine

    events = queue.Queue()
    engine = Engine(Config(min_post_interval_sec=0), events, demo=True, mock=True)
    engine.start()
    engine.join(5)
    assert not engine._thread.is_alive()
    lines = list(events.queue)
    assert "デモ完了" in lines and "停止しました" in lines
    engine.stop()
    engine.stop()


def test_engine_stop_when_loop_closes_during_scheduling(monkeypatch):
    import queue
    from kicktrans.engine import Engine

    engine = Engine(Config(), queue.Queue())
    loop = asyncio.new_event_loop()
    engine._loop = loop
    engine._stop = asyncio.Event()
    schedule = loop.call_soon_threadsafe

    def close_then_schedule(callback):
        loop.close()
        return schedule(callback)

    monkeypatch.setattr(loop, "call_soon_threadsafe", close_then_schedule)
    try:
        engine.stop()
    finally:
        loop.close()