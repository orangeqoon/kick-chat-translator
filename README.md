# Kick Chat Translator for Windows

Kickチャットを自動翻訳し、翻訳結果をチャットへ返信するWindows用アプリです。
日本語→英語、その他の言語→日本語が初期設定。画面で言語・除外ユーザー・投稿間隔を変更できます。

- 原文・訳文・投稿状態を一覧表示
- 受信のみの確認、通信なしのMOCKデモ、実翻訳デモ
- 自分の翻訳投稿・重複メッセージを除外してループを防止
- 直接WebSocket受信。通常はWebhook・公開URL・トンネル不要
- 投稿はKick公式OAuth/API。秘密情報はWindows DPAPIで暗号化

**Windows 10/11、Python 3.10以上（python.org版、pipとTcl/Tk付き）、インターネット接続が必要です。**
exe版ではありません。翻訳とKickのウェブ受信方式は非公式エンドポイントを含み、仕様変更やレート制限で動かなくなる場合があります。

## インストール

1. このリポジトリの **Code → Download ZIP** から取得して、書き込みできるフォルダーへ展開します。Git利用者はcloneでも構いません。
2. `setup_windows.bat` を実行します。専用の `.venv` に依存パッケージをインストールします。
3. `start_gui.bat` を実行して「設定」を開き、**自分のKickチャンネル名**を保存します。URL全体ではなく `your-channel` の部分を入力してください。
4. まず「デモ (MOCK・通信なし)」、次に「受信テスト（投稿なし）」で確認します。受信のみならKickアプリの登録やログインは不要です。

初期設定では投稿OFFです。第三者のClient Secretや作者のアプリを共用する必要はありません。

## 翻訳を投稿するための初回認証

1. 自分のKickアカウントで [Kick Developer設定](https://kick.com/settings/developer) を開き、自分のアプリを作成します。
2. Redirect URLに `http://localhost:3000/oauth2callback` を登録します。
3. GUIの「設定」に自分のアプリの **Client ID** と上記 **Redirect URL** を保存して、GUIを閉じます。Client IDは公開識別子です。
4. `login_windows.bat` を実行します。コンソールに **Client Secret** を入力します（画面には表示されません）。ブラウザーで自分のアプリを承認してください。
5. GUIを再起動し、「設定」で「翻訳をKickへ投稿する」をONにして保存。「開始」で受信・翻訳・投稿が始まります。

OAuth要求スコープは `user:read channel:read chat:write events:subscribe` です。`events:subscribe` は代替Webhook方式に使用し、通常の直接受信では購読を作成しません。
投稿はログインした配信者のチャンネルへ `type=bot` で送ります。設定チャンネルとログインアカウントが違う場合は停止します。別名の独立botアカウントへのログインを前提とした仕組みではありません。

認証に失敗したら、Client ID・Secretが同じ自分のアプリのものか、Redirect URLが完全一致しているか、localhostの3000番ポートが使われていないか確認してください。
Secretを設定JSONやGitHub Issueに貼らないでください。

公式資料: [OAuth](https://docs.kick.com/getting-started/generating-tokens-oauth2-flow)、[Chat API](https://docs.kick.com/apis/chat)。

## 画面の使い方

- **設定**: チャンネル、母語、母語からの翻訳先、除外ユーザー/言語、投稿間隔、投稿ON/OFFを保存。言語は `ja` / `en` / `ko` などのコードです。
- **開始**: 保存された設定で接続。投稿ONなら実際に返信します。
- **受信テスト（投稿なし）**: 保存設定にかかわらず投稿OFFで接続・翻訳します。
- **停止**: 受信と送信待ちを停止します。未送信の項目は破棄されます。
- **デモ**: Kickへ接続・投稿しません。Google翻訳デモはGoogleへの通信を行います。
- 一覧の行を選択すると下部に原文・訳文の全文を表示します。最大1,000行を保持します。

実行中に設定を保存すると停止後に新設定で再接続します。「母語のみ」は、母語の発言を訳さず外国語だけを母語へ訳す設定です。

## 保存先と秘密情報

単独配置では展開フォルダー内の `state/` に設定・暗号化認証データを保存します。`.venv/` と `state/` はGit管理対象外です。
既存のscriptsリポジトリ配下に統合した場合はホストの `state/` を使います。上級者は `SCRIPTS_ROOT` / `SCRIPTS_STATE` で変更できます。

DPAPIの認証ファイルは同じWindowsユーザーでのみ復号できます。PC間でのコピーには使わず、移行先で再ログインしてください。
アンインストールはアプリ終了後に展開フォルダーを削除します。Kick側の連携解除はDeveloper/接続済みアプリ設定で行ってください。

## 開発・検証

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m kicktrans demo --mock
```

Windows実機で受信→Google英訳→公式APIで返信→自分の投稿の除外まで確認済みです。長時間運用や実通信障害後の耐久試験は未実施です。Linux/macOSでの投稿認証はサポートしません。
バグ報告にはOS・Pythonバージョン・再現手順と、秘密情報を除いたエラー種別を添えてIssueを作成してください。

## License / Acknowledgement

MIT License。操作設計の参考: [sayonari/twitchTransFreeNeo](https://github.com/sayonari/twitchTransFreeNeo)。このアプリは独立実装で、参考リポジトリのコード・画像・配布物を同梱していません。KickまたはGoogleの公式製品ではありません。

## English quick start

Windows 10/11 + Python 3.10+ with Tcl/Tk are required. Download and extract the ZIP, run `setup_windows.bat`, then `start_gui.bat`. Open 設定 (Settings), enter your channel slug, and save. Posting is disabled by default; 受信テスト（投稿なし） receives and translates without posting.

For posting, create your own Kick developer application with redirect URL `http://localhost:3000/oauth2callback`. Save its Client ID and redirect URL in Settings, close the GUI, then run `login_windows.bat`. Enter your own Client Secret at the hidden prompt and approve OAuth in the browser. Reopen the GUI, enable posting in Settings and click 開始 (Start). The authenticated account must own the configured channel. No shared credentials, webhook or tunnel are needed for the default receiver. The interface is currently Japanese.