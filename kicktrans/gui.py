"""tkinter (標準ライブラリ) の最小 GUI: 開始/停止/デモ + ログ。"""
from __future__ import annotations

from dataclasses import replace
import queue
import tkinter as tk
from tkinter import scrolledtext, ttk
from .settings import show_settings

from .config import Config
from .engine import Engine


def main() -> None:
    cfg = Config.load()
    events: queue.Queue[str] = queue.Queue()
    root = tk.Tk()
    root.title("Kick Translator 0.5.0")
    root.geometry("1100x760")
    updates = queue.Queue()
    state = {"engine": None, "closing": False}

    mode = tk.Label(root, font=("", 12, "bold"), fg="white")
    mode.pack(fill="x")

    def refresh_mode(active: Config = cfg) -> None:
        if active.live_posting:
            mode.config(text="ライブ投稿モード: Kick に実際に投稿します", bg="#c62828")
        elif active.receive_events:
            mode.config(text="受信テスト: Kick への投稿はOFF", bg="#2e7d32")
        else:
            mode.config(text="ドライラン: Kick には投稿しません", bg="#2e7d32")

    refresh_mode()
    bar = tk.Frame(root)
    bar.pack(fill="x")
    connection = tk.Label(root, text="停止中", anchor="w")
    connection.pack(fill="x")
    tree = ttk.Treeview(root, columns=("time", "sender", "language", "original", "translated", "status"), show="headings", height=15)
    for key, label, width in [("time","時刻",65),("sender","発言者",110),("language","言語",70),
                              ("original","原文",300),("translated","翻訳",300),("status","状態",100)]:
        tree.heading(key,text=label)
        tree.column(key,width=width)
    tree.pack(fill="both",expand=True)
    detail = scrolledtext.ScrolledText(root,height=4,state="disabled")
    detail.pack(fill="x")
    rows = {}
    status_labels = {"received":"受信", "translated":"翻訳済み", "posted":"投稿済み", "dry_run":"未投稿", "skipped":"対象外", "failed":"失敗"}
    def show_detail(event=None):
        selection = tree.selection()
        if selection:
            row = rows.get(selection[0], {})
            detail.config(state="normal")
            detail.delete("1.0","end")
            detail.insert("end", f"{row.get('sender','')}: {row.get('original','')}\n{row.get('translated','')}\n{row.get('detail','')}")
            detail.config(state="disabled")
    tree.bind("<<TreeviewSelect>>",show_detail)
    log = scrolledtext.ScrolledText(root, width=90, height=7, state="disabled")
    log.pack(fill="both", expand=True)

    def start(demo: bool, mock: bool = False, receive_only: bool = False) -> None:
        if state["closing"]:
            return
        e = state["engine"]
        if e and e._thread and e._thread.is_alive():
            return
        active = replace(cfg, live_posting=False, receive_events=False) if demo else replace(cfg, receive_events=True)
        if receive_only:
            active = replace(cfg, live_posting=False, receive_events=True)
        refresh_mode(active)
        state["engine"] = Engine(active, events, demo=demo, mock=mock, updates=updates)
        state["engine"].start()

    tk.Button(bar, text="開始", command=lambda: start(False)).pack(side="left")
    tk.Button(bar, text="受信テスト（投稿なし）", command=lambda: start(False, receive_only=True)).pack(side="left")
    tk.Button(bar, text="停止", command=lambda: state["engine"] and state["engine"].stop()).pack(side="left")
    tk.Button(bar, text="デモ (Google翻訳)", command=lambda: start(True)).pack(side="left")
    tk.Button(bar, text="デモ (MOCK・通信なし)", command=lambda: start(True, True)).pack(side="left")
    channel_label = tk.Label(bar, text=f"チャンネル: {cfg.channel_slug}")
    channel_label.pack(side="right")
    def saved(updated):
        nonlocal cfg
        cfg = updated
        channel_label.config(text=f"チャンネル: {cfg.channel_slug}")
        engine = state["engine"]
        if engine and getattr(engine,"_thread",None) and engine._thread.is_alive():
            engine.stop()
            connection.config(text="設定を保存しました。停止後に再接続します")
            def restart():
                if state["closing"]:
                    return
                if engine._thread.is_alive():
                    root.after(100,restart)
                else:
                    start(False)
            root.after(100,restart)
        else:
            refresh_mode(cfg)
    tk.Button(bar,text="設定",command=lambda: show_settings(root,cfg,saved)).pack(side="left")

    def pump() -> None:  # GUI スレッドからのみウィジェット更新
        try:
            while True:
                update = updates.get_nowait()
                if update.kind == "state":
                    connection.config(text=update.detail or update.state)
                elif update.message_id:
                    key = update.message_id
                    row = rows.setdefault(key, {"time":update.timestamp})
                    for name in ("sender","original","translated","source_lang","target_lang","status","detail"):
                        value = getattr(update,name)
                        if value:
                            row[name] = value
                    values = (row["time"],row.get("sender",""),
                              f"{row.get('source_lang','')}>{row.get('target_lang','')}",
                              row.get("original",""),row.get("translated",""),
                              status_labels.get(row.get("status"),row.get("status","")))
                    if tree.exists(key):
                        tree.item(key,values=values)
                    else:
                        tree.insert("","end",iid=key,values=values)
                    if len(rows)>1000:
                        oldest=next(iter(rows))
                        rows.pop(oldest)
                        tree.delete(oldest)
                    tree.see(key)
        except queue.Empty:
            pass
        try:
            while True:
                line = events.get_nowait()
                log.config(state="normal")
                log.insert("end", line + "\n")
                if int(log.index("end-1c").split(".")[0]) > 1500:
                    log.delete("1.0","200.0")
                log.see("end")
                log.config(state="disabled")
        except queue.Empty:
            pass
        root.after(100, pump)

    def on_close() -> None:
        if state["closing"]:
            return
        state["closing"] = True
        for widget in bar.winfo_children():
            if isinstance(widget, tk.Button):
                widget.config(state="disabled")
        e = state["engine"]
        if e is None:
            root.destroy()
            return
        e.stop()
        mode.config(text="停止しています。終了処理が終わるまでお待ちください。", bg="#2e7d32")

        def await_cleanup() -> None:
            e.join(0)
            worker = getattr(e, "_thread", None)
            if worker is not None and worker.is_alive():
                root.after(100, await_cleanup)
            else:
                root.destroy()

        await_cleanup()

    root.protocol("WM_DELETE_WINDOW", on_close)
    pump()
    root.mainloop()
