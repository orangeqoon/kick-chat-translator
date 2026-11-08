@echo off
chcp 65001 >nul
rem Kick に接続せず、翻訳パイプラインだけ試す (投稿しない)
cd /d %~dp0
.venv\Scripts\python -m kicktrans demo
pause
