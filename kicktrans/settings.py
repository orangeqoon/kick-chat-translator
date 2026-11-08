"""Public configuration editor; credentials never enter this form."""
from dataclasses import replace
import tkinter as tk
from tkinter import ttk, messagebox


def show_settings(root, cfg, on_save):
    window = tk.Toplevel(root)
    window.title('翻訳設定')
    window.transient(root)
    fields = {}
    labels = [('channel_slug','Kickチャンネル'), ('home_lang','母語コード'),
              ('foreign_lang','母語からの翻訳先'), ('client_id','Kickアプリ Client ID'),
              ('redirect_uri','Kickアプリ Redirect URL'), ('output_prefix','翻訳の目印'),
              ('skip_users','除外ユーザー（カンマ区切り）'),
              ('skip_languages','除外言語（カンマ区切り）'),
              ('min_post_interval_sec','投稿間隔（秒）'), ('max_queue','送信待ち上限')]
    for row, (key, label) in enumerate(labels):
        ttk.Label(window, text=label).grid(row=row, column=0, sticky='w', padx=12, pady=5)
        value = getattr(cfg, key)
        value = ', '.join(value) if isinstance(value, list) else str(value)
        fields[key] = tk.StringVar(value=value)
        ttk.Entry(window, textvariable=fields[key], width=36).grid(row=row,column=1,padx=12)
    row = len(labels)
    for key, label in [('trans_to_home_only','外国語を母語へ訳すだけにする'),
                       ('show_author','投稿に発言者名を表示'), ('skip_commands','コマンドを除外'),
                       ('live_posting','翻訳をKickへ投稿する')]:
        fields[key] = tk.BooleanVar(value=getattr(cfg,key))
        ttk.Checkbutton(window,text=label,variable=fields[key]).grid(row=row,column=0,columnspan=2,sticky='w',padx=12,pady=4)
        row += 1
    ttk.Label(window,text='接続方式').grid(row=row,column=0,sticky='w',padx=12)
    fields['receive_method'] = tk.StringVar(value=cfg.receive_method)
    ttk.Combobox(window,textvariable=fields['receive_method'],values=('websocket','webhook'),state='readonly').grid(row=row,column=1)
    def save():
        try:
            values = {key: value.get() for key,value in fields.items()}
            for key in ('skip_users','skip_languages'):
                values[key] = [v.strip() for v in values[key].split(',') if v.strip()]
            values['min_post_interval_sec'] = float(values['min_post_interval_sec'])
            values['max_queue'] = int(values['max_queue'])
            updated = replace(cfg, **values)
            updated.save()
        except (ValueError, OSError) as error:
            messagebox.showerror('設定を保存できません',str(error),parent=window)
            return
        on_save(updated)
        window.destroy()
    ttk.Button(window,text='保存',command=save).grid(row=row+1,column=0,pady=15)
    ttk.Button(window,text='キャンセル',command=window.destroy).grid(row=row+1,column=1,pady=15)
    window.grab_set()