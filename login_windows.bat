@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
 echo 先に setup_windows.bat を実行してください。
 pause
 exit /b 1
)
echo 自分のKickアプリの Client Secret を入力します。画面には表示されません。
.venv\Scripts\python.exe -m kicktrans set-secret
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m kicktrans login
if errorlevel 1 goto failed
echo ログイン完了。start_gui.bat から開始できます。
pause
exit /b 0
:failed
echo 認証に失敗しました。READMEの認証設定を確認してください。
pause
exit /b 1
