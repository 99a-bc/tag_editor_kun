from __future__ import annotations

from pathlib import Path
import time
from functools import cmp_to_key
import re
import sys

from logger import (
    get_logger,
    log_user_error,
    log_user_info,
    log_user_ok,
    log_user_warning,
)
from dev_config import dev_log_enabled

from PySide6.QtCore import QEvent, Qt, QFile, QTimer, QUrl
from PySide6.QtGui import QAction, QCursor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from models import IMAGE_EXTENSIONS, ImageItem
from image_lightbox import EmbeddedImageViewer, ImageLightbox
from mouse_gesture import MouseGestureController
from preferences_dialog import PreferencesDialog
from preview_panel import PreviewPanel
from settings import (
    AppSettings,
    DEFAULT_RECURSIVE_LOAD_DEPTH,
    normalize_recursive_load_depth,
    load_app_settings,
    save_app_settings,
)
from tag_io import load_tags, save_tags, tag_mtime_ns
from tag_panel import TagPanel
from tagger.process_client import TaggerProcessClient
from tagger.result import TagCandidate, TaggerResult
from tagger.session import TaggerSession
from tagger_panel import TaggerPanel
from thumbnail_panel import ThumbnailPanel
from windows_shell import open_folder_and_select_file
from theme import APP_QSS, enable_windows_dark_title_bar


log = get_logger(__name__)
log.info("app_window.py loaded from: %s", Path(__file__).resolve())

_NATURAL_PART_RE = re.compile(r"(\d+)")


def _fallback_natural_key(text: str) -> tuple:
    """
    Windows APIが使えない場合の自然順ソート用キー。
    数字部分を数値として扱う。
    """
    parts: list[object] = []

    for part in _NATURAL_PART_RE.split(text.casefold()):
        if not part:
            continue

        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part))

    return tuple(parts)


def _logical_compare_text(left: str, right: str) -> int:
    """
    WindowsではExplorerの名前順に近い StrCmpLogicalW を使う。
    使えない環境では自然順ソートのフォールバックを使う。
    """
    if sys.platform == "win32":
        try:
            import ctypes

            result = ctypes.windll.shlwapi.StrCmpLogicalW(left, right)
            if result < 0:
                return -1
            if result > 0:
                return 1
            return 0
        except Exception:
            pass

    left_key = _fallback_natural_key(left)
    right_key = _fallback_natural_key(right)

    if left_key < right_key:
        return -1
    if left_key > right_key:
        return 1
    return 0



def _image_path_within_recursive_depth(path: Path, folder: Path, depth: int) -> bool:
    """path が指定深さのサブフォルダ読み込み対象かを返す。

    depth=0 は全階層を意味する。
    depth=1 は選択フォルダ直下 + 1階層下までを含める。
    """
    normalized_depth = normalize_recursive_load_depth(depth)
    if normalized_depth == 0:
        return True

    try:
        relative_parent = path.parent.relative_to(folder)
    except ValueError:
        return True

    if str(relative_parent) == ".":
        return True

    return len(relative_parent.parts) <= normalized_depth


def _sort_image_paths_like_explorer(paths: list[Path], folder: Path, *, recursive: bool) -> list[Path]:
    """
    Windows Explorerのファイル名順に近い形で画像パスを並べる。

    recursive=False:
        ファイル名の自然順で並べる。

    recursive=True:
        まず親フォルダの相対パスを自然順で並べ、
        同じフォルダ内ではファイル名を自然順で並べる。
    """

    def relative_parent_text(path: Path) -> str:
        try:
            parent = path.parent.relative_to(folder)
        except ValueError:
            parent = path.parent

        # 直下ファイルは最初のグループに置く
        if str(parent) == ".":
            return ""

        return str(parent)

    def compare(left: Path, right: Path) -> int:
        if recursive:
            folder_result = _logical_compare_text(
                relative_parent_text(left),
                relative_parent_text(right),
            )
            if folder_result != 0:
                return folder_result

        name_result = _logical_compare_text(left.name, right.name)
        if name_result != 0:
            return name_result

        # フォルダ名・ファイル名が同じと判定された場合でも、順序が不安定にならないように絶対パスで安定化する。
        return _logical_compare_text(str(left), str(right))

    return sorted(paths, key=cmp_to_key(compare))


class AppWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("タグエディタ君 v1.2.0")
        self.resize(1440, 900)

        self.folder: Path | None = None
        self.items: list[ImageItem] = []
        self.current_index: int = -1
        # タグ編集後の再フィルタで現在画像が消えた場合だけ、
        # 編集前の位置を基準に「次、なければ前」へフォーカスを移す。
        # 明示的なフィルタ条件変更では従来どおり結果先頭へ移動する。
        self._tag_change_focus_reference_index: int | None = None
        self._current_recursive_load: bool = False
        self._current_recursive_depth: int = DEFAULT_RECURSIVE_LOAD_DEPTH
        # 左側の操作対象タグ欄。ここへ入った時だけ操作対象プレビューを新規発動する。
        self._tag_panel_hovered: bool = False
        # 左右を含むタグパネル全体。発動後の操作対象プレビュー維持に使う。
        self._whole_tag_panel_hovered: bool = False
        self._preview_panel_hovered: bool = False
        self._checked_hover_preview_active: bool = False
        self._lightbox_active: bool = False
        self._lightbox_source: str = "normal"
        self._lightbox_indexes: list[int] = []
        self._lightbox_position: int = 0
        # 左側の通常プレビュー＋サムネイルと切り替える埋め込み拡大プレビュー。
        self._embedded_preview_active: bool = False
        self._embedded_preview_source: str = "normal"
        self._embedded_preview_indexes: list[int] = []
        self._embedded_preview_position: int = 0
        # 埋め込み側から select_image / select_tagger_image を呼んだ時の再同期ループ防止。
        self._embedded_preview_internal_selection: bool = False
        self._perf_filter_sequence: int = 0
        # checks_changed は、Qt側の itemChanged や内部更新の都合で短時間に複数回届くことがある。
        # 重いタグ再集計は直接実行せず、短いタイマーでまとめて1回だけ処理する。
        self._checks_changed_pending_count: int = 0
        self._checks_changed_first_started: float = 0.0
        self.app_settings: AppSettings = load_app_settings()

        self._checks_changed_timer = QTimer(self)
        self._checks_changed_timer.setSingleShot(True)
        self._checks_changed_timer.setInterval(30)
        self._checks_changed_timer.timeout.connect(self._process_checks_changed)

        self._hover_preview_restore_timer = QTimer(self)
        self._hover_preview_restore_timer.setSingleShot(True)
        # タグ欄⇔プレビュー欄の狭い境界だけに使う短い接続猶予。
        # サムネイル等へ離れた場合は待機せず通常プレビューへ戻す。
        self._hover_preview_restore_timer.setInterval(70)
        self._hover_preview_restore_timer.timeout.connect(
            self._finish_hover_preview_if_outside
        )

        self.setAcceptDrops(True)

        # 埋め込み拡大プレビュー中は、右側タグパネルへフォーカスが移っていても
        # ←/→・Space・Esc をプレビュー操作として扱う。
        # QApplication側で拾うことで、個々のタグボタンのFocusPolicyには依存しない。
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        self._setup_menu_bar()

        self.preview_panel = PreviewPanel()
        self.thumbnail_panel = ThumbnailPanel()
        self.tag_panel = TagPanel()
        self.tagger_panel = TaggerPanel()
        self.tagger_session = TaggerSession()
        # Taggerタブ用の現在画像。タグ編集タブの current_index とは分離する。
        self._tagger_current_index: int = -1
        # Tagger専用の常駐子プロセス。CUDA・モデルは子プロセスだけが所有する。
        self._tagger_process_client = TaggerProcessClient(Path(__file__).resolve().parent, self)
        self._tagger_process_client.ready.connect(self._on_tagger_process_ready)
        self._tagger_process_client.message_received.connect(self._on_tagger_process_message)
        self._tagger_process_client.failed.connect(self._on_tagger_process_failed)
        self._tagger_process_client.exited.connect(self._on_tagger_process_exited)
        self._tagger_pending_run: tuple[list[Path], dict[str, object]] | None = None
        self._tagger_process_ping_request_id: int | None = None
        # Phase 2c: 子プロセスへ送ったバッチ推論の要求を追跡する。
        # request_idで応答を対応付け、画像ごとの進捗・結果・完了通知を復元する。
        self._tagger_process_batch_request_id: int | None = None
        self._tagger_process_batch_paths: list[Path] = []
        self._tagger_ui_locked: bool = False
        # 中止要求後は、子プロセスの終了通知を待って未処理画像を中止扱いにする。
        self._tagger_force_cancel_in_progress: bool = False

        # Taggerの画像ごとの完了通知は短時間に連続する。
        # サムネイル欄の再描画は少しまとめ、結果件数が多い時もGUIを圧迫しない。
        self._tagger_status_dirty_indexes: set[int] = set()
        self._tagger_status_refresh_timer = QTimer(self)
        self._tagger_status_refresh_timer.setSingleShot(True)
        self._tagger_status_refresh_timer.setInterval(40)
        self._tagger_status_refresh_timer.timeout.connect(
            self._flush_tagger_thumbnail_status_updates
        )

        self._install_tagger_tab()
        self._apply_app_settings(apply_recursive=True)

        self.preview_panel.previous_requested.connect(self.select_previous)
        self.preview_panel.next_requested.connect(self.select_next)
        self.preview_panel.hover_changed.connect(self._on_preview_panel_hover_changed)
        self.preview_panel.enlarge_requested.connect(self._show_lightbox)
        self.thumbnail_panel.image_selected.connect(self.select_image)
        self.thumbnail_panel.tagger_image_selected.connect(self.select_tagger_image)
        self.thumbnail_panel.checks_changed.connect(self._on_checks_changed)
        self.thumbnail_panel.delete_requested.connect(self.delete_checked_images_to_trash)
        self.thumbnail_panel.open_tag_file_requested.connect(self.open_tag_file_for_index)
        self.thumbnail_panel.open_image_folder_requested.connect(self.open_image_folder_for_index)
        self.thumbnail_panel.checked_work_mode_changed.connect(self._on_checked_work_mode_changed)
        self.thumbnail_panel.tag_filter_view_changed.connect(self.tag_panel.set_tag_filter_view)
        self.thumbnail_panel.visible_view_changed.connect(self._on_thumbnail_visible_view_changed)
        self.thumbnail_panel.recursive_load_settings_changed.connect(self._on_recursive_load_settings_changed)
        self.tag_panel.tags_changed.connect(self._on_tags_changed)
        self.tag_panel.save_requested.connect(self.save_all)
        self.tag_panel.hover_changed.connect(self._on_tag_panel_hover_changed)
        self.tag_panel.panel_hover_changed.connect(self._on_whole_tag_panel_hover_changed)
        self.tag_panel.tag_filter_changed.connect(self._on_tag_filter_changed)
        self.tag_panel.single_target_override_changed.connect(
            self._on_single_target_override_changed
        )
        self.tagger_panel.run_requested.connect(self._run_tagger_stage1)
        self.tagger_panel.reset_requested.connect(self._reset_tagger_stage1)
        self.tagger_panel.apply_requested.connect(self._apply_tagger_stage1)
        self.tagger_panel.cancel_requested.connect(self._cancel_tagger_stage1)
        self.tagger_panel.settings_changed.connect(self._on_tagger_settings_changed)
        self.tag_panel.tabs.currentChanged.connect(self._on_tag_panel_tab_changed)

        # 左側の通常表示（プレビュー＋サムネイル）は常に残し、その上へ
        # 半透明の埋め込み拡大プレビューをオーバーレイする。
        # これにより、拡大プレビューの暗幕越しにも通常表示を薄く見せられる。
        self._normal_left_page = QWidget()
        normal_left_layout = QVBoxLayout(self._normal_left_page)
        normal_left_layout.setContentsMargins(0, 0, 0, 0)
        normal_left_layout.setSpacing(6)
        normal_left_layout.addWidget(self.preview_panel, 1)
        normal_left_layout.addWidget(self.thumbnail_panel, 1)

        self._left_view_host = QWidget()
        left_host_layout = QVBoxLayout(self._left_view_host)
        left_host_layout.setContentsMargins(0, 0, 0, 0)
        left_host_layout.setSpacing(0)
        left_host_layout.addWidget(self._normal_left_page)

        self.embedded_preview = EmbeddedImageViewer(self._left_view_host)
        self.embedded_preview.previous_requested.connect(
            lambda: self._navigate_embedded_preview(-1)
        )
        self.embedded_preview.next_requested.connect(
            lambda: self._navigate_embedded_preview(1)
        )
        self.embedded_preview.check_toggle_requested.connect(
            self._toggle_embedded_preview_current_check
        )
        self.embedded_preview.exit_requested.connect(
            lambda: self._set_embedded_preview_active(False)
        )
        self.embedded_preview.lightbox_requested.connect(
            self._activate_lightbox_from_embedded_preview
        )
        self.embedded_preview.hide()

        main_layout = QHBoxLayout()
        main_layout.addWidget(self._left_view_host, 1)
        main_layout.addWidget(self.tag_panel, 1)

        root_layout = QVBoxLayout()
        root_layout.addLayout(main_layout, 1)

        central = QWidget()
        central.setLayout(root_layout)
        self.setCentralWidget(central)

        # プレビュー画像クリック時に、アプリ内容領域全体を覆う拡大確認表示を出す。
        self.lightbox = ImageLightbox(central)
        self.lightbox.closed.connect(self._on_lightbox_closed)
        self.lightbox.previous_requested.connect(lambda: self._navigate_lightbox(-1))
        self.lightbox.next_requested.connect(lambda: self._navigate_lightbox(1))
        self.lightbox.check_toggle_requested.connect(self._toggle_lightbox_current_check)
        self.lightbox.embedded_preview_requested.connect(
            self._activate_embedded_preview_from_lightbox
        )
        self.lightbox.setGeometry(central.rect())

        # ①通常表示 / ②拡大確認 / ③拡大プレビューに共通の右ドラッグ操作。
        # 描画用レイヤーはマウス透過とし、既存のタグ右クリックメニュー等は維持する。
        self._mouse_gesture = MouseGestureController(
            self, central, self._navigate_from_mouse_gesture
        )

        self.setStyleSheet(APP_QSS)
        enable_windows_dark_title_bar(self)
        self._on_tag_panel_tab_changed(self.tag_panel.tabs.currentIndex())
        log.info("AppWindow initialized")

    def _setup_menu_bar(self) -> None:
        """上部ボタン列をメニューバーへ移し、読み込み系操作をまとめる。"""
        file_menu = self.menuBar().addMenu("ファイル(&F)")

        self.open_folder_action = QAction("画像フォルダを開く...", self)
        self.open_folder_action.setStatusTip("画像フォルダを選択して読み込みます")
        self.open_folder_action.triggered.connect(self.open_folder)
        file_menu.addAction(self.open_folder_action)

        file_menu.addSeparator()

        self.exit_action = QAction("終了", self)
        self.exit_action.setStatusTip("タグエディタ君を終了します")
        self.exit_action.triggered.connect(self.close)
        file_menu.addAction(self.exit_action)

        tools_menu = self.menuBar().addMenu("ツール(&T)")

        self.preferences_action = QAction("環境設定...", self)
        self.preferences_action.setStatusTip("アプリの環境設定を開きます")
        self.preferences_action.triggered.connect(self.open_preferences)
        tools_menu.addAction(self.preferences_action)

    @staticmethod
    def _embedded_preview_key_should_pass_to_focus_widget(widget: QWidget | None) -> bool:
        """文字入力中は拡大プレビュー用キーを奪わず、入力Widgetへ渡す。"""
        if widget is None:
            return False
        return isinstance(
            widget,
            (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox),
        )

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        """アプリ共通の右ドラッグと③拡大プレビューのキーを処理する。"""
        if hasattr(self, "_mouse_gesture"):
            if self._mouse_gesture.process(watched, event):
                return True
            if watched is self.centralWidget() and event.type() == QEvent.Type.Resize:
                self._mouse_gesture.resize_overlay()
        if (
            self._embedded_preview_active
            and event.type() == QEvent.Type.KeyPress
            and QApplication.activeModalWidget() is None
            and QApplication.activePopupWidget() is None
        ):
            # 別ウインドウがアクティブな時は横取りしない。
            active_window = QApplication.activeWindow()
            if active_window is self:
                focus_widget = QApplication.focusWidget()
                if not self._embedded_preview_key_should_pass_to_focus_widget(focus_widget):
                    modifiers = event.modifiers()
                    allowed_modifiers = (
                        Qt.KeyboardModifier.NoModifier,
                        Qt.KeyboardModifier.KeypadModifier,
                    )
                    if modifiers in allowed_modifiers:
                        key = event.key()
                        if key == Qt.Key.Key_Left:
                            self._navigate_embedded_preview(-1)
                            event.accept()
                            return True
                        if key == Qt.Key.Key_Right:
                            self._navigate_embedded_preview(1)
                            event.accept()
                            return True
                        if key == Qt.Key.Key_Space:
                            self._toggle_embedded_preview_current_check()
                            event.accept()
                            return True
                        if key == Qt.Key.Key_Escape:
                            self._set_embedded_preview_active(False)
                            event.accept()
                            return True

        return super().eventFilter(watched, event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "lightbox") and self.centralWidget() is not None:
            self.lightbox.setGeometry(self.centralWidget().rect())
        self._resize_embedded_preview_overlay()
        if hasattr(self, "_mouse_gesture"):
            self._mouse_gesture.resize_overlay()

    def _resize_embedded_preview_overlay(self) -> None:
        if not hasattr(self, "embedded_preview") or not hasattr(self, "_left_view_host"):
            return
        self.embedded_preview.setGeometry(self._left_view_host.rect())
        if self._embedded_preview_active:
            self.embedded_preview.raise_()

    def _set_enlarged_preview_mode_preference(self, mode: str) -> None:
        """拡大表示の既定モードを保存する。"""
        normalized = "embedded" if mode == "embedded" else "lightbox"
        if self.app_settings.enlarged_preview_mode == normalized:
            return

        self.app_settings.enlarged_preview_mode = normalized
        try:
            save_app_settings(self.app_settings)
        except OSError as error:
            log.warning("failed to save enlarged preview mode: %s", error)

    def _update_base_preview_suppression(self) -> None:
        """拡大系モード中は元のPreviewPanel内容だけを隠し、終了時に復元する。"""
        suppressed = bool(self._lightbox_active or self._embedded_preview_active)
        self.preview_panel.set_content_suppressed(suppressed)

    def _set_embedded_preview_active(self, active: bool) -> None:
        """埋め込み拡大プレビューの表示状態を切り替える。"""
        active = bool(active)
        if active:
            if self._embedded_preview_active:
                self._resize_embedded_preview_overlay()
                self.embedded_preview.show()
                self.embedded_preview.raise_()
            return

        if not self._embedded_preview_active:
            return

        self._embedded_preview_active = False
        self._embedded_preview_source = "normal"
        self._embedded_preview_indexes = []
        self._embedded_preview_position = 0
        self.embedded_preview.hide()
        self.embedded_preview.clear_view()
        self._update_base_preview_suppression()
        self._hover_preview_restore_timer.stop()
        self._update_preview_placeholder_state()

    def _show_embedded_preview(self, path: Path, source: str = "normal") -> None:
        """通常プレビュー等から、設定済み文脈で埋め込み拡大プレビューを開く。"""
        index = self._index_for_image_path(path)
        if index is None:
            QMessageBox.warning(self, "拡大プレビュー", f"画像が現在の一覧にありません。\n{path.name}")
            return
        if not path.exists():
            QMessageBox.warning(self, "拡大プレビュー", f"画像ファイルが見つかりません。\n{path.name}")
            return

        indexes = self._lightbox_target_indexes(source)
        if not indexes:
            indexes = [index]
        if index not in indexes:
            indexes = [index]

        self._embedded_preview_source = "checked" if source == "checked" else "normal"
        self._embedded_preview_indexes = indexes
        self._embedded_preview_position = indexes.index(index)
        self._embedded_preview_active = True
        self._update_base_preview_suppression()
        self._hover_preview_restore_timer.stop()
        self._resize_embedded_preview_overlay()
        self.embedded_preview.show()
        self.embedded_preview.raise_()

        if not self._show_embedded_preview_current():
            self._set_embedded_preview_active(False)

    def _activate_embedded_preview_from_lightbox(self) -> None:
        """全画面ライトボックスの現在位置・対象文脈を引き継いで切り替える。"""
        if self._tagger_execution_is_active():
            return
        if not self._lightbox_active or not self._lightbox_indexes:
            return
        if not (0 <= self._lightbox_position < len(self._lightbox_indexes)):
            return

        source = self._lightbox_source
        current_index = self._lightbox_indexes[self._lightbox_position]
        if not (0 <= current_index < len(self.items)):
            return
        path = self.items[current_index].image_path

        self._set_enlarged_preview_mode_preference("embedded")
        # 埋め込み側を先に有効化してからライトボックスを閉じることで、
        # 背後PreviewPanelの抑制をモード切替中も途切れさせない。
        self._show_embedded_preview(path, source)
        self.lightbox.close_overlay()

    def _activate_lightbox_from_embedded_preview(self) -> None:
        """埋め込み拡大プレビューから全画面ライトボックスへ切り替える。"""
        if self._tagger_execution_is_active():
            return
        if not self._embedded_preview_active or not self._embedded_preview_indexes:
            return
        if not (0 <= self._embedded_preview_position < len(self._embedded_preview_indexes)):
            return

        source = self._embedded_preview_source
        current_index = self._embedded_preview_indexes[self._embedded_preview_position]
        if not (0 <= current_index < len(self.items)):
            return
        path = self.items[current_index].image_path

        self._set_enlarged_preview_mode_preference("lightbox")
        # ライトボックスを先に有効化し、元プレビューの抑制を保ったまま切り替える。
        self._show_lightbox_direct(path, source)
        if self._lightbox_active:
            self._set_embedded_preview_active(False)

    def _show_embedded_preview_current(self) -> bool:
        if not self._embedded_preview_active or not self._embedded_preview_indexes:
            return False

        self._embedded_preview_position %= len(self._embedded_preview_indexes)
        index = self._embedded_preview_indexes[self._embedded_preview_position]
        if not (0 <= index < len(self.items)):
            return False

        item = self.items[index]
        if not item.image_path.exists():
            QMessageBox.warning(
                self,
                "拡大プレビュー",
                f"画像ファイルが見つかりません。\n{item.image_path.name}",
            )
            return False

        self._embedded_preview_internal_selection = True
        try:
            if self._tagger_tab_is_active():
                self.select_tagger_image(index)
                checked = index in self.thumbnail_panel.tagger_selected_indexes()
                selection_context = "tagger"
            else:
                self.select_image(index)
                checked = item.checked
                selection_context = "edit"
        finally:
            self._embedded_preview_internal_selection = False

        if not self.embedded_preview.show_image(
            item.image_path,
            position=self._embedded_preview_position,
            total=len(self._embedded_preview_indexes),
            checked=checked,
            selection_context=selection_context,
        ):
            QMessageBox.warning(
                self,
                "拡大プレビュー",
                f"画像を読み込めませんでした。\n{item.image_path.name}",
            )
            return False

        return True

    def _sync_embedded_preview_to_index(self, index: int) -> None:
        """
        フィルタ再評価など本体側から現在画像が変わった時も、
        埋め込み拡大プレビューを同じ画像へ追従させる。
        """
        if (
            not self._embedded_preview_active
            or self._embedded_preview_internal_selection
            or not (0 <= index < len(self.items))
        ):
            return

        indexes = self._lightbox_target_indexes(self._embedded_preview_source)
        if not indexes:
            self._set_embedded_preview_active(False)
            return

        if index not in indexes:
            # checked文脈では、外部要因で非チェック画像へ現在位置が移っても
            # 操作対象プレビューの対象自体は変えない。
            if self._embedded_preview_source == "checked":
                return
            index = indexes[0]

        self._embedded_preview_indexes = indexes
        self._embedded_preview_position = indexes.index(index)

        item = self.items[index]
        if self._tagger_tab_is_active():
            checked = index in self.thumbnail_panel.tagger_selected_indexes()
            selection_context = "tagger"
        else:
            checked = item.checked
            selection_context = "edit"

        if not self.embedded_preview.show_image(
            item.image_path,
            position=self._embedded_preview_position,
            total=len(indexes),
            checked=checked,
            selection_context=selection_context,
            take_focus=False,
        ):
            self._set_embedded_preview_active(False)

    def _navigate_embedded_preview(self, direction: int) -> None:
        if not self._embedded_preview_active:
            return

        current_index = (
            self._embedded_preview_indexes[self._embedded_preview_position]
            if (
                self._embedded_preview_indexes
                and 0 <= self._embedded_preview_position < len(self._embedded_preview_indexes)
            )
            else (
                self._tagger_current_index
                if self._tagger_tab_is_active()
                else self.current_index
            )
        )

        # 前後移動のたびに対象リストを取り直し、フィルタや表示範囲の変更へ追従する。
        # タグ編集側は source="checked" でも visible_indexes() 全体を対象にする。
        indexes = self._lightbox_target_indexes(self._embedded_preview_source)
        if not indexes:
            self._set_embedded_preview_active(False)
            return

        if current_index in indexes:
            base_position = indexes.index(current_index)
        else:
            base_position = min(self._embedded_preview_position, len(indexes) - 1)

        self._embedded_preview_indexes = indexes
        self._embedded_preview_position = (base_position + direction) % len(indexes)
        self._show_embedded_preview_current()

    def _toggle_embedded_preview_current_check(self) -> None:
        """埋め込み拡大プレビューで現在画像のチェックをSpaceで切り替える。"""
        if self._tagger_execution_is_active():
            return
        if not self._embedded_preview_active or not self._embedded_preview_indexes:
            return
        if not (0 <= self._embedded_preview_position < len(self._embedded_preview_indexes)):
            return

        index = self._embedded_preview_indexes[self._embedded_preview_position]
        if not (0 <= index < len(self.items)):
            return

        if self._tagger_tab_is_active():
            was_checked = index in self.thumbnail_panel.tagger_selected_indexes()
            self.thumbnail_panel._set_tagger_selected_indexes([index], not was_checked)
            self._show_embedded_preview_current()
            return

        was_checked = self.items[index].checked
        self.thumbnail_panel._set_checked_indexes([index], not was_checked)

        # タグ編集側の拡大表示は、黄色チェックの有無にかかわらず
        # 現在の表示画像全体を前後移動対象にする。Spaceでチェックを外しても
        # 現在画像を対象リストから外したり、次画像へ自動移動したりしない。
        next_indexes = self._lightbox_target_indexes(self._embedded_preview_source)
        if next_indexes and index in next_indexes:
            self._embedded_preview_indexes = next_indexes
            self._embedded_preview_position = next_indexes.index(index)
        self._show_embedded_preview_current()

    def _show_lightbox(self, path: Path, source: str = "normal") -> None:
        """現在の既定モードで拡大表示を開く。"""
        if self.app_settings.enlarged_preview_mode == "embedded":
            self._show_embedded_preview(path, source)
            return
        self._show_lightbox_direct(path, source)

    def _show_lightbox_direct(self, path: Path, source: str = "normal") -> None:
        """プレビューでクリックされた元画像を、ライトボックスで拡大表示する。"""
        index = self._index_for_image_path(path)
        if index is None:
            QMessageBox.warning(self, "拡大表示", f"画像が現在の一覧にありません。\n{path.name}")
            return
        if not path.exists():
            QMessageBox.warning(self, "拡大表示", f"画像ファイルが見つかりません。\n{path.name}")
            return

        indexes = self._lightbox_target_indexes(source)
        if not indexes:
            indexes = [index]
        if index not in indexes:
            indexes = [index]

        self._lightbox_source = "checked" if source == "checked" else "normal"
        self._lightbox_indexes = indexes
        self._lightbox_position = indexes.index(index)

        self._hover_preview_restore_timer.stop()
        self.lightbox.setGeometry(self.centralWidget().rect())
        self._lightbox_active = True
        self._update_base_preview_suppression()

        if not self._show_lightbox_current():
            self._lightbox_active = False
            self._update_base_preview_suppression()

    def _on_lightbox_closed(self) -> None:
        """拡大確認を閉じ、開く前に有効だったプレビュー表示を再描画する。"""
        self._lightbox_active = False
        self._lightbox_indexes = []
        self._lightbox_position = 0
        self._update_base_preview_suppression()
        self._hover_preview_restore_timer.stop()
        self._update_preview_placeholder_state()

    def _index_for_image_path(self, path: Path) -> int | None:
        for index, item in enumerate(self.items):
            if item.image_path == path:
                return index
        return None

    def _lightbox_target_indexes(self, source: str | None = None) -> list[int]:
        """拡大表示の前後移動対象index列を返す。

        タグ編集側は、操作対象プレビュー起点（source="checked"）でも
        黄色チェック中だけには限定せず、現在サムネイル欄に表示されている
        画像全体を移動対象にする。Tagger側の checked 文脈だけは従来通り
        Tagger専用チェック中画像を対象にする。
        """
        effective_source = source or self._lightbox_source

        if effective_source == "checked" and self._tagger_tab_is_active():
            return self.thumbnail_panel.tagger_selected_indexes()

        return self.thumbnail_panel.visible_indexes()

    def _show_lightbox_current(self) -> bool:
        if not self._lightbox_indexes:
            self.lightbox.close_overlay()
            return False

        self._lightbox_position %= len(self._lightbox_indexes)
        index = self._lightbox_indexes[self._lightbox_position]
        if not (0 <= index < len(self.items)):
            self.lightbox.close_overlay()
            return False

        item = self.items[index]
        if not item.image_path.exists():
            QMessageBox.warning(self, "拡大表示", f"画像ファイルが見つかりません。\n{item.image_path.name}")
            self.lightbox.close_overlay()
            return False

        # ライトボックス内の現在画像は、開いている右タブ側の青枠・プレビューへ追従させる。
        if self._tagger_tab_is_active():
            self.select_tagger_image(index)
            checked = index in self.thumbnail_panel.tagger_selected_indexes()
            selection_context = "tagger"
        else:
            self.select_image(index)
            checked = item.checked
            selection_context = "edit"

        if not self.lightbox.show_image(
            item.image_path,
            position=self._lightbox_position,
            total=len(self._lightbox_indexes),
            checked=checked,
            selection_context=selection_context,
        ):
            QMessageBox.warning(self, "拡大表示", f"画像を読み込めませんでした。\n{item.image_path.name}")
            self.lightbox.close_overlay()
            return False

        return True

    def _navigate_lightbox(self, direction: int) -> None:
        """ライトボックス内で前後移動し、背後の青枠も追従させる。"""
        if not self._lightbox_active:
            return

        if self._lightbox_source == "checked":
            current_index = (
                self._lightbox_indexes[self._lightbox_position]
                if self._lightbox_indexes and 0 <= self._lightbox_position < len(self._lightbox_indexes)
                else self.current_index
            )
            indexes = self._lightbox_target_indexes("checked")
            if not indexes:
                self.lightbox.close_overlay()
                return

            if current_index in indexes:
                base_position = indexes.index(current_index)
            else:
                base_position = min(self._lightbox_position, len(indexes) - 1)

            self._lightbox_indexes = indexes
            self._lightbox_position = (base_position + direction) % len(indexes)
        else:
            if not self._lightbox_indexes:
                self._lightbox_indexes = self._lightbox_target_indexes("normal")
            if not self._lightbox_indexes:
                return
            self._lightbox_position = (self._lightbox_position + direction) % len(self._lightbox_indexes)

        self._show_lightbox_current()

    def _toggle_lightbox_current_check(self) -> None:
        """ライトボックスで現在表示している画像のチェック状態をSpaceで切り替える。"""
        if self._tagger_execution_is_active():
            return
        if not self._lightbox_active or not self._lightbox_indexes:
            return

        if not (0 <= self._lightbox_position < len(self._lightbox_indexes)):
            return

        index = self._lightbox_indexes[self._lightbox_position]
        if not (0 <= index < len(self.items)):
            return

        if self._tagger_tab_is_active():
            was_checked = index in self.thumbnail_panel.tagger_selected_indexes()
            self.thumbnail_panel._set_tagger_selected_indexes([index], not was_checked)
            self._show_lightbox_current()
            return

        was_checked = self.items[index].checked
        self.thumbnail_panel._set_checked_indexes([index], not was_checked)

        # タグ編集側では、操作対象プレビュー起点でも黄色チェック中だけに限定しない。
        # Spaceでチェック状態を変えても、同じ画像を表示し続ける。
        next_indexes = self._lightbox_target_indexes(self._lightbox_source)
        if next_indexes and index in next_indexes:
            self._lightbox_indexes = next_indexes
            self._lightbox_position = next_indexes.index(index)
        self._show_lightbox_current()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._tagger_execution_is_active():
            event.ignore()
            return
        urls = event.mimeData().urls()

        if not urls:
            event.ignore()
            return

        for url in urls:
            path = Path(url.toLocalFile())
            if path.is_dir() or path.suffix.lower() in IMAGE_EXTENSIONS:
                event.acceptProposedAction()
                return

        event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        if self._tagger_execution_is_active():
            event.ignore()
            return
        urls = event.mimeData().urls()

        if not urls:
            event.ignore()
            return

        if not self._confirm_discard_unsaved():
            event.ignore()
            return

        for url in urls:
            path = Path(url.toLocalFile())

            if path.is_dir():
                self.load_folder(path)
                event.acceptProposedAction()
                return

            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                self.load_folder(path.parent)
                event.acceptProposedAction()
                return

        event.ignore()

    def _apply_app_settings(self, *, apply_recursive: bool = False) -> None:
        """読み込み済みの環境設定を各UI部品へ反映する。"""
        self.thumbnail_panel.set_wheel_scroll_px(self.app_settings.thumbnail_wheel_scroll_px)
        self.tag_panel.set_tag_color_enabled(self.app_settings.tag_color_enabled)
        self.tag_panel.set_tag_completion_settings(
            enabled=self.app_settings.tag_completion_enabled,
            limit=self.app_settings.tag_completion_limit,
        )
        self.tagger_panel.set_tag_color_enabled(self.app_settings.tag_color_enabled)
        self.tagger_panel.restore_persisted_settings(
            model_repo=self.app_settings.tagger_model_repo,
            device=self.app_settings.tagger_device,
            include_general=self.app_settings.tagger_include_general,
            general_threshold=self.app_settings.tagger_general_threshold,
            include_character=self.app_settings.tagger_include_character,
            character_threshold=self.app_settings.tagger_character_threshold,
            include_copyright=self.app_settings.tagger_include_copyright,
            copyright_expanded=self.app_settings.tagger_copyright_expanded,
            apply_mode=self.app_settings.tagger_apply_mode,
        )

        if apply_recursive:
            enabled = (
                self.app_settings.recursive_load_enabled
                if self.app_settings.remember_recursive_load
                else False
            )
            depth = (
                self.app_settings.recursive_load_depth
                if self.app_settings.remember_recursive_load
                else DEFAULT_RECURSIVE_LOAD_DEPTH
            )
            self.thumbnail_panel.set_recursive_load_settings(enabled, depth, emit=False)

    def _on_tagger_settings_changed(self, tagger_settings: dict[str, object]) -> None:
        """Taggerタブで変えた設定を即時にアプリ設定へ保存する。"""
        settings_data = self.app_settings.to_dict()
        settings_data.update(
            {
                "tagger_model_repo": tagger_settings.get("model_repo"),
                "tagger_device": tagger_settings.get("device"),
                "tagger_include_general": tagger_settings.get("include_general"),
                "tagger_general_threshold": tagger_settings.get("general_threshold"),
                "tagger_include_character": tagger_settings.get("include_character"),
                "tagger_character_threshold": tagger_settings.get("character_threshold"),
                "tagger_include_copyright": tagger_settings.get("include_copyright"),
                "tagger_copyright_expanded": tagger_settings.get("copyright_expanded"),
                "tagger_apply_mode": tagger_settings.get("apply_mode"),
            }
        )
        self.app_settings = AppSettings.from_dict(settings_data)
        try:
            save_app_settings(self.app_settings)
        except OSError as error:
            # スピンボックスの操作ごとにモーダルを出さず、UIはそのまま維持する。
            log.warning("failed to save Tagger settings: %s", error)

    def _save_recursive_load_settings_if_needed(self, enabled: bool, depth: int) -> None:
        """記憶設定がONの場合だけ、サムネイル欄の読み込み範囲設定を保存する。"""
        if not self.app_settings.remember_recursive_load:
            return

        normalized_depth = normalize_recursive_load_depth(depth)
        if (
            self.app_settings.recursive_load_enabled == bool(enabled)
            and self.app_settings.recursive_load_depth == normalized_depth
        ):
            return

        self.app_settings.recursive_load_enabled = bool(enabled)
        self.app_settings.recursive_load_depth = normalized_depth

        try:
            save_app_settings(self.app_settings)
        except OSError as error:
            log.warning("failed to save recursive load setting: %s", error)

    def _on_recursive_load_settings_changed(self, enabled: bool, depth: int) -> None:
        """サムネイル欄フッターのサブフォルダ読み込み設定変更を反映する。"""
        if self._tagger_execution_is_active():
            return
        normalized_depth = normalize_recursive_load_depth(depth)

        changed_from_current_load = (
            bool(enabled) != self._current_recursive_load
            or normalized_depth != self._current_recursive_depth
        )

        if self.folder is not None and changed_from_current_load:
            if not self._confirm_discard_unsaved():
                self.thumbnail_panel.set_recursive_load_settings(
                    self._current_recursive_load,
                    self._current_recursive_depth,
                    emit=False,
                )
                return

        self._save_recursive_load_settings_if_needed(enabled, normalized_depth)

        if self.folder is not None and changed_from_current_load:
            self.load_folder(self.folder)

    def open_preferences(self) -> None:
        """環境設定ダイアログを開く。"""
        if self._tagger_execution_is_active():
            return
        dialog = PreferencesDialog(self.app_settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        next_settings = dialog.settings()

        # 「記憶する」をONにした場合は、現在のサムネイル欄フッターの状態を保存する。
        # OFFの場合は、次回起動時に必ずOFFへ戻すため保存値もFalseへ寄せる。
        if next_settings.remember_recursive_load:
            next_settings.recursive_load_enabled = self.thumbnail_panel.recursive_load_enabled()
            next_settings.recursive_load_depth = self.thumbnail_panel.recursive_load_depth()
        else:
            next_settings.recursive_load_enabled = False
            next_settings.recursive_load_depth = DEFAULT_RECURSIVE_LOAD_DEPTH

        try:
            save_app_settings(next_settings)
        except OSError as error:
            QMessageBox.warning(
                self,
                "環境設定",
                f"設定ファイルの保存に失敗しました。\n{error}",
            )
            return

        self.app_settings = next_settings
        self._apply_app_settings()

    def open_folder(self) -> None:
        if self._tagger_execution_is_active():
            return
        log.info("open_folder requested")
        if not self._confirm_discard_unsaved():
            return

        # Windows標準のフォルダ選択を優先する。
        # これはWindowsのシステムテーマで描画されるため、アプリ側のダーク配色・
        # タイトルバー指定は適用されないが、操作性とエクスプローラー連携を優先する。
        folder_text = QFileDialog.getExistingDirectory(self, "画像フォルダを開く")
        if not folder_text:
            return

        log.info("folder selected: %s", folder_text)
        self.load_folder(Path(folder_text))

    def load_folder(self, folder: Path) -> None:
        if self._tagger_execution_is_active():
            return
        if self._lightbox_active:
            self.lightbox.close_overlay()
        if self._embedded_preview_active:
            self._set_embedded_preview_active(False)

        self._clear_pending_tagger_thumbnail_status_updates()
        self._checks_changed_timer.stop()
        self._checks_changed_pending_count = 0
        self._checks_changed_first_started = 0.0

        perf_enabled = dev_log_enabled("PERF_FOLDER_LOAD")
        if perf_enabled:
            perf_started = time.perf_counter()

        log.info("load_folder start folder=%s", folder)
        if not folder.exists() or not folder.is_dir():
            QMessageBox.warning(self, "フォルダを開く", "有効なフォルダではありません。")
            return

        log_user_info("画像フォルダを読み込み中: %s", folder)
        recursive = self.thumbnail_panel.recursive_load_enabled()
        recursive_depth = self.thumbnail_panel.recursive_load_depth()
        log.info("load_folder recursive=%s depth=%d", recursive, recursive_depth)
        self._current_recursive_load = recursive
        self._current_recursive_depth = recursive_depth

        if perf_enabled:
            enumerate_started = time.perf_counter()

        if recursive:
            image_paths = _sort_image_paths_like_explorer(
                [
                    path
                    for path in folder.rglob("*")
                    if (
                        path.is_file()
                        and path.suffix.lower() in IMAGE_EXTENSIONS
                        and _image_path_within_recursive_depth(path, folder, recursive_depth)
                    )
                ],
                folder,
                recursive=True,
            )
        else:
            image_paths = _sort_image_paths_like_explorer(
                [
                    path
                    for path in folder.iterdir()
                    if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
                ],
                folder,
                recursive=False,
            )

        if perf_enabled:
            enumerate_ms = (time.perf_counter() - enumerate_started) * 1000

        log.info("image_paths loaded count=%d", len(image_paths))
        if dev_log_enabled("FOLDER_LOAD_DETAIL"):
            for path in image_paths[:20]:
                log.debug("loaded image: %s", path)
            if len(image_paths) > 20:
                log.debug("... %d more images", len(image_paths) - 20)

        self._tag_panel_hovered = False
        self._whole_tag_panel_hovered = False
        self._preview_panel_hovered = False
        self._checked_hover_preview_active = False
        self._hover_preview_restore_timer.stop()

        self.folder = folder

        if perf_enabled:
            tag_txt_started = time.perf_counter()

        self.items = []
        for path in image_paths:
            tag_path = path.with_suffix(".txt")
            tags = load_tags(tag_path)
            self.items.append(
                ImageItem(
                    image_path=path,
                    tags=tags,
                    saved_tags=list(tags),
                    tag_mtime_ns=tag_mtime_ns(tag_path),
                )
            )

        if perf_enabled:
            tag_txt_ms = (time.perf_counter() - tag_txt_started) * 1000

        self.current_index = 0 if self.items else -1

        if perf_enabled:
            thumbnail_started = time.perf_counter()
        self.thumbnail_panel.set_items(self.items, folder, recursive=recursive, recursive_depth=recursive_depth)
        if perf_enabled:
            thumbnail_ms = (time.perf_counter() - thumbnail_started) * 1000
            tag_panel_started = time.perf_counter()

        self.tag_panel.set_items(self.items)
        self.tagger_session.clear()
        self.thumbnail_panel.clear_tagger_selection()
        self._tagger_current_index = self.current_index
        self.tagger_panel.set_items(self.items)
        self.tagger_panel.set_current_index(self._tagger_current_index)
        if self._tagger_tab_is_active() and self._tagger_current_index >= 0:
            self.thumbnail_panel.select_tagger_index(self._tagger_current_index)
        self._refresh_tagger_thumbnail_statuses()
        self._refresh_tagger_current_result()

        if perf_enabled:
            tag_panel_ms = (time.perf_counter() - tag_panel_started) * 1000

        log.info("items created count=%d current_index=%d", len(self.items), self.current_index)

        if perf_enabled:
            initial_select_started = time.perf_counter()

        if self.current_index >= 0:
            self.select_image(self.current_index)
        else:
            self.preview_panel.set_image(None)
            self.tag_panel.set_current_index(-1)

        if self.items:
            log_user_ok("%d枚の画像を読み込みました。", len(self.items))
        else:
            log_user_warning("画像フォルダに対応画像が見つかりませんでした。")

        if not perf_enabled:
            return

        initial_select_ms = (time.perf_counter() - initial_select_started) * 1000
        sync_total_ms = (time.perf_counter() - perf_started) * 1000
        log.info(
            "[PERF_LOAD] app.load_folder_sync images=%d recursive=%s depth=%d "
            "enumerate_and_sort=%.2fms tag_txt_and_items=%.2fms "
            "thumbnail_set_items=%.2fms tag_panel_set_items=%.2fms "
            "initial_select=%.2fms sync_total=%.2fms",
            len(self.items),
            recursive,
            recursive_depth,
            enumerate_ms,
            tag_txt_ms,
            thumbnail_ms,
            tag_panel_ms,
            initial_select_ms,
            sync_total_ms,
        )

        def log_after_initial_layout_settles() -> None:
            log.info(
                "[PERF_LOAD] app.load_folder_settled images=%d elapsed=%.2fms",
                len(self.items),
                (time.perf_counter() - perf_started) * 1000,
            )

        # 右側タグ一覧のFlowLayoutや初回描画はQTimer経由で完了するため、
        # 2イベントループ待った体感に近い経過時間も記録する。
        QTimer.singleShot(0, lambda: QTimer.singleShot(0, log_after_initial_layout_settles))

    def select_image(self, index: int) -> None:
        perf_enabled = dev_log_enabled("PERF_FILTER") or dev_log_enabled("UI_INTERACTION_DETAIL")
        if dev_log_enabled("UI_INTERACTION_DETAIL"):
            log.debug("select_image index=%s item_count=%d", index, len(self.items))
        if not (0 <= index < len(self.items)):
            return

        perf_started = time.perf_counter() if perf_enabled else 0.0

        self.current_index = index

        if perf_enabled:
            thumbnail_started = time.perf_counter()
        self.thumbnail_panel.select_index(index)
        thumbnail_ms = (time.perf_counter() - thumbnail_started) * 1000 if perf_enabled else 0.0

        if perf_enabled:
            preview_started = time.perf_counter()
        preview_position, preview_total = self._preview_position_and_total_label(index)
        self.preview_panel.set_image(self.items[index].image_path, preview_position, preview_total)
        preview_ms = (time.perf_counter() - preview_started) * 1000 if perf_enabled else 0.0

        if perf_enabled:
            tag_started = time.perf_counter()
        defer_checked_refresh = (
            self._checks_changed_pending_count > 0
            or self._checks_changed_timer.isActive()
        )
        self.tag_panel.set_current_index(
            index,
            defer_checked_refresh=defer_checked_refresh,
        )
        tag_panel_ms = (time.perf_counter() - tag_started) * 1000 if perf_enabled else 0.0

        if perf_enabled:
            placeholder_started = time.perf_counter()
        self._update_preview_placeholder_state()
        placeholder_ms = (time.perf_counter() - placeholder_started) * 1000 if perf_enabled else 0.0

        if self._embedded_preview_active and not self._embedded_preview_internal_selection:
            self._sync_embedded_preview_to_index(index)

        if perf_enabled:
            log.info(
                "[PERF] app.select_image index=%d checked_count=%d defer_checked_refresh=%s "
                "thumbnail=%.2fms preview=%.2fms tag_panel=%.2fms placeholder=%.2fms total=%.2fms",
                index,
                self._checked_count(),
                defer_checked_refresh,
                thumbnail_ms,
                preview_ms,
                tag_panel_ms,
                placeholder_ms,
                (time.perf_counter() - perf_started) * 1000,
            )

    def _preview_position_and_total_label(self, index: int) -> tuple[int, int | str]:
        """プレビュー下の n/m 表示用に、表示中サムネ一覧基準の位置と分母を返す。"""
        total_count = len(self.items)
        if total_count <= 0:
            return 0, 0

        visible_indexes = self.thumbnail_panel.visible_indexes()
        if index in visible_indexes:
            position = visible_indexes.index(index) + 1
            visible_count = len(visible_indexes)
        else:
            # フィルタ切替直後など、現在画像が一時的に表示対象外の場合の保険。
            position = index + 1
            visible_count = total_count

        if visible_count != total_count:
            return position, f"{visible_count}（全{total_count}）"

        return position, total_count

    def _refresh_current_preview_counter(self) -> None:
        """現在画像はそのまま、プレビュー下の n/m 表示を現在の表示範囲に合わせ直す。"""
        if not (0 <= self.current_index < len(self.items)):
            return
        if not self.thumbnail_panel.is_index_visible(self.current_index):
            return

        preview_position, preview_total = self._preview_position_and_total_label(self.current_index)
        self.preview_panel.set_image(
            self.items[self.current_index].image_path,
            preview_position,
            preview_total,
        )

    def _on_thumbnail_visible_view_changed(self) -> None:
        """チェック中のみ表示・作業セット・タグフィルタで表示母集団が変わった時に追従する。"""
        if not self.items:
            return

        if self._tagger_tab_is_active():
            visible_indexes = self.thumbnail_panel.visible_indexes()
            if visible_indexes and self._tagger_current_index not in visible_indexes:
                self.select_tagger_image(visible_indexes[0])
            return

        self._select_first_visible_if_current_hidden()
        self._refresh_current_preview_counter()

    def _navigate_from_mouse_gesture(self, direction: int) -> None:
        """ジェスチャー成立時に、現在有効な表示モードの画像送りへ接続する。"""
        if self._lightbox_active:
            self._navigate_lightbox(direction)
        elif self._embedded_preview_active:
            self._navigate_embedded_preview(direction)
        elif direction < 0:
            self.select_previous()
        else:
            self.select_next()

    def select_previous(self) -> None:
        if not self.items:
            return

        visible_indexes = self.thumbnail_panel.visible_indexes()
        if self._tagger_tab_is_active():
            if not visible_indexes:
                return
            if self._tagger_current_index not in visible_indexes:
                self.select_tagger_image(visible_indexes[-1])
                return
            position = visible_indexes.index(self._tagger_current_index)
            self.select_tagger_image(visible_indexes[(position - 1) % len(visible_indexes)])
            return
        if not visible_indexes:
            return

        if self.current_index not in visible_indexes:
            self.select_image(visible_indexes[-1])
            return

        position = visible_indexes.index(self.current_index)
        self.select_image(visible_indexes[(position - 1) % len(visible_indexes)])

    def select_next(self) -> None:
        if not self.items:
            return

        visible_indexes = self.thumbnail_panel.visible_indexes()
        if self._tagger_tab_is_active():
            if not visible_indexes:
                return
            if self._tagger_current_index not in visible_indexes:
                self.select_tagger_image(visible_indexes[0])
                return
            position = visible_indexes.index(self._tagger_current_index)
            self.select_tagger_image(visible_indexes[(position + 1) % len(visible_indexes)])
            return
        if not visible_indexes:
            return

        if self.current_index not in visible_indexes:
            self.select_image(visible_indexes[0])
            return

        position = visible_indexes.index(self.current_index)
        self.select_image(visible_indexes[(position + 1) % len(visible_indexes)])

    def _tagger_tab_is_active(self) -> bool:
        # 右側の現在タブがTaggerかを返す。
        tabs = getattr(self.tag_panel, "tabs", None)
        return tabs is not None and tabs.currentWidget() is self.tagger_panel

    def _on_tag_panel_tab_changed(self, _index: int) -> None:
        # 右タブ切替に合わせ、左側の選択・青枠・Tagger表示文脈を切り替える。
        self._checked_hover_preview_active = False
        self._hover_preview_restore_timer.stop()

        if self._embedded_preview_active:
            # タブをまたいで黄色チェック/Taggerチェックの対象文脈を混在させない。
            # タブ切替後は通常プレビュー文脈へ寄せ、select_* 側の同期で現在画像を表示する。
            self._embedded_preview_source = "normal"
            self._embedded_preview_indexes = self._lightbox_target_indexes("normal")
            if not self._embedded_preview_indexes:
                self._set_embedded_preview_active(False)

        if self._tagger_tab_is_active():
            self.thumbnail_panel.set_interaction_context("tagger")
            if not (0 <= self._tagger_current_index < len(self.items)) and self.items:
                self._tagger_current_index = 0
            if 0 <= self._tagger_current_index < len(self.items):
                self.select_tagger_image(self._tagger_current_index)
            else:
                self.preview_panel.set_image(None)
                self.tagger_panel.set_current_index(-1)
                self._refresh_tagger_current_result()
            self._refresh_tagger_thumbnail_statuses()
            return

        self.thumbnail_panel.set_interaction_context("edit")
        if 0 <= self.current_index < len(self.items):
            self.select_image(self.current_index)
        else:
            self.preview_panel.set_image(None)
        self._refresh_tagger_thumbnail_statuses()

        # Tagger反映中はタグ編集ページが非表示のままタグチップ再構築が走りうる。
        # QTabWidgetがページを表示し終えた後に、実際の幅で左右タグ欄を再配置する。
        QTimer.singleShot(
            0,
            lambda: QTimer.singleShot(
                0,
                self.tag_panel.refresh_editor_layouts_after_show,
            ),
        )

    def select_tagger_image(self, index: int) -> None:
        # Taggerタブだけの現在画像を移動する。編集側の青枠・左タグ欄は変えない。
        if not (0 <= index < len(self.items)):
            return

        self._tagger_current_index = index
        self.thumbnail_panel.select_tagger_index(index)

        preview_position, preview_total = self._preview_position_and_total_label(index)
        self.preview_panel.set_image(self.items[index].image_path, preview_position, preview_total)
        self.tagger_panel.set_current_index(index)
        self._refresh_tagger_current_result()

        if self._embedded_preview_active and not self._embedded_preview_internal_selection:
            self._sync_embedded_preview_to_index(index)

    def _install_tagger_tab(self) -> None:
        """既存TagPanel内のdanbooruタグ枠を、TaggerPanelへ置き換える。"""
        tabs = getattr(self.tag_panel, "tabs", None)
        if tabs is None:
            return

        index = 0
        while index < tabs.count():
            if tabs.tabText(index) == "Tagger" or tabs.widget(index) is self.tagger_panel:
                tabs.removeTab(index)
                continue
            index += 1

        insert_index = 1
        for tab_index in range(tabs.count()):
            if tabs.tabText(tab_index) == "danbooruタグ":
                insert_index = tab_index
                tabs.removeTab(tab_index)
                break

        insert_index = max(0, min(insert_index, tabs.count()))
        tagger_index = tabs.insertTab(insert_index, self.tagger_panel, "Tagger")
        tabs.setTabEnabled(tagger_index, True)
        tabs.setTabToolTip(tagger_index, "WD Taggerで表示中画像の候補タグを確認します。")

    def _tagger_thumbnail_statuses(self) -> dict[int, str | None]:
        if not hasattr(self, "tagger_session") or not self.tagger_session.active:
            return {}

        statuses: dict[int, str | None] = {}
        for index, item in enumerate(self.items):
            statuses[index] = self.tagger_session.status_for_path(item.image_path)
        return statuses

    def _refresh_tagger_thumbnail_statuses(self) -> None:
        if hasattr(self.thumbnail_panel, "set_tagger_statuses"):
            self.thumbnail_panel.set_tagger_statuses(self._tagger_thumbnail_statuses())

    def _clear_pending_tagger_thumbnail_status_updates(self) -> None:
        """未反映のTaggerサムネイル状態更新を破棄する。"""
        self._tagger_status_refresh_timer.stop()
        self._tagger_status_dirty_indexes.clear()

    def _queue_tagger_thumbnail_status_update(self, index: int) -> None:
        """完了した1画像だけを、短い間隔でまとめてサムネイル欄へ反映する。"""
        if not (0 <= index < len(self.items)):
            return

        self._tagger_status_dirty_indexes.add(index)
        if not self._tagger_status_refresh_timer.isActive():
            self._tagger_status_refresh_timer.start()

    def _flush_tagger_thumbnail_status_updates(self) -> None:
        """保留中のTagger状態を差分だけサムネイル欄へ反映する。"""
        dirty_indexes = sorted(self._tagger_status_dirty_indexes)
        self._tagger_status_dirty_indexes.clear()

        if not dirty_indexes or not hasattr(self.thumbnail_panel, "set_tagger_statuses_for_indexes"):
            return

        statuses = {
            index: self.tagger_session.status_for_path(self.items[index].image_path)
            for index in dirty_indexes
            if 0 <= index < len(self.items)
        }
        if not statuses:
            return

        started = time.perf_counter() if dev_log_enabled("TAGGER_PERF") else 0.0
        self.thumbnail_panel.set_tagger_statuses_for_indexes(statuses)
        if started:
            log.info(
                "[TAGGER_PERF] thumbnail_status_flush updated=%d elapsed=%.2fms",
                len(statuses),
                (time.perf_counter() - started) * 1000,
            )

    def _refresh_tagger_current_result(self) -> None:
        if not hasattr(self, "tagger_panel") or not hasattr(self, "tagger_session"):
            return

        perf_started = time.perf_counter() if dev_log_enabled("TAGGER_PERF") else 0.0

        if not (0 <= self._tagger_current_index < len(self.items)):
            self.tagger_panel.set_current_result(None, None)
            return

        item = self.items[self._tagger_current_index]
        result = self.tagger_session.result_for_path(item.image_path)
        error = self.tagger_session.error_for_path(item.image_path)
        status = self.tagger_session.status_for_path(item.image_path)

        self.tagger_panel.set_current_result(
            item.image_path.name,
            result,
            error,
            status,
        )

        if perf_started:
            candidate_count = len(result.all_candidates) if result is not None else 0
            log.info(
                "[TAGGER_PERF] current_result path=%s status=%s candidates=%d elapsed=%.2fms",
                item.image_path.name,
                status,
                candidate_count,
                (time.perf_counter() - perf_started) * 1000,
            )

    def _tagger_execution_is_active(self) -> bool:
        """Tagger実行、または子プロセス起動・バッチ通信中かを返す。"""
        return (
            self._tagger_pending_run is not None
            or self._tagger_process_batch_request_id is not None
            or self._tagger_ui_locked
            or self._tagger_force_cancel_in_progress
        )

    def _set_tagger_execution_locked(self, locked: bool) -> None:
        """Tagger実行中に、結果と対象集合を競合させる操作だけを止める。"""
        locked = bool(locked)
        if self._tagger_ui_locked == locked:
            return

        self._tagger_ui_locked = locked
        self.thumbnail_panel.set_tagger_execution_locked(locked)
        self.tag_panel.set_tagger_execution_locked(locked)
        self.open_folder_action.setEnabled(not locked)
        self.preferences_action.setEnabled(not locked)
        self.exit_action.setEnabled(not locked)

    @staticmethod
    def _tagger_model_display_name(settings: dict[str, object]) -> str:
        """コンソール案内向けに、選択モデルの表示名を返す。"""
        model_repo = str(settings.get("model_repo", "")).strip()
        known_names = {
            "SmilingWolf/wd-eva02-large-tagger-v3": "WD EVA02 Large Tagger v3",
            "pixai-labs/pixai-tagger-v0.9": "PixAI Tagger v0.9",
        }
        return known_names.get(model_repo, model_repo or "選択中のモデル")

    def _run_tagger_stage1(self, settings: dict[str, object]) -> None:
        if self._tagger_execution_is_active():
            QMessageBox.information(self, "Tagger", "Taggerを実行中です。")
            return

        target_indexes = self.thumbnail_panel.tagger_selected_indexes()
        if not target_indexes:
            QMessageBox.information(self, "Tagger", "Taggerを実行する画像をチェックしてください。")
            return

        target_paths = [
            self.items[index].image_path
            for index in target_indexes
            if 0 <= index < len(self.items)
        ]
        if not target_paths:
            QMessageBox.information(self, "Tagger", "Taggerを実行する画像をチェックしてください。")
            return

        # Phase 2cでは、緑チェック中の全画像を常駐子プロセスへ一括依頼する。
        # 推論の進捗・画像ごとの結果はJSON Linesで受け取り、既存のセッション/UIへ戻す。
        log_user_info(
            "Taggerを実行しています...（%d枚 / %s）",
            len(target_paths),
            self._tagger_model_display_name(settings),
        )
        self._set_tagger_execution_locked(True)
        self.tagger_panel.set_running(True)

        if self._tagger_process_client.is_ready:
            self._start_tagger_phase2c_batch(target_paths, settings)
            return

        self._tagger_pending_run = (target_paths, dict(settings))
        self.tagger_panel.set_status_text("Tagger子プロセスを起動しています...")

        if not self._tagger_process_client.ensure_started():
            self._fail_pending_tagger_process_start(
                "Tagger子プロセスを起動できませんでした。"
            )

    def _on_tagger_process_ready(self, _message: dict[str, object]) -> None:
        """子プロセスのreadyを受け、双方向通信をpingで確認する。"""
        if self._tagger_force_cancel_in_progress:
            return
        if self._tagger_pending_run is None:
            log.info("Tagger subprocess became ready without a pending run")
            return

        self.tagger_panel.set_status_text("Tagger子プロセスの起動確認中...")
        request_id = self._tagger_process_client.send_ping()
        if request_id is None:
            self._fail_pending_tagger_process_start(
                "Tagger子プロセスとの通信を開始できませんでした。"
            )
            return

        self._tagger_process_ping_request_id = request_id
        log.info("Tagger subprocess ping sent request_id=%d", request_id)

    def _on_tagger_process_message(self, message: dict[str, object]) -> None:
        """子プロセスからのPhase 2a/2cメッセージを処理する。"""
        message_type = str(message.get("type", ""))

        if message_type == "pong":
            self._on_tagger_process_pong(message)
            return

        if message_type == "batch_started":
            self._on_tagger_process_batch_started(message)
            return

        if message_type == "batch_item_result":
            self._on_tagger_process_batch_item_result(message)
            return

        if message_type == "batch_item_error":
            self._on_tagger_process_batch_item_error(message)
            return

        if message_type == "batch_progress":
            self._on_tagger_process_batch_progress(message)
            return

        if message_type == "batch_finished":
            self._on_tagger_process_batch_finished(message)
            return

        if message_type in {"batch_failed", "error"}:
            self._on_tagger_process_batch_failed(message)

    def _on_tagger_process_pong(self, message: dict[str, object]) -> None:
        if self._tagger_force_cancel_in_progress:
            return
        request_id = message.get("request_id")
        if request_id != self._tagger_process_ping_request_id:
            return

        self._tagger_process_ping_request_id = None
        pending_run = self._tagger_pending_run
        self._tagger_pending_run = None
        if pending_run is None:
            return

        target_paths, settings = pending_run
        log.info("Tagger subprocess ping confirmed; starting Phase 2c batch")
        self._start_tagger_phase2c_batch(target_paths, settings)

    def _start_tagger_phase2c_batch(
        self,
        target_paths: list[Path],
        settings: dict[str, object],
    ) -> None:
        """常駐子プロセスへ、緑チェック中の全画像の逐次推論を依頼する。"""
        if self._tagger_force_cancel_in_progress:
            return
        if not target_paths:
            self._fail_tagger_phase2c_batch("Tagger実行対象がありません。")
            return

        self._clear_pending_tagger_thumbnail_status_updates()
        self.tagger_session.start(target_paths)
        self._tagger_process_batch_paths = list(target_paths)
        self.tagger_panel.set_running(True)
        self.tagger_panel.set_status_text(f"Tagger実行準備中... 0/{len(target_paths)}")
        self._refresh_tagger_thumbnail_statuses()
        self._refresh_tagger_current_result()

        request_id = self._tagger_process_client.send_predict_batch(target_paths, settings)
        if request_id is None:
            self._fail_tagger_phase2c_batch(
                "Tagger子プロセスへ推論を依頼できませんでした。"
            )
            return

        self._tagger_process_batch_request_id = request_id
        log.info(
            "Tagger Phase 2c batch sent request_id=%d images=%d",
            request_id,
            len(target_paths),
        )

    def _is_current_tagger_batch_message(self, message: dict[str, object]) -> bool:
        return message.get("request_id") == self._tagger_process_batch_request_id

    def _on_tagger_process_batch_started(self, message: dict[str, object]) -> None:
        if not self._is_current_tagger_batch_message(message):
            return
        if self._tagger_force_cancel_in_progress:
            return

        total = int(message.get("total", len(self._tagger_process_batch_paths)))
        device = str(message.get("device", "?"))
        reused = bool(message.get("backend_reused", False))
        reuse_text = "再利用" if reused else "読込"
        self.tagger_panel.set_status_text(f"Tagger実行中... 0/{total}（{reuse_text} / {device}）")
        log.info(
            "Tagger Phase 2c batch started request_id=%s images=%d reused=%s device=%s",
            message.get("request_id"),
            total,
            reused,
            device,
        )

    def _on_tagger_process_batch_item_result(self, message: dict[str, object]) -> None:
        if not self._is_current_tagger_batch_message(message):
            return

        path = Path(str(message.get("path", "")))
        if path not in self.tagger_session.target_paths:
            log.warning("Tagger Phase 2c received result for non-target path: %s", path)
            return

        try:
            result = self._tagger_result_from_process_message(message)
        except (TypeError, ValueError) as exc:
            self.tagger_session.set_error(path, f"Tagger子プロセスの結果を読み取れませんでした。\n{exc}")
        else:
            if result.all_candidates:
                self.tagger_session.set_result(path, result)
            else:
                self.tagger_session.set_no_result(path)

        index = self._index_for_image_path(path)
        if index is not None:
            self._queue_tagger_thumbnail_status_update(index)
            if index == self._tagger_current_index:
                self._refresh_tagger_current_result()

    def _on_tagger_process_batch_item_error(self, message: dict[str, object]) -> None:
        if not self._is_current_tagger_batch_message(message):
            return

        path = Path(str(message.get("path", "")))
        if path not in self.tagger_session.target_paths:
            log.warning("Tagger Phase 2c received error for non-target path: %s", path)
            return

        detail = str(message.get("message", "Tagger子プロセスで推論エラーが発生しました。"))
        self.tagger_session.set_error(path, detail)

        index = self._index_for_image_path(path)
        if index is not None:
            self._queue_tagger_thumbnail_status_update(index)
            if index == self._tagger_current_index:
                self._refresh_tagger_current_result()

    def _on_tagger_process_batch_progress(self, message: dict[str, object]) -> None:
        if not self._is_current_tagger_batch_message(message):
            return
        if self._tagger_force_cancel_in_progress:
            return

        completed = int(message.get("completed", 0))
        total = int(message.get("total", len(self._tagger_process_batch_paths)))
        image_name = str(message.get("image_name", ""))
        if completed >= total:
            self.tagger_panel.set_status_text(f"Tagger実行完了中... {total}/{total}")
        else:
            self.tagger_panel.set_status_text(f"Tagger実行中... {completed}/{total} : {image_name}")

    def _on_tagger_process_batch_finished(self, message: dict[str, object]) -> None:
        if not self._is_current_tagger_batch_message(message):
            return
        if self._tagger_force_cancel_in_progress:
            return

        summary = message.get("summary")
        if not isinstance(summary, dict):
            self._fail_tagger_phase2c_batch(
                "Tagger子プロセスから完了結果を読み取れませんでした。"
            )
            return

        total = int(summary.get("total", len(self._tagger_process_batch_paths)))
        generated = int(summary.get("generated", 0))
        no_result = int(summary.get("no_result", 0))
        error_count = int(summary.get("error", 0))

        self._tagger_process_batch_request_id = None
        self._tagger_process_batch_paths = []
        self._flush_tagger_thumbnail_status_updates()
        self._refresh_tagger_current_result()
        self.tagger_panel.set_running(False)
        self._set_tagger_execution_locked(False)
        self.tagger_panel.set_status_text(
            f"Tagger完了: 生成 {generated}件 / 候補なし {no_result}件 / "
            f"エラー {error_count}件 (全{total}件)"
        )
        log.info(
            "Tagger Phase 2c batch finished total=%d generated=%d no_result=%d error=%d "
            "reused=%s device=%s",
            total,
            generated,
            no_result,
            error_count,
            bool(message.get("backend_reused", False)),
            message.get("device", "?"),
        )
        summary_message = (
            "Tagger候補の生成が完了しました。生成 %d件 / 候補なし %d件 / エラー %d件"
        )
        if error_count:
            log_user_warning(summary_message, generated, no_result, error_count)
        else:
            log_user_ok(summary_message, generated, no_result, error_count)

    def _on_tagger_process_batch_failed(self, message: dict[str, object]) -> None:
        if not self._is_current_tagger_batch_message(message):
            return
        if self._tagger_force_cancel_in_progress:
            return

        detail = str(message.get("message", "Tagger子プロセスで推論エラーが発生しました。"))
        self._fail_tagger_phase2c_batch(detail)

    @staticmethod
    def _tagger_result_from_process_message(message: dict[str, object]) -> TaggerResult:
        """JSON形式の子プロセス結果を、既存UI用TaggerResultへ復元する。"""
        result_data = message.get("result")
        if not isinstance(result_data, dict):
            raise ValueError("result がJSONオブジェクトではありません。")

        path_text = str(message.get("path", "")).strip()
        if not path_text:
            raise ValueError("結果画像パスがありません。")
        path = Path(path_text)

        def decode_candidates(key: str, default_category: str) -> list[TagCandidate]:
            raw_candidates = result_data.get(key, [])
            if not isinstance(raw_candidates, list):
                raise ValueError(f"{key} が配列ではありません。")

            candidates: list[TagCandidate] = []
            for raw_candidate in raw_candidates:
                if not isinstance(raw_candidate, dict):
                    raise ValueError(f"{key} に不正な候補があります。")

                tag = " ".join(str(raw_candidate.get("tag", "")).split())
                if not tag:
                    raise ValueError(f"{key} に空のタグがあります。")

                try:
                    score = float(raw_candidate.get("score"))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} のスコアが不正です。") from exc

                category = str(raw_candidate.get("category", default_category))
                raw_source_tags = raw_candidate.get("source_tags", [])
                if not isinstance(raw_source_tags, list):
                    raise ValueError(f"{key} のsource_tagsが配列ではありません。")

                source_tags: list[str] = []
                for raw_source_tag in raw_source_tags:
                    source_tag = " ".join(str(raw_source_tag).split())
                    if source_tag and source_tag not in source_tags:
                        source_tags.append(source_tag)

                candidates.append(
                    TagCandidate(
                        tag,
                        score,
                        category,
                        tuple(source_tags),
                    )
                )

            return candidates

        return TaggerResult(
            image_path=path,
            general_candidates=decode_candidates("general_candidates", "general"),
            character_candidates=decode_candidates("character_candidates", "character"),
            copyright_candidates=decode_candidates("copyright_candidates", "copyright"),
        )

    def _on_tagger_process_failed(self, message: str) -> None:
        """子プロセス起動・通信失敗時、現在のTagger操作だけを安全に解除する。"""
        if self._tagger_force_cancel_in_progress:
            return
        log.warning("Tagger subprocess failure reported: %s", message.replace("\n", " | "))
        if self._tagger_pending_run is not None:
            self._fail_pending_tagger_process_start(message)
            return

        if self._tagger_process_batch_request_id is not None:
            self._fail_tagger_phase2c_batch(message)
            return

        self.tagger_panel.set_status_text(
            "Tagger子プロセスが終了しました。次回実行時に再起動します。"
        )

    def _on_tagger_process_exited(self, exit_code: int, exit_status: int) -> None:
        log.info(
            "Tagger subprocess exit observed exit_code=%d exit_status=%d",
            exit_code,
            exit_status,
        )

        if self._tagger_force_cancel_in_progress:
            self._finish_tagger_force_cancel()
            return

        # TaggerProcessClient は通常 failed も通知するが、終了通知だけが先に届く
        # 経路でもUIロックを残さないため、次のイベントループで保険をかける。
        QTimer.singleShot(
            0,
            lambda: self._recover_from_unexpected_tagger_process_exit(
                exit_code,
                exit_status,
            ),
        )

    def _recover_from_unexpected_tagger_process_exit(
        self,
        exit_code: int,
        exit_status: int,
    ) -> None:
        """failed通知が届かなかった子プロセス終了でも、保留中のTagger状態を解除する。"""
        if self._tagger_pending_run is not None:
            self._fail_pending_tagger_process_start(
                "Tagger子プロセスが起動確認中に終了しました。\n"
                f"終了コード: {exit_code}"
            )
            return

        if self._tagger_process_batch_request_id is not None:
            self._fail_tagger_phase2c_batch(
                "Tagger子プロセスが実行中に終了しました。\n"
                f"終了コード: {exit_code}"
            )

    def _fail_pending_tagger_process_start(self, message: str) -> None:
        """子プロセスのready/ping失敗時にTaggerタブだけを実行前状態へ戻す。"""
        self._tagger_pending_run = None
        self._tagger_process_ping_request_id = None
        self.tagger_panel.set_running(False)
        self._set_tagger_execution_locked(False)
        self.tagger_panel.set_status_text("Tagger子プロセスを起動できませんでした。")
        log_user_error(
            "Tagger子プロセスを起動できませんでした。詳細はダイアログとログを確認してください。"
        )
        QMessageBox.warning(self, "Tagger", message)

    def _fail_tagger_phase2c_batch(self, message: str) -> None:
        """バッチ途中の異常を、未処理の対象画像へエラーとして反映して解除する。"""
        pending_paths = self.tagger_session.pending_paths()
        for path in pending_paths:
            self.tagger_session.set_error(path, message)
            index = self._index_for_image_path(path)
            if index is not None:
                self._queue_tagger_thumbnail_status_update(index)

        self._tagger_process_batch_request_id = None
        self._tagger_process_batch_paths = []
        self._flush_tagger_thumbnail_status_updates()
        self._refresh_tagger_current_result()
        self.tagger_panel.set_running(False)
        self._set_tagger_execution_locked(False)
        if pending_paths:
            self.tagger_panel.set_status_text(
                f"Tagger実行でエラーが発生しました。未処理 {len(pending_paths)}件をエラー扱いにしました。"
            )
        else:
            self.tagger_panel.set_status_text("Tagger実行でエラーが発生しました。")
        log_user_error(
            "Taggerの実行に失敗しました。詳細はダイアログとログを確認してください。"
        )
        QMessageBox.warning(self, "Tagger", message)


    def _cancel_tagger_stage1(self) -> None:
        """実行中のTagger子プロセスを停止し、未処理画像を中止扱いにする。"""
        if not self._tagger_execution_is_active() or self._tagger_force_cancel_in_progress:
            return

        self._tagger_force_cancel_in_progress = True
        self.tagger_panel.set_cancelling(True)
        self.tagger_panel.set_status_text("Taggerを中止しています...")
        log_user_info("Taggerを中止しています...")

        if self._tagger_process_client.abort_current_run():
            return

        # 起動完了前など、既に子プロセスが終了している経路でもUIロックを残さない。
        QTimer.singleShot(0, self._finish_tagger_force_cancel)

    def _finish_tagger_force_cancel(self) -> None:
        """子プロセス停止後、届き済み結果を残して未処理画像だけを中止へ確定する。"""
        if not self._tagger_force_cancel_in_progress:
            return

        # 起動確認中に中止された場合は、まだTaggerSessionが開始されていない。
        # その場合だけ、実行予定だった画像群をセッションへ入れて全件を中止表示にする。
        if not self.tagger_session.active:
            if self._tagger_process_batch_paths:
                self.tagger_session.start(self._tagger_process_batch_paths)
            elif self._tagger_pending_run is not None:
                pending_paths, _settings = self._tagger_pending_run
                self.tagger_session.start(pending_paths)

        pending_paths = sorted(
            self.tagger_session.pending_paths(),
            key=lambda path: str(path).casefold(),
        )
        for path in pending_paths:
            self.tagger_session.set_cancelled(path)
            index = self._index_for_image_path(path)
            if index is not None:
                self._queue_tagger_thumbnail_status_update(index)

        self._tagger_pending_run = None
        self._tagger_process_ping_request_id = None
        self._tagger_process_batch_request_id = None
        self._tagger_process_batch_paths = []
        self._tagger_force_cancel_in_progress = False

        self._flush_tagger_thumbnail_status_updates()
        self._refresh_tagger_current_result()
        self.tagger_panel.set_running(False)
        self._set_tagger_execution_locked(False)

        if pending_paths:
            self.tagger_panel.set_status_text(
                f"Taggerを中止しました。未処理 {len(pending_paths)}件を中止扱いにしました。"
            )
        else:
            self.tagger_panel.set_status_text("Taggerを中止しました。")

        log.info("Tagger force-cancel finished pending=%d", len(pending_paths))
        if pending_paths:
            log_user_info("Taggerを中止しました。未処理 %d件を中止扱いにしました。", len(pending_paths))
        else:
            log_user_info("Taggerを中止しました。")

    def _reset_tagger_stage1(self) -> None:
        if self._tagger_execution_is_active():
            QMessageBox.information(self, "Tagger", "Tagger実行中はリセットできません。")
            return

        self._clear_pending_tagger_thumbnail_status_updates()
        self.tagger_session.clear()
        self.thumbnail_panel.clear_tagger_selection()
        self._refresh_tagger_thumbnail_statuses()
        self._refresh_tagger_current_result()
        self.tagger_panel.set_status_text("Tagger結果と実行対象をリセットしました。")
        log_user_info("Tagger結果と実行対象をリセットしました。")

    def _apply_tagger_stage1(self, apply_mode: str) -> None:
        """今回のTagger実行で候補が得られた画像へ、候補タグを一括反映する。

        ここでは ImageItem.tags だけを書き換える。実際のtxt保存は既存の保存操作で行う。
        """
        if self._tagger_execution_is_active():
            QMessageBox.information(self, "Tagger反映", "Tagger実行中は反映できません。")
            return

        if apply_mode not in {"empty_only", "overwrite", "append"}:
            QMessageBox.warning(self, "Tagger反映", "反映方法が不明です。")
            return

        candidate_entries: list[tuple[ImageItem, list[str]]] = []
        for item in self.items:
            result = self.tagger_session.result_for_path(item.image_path)
            if result is None:
                continue

            tags = self._unique_tagger_output_tags(
                result.output_tags(
                    include_character=True,
                    include_copyright=True,
                )
            )
            if tags:
                candidate_entries.append((item, tags))

        if not candidate_entries:
            QMessageBox.information(
                self,
                "Tagger反映",
                "今回のTagger実行結果に、反映できる候補タグはありません。",
            )
            return

        skipped_existing_count = 0
        if apply_mode == "empty_only":
            target_entries = [
                (item, tags)
                for item, tags in candidate_entries
                if not item.tags
            ]
            skipped_existing_count = len(candidate_entries) - len(target_entries)
            mode_text = "タグが空の場合のみ"
        elif apply_mode == "overwrite":
            target_entries = candidate_entries
            mode_text = "上書き"
        else:
            # 既にある同名タグは追加せず、候補だけを既存txtの末尾へ足す。
            target_entries = [
                (
                    item,
                    [
                        *item.tags,
                        *(tag for tag in tags if tag not in item.tags),
                    ],
                )
                for item, tags in candidate_entries
            ]
            mode_text = "既存txtの末尾に追加"

        changed_entries = [
            (item, next_tags)
            for item, next_tags in target_entries
            if list(item.tags) != list(next_tags)
        ]
        unchanged_count = len(target_entries) - len(changed_entries)

        if not changed_entries:
            informative_lines = [
                "反映対象に、変更が必要な画像はありません。",
                f"候補あり: {len(candidate_entries)}件",
            ]
            if skipped_existing_count:
                informative_lines.append(
                    f"既存タグありのため対象外: {skipped_existing_count}件"
                )
            if unchanged_count:
                informative_lines.append(
                    f"候補が現在タグと同一: {unchanged_count}件"
                )
            QMessageBox.information(
                self,
                "Tagger反映",
                "\n".join(informative_lines),
            )
            return

        if not self._confirm_tagger_apply(
            mode_text=mode_text,
            candidate_count=len(candidate_entries),
            changed_entries=changed_entries,
            skipped_existing_count=skipped_existing_count,
            unchanged_count=unchanged_count,
        ):
            return

        before_history = {
            item.image_path: list(item.tags)
            for item, _tags in changed_entries
        }
        for item, tags in changed_entries:
            item.tags = list(tags)
            item.refresh_dirty()

        self.tag_panel.record_tagger_apply_history(
            before_history,
            apply_mode=apply_mode,
        )
        self.tag_panel.refresh_current_tags(reason="tagger_apply")
        self.tag_panel.refresh_all_tags()
        self.tag_panel.refresh_target_label()
        self._on_tags_changed()
        self._update_preview_placeholder_state()

        self.tagger_panel.set_status_text(
            f"Tagger候補を反映しました: {len(changed_entries)}件（未保存）"
        )
        log_user_ok("Tagger候補を反映しました: %d件（未保存）", len(changed_entries))

    @staticmethod
    def _unique_tagger_output_tags(tags: list[str]) -> list[str]:
        """Tagger候補を空白表記・順序維持・重複なしのタグ列へ整える。"""
        normalized_tags: list[str] = []
        seen: set[str] = set()

        for raw_tag in tags:
            # backend側でも変換済みだが、将来別backendが候補を返しても
            # 反映結果とtxtの表記を統一できるよう、ここでも最終保証する。
            tag = " ".join(str(raw_tag).replace("_", " ").split())
            if not tag or tag in seen:
                continue
            normalized_tags.append(tag)
            seen.add(tag)

        return normalized_tags

    def _confirm_tagger_apply(
        self,
        *,
        mode_text: str,
        candidate_count: int,
        changed_entries: list[tuple[ImageItem, list[str]]],
        skipped_existing_count: int,
        unchanged_count: int,
    ) -> bool:
        """Tagger候補の一括反映前に、対象件数と未保存扱いを確認する。"""
        visible_limit = 10
        names = "\n".join(
            f"- {item.image_path.name}" for item, _tags in changed_entries[:visible_limit]
        )
        if len(changed_entries) > visible_limit:
            names += f"\n...ほか {len(changed_entries) - visible_limit}件"

        lines = [
            "今回のTagger実行結果を、アプリ内のタグへ反映します。",
            "",
            f"反映方法: {mode_text}",
            f"候補あり: {candidate_count}件",
            f"反映される画像: {len(changed_entries)}件",
        ]
        if skipped_existing_count:
            lines.append(
                f"既存タグありのため対象外: {skipped_existing_count}件"
            )
        if unchanged_count:
            lines.append(f"候補が現在タグと同一: {unchanged_count}件")

        lines.extend(
            [
                "",
                "反映対象:",
                names,
                "",
                "この時点ではtxtファイルへ保存しません。",
                "既存の保存ボタンを押すと、反映内容がtxtへ書き込まれます。",
            ]
        )

        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Question)
        message.setWindowTitle("Tagger候補を反映")
        message.setText("Tagger候補を反映しますか？")
        message.setInformativeText("\n".join(lines))

        apply_button = message.addButton("反映", QMessageBox.ButtonRole.AcceptRole)
        cancel_button = message.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        message.setDefaultButton(cancel_button)
        message.exec()

        return message.clickedButton() == apply_button

    def save_all(self) -> None:
        if self._tagger_execution_is_active():
            return
        dirty_items = [item for item in self.items if item.dirty]
        log.info("save_all requested dirty_count=%d", len(dirty_items))
        if not self.items:
            return

        # 通常は未保存変更のあるtxtだけ保存する。
        # 変更がない状態でsaveされた場合は、一般的なエディタの挙動に寄せて
        # 読み込み中の全txtを強制保存する。成功時メッセージは出さない。
        save_targets = dirty_items if dirty_items else list(self.items)

        externally_changed_items = [
            item for item in save_targets if self._is_tag_file_externally_changed(item)
        ]

        if externally_changed_items:
            action = self._ask_external_tag_file_action(externally_changed_items)
            if action == "cancel":
                return
            if action == "reload":
                self._reload_external_tag_files(externally_changed_items)
                return

            # action == "overwrite" の場合は、アプリ側の内容で保存を続行する。

        if dirty_items:
            log_user_info("タグを保存しています...（%d件）", len(dirty_items))
        else:
            log_user_info("タグを保存しています...")

        for item in save_targets:
            saved_mtime_ns = save_tags(item.tag_path, item.tags)
            item.mark_saved(saved_mtime_ns)

        log_user_ok("%d件のタグファイルを保存しました。", len(save_targets))

    def _is_tag_file_externally_changed(self, item: ImageItem) -> bool:
        """読み込み/保存時点のmtimeと現在のmtimeを比較し、外部変更を検知する。"""
        return tag_mtime_ns(item.tag_path) != item.tag_mtime_ns

    def _ask_external_tag_file_action(self, items: list[ImageItem]) -> str:
        """外部変更検知時の保存方針をユーザーに確認する。"""
        visible_limit = 10
        names = "\n".join(f"- {item.tag_path.name}" for item in items[:visible_limit])
        if len(items) > visible_limit:
            names += f"\n...ほか {len(items) - visible_limit}件"

        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle("外部変更の検知")
        message.setText(f"保存対象のtxt {len(items)}件が、外部で変更されています。")
        message.setInformativeText(
            "アプリ側の内容で保存すると、外部エディタでの変更を上書きする可能性があります。\n\n"
            f"{names}\n\n"
            "どうしますか？"
        )

        overwrite_button = message.addButton("上書き", QMessageBox.ButtonRole.AcceptRole)
        reload_button = message.addButton("再読み込み", QMessageBox.ButtonRole.ActionRole)
        cancel_button = message.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        message.setDefaultButton(cancel_button)

        message.exec()
        clicked_button = message.clickedButton()

        if clicked_button == overwrite_button:
            return "overwrite"
        if clicked_button == reload_button:
            return "reload"
        return "cancel"

    def _reload_external_tag_files(self, items: list[ImageItem]) -> None:
        """外部変更されたtxtを再読み込みし、該当itemの未保存変更を破棄する。"""
        reloaded_tag_paths = [item.tag_path for item in items]

        for item in items:
            item.tags = load_tags(item.tag_path)
            item.mark_saved(tag_mtime_ns(item.tag_path))

        self.tag_panel.invalidate_history_for_tag_paths(reloaded_tag_paths)
        self.tag_panel.refresh_current_tags(reason="external_reload")
        self.tag_panel.refresh_all_tags()
        self.tag_panel.refresh_target_label()
        self._on_tags_changed()
        self._update_preview_placeholder_state()
        log_user_ok("外部変更されたタグを再読み込みしました: %d件", len(items))

    def open_image_folder_for_index(self, index: int) -> None:
        """右クリックしたサムネイル画像をExplorer上で選択表示する。"""
        if not (0 <= index < len(self.items)):
            return
        path = self.items[index].image_path
        try:
            used_method = open_folder_and_select_file(path)
            log.info("画像フォルダを開く: %s method=%s", path, used_method)
        except (OSError, ValueError) as exc:
            log.warning("画像フォルダを開けませんでした: %s (%s)", path, exc)
            QMessageBox.warning(self, "画像フォルダを開く", f"画像を選択して開けませんでした。\n{path}\n\n{exc}")

    def open_tag_file_for_index(self, index: int) -> None:
        """サムネイル右クリックから、対応するtxtを既定アプリで開く。"""
        if not (0 <= index < len(self.items)):
            return

        item = self.items[index]
        tag_path = item.tag_path

        if not tag_path.exists():
            QMessageBox.information(
                self,
                "対応txtを開く",
                f"対応するtxtファイルがありません。\n{tag_path.name}",
            )
            return

        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(tag_path))):
            QMessageBox.warning(
                self,
                "対応txtを開く",
                f"txtファイルを開けませんでした。\n{tag_path}",
            )

    def delete_checked_images_to_trash(self) -> None:
        """チェック中の画像と対応するtxtを、確認後にOSのゴミ箱へ移動する。"""
        if self._tagger_execution_is_active():
            return
        target_indexes = self.thumbnail_panel.checked_indexes()
        if not target_indexes:
            QMessageBox.information(self, "画像を削除", "削除する画像にチェックを入れてください。")
            return

        target_items = [
            self.items[index]
            for index in target_indexes
            if 0 <= index < len(self.items)
        ]
        existing_images = [item.image_path for item in target_items if item.image_path.exists()]
        existing_txts = [item.tag_path for item in target_items if item.tag_path.exists()]
        dirty_count = len([item for item in target_items if item.dirty])

        if not existing_images:
            QMessageBox.warning(self, "画像を削除", "削除対象の画像ファイルが見つかりません。")
            return

        lines = [
            "チェック中の画像と対応するtxtをゴミ箱へ移動します。",
            "",
            f"画像：{len(existing_images)}件",
            f"対応txt：{len(existing_txts)}件",
        ]
        if dirty_count > 0:
            lines.extend([
                "",
                f"注意：未保存のタグ変更を含む画像が {dirty_count}件あります。",
                "削除すると、その未保存の変更も破棄されます。",
            ])

        lines.extend(["", "対象画像："])
        max_visible_names = 10
        for path in existing_images[:max_visible_names]:
            lines.append(f"- {path.name}")
        if len(existing_images) > max_visible_names:
            lines.append(f"...ほか {len(existing_images) - max_visible_names}件")
        lines.extend(["", "よろしいですか？"])

        result = QMessageBox.warning(
            self,
            "画像を削除",
            "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            return

        if not hasattr(QFile, "moveToTrash") or not QFile.supportsMoveToTrash():
            log_user_error("この環境ではゴミ箱への移動を利用できません。")
            QMessageBox.warning(
                self,
                "画像を削除",
                "この環境ではゴミ箱への移動がサポートされていません。削除は行いませんでした。",
            )
            return

        log_user_info("画像をゴミ箱へ移動しています...（%d枚）", len(existing_images))
        old_current_item = (
            self.items[self.current_index]
            if 0 <= self.current_index < len(self.items)
            else None
        )
        old_current_index = self.current_index
        moved_indexes: list[int] = []
        failed_paths: list[Path] = []

        for index in target_indexes:
            if not (0 <= index < len(self.items)):
                continue

            item = self.items[index]
            if not item.image_path.exists():
                failed_paths.append(item.image_path)
                continue

            if not QFile.moveToTrash(str(item.image_path)):
                failed_paths.append(item.image_path)
                continue

            moved_indexes.append(index)

            # 画像の移動に成功した場合だけ、対応txtも一緒にゴミ箱へ送る。
            if item.tag_path.exists() and not QFile.moveToTrash(str(item.tag_path)):
                failed_paths.append(item.tag_path)

        if not moved_indexes:
            log_user_error("画像をゴミ箱へ移動できませんでした。")
            QMessageBox.warning(self, "画像を削除", "画像をゴミ箱へ移動できませんでした。")
            return

        # 各パネルが共有している list オブジェクト自体は維持し、中身だけ除去する。
        for index in sorted(moved_indexes, reverse=True):
            del self.items[index]

        include_tags, exclude_tags = self.tag_panel.current_tag_filters()
        self.thumbnail_panel.set_items(self.items, self.folder, recursive=self._current_recursive_load, recursive_depth=self._current_recursive_depth)
        self.thumbnail_panel.set_tag_filters(include_tags, exclude_tags)
        self.tag_panel.refresh_all_tags()
        self._clear_pending_tagger_thumbnail_status_updates()
        self.tagger_session.clear()
        self.thumbnail_panel.clear_tagger_selection()
        # 画像削除でTaggerセッションを破棄した場合も、前回実行の完了表示を残さない。
        self.tagger_panel.set_status_text("Tagger未実行")
        self._tagger_current_index = -1
        self._refresh_tagger_thumbnail_statuses()
        self._refresh_tagger_current_result()

        visible_indexes = self.thumbnail_panel.visible_indexes()
        if visible_indexes:
            if old_current_item in self.items:
                candidate_index = self.items.index(old_current_item)
            else:
                candidate_index = min(max(old_current_index, 0), len(self.items) - 1)

            next_index = min(visible_indexes, key=lambda index: abs(index - candidate_index))
            if self._tagger_tab_is_active():
                self.select_tagger_image(next_index)
            else:
                self.select_image(next_index)
        else:
            self.current_index = -1
            self.thumbnail_panel.clear_selection()
            self.preview_panel.set_image(None)
            self.tag_panel.set_current_index(-1)
            self._update_preview_placeholder_state()

        moved_image_count = len(moved_indexes)
        if failed_paths:
            failed_text = "\n".join(f"- {path.name}" for path in failed_paths[:10])
            if len(failed_paths) > 10:
                failed_text += f"\n...ほか {len(failed_paths) - 10}件"
            QMessageBox.warning(
                self,
                "画像を削除",
                f"画像 {moved_image_count}件をゴミ箱へ移動しました。\n\n"
                f"ただし、以下のファイルは移動できませんでした。\n{failed_text}",
            )
        else:
            QMessageBox.information(
                self,
                "画像を削除",
                f"画像 {moved_image_count}件と対応するtxtをゴミ箱へ移動しました。",
            )

        if failed_paths:
            log_user_warning(
                "画像 %d件をゴミ箱へ移動しました。移動できなかったファイルがあります。",
                moved_image_count,
            )
        else:
            log_user_ok("画像 %d件と対応するtxtをゴミ箱へ移動しました。", moved_image_count)

    def _on_checked_work_mode_changed(self, enabled: bool, work_set_paths: set[Path]) -> None:
        log.info("checked work mode changed enabled=%s work_set_count=%d", enabled, len(work_set_paths))
        self.tag_panel.set_checked_work_mode(enabled, work_set_paths)
        self._select_first_visible_if_current_hidden()
        self._update_preview_placeholder_state()

    def _on_checks_changed(self) -> None:
        """
        チェック変更通知を短時間だけまとめる。

        Qt側の itemChanged や、フィルタ適用時の自動チェック解除などで、
        実質1操作なのに checks_changed が複数回届くことがある。
        ここでは重いタグ再集計を直接実行せず、タイマーで最後に1回だけ処理する。
        """
        if self._checks_changed_pending_count == 0:
            self._checks_changed_first_started = time.perf_counter()

        self._checks_changed_pending_count += 1

        if dev_log_enabled("UI_INTERACTION_DETAIL"):
            log.debug(
                "checks_changed queued pending_count=%d checked_count=%d",
                self._checks_changed_pending_count,
                self._checked_count(),
            )

        # 連続通知が来た場合は、最後の通知から30ms後に1回だけ処理する。
        self._checks_changed_timer.start()

    def _process_checks_changed(self) -> None:
        perf_enabled = dev_log_enabled("PERF_FILTER") or dev_log_enabled("UI_INTERACTION_DETAIL")
        perf_started = time.perf_counter() if perf_enabled else 0.0
        first_started = self._checks_changed_first_started
        pending_count = self._checks_changed_pending_count

        self._checks_changed_pending_count = 0
        self._checks_changed_first_started = 0.0

        checked_count = self._checked_count()

        log.info(
            "checks_changed processed checked_count=%d coalesced_count=%d",
            checked_count,
            pending_count,
        )

        if perf_enabled:
            current_tags_started = time.perf_counter()
        self.tag_panel.refresh_current_tags(reason="checks_changed")
        current_tags_ms = (time.perf_counter() - current_tags_started) * 1000 if perf_enabled else 0.0

        if perf_enabled:
            all_tags_started = time.perf_counter()
        self.tag_panel.refresh_all_tags_for_check_change()
        all_tags_ms = (time.perf_counter() - all_tags_started) * 1000 if perf_enabled else 0.0

        if perf_enabled:
            label_started = time.perf_counter()
        self.tag_panel.refresh_target_label()
        label_ms = (time.perf_counter() - label_started) * 1000 if perf_enabled else 0.0

        if perf_enabled:
            visible_started = time.perf_counter()
        self._select_first_visible_if_current_hidden()
        visible_ms = (time.perf_counter() - visible_started) * 1000 if perf_enabled else 0.0

        if perf_enabled:
            preview_started = time.perf_counter()
        self._update_preview_placeholder_state()
        preview_ms = (time.perf_counter() - preview_started) * 1000 if perf_enabled else 0.0

        if perf_enabled:
            wait_ms = (
                (perf_started - first_started) * 1000
                if first_started
                else 0.0
            )
            log.info(
                "[PERF] app._process_checks_changed checked_count=%d coalesced_count=%d "
                "wait=%.2fms refresh_current=%.2fms refresh_all_for_check=%.2fms "
                "label=%.2fms visible_select=%.2fms preview_state=%.2fms total=%.2fms",
                checked_count,
                pending_count,
                wait_ms,
                current_tags_ms,
                all_tags_ms,
                label_ms,
                visible_ms,
                preview_ms,
                (time.perf_counter() - perf_started) * 1000,
            )

    def _on_single_target_override_changed(self, active: bool) -> None:
        """単体編集中は、黄色チェック画像の一覧プレビューを表示しない。"""
        if active:
            self._checked_hover_preview_active = False
            self._hover_preview_restore_timer.stop()

        self._update_preview_placeholder_state()

    def _on_tag_panel_hover_changed(self, hovered: bool) -> None:
        if self._lightbox_active or self._tagger_tab_is_active():
            return

        # tag_panel.py 側で、左側のチェック対象タグ欄に限定して通知される。
        if dev_log_enabled("HOVER_PREVIEW"):
            log.debug("current tag area hover_changed=%s checked_count=%d", hovered, self._checked_count())
        self._tag_panel_hovered = hovered

        if hovered:
            self._hover_preview_restore_timer.stop()

            if self.tag_panel.is_single_target_override_active():
                self._checked_hover_preview_active = False
                self.preview_panel.restore_current_image()
                return

            self._checked_hover_preview_active = True
            self._update_preview_placeholder_state()
            return

        self._schedule_hover_preview_restore()

    def _on_whole_tag_panel_hover_changed(self, hovered: bool) -> None:
        if self._lightbox_active or self._tagger_tab_is_active():
            return

        """
        左側タグ欄でいったん発動した操作対象プレビューは、
        右側タグ一覧を含むタグパネル全体へ移動しても維持する。

        ただし、右側へ直接入った場合はここでは新規発動しない。
        """
        self._whole_tag_panel_hovered = hovered

        if not self._checked_hover_preview_active:
            return

        if hovered:
            self._hover_preview_restore_timer.stop()
        else:
            self._schedule_hover_preview_restore()

    def _on_preview_panel_hover_changed(self, hovered: bool) -> None:
        if self._lightbox_active or self._tagger_tab_is_active():
            return

        """
        左側タグ欄から発動した操作対象プレビューは、プレビュー領域へ
        カーソルを移した場合も維持する。これにより一覧をスクロールできる。
        """
        self._preview_panel_hovered = hovered

        if not self._checked_hover_preview_active:
            return

        if hovered:
            self._hover_preview_restore_timer.stop()
        else:
            self._schedule_hover_preview_restore()

    def _tag_panel_modal_dialog_active(self) -> bool:
        """TagPanelを親階層に持つモーダルダイアログが開いているか返す。"""
        modal = QApplication.activeModalWidget()

        while modal is not None:
            if modal is self.tag_panel:
                return True
            modal = modal.parentWidget()

        return False

    def _hover_preview_cursor_near_transfer_boundary(self) -> bool:
        """パネル間移動の短い隙間か、入場イベント未到着の隣接パネル上かを判定する。"""
        cursor = QCursor.pos()
        # 12pxだけ外周を広げ、イベントの届く順序が前後する領域も吸収する。
        # 大きなサムネイル領域・画面外への移動では待機しない。
        for panel in (self.tag_panel, self.preview_panel):
            if panel.isVisible() and panel.rect().adjusted(-12, -12, 12, 12).contains(
                panel.mapFromGlobal(cursor)
            ):
                return True
        return False

    def _schedule_hover_preview_restore(self) -> None:
        if not self._checked_hover_preview_active:
            return
        if (
            self._tag_panel_hovered
            or self._whole_tag_panel_hovered
            or self._preview_panel_hovered
            or self._tag_panel_modal_dialog_active()
        ):
            return

        if self._hover_preview_cursor_near_transfer_boundary():
            # タグ欄→プレビュー欄の間にある隙間を通過する間だけ猶予を残す。
            # モーダル表示中の250msリトライでQTimerのintervalが変わっても、
            # 通常の補助猶予は毎回70msで開始する。
            self._hover_preview_restore_timer.start(70)
        else:
            # サムネイル／その他の領域へ出たなら、250msの待ちを入れずに復帰。
            self._hover_preview_restore_timer.stop()
            self._finish_hover_preview_if_outside()

    def _finish_hover_preview_if_outside(self) -> None:
        if self._lightbox_active:
            return

        if self._tag_panel_modal_dialog_active():
            # 追加・編集・履歴などのTagPanel配下ダイアログを開くと、
            # マウスは一時的にTagPanel外へ出た扱いになる。
            # すでに発動中の操作対象プレビューは維持する。
            # モーダル中は頻繁に再確認する必要がないため、低頻度で待つ。
            self._hover_preview_restore_timer.start(250)
            return

        if (
            self._tag_panel_hovered
            or self._whole_tag_panel_hovered
            or self._preview_panel_hovered
        ):
            return

        self._checked_hover_preview_active = False
        self.preview_panel.restore_current_image()

    def _checked_items(self) -> list[ImageItem]:
        return [item for item in self.items if item.checked]

    def _checked_count(self) -> int:
        return len(self._checked_items())

    def _update_preview_placeholder_state(self) -> None:
        # Taggerタブ中は編集用のチェック対象プレビューを出さない。
        if self._tagger_tab_is_active():
            return

        # 黄色チェックを保持した一時単体編集中は、表示と実際の操作対象を揃えるため、
        # 左側タグ欄へホバーしてもチェック画像一覧を発動しない。
        if self.tag_panel.is_single_target_override_active():
            self._checked_hover_preview_active = False
            self._hover_preview_restore_timer.stop()
            self.preview_panel.restore_current_image()
            return

        checked_items = self._checked_items()

        if not checked_items:
            self._checked_hover_preview_active = False
            self._hover_preview_restore_timer.stop()
            self.preview_panel.restore_current_image()
            return

        if not self._checked_hover_preview_active:
            self.preview_panel.restore_current_image()
            return

        if len(checked_items) == 1:
            checked_item = checked_items[0]
            self.preview_panel.set_temporary_image(
                checked_item.image_path,
                f"操作対象プレビュー：チェック中画像 1件　{checked_item.image_path.name}",
            )
            return

        current_path = None
        if 0 <= self.current_index < len(self.items):
            current_item = self.items[self.current_index]
            if current_item.checked:
                current_path = current_item.image_path

        self.preview_panel.set_checked_gallery(
            self.thumbnail_panel.checked_thumbnail_entries(),
            current_path=current_path,
            status_text=f"操作対象プレビュー：チェック中画像 {len(checked_items)}件",
        )

    def _on_tag_filter_changed(self, include_tags: set[str], exclude_tags: set[str]) -> None:
        perf_enabled = dev_log_enabled("PERF_FILTER")
        if perf_enabled:
            self._perf_filter_sequence += 1
            sequence = self._perf_filter_sequence
            perf_started = time.perf_counter()
            thumbnail_started = time.perf_counter()

        visible_count = self.thumbnail_panel.set_tag_filters(include_tags, exclude_tags)

        if perf_enabled:
            thumbnail_ms = (time.perf_counter() - thumbnail_started) * 1000

        log.info(
            "tag filter changed include=%s exclude=%s visible_count=%d",
            sorted(include_tags),
            sorted(exclude_tags),
            visible_count,
        )

        if perf_enabled:
            selection_started = time.perf_counter()
        self._sync_current_image_after_tag_filter_change()
        if perf_enabled:
            selection_ms = (time.perf_counter() - selection_started) * 1000
            preview_started = time.perf_counter()

        self._update_preview_placeholder_state()

        if not perf_enabled:
            return

        preview_ms = (time.perf_counter() - preview_started) * 1000
        sync_ms = (time.perf_counter() - perf_started) * 1000

        log.info(
            "[PERF] app.filter_pipeline_sync seq=%d thumbnail_and_tagpanel=%.2fms "
            "select_visible=%.2fms preview_update=%.2fms sync_total=%.2fms",
            sequence,
            thumbnail_ms,
            selection_ms,
            preview_ms,
            sync_ms,
        )

        def log_after_layout_settles() -> None:
            log.info(
                "[PERF] app.filter_pipeline_settled seq=%d elapsed=%.2fms",
                sequence,
                (time.perf_counter() - perf_started) * 1000,
            )

        # タグチップの折り返し再構築やスクロール位置復元は QTimer 経由で走るため、
        # 2イベントループ待った時点の経過時間も残す。
        QTimer.singleShot(0, lambda: QTimer.singleShot(0, log_after_layout_settles))

    def _sync_current_image_after_tag_filter_change(self) -> None:
        """タグフィルタ反映後、現在画像とサムネイル表示位置をそろえる。"""
        # Taggerタブでは編集用のinclude / exclude条件をサムネイル表示へ適用しない。
        if self._tagger_tab_is_active():
            return

        self._select_first_visible_if_current_hidden()

        if (
            0 <= self.current_index < len(self.items)
            and self.thumbnail_panel.is_index_visible(self.current_index)
        ):
            # setHidden() によるフィルタ反映直後は行レイアウトとスクロール範囲が
            # まだ更新途中の場合があるため、ThumbnailPanel側で確定後に可視化する。
            self.thumbnail_panel.ensure_index_visible(self.current_index)

    def _select_first_visible_if_current_hidden(self) -> None:
        """
        現在画像が表示対象から外れた時、表示中画像へ現在選択を移す。

        明示的なフィルタ条件変更では従来どおり結果先頭へ移動する。
        タグ編集後の再フィルタでは、編集前の位置より後ろで最初に残る画像を優先し、
        後ろに残っていなければ手前で最後に残る画像へ移動する。
        """
        if not self.items:
            return

        if (
            self.current_index >= 0
            and self.thumbnail_panel.is_index_visible(self.current_index)
        ):
            return

        visible_indexes = self.thumbnail_panel.visible_indexes()
        if visible_indexes:
            target_index = visible_indexes[0]
            reference_index = self._tag_change_focus_reference_index

            if reference_index is not None and 0 <= reference_index < len(self.items):
                later_indexes = [
                    index for index in visible_indexes if index > reference_index
                ]
                earlier_indexes = [
                    index for index in visible_indexes if index < reference_index
                ]

                if later_indexes:
                    target_index = later_indexes[0]
                elif earlier_indexes:
                    target_index = earlier_indexes[-1]

            self.select_image(target_index)
            return

        log.info("no visible images after filter")
        self.current_index = -1
        self.thumbnail_panel.clear_selection()
        self.preview_panel.set_image(None)
        self.tag_panel.set_current_index(-1)

    def _on_tags_changed(self) -> None:
        include_tags, exclude_tags = self.tag_panel.current_tag_filters()

        # タグ変更によって現在画像がフィルタ外へ落ちる場合は、
        # 編集前の位置を基準に次の表示画像、なければ前の表示画像へ移す。
        # set_tag_filters() 内の visible_view_changed は同期的に届くため、
        # 最後の同期処理まで参照indexを保持して既存の先頭移動と区別する。
        self._tag_change_focus_reference_index = self.current_index
        try:
            self.thumbnail_panel.set_tag_filters(include_tags, exclude_tags)
            self._sync_current_image_after_tag_filter_change()
        finally:
            self._tag_change_focus_reference_index = None

    def _has_unsaved_changes(self) -> bool:
        return any(item.dirty for item in self.items)

    def _confirm_discard_unsaved(self) -> bool:
        if not self._has_unsaved_changes():
            return True

        result = QMessageBox.question(
            self,
            "未保存の変更",
            "未保存のタグ変更があります。保存せずに続行しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return result == QMessageBox.StandardButton.Yes

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._tagger_execution_is_active():
            QMessageBox.information(
                self,
                "Tagger実行中",
                "Tagger実行中は終了できません。完了するまでお待ちください。",
            )
            event.ignore()
            return

        if self._confirm_discard_unsaved():
            self._tagger_process_client.shutdown_and_wait()
            event.accept()
        else:
            event.ignore()
