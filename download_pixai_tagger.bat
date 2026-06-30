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
echo PixAI Tagger v0.9 のモデルファイルをダウンロードします
echo ============================================================
echo ダウンロード先: Hugging Face の共有キャッシュ
echo.

"%PY%" -c "from huggingface_hub import snapshot_download; repo='pixai-labs/pixai-tagger-v0.9'; path=snapshot_download(repo_id=repo, allow_patterns=['model_v0.9.pth','tags_v0.9_13k.json','char_ip_map.json']); print(path)"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" goto :end_error

echo.
echo [OK] ダウンロードが完了しました。
echo 保存先は上に表示されたキャッシュのパスです。
goto :end

:end_error
echo.
echo [ERROR] ダウンロードに失敗しました。
echo hf_login.bat を、PixAI Tagger へのアクセス許可があるアカウントで完了させてください。

:end
popd
endlocal
pause
exit /b %EXIT_CODE%
