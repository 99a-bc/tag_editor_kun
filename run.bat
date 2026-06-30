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

"%PY%" "%CD%\main.py"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] タグエディタ君がエラー終了しました。
)

:end
popd
endlocal
pause
exit /b %EXIT_CODE%
