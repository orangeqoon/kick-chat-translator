# Kick Chat Translator 公開版 引継書

更新日: 2026-11-09 (JST)

## 現在の公開先

- GitHub: https://github.com/orangeqoon/kick-chat-translator
- 公開リリース: `v0.5.0`
- ZIP: https://github.com/orangeqoon/kick-chat-translator/releases/download/v0.5.0/kick-chat-translator-v0.5.0.zip
- 元の作業ツリー: `C:\scripts\streaming\kick-translator\`
- 公開用の独立コピー: `dist\public-v0.5.0\`

元の非公開 `orangeqoon/scripts` リポジトリと、そのホスト側の認証情報・実行状態は公開対象に含めていない。公開用ZIPには `.venv`、`state`、DPAPIファイル、バックアップ、旧HANDOFF、Cookie、トークン、Client Secretを含めていない。

## 利用者向けの開始手順

1. GitHub ReleaseからZIPをダウンロードして、書き込み可能なフォルダーへ展開する。
2. `setup_windows.bat` を実行する。Python 3.10以上、pip、Tcl/Tkが必要。
3. `start_gui.bat` を実行し、「設定」で自分のKickチャンネル名を保存する。
4. まず「デモ (MOCK・通信なし)」、次に「受信テスト（投稿なし）」を実行する。
5. 投稿する場合は、自分のKick Developerアプリに `http://localhost:3000/oauth2callback` を登録し、Client IDとRedirect URLを設定画面へ保存する。
6. GUIを閉じて `login_windows.bat` を実行し、自分のClient Secretを入力してブラウザーで承認する。Secretはチャット・設定JSON・Issueへ貼らない。
7. GUIを再起動し、「設定」で「翻訳をKickへ投稿する」をONにして「開始」を押す。

既定の受信方式は直接WebSocketで、通常はWebhook、公開URL、cloudflaredを必要としない。投稿はログインしたアカウントのチャンネルへ行う。設定チャンネルとログインアカウントが一致しない場合は停止する。

## 実装済みの機能

- Kickチャットの直接受信、再接続、停止時のWebSocketクリーンアップ
- 日本語→英語、外国語→日本語の双方向翻訳
- 母語のみ、除外ユーザー、除外言語、コマンド除外、接頭辞設定
- 原文・訳文・言語・投稿状態の一覧表示と全文確認
- 投稿キュー、間隔制御、429/5xxの再試行
- 翻訳投稿のmessage_id、接頭辞、重複IDによるループ防止
- OAuthトークンとClient SecretのWindows DPAPI保管
- 投稿OFFの受信テストと、ネットワークを使わないMOCKデモ

## 検証済み事項

- 公開用コピー: pytest 117件成功
- Python構文コンパイル成功
- MOCKデモ成功
- GitHub Actions: Windows Python 3.10 / 3.12 成功
- パス検査: 検出0件
- 公開ZIP: 認証値スキャン成功、匿名ダウンロードとSHA256一致を確認
- 実Kick: チャット受信→日本語から英語への翻訳→公式APIによる返信→自分の返信のループ除外まで確認済み

## 未確認・注意点

- 別ユーザーのKick Developerアプリでの初回OAuthは未実施。公開READMEの手順に沿って利用者自身のアプリを使う。
- Kickウェブサイト側の受信方式と無料Google翻訳エンドポイントは仕様変更・レート制限の影響を受ける。
- 長時間運用、ネットワーク障害後の耐久再接続、Linux/macOSは未確認。
- WindowsのPythonはpython.org版を使用する。Microsoft Store版などでTkが欠ける場合は動かないことがある。

## 次の作業候補

- 別アカウントでOAuthから投稿までの受入試験
- 長時間接続と再接続の実機試験
- Google Cloud Translationなど安定した翻訳APIの選択肢追加
- GUIの英語化と、配布用exe化の検討
- Kick公式の安定したイベント受信APIが提供された場合の移行検討

## 変更時のルール

- 認証情報・Cookie・トークン・`state/`はコミットしない。
- Kick固有の調査・変更は `streaming/marust/docs/sites/kick.md` に症状、原因候補、事実、対策、試験結果、未確認事項を記録する。
- `README.md`、`LICENSE`、`requirements*.txt`、`.github/workflows/tests.yml`を公開版の導入・検証の正本とする。
- 公開版を更新する場合は、公開用コピーでテスト、秘密情報スキャン、ZIPハッシュ確認を行ってからGitHub Releaseを作成する。