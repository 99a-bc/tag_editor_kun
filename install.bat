@echo off
chcp 932 >nul
setlocal EnableExtensions
pushd "%~dp0"

echo ============================================================
echo タグエディタ君 セットアップ
echo アプリ本体とTagger推論環境をまとめてセットアップします
echo ============================================================
echo.

if not exist "%~dp0requirements.txt" goto missing_requirements

if not exist "%~dp0.venv\Scripts\activate.bat" goto create_venv
echo [INFO] .venv は既に存在します: .venv\
goto activate_venv

:create_venv
echo [INFO] .venv を作成します...
py -3.12 -m venv .venv
if not errorlevel 1 goto activate_venv

echo [INFO] Python 3.12 が見つかりません。Python 3.11 を試します...
py -3.11 -m venv .venv
if not errorlevel 1 goto activate_venv

echo [INFO] python コマンドで作成を試みます...
python -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)"
if errorlevel 1 goto python_version_error
python -m venv .venv
if errorlevel 1 goto err
goto activate_venv

:activate_venv
echo [INFO] .venv を使用します: .venv\
call "%~dp0.venv\Scripts\activate.bat"
if errorlevel 1 goto err

echo [INFO] Python バージョンを確認します...
python -c "import sys; print('[INFO] Python', sys.version.split()[0]); raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)"
if errorlevel 1 goto python_version_error

echo.
echo [STEP] pip / setuptools / wheel を更新します...
python -m pip install -U pip "setuptools<82" wheel
if errorlevel 1 goto err

echo.
set "TORCH_VERSION=2.11.0"
set "TORCHVISION_VERSION=0.26.0"

echo [SELECT] PyTorch の実行環境を選んでください
echo   1  NVIDIA GPU cu128
echo   2  NVIDIA GPU cu130
echo   3  NVIDIA GPU cu126
echo   4  CPU
echo.
set "MODE="
set /p MODE=番号を入力 1-4 Enter=1: 
if "%MODE%"=="" set "MODE=1"

if "%MODE%"=="1" goto mode_cu128
if "%MODE%"=="2" goto mode_cu130
if "%MODE%"=="3" goto mode_cu126
if "%MODE%"=="4" goto mode_cpu

echo [ERROR] 不正な入力です。1から4を入力してください。
goto err

:mode_cu128
set "TORCH_INDEX=https://download.pytorch.org/whl/cu128"
set "MODE_NAME=NVIDIA GPU cu128"
goto mode_selected

:mode_cu130
set "TORCH_INDEX=https://download.pytorch.org/whl/cu130"
set "MODE_NAME=NVIDIA GPU cu130"
goto mode_selected

:mode_cu126
set "TORCH_INDEX=https://download.pytorch.org/whl/cu126"
set "MODE_NAME=NVIDIA GPU cu126"
goto mode_selected

:mode_cpu
set "TORCH_INDEX=https://download.pytorch.org/whl/cpu"
set "MODE_NAME=CPU"
goto mode_selected

:mode_selected
echo.
echo ============================================================
echo モード: %MODE_NAME%
echo PyTorch: torch==%TORCH_VERSION% / torchvision==%TORCHVISION_VERSION%
echo PyTorch index-url: %TORCH_INDEX%
echo ============================================================
echo.

if "%MODE%"=="4" goto install_torch

where nvidia-smi >nul 2>&1
if errorlevel 1 goto nvidia_missing
echo [INFO] nvidia-smi を確認します
nvidia-smi
echo.
goto install_torch

:nvidia_missing
echo [WARN] nvidia-smi が見つかりません。
echo [WARN] NVIDIAドライバ未導入、または PATH に無い可能性があります。
echo [WARN] インストールは続行します。
echo.

:install_torch
echo [STEP] 既存の torch / torchvision / torchaudio を削除します。
echo [INFO] 入っていない場合の警告は無視してOKです。
python -m pip uninstall -y torch torchvision torchaudio

echo.
echo [STEP] PyTorch をインストールします。かなり時間がかかる場合があります。
python -m pip install --upgrade --force-reinstall torch==%TORCH_VERSION% torchvision==%TORCHVISION_VERSION% --index-url "%TORCH_INDEX%"
if errorlevel 1 goto torch_err

echo.
echo [STEP] アプリ本体とTaggerの依存関係をインストールします...
python -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto err

echo.
echo [STEP] 動作確認...
python -c "import torch; print('torch', torch.__version__)"
if errorlevel 1 goto check_err
python -c "import torch; print('cuda_runtime', torch.version.cuda); print('cuda_available', torch.cuda.is_available())"
if errorlevel 1 goto check_err
python -c "import timm; print('timm ok')"
if errorlevel 1 goto check_err
python -c "import safetensors; print('safetensors ok')"
if errorlevel 1 goto check_err
python -c "import huggingface_hub; print('huggingface_hub ok')"
if errorlevel 1 goto check_err
python -c "import PIL; print('Pillow ok')"
if errorlevel 1 goto check_err
python -c "import numpy; print('numpy ok')"
if errorlevel 1 goto check_err
python -c "from PySide6.QtWidgets import QApplication; print('PySide6 ok')"
if errorlevel 1 goto check_err

echo.
echo [OK] セットアップが完了しました。
echo.
echo 起動するには run.bat を実行してください。
echo.
goto end

:missing_requirements
echo.
echo [ERROR] requirements.txt が見つかりません。
echo "%~dp0requirements.txt"
goto err

:python_version_error
echo.
echo [ERROR] Python 3.11以上が必要です。
echo [INFO] 現在の .venv がPython 3.10以下の場合は、.venv フォルダを削除してから再実行してください。
echo [INFO] Python 3.11以上をインストール後、install.bat を再実行してください。
goto err

:torch_err
echo.
echo [ERROR] PyTorch のインストールに失敗しました。
echo ネット接続を確認してください。
echo GPU版で失敗した場合は、別のGPUタグやCPUを選び直してください。
goto err

:check_err
echo.
echo [ERROR] 依存ライブラリの確認に失敗しました。
echo install.bat を再実行してください。
goto err

:err
echo.
echo [ERROR] セットアップに失敗しました。
echo Python 3.11以上、ネット接続、pipの権限を確認してください。
echo.
popd
endlocal
pause
exit /b 1

:end
popd
endlocal
pause
exit /b 0
