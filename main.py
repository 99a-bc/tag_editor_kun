import sys
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QTranslator
from PySide6.QtWidgets import QApplication

from theme import enable_dark_color_scheme, install_windows_dark_title_bar_support

from logger import (
    configure_logging,
    get_logger,
    install_qt_message_handler,
    log_user_info,
    log_user_ok,
)


def _qt_translation_paths() -> list[Path]:
    """PySide6/Qt の翻訳ファイル配置候補を返す。"""
    paths: list[Path] = []

    try:
        qt_path = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
        paths.append(qt_path)
    except Exception:
        pass

    try:
        import PySide6

        paths.append(Path(PySide6.__file__).resolve().parent / "Qt" / "translations")
    except Exception:
        pass

    unique_paths: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_paths.append(resolved)

    return unique_paths


def _install_qt_japanese_translations(app: QApplication, log) -> None:
    """
    Qt標準UIの日本語翻訳を読み込む。

    QLineEditなどの標準右クリックメニュー
    Undo / Redo / Cut / Copy / Paste / Delete / Select All は、
    Qt翻訳ファイル qtbase_ja.qm が読み込まれていないと英語表示になる。
    """
    installed_translators: list[QTranslator] = []

    for translation_name in ("qtbase_ja", "qt_ja"):
        for translation_path in _qt_translation_paths():
            translator = QTranslator(app)
            if not translator.load(translation_name, str(translation_path)):
                continue

            app.installTranslator(translator)
            installed_translators.append(translator)
            log.info(
                "Qt Japanese translation loaded: %s from %s",
                translation_name,
                translation_path,
            )
            break

    if installed_translators:
        # QTranslator は参照を保持しておかないとGCされる可能性があるため、
        # QApplicationにぶら下げておく。
        app._qt_japanese_translators = installed_translators  # type: ignore[attr-defined]
    else:
        log.warning("Qt Japanese translation files were not found")


def _display_log_path(log_file: Path) -> str:
    """起動案内では、可能ならリポジトリ直下からの相対パスでログを示す。"""
    try:
        return str(log_file.relative_to(Path.cwd()))
    except ValueError:
        return str(log_file)


def main() -> int:
    log_file = configure_logging()
    log = get_logger(__name__)
    install_qt_message_handler(log)
    log_user_info("タグエディタ君 v1.2.0 を起動しています...")
    log.info("main.py loaded from: %s", Path(__file__).resolve())
    log.info("log file: %s", log_file)

    app = QApplication(sys.argv)

    # Windowsのシステムテーマがライトでも、Qt標準部品はプラットフォーム側の
    # ダーク配色で描画させる。QPalette/Fusion/標準部品QSSは使わない。
    if not enable_dark_color_scheme(app):
        log.warning("Qt ColorScheme.Dark could not be enabled")

    # 子ダイアログを含むQtトップレベルのDWMタイトルバーをダーク化する。
    install_windows_dark_title_bar_support(app)

    _install_qt_japanese_translations(app, log)

    from app_window import AppWindow

    log.info("AppWindow imported from: %s", Path(sys.modules["app_window"].__file__).resolve())

    window = AppWindow()
    window.show()
    log_user_ok("起動しました。ログ: %s", _display_log_path(log_file))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
