@echo off
chcp 932 >nul
setlocal EnableExtensions
pushd "%~dp0"

set "EXIT_CODE=1"
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] Python 仮想環境が見つかりません。
  echo 先に install.bat を実行して .venv を作成してください。
  goto :end
)

echo.
echo ============================================================
echo Hugging Face へログインします
echo ============================================================
echo PixAI Tagger を使うには、PixAIモデルへのアクセス許可がある
echo Hugging Faceアカウントの Read トークンが必要です。
echo.

"%PY%" "%CD%\hf_login_helper.py"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] Hugging Face へのログインに失敗しました。
  echo PixAI Tagger へのアクセス許可があるアカウントの Read トークンを入力してください。
  goto :end
)

echo.
echo [OK] ログインが完了しました。
echo 次に download_pixai_tagger.bat を実行して、モデルファイルを取得してください。

:end
popd
endlocal
pause
exit /b %EXIT_CODE%
