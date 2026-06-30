from __future__ import annotations

import logging
import os
import platform
import sys
from datetime import datetime
from pathlib import Path

from dev_config import enabled_dev_log_categories


_CONFIGURED = False
_LOG_FILE: Path | None = None

# 公開版では起動ごとにログを作るが、無制限には増やさない。
LOG_FILE_RETENTION_COUNT = 20
_LOG_FILE_GLOB = "tag_editor_*.log"
_QT_MESSAGE_HANDLER = None
_OPERATION_LOGGER_NAME = "tag_editor.operation"
_OPERATION_CONSOLE_LOGGER_NAME = "tag_editor.operation_console"


def app_root() -> Path:
    return Path(__file__).resolve().parent


def _cleanup_old_log_files(log_dir: Path) -> tuple[int, list[Path]]:
    """今回のログを含め、最新LOG_FILE_RETENTION_COUNT件だけを残す。"""
    try:
        log_files = sorted(
            log_dir.glob(_LOG_FILE_GLOB),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
    except OSError:
        # ログの掃除に失敗しても、アプリ起動自体は妨げない。
        return 0, []

    removed_count = 0
    failed_paths: list[Path] = []
    for path in log_files[LOG_FILE_RETENTION_COUNT:]:
        try:
            path.unlink()
            removed_count += 1
        except OSError:
            failed_paths.append(path)

    return removed_count, failed_paths


def _configure_operation_console_logger() -> None:
    """通常操作だけを簡潔に表示する、専用コンソール出力を設定する。"""
    console_logger = logging.getLogger(_OPERATION_CONSOLE_LOGGER_NAME)
    console_logger.handlers.clear()
    console_logger.setLevel(logging.INFO)
    console_logger.propagate = False

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    console_logger.addHandler(console_handler)


def _log_operation(kind: str, message: str, *args: object) -> None:
    """通常ログへも残しつつ、利用者向けの一行だけをコンソールへ出す。"""
    operation_message = f"[{kind}] {message}"
    logging.getLogger(_OPERATION_LOGGER_NAME).info(operation_message, *args)
    logging.getLogger(_OPERATION_CONSOLE_LOGGER_NAME).info(operation_message, *args)


def log_user_info(message: str, *args: object) -> None:
    """通常操作の進捗をコンソールとログへ表示する。"""
    _log_operation("INFO", message, *args)


def log_user_ok(message: str, *args: object) -> None:
    """通常操作の完了をコンソールとログへ表示する。"""
    _log_operation("OK", message, *args)


def log_user_warning(message: str, *args: object) -> None:
    """利用者が確認すべき軽度の問題をコンソールとログへ表示する。"""
    _log_operation("WARNING", message, *args)


def log_user_error(message: str, *args: object) -> None:
    """利用者が確認すべき操作失敗をコンソールとログへ表示する。"""
    _log_operation("ERROR", message, *args)


def install_qt_message_handler(log: logging.Logger) -> None:
    """Qtがstderrへ直接出す警告も、通常ログへ集約する。"""
    global _QT_MESSAGE_HANDLER

    if _QT_MESSAGE_HANDLER is not None:
        return

    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception as exc:  # pragma: no cover - PySide6なしの補助実行向け
        log.warning("Qt message handler was not installed: %s", exc)
        return

    def _to_text(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        return str(value) if value else ""

    def handle_qt_message(message_type, context, message) -> None:
        try:
            location_parts: list[str] = []
            file_name = _to_text(getattr(context, "file", ""))
            function_name = _to_text(getattr(context, "function", ""))
            line_number = getattr(context, "line", 0)

            if file_name:
                location_parts.append(file_name)
            if function_name:
                location_parts.append(function_name)
            if line_number:
                location_parts.append(f"line {line_number}")

            location = f" ({' | '.join(location_parts)})" if location_parts else ""
            message_text = f"[Qt] {message}{location}"

            if message_type == QtMsgType.QtDebugMsg:
                log.debug(message_text)
            elif message_type == QtMsgType.QtInfoMsg:
                log.info(message_text)
            elif message_type == QtMsgType.QtWarningMsg:
                log.warning(message_text)
            elif message_type == QtMsgType.QtCriticalMsg:
                log.error(message_text)
            else:
                log.critical(message_text)
        except Exception:
            # Qtのメッセージ処理中に例外を出すと、元の警告より危険な状態になり得る。
            return

    _QT_MESSAGE_HANDLER = handle_qt_message
    qInstallMessageHandler(_QT_MESSAGE_HANDLER)
    log.info("Qt message handler configured")


def configure_logging() -> Path:
    """
    アプリ全体のログ設定。
    logs/tag_editor_YYYYMMDD_HHMMSS.log へINFO以上を出力する。
    ログは最新20件を残す。コンソールには警告・エラーに加え、
    利用者向けの通常操作メッセージだけを簡潔に表示する。
    """
    global _CONFIGURED, _LOG_FILE

    if _CONFIGURED and _LOG_FILE is not None:
        return _LOG_FILE

    log_dir = app_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    _LOG_FILE = log_dir / f"tag_editor_{datetime.now():%Y%m%d_%H%M%S}.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # 二重設定を避ける
    root_logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    _configure_operation_console_logger()

    removed_log_count, failed_log_cleanup_paths = _cleanup_old_log_files(log_dir)

    # 依存ライブラリのDEBUGは、通常の原因調査ログを埋め尽くしやすい。
    # アプリ側のTAGGER_PERF等を読みやすく保つため、外部ライブラリは警告以上に抑える。
    for logger_name in ("httpcore", "httpx", "huggingface_hub", "PIL", "timm"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    _CONFIGURED = True

    log = logging.getLogger(__name__)
    log.info("logging configured")
    log.info("log_file=%s", _LOG_FILE)
    log.info("log_retention_count=%d", LOG_FILE_RETENTION_COUNT)
    if removed_log_count:
        log.info("old_log_files_removed=%d", removed_log_count)
    for failed_path in failed_log_cleanup_paths:
        log.warning("old_log_file_cleanup_failed=%s", failed_path)
    log.info("app_root=%s", app_root())
    log.info("cwd=%s", Path.cwd())
    log.info("python=%s", sys.version.replace("\n", " "))
    log.info("platform=%s", platform.platform())
    log.info("executable=%s", sys.executable)
    log.info("argv=%s", sys.argv)
    log.info("env GAZOU_TAG_DEBUG=%s", os.environ.get("GAZOU_TAG_DEBUG", ""))
    log.info("dev_log_categories=%s", enabled_dev_log_categories() or ["none"])

    return _LOG_FILE


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_file_path() -> Path | None:
    return _LOG_FILE
