@echo off
chcp 932 >nul
setlocal EnableExtensions
pushd "%~dp0"

set "EXIT_CODE=1"
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] Python virtual environment was not found.
  echo Run install.bat first to create .venv.
  goto :end
)

echo.
echo ============================================================
echo Downloading PixAI Tagger v0.9 model files
echo ============================================================
echo Complete the PixAI Tagger model access approval on Hugging Face first.
echo Enter an access-approved Read token for this download only.
echo The token is not displayed or saved.
echo.

"%PY%" "%CD%\download_pixai_tagger_helper.py"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" goto :end_error

echo.
echo [OK] Download completed.
echo Restart the app, then select PixAI Tagger in the Tagger tab.
goto :end

:end_error
echo.
echo [ERROR] Download failed.
echo Check model access approval, the Read token, and your network connection.

:end
popd
endlocal
pause
exit /b %EXIT_CODE%
