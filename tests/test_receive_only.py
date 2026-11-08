import asyncio
import queue
import threading
from types import SimpleNamespace

import pytest


def make_config(**overrides):
    values = {
        "channel_slug": "synthetic-channel",
        "client_id": "synthetic-client",
        "webhook_host": "127.0.0.1",
        "webhook_port": 8080,
        "webhook_path": "/synthetic-hook",
        "max_event_age_sec": 300,
        "live_posting": False,
        "receive_events": False,
        "output_prefix": "[TR]",
        "show_author": True,
        "skip_users": [],
        "skip_languages": [],
        "skip_commands": True,
        "min_post_interval_sec": 0,
        "max_queue": 10,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeRunner:
    def __init__(self, fail_setup=False):
        self.fail_setup = fail_setup
        self.cleanup_calls = 0

    async def setup(self):
        if self.fail_setup:
            raise RuntimeError("synthetic setup failure")

    async def cleanup(self):
        self.cleanup_calls += 1


class FakeClient:
    def __init__(self, fail_subscribe=False):
        self.fail_subscribe = fail_subscribe
        self.posts = []
        self.subscribe_args = []
        self.unsubscribe_args = []

    async def me(self):
        return {"user_id": 42}

    async def channel(self, slug):
        return {"broadcaster_user_id": 42}

    async def post_chat(self, content):
        self.posts.append(content)
        return "posted-message"

    async def subscribe_chat(self, broadcaster_user_id):
        self.subscribe_args.append(broadcaster_user_id)
        if self.fail_subscribe:
            raise RuntimeError("synthetic subscribe failure")
        return ["owned-subscription"]

    async def unsubscribe(self, ids):
        self.unsubscribe_args.append(list(ids))


class DelayedSubscribeClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.subscribe_started = asyncio.Event()
        self.release_subscribe = asyncio.Event()

    async def subscribe_chat(self, broadcaster_user_id):
        self.subscribe_args.append(broadcaster_user_id)
        self.subscribe_started.set()
        await self.release_subscribe.wait()
        return ["owned-subscription"]


async def run_event_engine(monkeypatch, cfg, client):
    from kicktrans import engine as engine_module, kick_api
    from kicktrans.pipeline import ChatIn

    monkeypatch.setattr(engine_module.aiohttp, "ClientSession", lambda *args, **kwargs: FakeSession())
    monkeypatch.setattr(kick_api, "load_tokens", lambda: object())
    monkeypatch.setattr(kick_api, "KickClient", lambda client_id, tokens, session: client)

    events = queue.Queue()
    engine = engine_module.Engine(cfg, events, mock=True)
    runner = FakeRunner()

    async def fake_start(session, pipe, client_arg, handle, sub_ids):
        sub_ids.extend(await client_arg.subscribe_chat(42))
        await handle(ChatIn("event-1", 7, "viewer", "Hello from event"))
        for _ in range(10):
            await asyncio.sleep(0)
        engine._stop.set()
        return runner

    monkeypatch.setattr(engine, "_start_webhook", fake_start)
    await engine._main()
    return list(events.queue), runner


@pytest.mark.asyncio
async def test_receive_only_translates_without_posting(monkeypatch):
    client = FakeClient()
    lines, runner = await run_event_engine(
        monkeypatch,
        make_config(receive_events=True),
        client,
    )

    assert any("受信 viewer: Hello from event" in line for line in lines)
    assert any("(未投稿・ドライラン) [TR]" in line for line in lines)
    assert not any("投稿済み:" in line for line in lines)
    assert client.posts == []
    assert client.subscribe_args == [42]
    assert client.unsubscribe_args == [["owned-subscription"]]
    assert runner.cleanup_calls == 1


@pytest.mark.asyncio
async def test_live_posting_keeps_existing_send_path(monkeypatch):
    client = FakeClient()
    lines, _ = await run_event_engine(
        monkeypatch,
        make_config(live_posting=True),
        client,
    )

    assert len(client.posts) == 1
    assert client.posts[0].startswith("[TR] ")
    assert any("投稿済み: [TR]" in line for line in lines)


@pytest.mark.asyncio
async def test_demo_receive_only_never_loads_client_or_tokens(monkeypatch):
    from kicktrans import engine as engine_module, kick_api

    monkeypatch.setattr(engine_module.aiohttp, "ClientSession", lambda *args, **kwargs: FakeSession())
    token_calls = []

    def fail_load_tokens():
        token_calls.append(True)
        raise AssertionError("demo must not load tokens")

    def fail_client(*args):
        raise AssertionError("demo must not construct a client")

    monkeypatch.setattr(kick_api, "load_tokens", fail_load_tokens)
    monkeypatch.setattr(kick_api, "KickClient", fail_client)
    events = queue.Queue()
    engine = engine_module.Engine(make_config(receive_events=True), events, demo=True, mock=True)

    await engine._main()

    assert token_calls == []
    assert "デモ完了" in list(events.queue)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["setup", "start", "subscribe"])
async def test_start_webhook_cleans_runner_on_failure(monkeypatch, phase):
    from kicktrans import engine as engine_module, webhook

    runner = FakeRunner(fail_setup=phase == "setup")

    class FakeSite:
        def __init__(self, *args):
            pass

        async def start(self):
            if phase == "start":
                raise RuntimeError("synthetic start failure")

    monkeypatch.setattr(engine_module.web, "AppRunner", lambda app: runner)
    monkeypatch.setattr(engine_module.web, "TCPSite", FakeSite)
    monkeypatch.setattr(webhook, "make_app", lambda verifier, path, handler: object())

    client = FakeClient(fail_subscribe=phase == "subscribe")
    engine = engine_module.Engine(make_config(), queue.Queue(), mock=True)
    with pytest.raises(RuntimeError):
        await engine._start_webhook(object(), object(), client, lambda msg: None, [])

    assert runner.cleanup_calls == 1


def test_stop_before_worker_initialization_latches_request(monkeypatch):
    from kicktrans import engine as engine_module, kick_api

    def fail_session():
        raise AssertionError("stop-before-worker must avoid creating a session")

    monkeypatch.setattr(engine_module.aiohttp, "ClientSession", fail_session)
    monkeypatch.setattr(kick_api, "load_tokens", lambda: (_ for _ in ()).throw(AssertionError("tokens accessed")))
    monkeypatch.setattr(kick_api, "KickClient", lambda *args: (_ for _ in ()).throw(AssertionError("client created")))

    engine = engine_module.Engine(make_config(receive_events=True), queue.Queue(), mock=True)
    startup = threading.Event()
    release = threading.Event()
    original_run = engine._run

    def delayed_run():
        startup.set()
        release.wait(20)
        original_run()

    monkeypatch.setattr(engine, "_run", delayed_run)
    engine.start()
    assert startup.wait(20)
    engine.stop()
    release.set()
    engine.join(20)

    assert engine._thread is not None and not engine._thread.is_alive()


@pytest.mark.asyncio
async def test_stop_during_subscribe_unsubscribes_returned_id_and_cleans_runner(monkeypatch):
    from kicktrans import engine as engine_module, kick_api, webhook

    monkeypatch.setattr(engine_module.aiohttp, "ClientSession", lambda *args, **kwargs: FakeSession())
    monkeypatch.setattr(kick_api, "load_tokens", lambda: object())
    client = DelayedSubscribeClient()
    monkeypatch.setattr(kick_api, "KickClient", lambda client_id, tokens, session: client)

    runner = FakeRunner()

    class FakeSite:
        def __init__(self, *args):
            pass

        async def start(self):
            return None

    monkeypatch.setattr(engine_module.web, "AppRunner", lambda app: runner)
    monkeypatch.setattr(engine_module.web, "TCPSite", FakeSite)
    monkeypatch.setattr(webhook, "make_app", lambda verifier, path, handler: object())

    engine = engine_module.Engine(make_config(receive_events=True), queue.Queue(), mock=True)
    engine._loop = asyncio.get_running_loop()
    main_task = asyncio.create_task(engine._main())
    await asyncio.wait_for(client.subscribe_started.wait(), 20)
    engine.stop()
    client.release_subscribe.set()
    await asyncio.wait_for(main_task, 20)

    assert client.unsubscribe_args == [["owned-subscription"]]
    assert runner.cleanup_calls == 1
