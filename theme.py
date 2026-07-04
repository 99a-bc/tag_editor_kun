from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import QWidget


# ==============================
# Gazou Dark Theme
# ==============================

APP_BG = "#121212"
PANEL_BG = "#1b1b1b"
PANEL_BG_ALT = "#202020"
CARD_BG = "#242424"

TEXT = "#e8e8e8"
TEXT_MUTED = "#a8a8a8"
TEXT_DARK = "#171717"

BORDER = "#3a3a3a"
BORDER_SOFT = "#4a4a4a"

BLUE = "#3f8cff"
BLUE_SOFT = "#7fb2ff"

ORANGE = "#ff9f2f"
ORANGE_SOFT = "#ffc46b"

GREEN = "#45d483"
GREEN_SOFT = "#8ff0b8"

PURPLE = "#8a5cff"
PURPLE_SOFT = "#b79cff"

YELLOW = "#ffe58a"
YELLOW_DARK = "#3a3216"

RED = "#ef4444"
RED_SOFT = "#ff8a8a"

# タグ選択状態はカテゴリ色と衝突しないよう、背景色を変えずに破線枠で示す。
# 通常タグは元の枠色 #3d4a5c が暗すぎるため、選択時だけ明るい青灰へ寄せる。
SELECTED_TAG_DASH = "#8fb6e8"
SELECTED_TAG_DASH_HOVER = "#b7d0ff"


APP_QSS = f"""
QMainWindow {{
    background: {APP_BG};
    color: {TEXT};
}}

QWidget {{
    color: {TEXT};
    font-family: "Meiryo", "Yu Gothic UI", "Segoe UI";
    font-size: 9pt;
}}

QMenuBar {{
    color: {TEXT};
    background: #181818;
    border-bottom: 1px solid {BORDER};
    padding: 2px 6px;
}}

QMenuBar::item {{
    color: {TEXT};
    background: transparent;
    padding: 6px 12px;
    border-radius: 6px;
}}

QMenuBar::item:selected {{
    background: #2b2b2b;
    color: white;
}}

QMenuBar::item:pressed {{
    background: #202020;
    color: {YELLOW};
}}

QMenu {{
    color: {TEXT};
    background: {PANEL_BG_ALT};
    border: 1px solid {BORDER_SOFT};
    padding: 6px;
}}

QMenu::item {{
    padding: 7px 28px 7px 24px;
    border-radius: 6px;
}}

QMenu::item:selected {{
    background: #2d3a4a;
    color: white;
}}

QMenu::item:checked {{
    color: {YELLOW};
    font-weight: bold;
}}

QMenu::separator {{
    height: 1px;
    background: {BORDER_SOFT};
    margin: 6px 8px;
}}

QLabel {{
    color: {TEXT};
}}

QPushButton {{
    color: {TEXT};
    background: {PANEL_BG_ALT};
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
    padding: 6px 12px;
}}

QPushButton:hover {{
    background: #2b2b2b;
    border-color: {BLUE_SOFT};
}}

QPushButton:pressed {{
    background: #161616;
    border-color: {YELLOW};
}}

QPushButton:checked {{
    background: {YELLOW_DARK};
    border-color: {YELLOW};
    color: {TEXT};
}}

QPushButton:disabled {{
    background: #262626;
    color: #747474;
    border-color: #3a3a3a;
}}

QFrame#PreviewPanel {{
    background: {PANEL_BG};
    border: 2px solid {BLUE};
    border-radius: 12px;
}}

QFrame#ThumbnailPanel {{
    background: {PANEL_BG};
    border: 2px solid {GREEN};
    border-radius: 12px;
}}

QFrame#TagPanel {{
    background: {PANEL_BG};
    border: 2px solid {ORANGE};
    border-radius: 12px;
}}

QLabel#PanelHeader {{
    color: {TEXT_DARK};
    background: {GREEN_SOFT};
    border: 1px solid {GREEN};
    border-radius: 8px;
    padding: 6px 10px;
    font-weight: bold;
}}

QPushButton#NavButton {{
    color: {TEXT};
    font-size: 27pt;
    border: none;
    background: transparent;
}}

QPushButton#NavButton:hover {{
    color: {BLUE_SOFT};
    background: transparent;
}}

QLineEdit {{
    color: {TEXT};
    background: #101010;
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
    padding: 8px 10px;
    selection-background-color: {BLUE};
}}

QLineEdit:focus {{
    border-color: {BLUE_SOFT};
}}

QListWidget {{
    color: {TEXT};
    background: #101010;
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
}}

QScrollArea {{
    background: #101010;
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
}}

QScrollBar:vertical {{
    background: #101010;
    width: 12px;
    margin: 2px;
    border-radius: 6px;
}}

QScrollBar::handle:vertical {{
    background: #6f7f95;
    min-height: 28px;
    border-radius: 6px;
}}

QScrollBar::handle:vertical:hover {{
    background: #8aa4c5;
}}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QScrollBar:horizontal {{
    background: #101010;
    height: 12px;
    margin: 2px;
    border-radius: 6px;
}}

QScrollBar::handle:horizontal {{
    background: #6f7f95;
    min-width: 28px;
    border-radius: 6px;
}}

QScrollBar::handle:horizontal:hover {{
    background: #8aa4c5;
}}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

QMessageBox {{
    background: {PANEL_BG};
}}

QMessageBox QLabel {{
    color: {TEXT};
}}

QInputDialog {{
    background: {PANEL_BG};
}}
"""


PREVIEW_IMAGE_LABEL_QSS = f"""
QLabel {{
    background: #101010;
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
    color: {TEXT_MUTED};
}}
"""


PREVIEW_STATUS_LABEL_QSS = f"""
QLabel {{
    background: #101010;
    border: 1px solid {BORDER_SOFT};
    border-radius: 6px;
    padding: 4px;
    color: {TEXT_MUTED};
}}
"""


TAG_INPUT_BOX_QSS = f"""
QFrame {{
    border: 1px solid {BORDER_SOFT};
    border-radius: 10px;
    background: {CARD_BG};
}}
"""


TAG_CHIP_QSS = f"""
QPushButton {{
    padding: 6px 13px;
    border: 1px solid #3d4a5c;
    border-radius: 9px;
    background: #17202c;
    color: {TEXT};
    text-align: center;
}}

QPushButton:hover {{
    background: #202d3d;
    border-color: {BLUE_SOFT};
}}

QPushButton:checked {{
    padding: 5px 12px;
    background: #17202c;
    border: 2px dashed {SELECTED_TAG_DASH};
    border-radius: 5px;
    color: {TEXT};
}}

QPushButton:checked:hover {{
    padding: 5px 12px;
    background: #202d3d;
    border: 2px dashed {SELECTED_TAG_DASH_HOVER};
    border-radius: 5px;
    color: #ffffff;
}}
"""


TAG_CHIP_SEARCH_HIGHLIGHT_QSS = f"""
QPushButton {{
    padding: 6px 13px;
    border: 2px solid {YELLOW};
    border-radius: 9px;
    background: #2d2a18;
    color: {YELLOW};
    text-align: center;
    font-weight: bold;
}}

QPushButton:hover {{
    background: #3a351c;
    border-color: {YELLOW};
}}

QPushButton:checked {{
    background: #2d2a18;
    border: 2px dashed {YELLOW};
    border-radius: 5px;
    color: {YELLOW};
}}

QPushButton:checked:hover {{
    background: #3a351c;
    border: 2px dashed {YELLOW};
    border-radius: 5px;
    color: #fff1b5;
}}
"""


TAG_CHIP_SEARCH_CURRENT_QSS = f"""
QPushButton {{
    padding: 6px 13px;
    border: 2px solid {BLUE_SOFT};
    border-radius: 9px;
    background: #1d3044;
    color: #ffffff;
    text-align: center;
    font-weight: bold;
}}

QPushButton:hover {{
    background: #263b54;
    border-color: {YELLOW};
}}

QPushButton:checked {{
    background: #1d3044;
    border: 2px dashed {SELECTED_TAG_DASH_HOVER};
    border-radius: 5px;
    color: #ffffff;
}}

QPushButton:checked:hover {{
    background: #263b54;
    border: 2px dashed {SELECTED_TAG_DASH_HOVER};
    border-radius: 5px;
    color: #ffffff;
}}
"""


TAG_CHIP_DRAGGING_QSS = f"""
QPushButton {{
    padding: 6px 13px;
    border: 2px dashed {BLUE_SOFT};
    border-radius: 9px;
    background: #111827;
    color: {TEXT_MUTED};
    text-align: center;
}}
"""


TAG_REORDER_MARKER_QSS = f"""
QFrame#TagReorderMarker {{
    background: {YELLOW};
    border: none;
    border-radius: 2px;
}}
"""


ALL_TAG_CHIP_QSS = f"""
QFrame#AllTagChip {{
    border: 1px solid #3d4a5c;
    border-radius: 9px;
    background: #17202c;
}}

QFrame#AllTagChip:hover {{
    border-color: {BLUE_SOFT};
    background: #202d3d;
}}
"""


ALL_TAG_BUTTON_QSS = f"""
QPushButton {{
    padding: 6px 9px;
    border: 1px solid transparent;
    border-radius: 6px;
    background: transparent;
    color: {TEXT};
}}

QPushButton:hover {{
    background: transparent;
    border-color: transparent;
    color: white;
}}

QPushButton:checked {{
    padding: 5px 8px;
    background: transparent;
    color: {TEXT};
    border: 2px dashed {SELECTED_TAG_DASH};
    border-radius: 4px;
}}

QPushButton:checked:hover {{
    padding: 5px 8px;
    background: rgba(127, 178, 255, 0.10);
    color: #ffffff;
    border: 2px dashed {SELECTED_TAG_DASH_HOVER};
    border-radius: 4px;
}}
"""


ALL_TAG_CHIP_SEARCH_HIGHLIGHT_QSS = f"""
QFrame#AllTagChip {{
    border: 2px solid {YELLOW};
    border-radius: 9px;
    background: #2d2a18;
}}

QFrame#AllTagChip:hover {{
    border-color: {YELLOW};
    background: #3a351c;
}}
"""


ALL_TAG_BUTTON_SEARCH_HIGHLIGHT_QSS = f"""
QPushButton {{
    padding: 6px 9px;
    border: 1px solid transparent;
    border-radius: 6px;
    background: transparent;
    color: {YELLOW};
    font-weight: bold;
}}

QPushButton:hover {{
    background: transparent;
    border-color: transparent;
    color: white;
}}

QPushButton:checked {{
    padding: 5px 8px;
    background: rgba(255, 229, 138, 0.10);
    color: {YELLOW};
    border: 2px dashed {YELLOW};
    border-radius: 4px;
}}

QPushButton:checked:hover {{
    padding: 5px 8px;
    background: rgba(255, 229, 138, 0.16);
    color: #fff1b5;
    border: 2px dashed {YELLOW};
    border-radius: 4px;
}}
"""


ALL_TAG_CHIP_SEARCH_CURRENT_QSS = f"""
QFrame#AllTagChip {{
    border: 2px solid {BLUE_SOFT};
    border-radius: 9px;
    background: #1d3044;
}}

QFrame#AllTagChip:hover {{
    border-color: {YELLOW};
    background: #263b54;
}}
"""


ALL_TAG_BUTTON_SEARCH_CURRENT_QSS = f"""
QPushButton {{
    padding: 6px 9px;
    border: 1px solid transparent;
    border-radius: 6px;
    background: transparent;
    color: #ffffff;
    font-weight: bold;
}}

QPushButton:hover {{
    background: transparent;
    border-color: transparent;
    color: white;
}}

QPushButton:checked {{
    padding: 5px 8px;
    background: rgba(127, 178, 255, 0.10);
    color: #ffffff;
    border: 2px dashed {SELECTED_TAG_DASH_HOVER};
    border-radius: 4px;
}}

QPushButton:checked:hover {{
    padding: 5px 8px;
    background: rgba(127, 178, 255, 0.16);
    color: #ffffff;
    border: 2px dashed {SELECTED_TAG_DASH_HOVER};
    border-radius: 4px;
}}
"""


TAG_SEARCH_BAR_QSS = f"""
QFrame#TagSearchBar {{
    background: #141a22;
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
}}

QLineEdit {{
    color: {TEXT};
    background: #101010;
    border: 1px solid #36577c;
    border-radius: 7px;
    padding: 6px 8px;
    selection-background-color: {BLUE};
}}

QLineEdit:focus {{
    border-color: {BLUE_SOFT};
}}

QLabel#TagSearchCount {{
    color: {YELLOW};
    font-weight: bold;
    padding-left: 4px;
    padding-right: 4px;
}}

QPushButton {{
    color: {TEXT};
    background: #202020;
    border: 1px solid {BORDER_SOFT};
    border-radius: 6px;
    padding: 4px 7px;
    font-weight: bold;
}}

QPushButton:hover {{
    background: #2b2b2b;
    border-color: {BLUE_SOFT};
}}

QPushButton:pressed {{
    background: #161616;
    border-color: {YELLOW};
}}

QPushButton:disabled {{
    background: #1a1a1a;
    color: #666666;
    border-color: #333333;
}}
"""


TAG_SORT_SEARCH_POPUP_QSS = f"""
QFrame#TagSortSearchPopup {{
    background: {CARD_BG};
    border: 2px solid {BLUE_SOFT};
    border-radius: 12px;
}}

QLabel#TagSortSearchTitle {{
    color: {TEXT};
    font-weight: bold;
    font-size: 10pt;
}}

QLabel#TagSortSearchHint {{
    color: {TEXT_MUTED};
}}

QLabel#TagSortSearchCount {{
    color: {YELLOW};
    font-weight: bold;
}}

QRadioButton {{
    color: {TEXT};
    spacing: 8px;
    padding: 3px;
}}

QRadioButton:hover {{
    color: white;
}}

QRadioButton::indicator {{
    width: 14px;
    height: 14px;
}}

QLineEdit {{
    color: {TEXT};
    background: #101010;
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
    padding: 7px 9px;
    selection-background-color: {BLUE};
}}

QLineEdit:focus {{
    border-color: {BLUE_SOFT};
}}

QPushButton {{
    color: {TEXT};
    background: {PANEL_BG_ALT};
    border: 1px solid {BORDER_SOFT};
    border-radius: 7px;
    padding: 5px 10px;
}}

QPushButton:hover {{
    background: #2b2b2b;
    border-color: {BLUE_SOFT};
}}

QPushButton:pressed {{
    background: #161616;
    border-color: {YELLOW};
}}
"""


CHIP_SCROLL_QSS = f"""
QScrollArea {{
    border: 1px solid {BORDER_SOFT};
    border-radius: 9px;
    background: #101010;
}}
"""


TAB_ACTIVE_QSS = f"""
QPushButton {{
    background: {GREEN_SOFT};
    color: {TEXT_DARK};
    border: 1px solid {GREEN};
    border-radius: 8px;
    padding: 6px 12px;
    font-weight: bold;
}}
"""


TAB_DISABLED_QSS = f"""
QPushButton {{
    background: #2b2b2b;
    color: #777777;
    border: 1px solid #444444;
    border-radius: 8px;
    padding: 6px 12px;
}}
"""


TARGET_LABEL_QSS = f"""
QLabel {{
    color: {TEXT_MUTED};
    padding: 3px 6px;
}}
"""


THUMBNAIL_FOOTER_QSS = f"""
QLabel {{
    color: {TEXT_MUTED};
    padding: 4px;
}}
"""

TAG_TABS_QSS = f"""
QTabWidget::pane {{
    background: {PANEL_BG};
    border: 1px solid {BORDER_SOFT};
    border-radius: 10px;
    top: -1px;
}}

QTabBar {{
    background: transparent;
}}

QTabBar::tab {{
    color: {TEXT_MUTED};
    background: #242424;
    border: 1px solid {BORDER_SOFT};
    border-bottom-color: {BORDER_SOFT};
    border-top-left-radius: 9px;
    border-top-right-radius: 9px;
    padding: 7px 16px;
    margin-right: 4px;
    min-width: 92px;
}}

QTabBar::tab:hover {{
    color: {TEXT};
    background: #2d2d2d;
    border-color: {GREEN_SOFT};
}}

QTabBar::tab:selected {{
    color: {TEXT_DARK};
    background: {GREEN_SOFT};
    border: 1px solid {GREEN};
    border-bottom-color: {GREEN_SOFT};
    font-weight: bold;
}}

QTabBar::tab:disabled {{
    color: #777777;
    background: #202020;
    border-color: #3a3a3a;
}}

QTabBar::tab:disabled:hover {{
    color: #777777;
    background: #202020;
    border-color: #3a3a3a;
}}
"""

TAG_SUB_TABS_QSS = f"""
QTabWidget::pane {{
    background: {PANEL_BG};
    border: 1px solid {BORDER_SOFT};
    border-radius: 9px;
    top: -1px;
}}

QTabBar {{
    background: transparent;
}}

QTabBar::tab {{
    color: {TEXT_MUTED};
    background: #202020;
    border: 1px solid #3a3a3a;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    padding: 5px 12px;
    margin-right: 4px;
    min-width: 78px;
}}

QTabBar::tab:hover {{
    color: {TEXT};
    background: #292929;
    border-color: {BLUE_SOFT};
}}

QTabBar::tab:selected {{
    color: {TEXT_DARK};
    background: {GREEN_SOFT};
    border: 1px solid {GREEN};
    border-bottom-color: {GREEN_SOFT};
    font-weight: bold;
}}

QTabBar::tab:disabled {{
    color: #777777;
    background: #202020;
    border-color: #3a3a3a;
}}

QTabBar::tab:disabled:hover {{
    color: #777777;
    background: #202020;
    border-color: #3a3a3a;
}}
"""


ADD_TAG_DIALOG_QSS = f"""
QDialog {{
    background: {PANEL_BG};
    color: {TEXT};
}}

QFrame#DialogCard {{
    background: {CARD_BG};
    border: 1px solid {BORDER_SOFT};
    border-radius: 14px;
}}

QLabel#DialogTitle {{
    color: {TEXT};
    font-size: 11pt;
    font-weight: bold;
}}

QLabel#DialogHint {{
    color: {TEXT_MUTED};
}}

QLineEdit {{
    color: {TEXT};
    background: #101010;
    border: 1px solid {BORDER_SOFT};
    border-radius: 10px;
    padding: 10px 12px;
    selection-background-color: {BLUE};
}}

QLineEdit:focus {{
    border-color: {BLUE_SOFT};
}}

QPushButton {{
    color: {TEXT};
    background: {PANEL_BG_ALT};
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
    padding: 7px 18px;
}}

QPushButton:hover {{
    background: #2b2b2b;
    border-color: {BLUE_SOFT};
}}

QPushButton:pressed {{
    background: #161616;
    border-color: {YELLOW};
}}
"""


def enable_windows_dark_title_bar(widget) -> None:
    """
    Windowsのタイトルバーをダーク化する。
    失敗してもアプリ動作には影響させない。
    """
    if sys.platform != "win32":
        return

    try:
        import ctypes
        from ctypes import wintypes

        # 子ダイアログもDWM設定の前にネイティブHWNDを確保する。
        # これをしないと、show/exec後に作り直されたタイトルバーが白のまま残ることがある。
        widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        hwnd = int(widget.winId())
        value = ctypes.c_int(1)

        for attr in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd),
                ctypes.c_uint(attr),
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
            if result == 0:
                break
    except Exception:
        pass
# LIGHT_THEME_DARK_SCHEME_PATCH_V1
def enable_dark_color_scheme(app) -> bool:
    # Qt 6.8以降のプラットフォーム提供ダーク配色を有効化する。
    # QPaletteの手書き上書き、Fusionへの切替、標準部品のQSS描画は使わない。
    try:
        set_color_scheme = getattr(app.styleHints(), "setColorScheme", None)
        if set_color_scheme is None:
            return False

        set_color_scheme(Qt.ColorScheme.Dark)
        return True
    except Exception:
        return False


def schedule_windows_dark_title_bar(widget) -> None:
    # Qtがshow()/exec()中にHWNDを作り直す場合があるため、即時設定に加え
    # イベントループへ戻った直後にも数回だけ再適用する。
    enable_windows_dark_title_bar(widget)

    for delay_ms in (0, 50, 200):
        QTimer.singleShot(
            delay_ms,
            lambda target=widget: enable_windows_dark_title_bar(target),
        )


class WindowsDarkTitleBarEventFilter(QObject):
    # アプリが開く全てのQtトップレベルウィンドウへダークタイトルバーを適用する。

    _WATCHED_EVENTS = {
        QEvent.Type.Show,
        QEvent.Type.WinIdChange,
    }

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if (
            event.type() in self._WATCHED_EVENTS
            and isinstance(watched, QWidget)
            and watched.isWindow()
            and not (watched.windowFlags() & Qt.WindowType.Popup)
        ):
            schedule_windows_dark_title_bar(watched)

        return super().eventFilter(watched, event)


def install_windows_dark_title_bar_support(app) -> None:
    # QApplicationへ一度だけイベントフィルタを登録する。
    # QMessageBox / QInputDialog / PreferencesDialog / 非ネイティブQFileDialogなどをカバーする。
    attribute_name = "_windows_dark_title_bar_event_filter"
    if getattr(app, attribute_name, None) is not None:
        return

    event_filter = WindowsDarkTitleBarEventFilter(app)
    app.installEventFilter(event_filter)
    setattr(app, attribute_name, event_filter)
