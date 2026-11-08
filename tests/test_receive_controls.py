import asyncio
import importlib.util
from pathlib import Path

import pytest

from kicktrans.config import Config
from kicktrans.kick_api import KickClient, Tokens


@pytest.mark.parametrize("data", [[], None, [{"id": "not-a-post-id"}], [{"error": "synthetic-sensitive-provider-error"}], [{"subscription_id": None}]])
def test_subscription_without_successful_post_id_is_rejected(data):
    client = KickClient("synthetic-app", Tokens("synthetic-access", "synthetic-refresh", 1900000000), object())
    async def fake_request(*args, **kwargs):
        return {"data": data}
    client._req = fake_request
    with pytest.raises(RuntimeError) as error:
        asyncio.run(client.subscribe_chat(42))
    assert "synthetic-sensitive" not in str(error.value)


def test_subscription_returns_only_its_post_ids():
    client = KickClient("synthetic-app", Tokens("synthetic-access", "synthetic-refresh", 1900000000), object())
    async def fake_request(method, path, **kwargs):
        assert method == "POST" and path == "/public/v1/events/subscriptions"
        assert kwargs["json"]["events"] == [{"name": "chat.message.sent", "version": 1}]
        return {"data": [{"name": "chat.message.sent", "version": 1, "subscription_id": "owned-subscription", "error": None}]}
    client._req = fake_request
    assert asyncio.run(client.subscribe_chat(42)) == ["owned-subscription"]


def test_receive_events_requires_boolean(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"receive_events":"false"}', encoding="utf-8")
    with pytest.raises(ValueError):
        Config.load(path)


def test_gui_receive_button_overrides_live_setting(monkeypatch):
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows tkinter smoke")
    import tkinter as tk
    from kicktrans import gui

    original_tk = tk.Tk
    observed = []
    results = []
    class FakeEngine:
        def __init__(self, cfg, events, **kwargs):
            observed.append(cfg)
        def start(self):
            pass
        def stop(self):
            pass
        def join(self, timeout):
            pass
    monkeypatch.setattr(gui, "Engine", FakeEngine)
    monkeypatch.setattr(gui.Config, "load", lambda: Config(live_posting=True))

    def factory(*args, **kwargs):
        root = original_tk(*args, **kwargs)
        root.withdraw()
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        def exercise():
            widgets = list(descendants(root))
            button = next(w for w in widgets if isinstance(w, tk.Button) and w.cget("text") == "受信テスト（投稿なし）")
            button.invoke()
            results.append(any(isinstance(w, tk.Label) and "投稿はOFF" in str(w.cget("text")) for w in widgets))
            root.tk.call(root.protocol("WM_DELETE_WINDOW"))
        root.after(20, exercise)
        root.after(3000, root.destroy)
        return root
    monkeypatch.setattr(tk, "Tk", factory)
    gui.main()
    assert len(observed) == 1 and observed[0].receive_events is True
    assert observed[0].live_posting is False
    assert results == [True]


def test_gui_keeps_window_until_worker_cleanup_finishes(monkeypatch):
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows tkinter smoke")
    import tkinter as tk
    from kicktrans import gui

    original_tk = tk.Tk
    checks = []
    engines = []
    class FakeThread:
        alive = True
        def is_alive(self):
            return self.alive
    class SlowEngine:
        def __init__(self, *args, **kwargs):
            self._thread = FakeThread()
            self.stopped = False
            self.joins = []
            engines.append(self)
        def start(self):
            pass
        def stop(self):
            self.stopped = True
        def join(self, timeout):
            self.joins.append(timeout)
    monkeypatch.setattr(gui, "Engine", SlowEngine)
    monkeypatch.setattr(gui.Config, "load", lambda: Config())

    def factory(*args, **kwargs):
        root = original_tk(*args, **kwargs)
        root.withdraw()
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        def exercise():
            widgets = list(descendants(root))
            button = next(w for w in widgets if isinstance(w, tk.Button) and w.cget("text") == "受信テスト（投稿なし）")
            button.invoke()
            root.tk.call(root.protocol("WM_DELETE_WINDOW"))
            def finish_worker():
                checks.append(bool(root.winfo_exists()) and engines[0].stopped)
                checks.append(all(str(w.cget("state")) == "disabled" for w in widgets if isinstance(w, tk.Button)))
                engines[0]._thread.alive = False
            root.after(30, finish_worker)
        root.after(20, exercise)
        root.after(3000, root.destroy)
        return root
    monkeypatch.setattr(tk, "Tk", factory)
    gui.main()
    assert checks == [True, True]
    assert engines[0].joins and all(timeout == 0 for timeout in engines[0].joins)