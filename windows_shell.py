"""Windowsエクスプローラーで指定ファイルを選択状態にして開く。

SHOpenFolderAndSelectItems を優先し、API失敗時のみ explorer.exe /select, に戻す。
通常のフォルダ表示とは違い、右クリックで選んだファイル自身を対象にする。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import subprocess
import sys


def _select_file_with_shell_api(path: Path) -> bool:
    """Windows ShellのPIDLでファイルを選択。API成功時True。"""
    if sys.platform != "win32":
        return False

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long  # Windows上のHRESULTは32bit
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    shell32.SHParseDisplayName.argtypes = [
        wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    shell32.SHParseDisplayName.restype = ctypes.c_long
    shell32.SHOpenFolderAndSelectItems.argtypes = [
        ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD,
    ]
    shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long

    # Qt等が既にCOMを初期化した場合、RPC_E_CHANGED_MODEも受け入れる。
    initialized = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    should_uninitialize = initialized in (0, 1)  # S_OK, S_FALSE
    if initialized < 0 and initialized != -2147417850:  # RPC_E_CHANGED_MODE
        return False

    pidl = ctypes.c_void_p()
    try:
        attributes = wintypes.DWORD()
        if shell32.SHParseDisplayName(str(path), None, ctypes.byref(pidl),
                                      0, ctypes.byref(attributes)) < 0:
            return False
        if not pidl.value:
            return False
        # cidl=0の場合はpidlの親フォルダを開き、そのアイテムを選択する。
        return shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) >= 0
    finally:
        if pidl.value:
            ole32.CoTaskMemFree(pidl)
        if should_uninitialize:
            ole32.CoUninitialize()


def open_folder_and_select_file(image_path: Path) -> str:
    """選択表示を起動。利用した方式(shell_api/explorer)を返す。

    起動要求の成否であり、Explorerの最前面化・選択描画完了までを保証しない。
    """
    path = Path(image_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"画像ファイルが見つかりません: {path}")
    if sys.platform != "win32":
        raise OSError("画像フォルダを開く機能はWindows専用です。")

    try:
        if _select_file_with_shell_api(path):
            return "shell_api"
    except (OSError, AttributeError, ValueError):
        # COMやWindows APIの失敗時のみ標準のExplorer選択構文へ。
        pass

    subprocess.Popen(["explorer.exe", "/select,", str(path)])
    return "explorer"
