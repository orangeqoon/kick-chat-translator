"""使い方: python -m kicktrans [gui|demo|run|login|set-secret|init-config|migrate-auth]"""
from __future__ import annotations

import argparse
from dataclasses import replace
import asyncio
import getpass
import logging
import queue
import signal

from .config import CONFIG_PATH, Config


def _run_engine(demo: bool, mock: bool) -> None:
    from .engine import Engine
    q: queue.Queue[str] = queue.Queue()
    cfg = Config.load()
    if not demo:
        cfg = replace(cfg, receive_events=True)
    e = Engine(cfg, q, demo=demo, mock=mock)
    signal.signal(signal.SIGINT, lambda *_: e.stop())
    e.start()
    while True:
        try:
            line = q.get(timeout=0.5)
            print(line, flush=True)
            if line == "停止しました":
                break
        except queue.Empty:
            if not e._thread.is_alive() and q.empty():
                break
            continue
        if demo and line == "デモ完了":
            e.stop()


def main() -> None:
    p = argparse.ArgumentParser(prog="kicktrans")
    p.add_argument("cmd", nargs="?", default="gui",
                   choices=["gui", "demo", "run", "login", "set-secret", "init-config", "migrate-auth"])
    p.add_argument("--mock", action="store_true", help="翻訳を通信なしの MOCK にする")
    p.add_argument("--source", choices=["helper", "keyring"], help="migrate-auth の旧認証データ移行元")
    a = p.parse_args()
    if a.cmd == "migrate-auth" and not a.source:
        p.error("migrate-auth には --source helper または --source keyring が必要です")
    if a.source and a.cmd != "migrate-auth":
        p.error("--source は migrate-auth 専用です")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if a.cmd == "gui":
        from .gui import main as gui
        gui()
    elif a.cmd == "demo":
        _run_engine(True, a.mock)
    elif a.cmd == "run":
        _run_engine(False, a.mock)
    elif a.cmd == "init-config":
        if CONFIG_PATH.exists():
            print(f"既に存在します: {CONFIG_PATH}")
        else:
            Config.load().save(overwrite=False)
            print(f"作成しました: {CONFIG_PATH}")
    elif a.cmd == "set-secret":
        from .storage import save_client_secret
        s = getpass.getpass("Kick client_secret (表示されません): ")
        save_client_secret(s.strip())
        print("秘密情報を state/tokens のWindows DPAPI暗号ストアに保存しました")
    elif a.cmd == "migrate-auth":
        from .storage import migrate_legacy
        migrated = migrate_legacy(a.source)
        if migrated:
            print("認証データを暗号ストアへ移行しました。旧データは保持しています")
        else:
            print("移行できる旧認証データがありません")
    elif a.cmd == "login":
        from .kick_api import login_interactive
        cfg = Config.load()
        if not cfg.client_id:
            raise SystemExit(f"{CONFIG_PATH} の client_id を設定してください")
        asyncio.run(login_interactive(cfg.client_id, cfg.redirect_uri))
        print("ログイン成功。トークンは state/tokens のWindows DPAPI暗号ストアに保存しました")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # provider/storage exception messages may contain secrets
        # Provider responses or corrupt plaintext must not enter a traceback.
        raise SystemExit(f"操作に失敗しました ({type(exc).__name__})。設定・認証ストア・移行先の既存データを確認してください") from None
