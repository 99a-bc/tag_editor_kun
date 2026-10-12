from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from bisect import bisect_left, bisect_right
import time

from PySide6.QtCore import QPoint, QRect, Qt, QSize, Signal, QTimer, QItemSelectionModel
from PySide6.QtGui import QColor, QIcon, QImage, QImageReader, QPainter, QPen, QPixmap

from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QListWidgetItem,
    QMessageBox,
    QProxyStyle,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from models import ImageItem
from settings import DEFAULT_THUMBNAIL_WHEEL_SCROLL_PX, normalize_recursive_load_depth, normalize_thumbnail_wheel_scroll_px
from theme import THUMBNAIL_FOOTER_QSS, TAG_SEARCH_BAR_QSS

from logger import get_logger
from dev_config import dev_log_enabled


log = get_logger(__name__)
log.info("thumbnail_panel.py loaded from: %s", Path(__file__).resolve())

class ThumbnailCheckStyle(QProxyStyle):
    """
    サムネイル欄のチェックボックスだけをダークUI向けに描画する。
    未チェック: 明るめグレー枠
    チェック済み: グレー枠 + 白い✓
    """

    def pixelMetric(self, metric, option=None, widget=None):  # noqa: N802
        if metric in (
            QStyle.PixelMetric.PM_IndicatorWidth,
            QStyle.PixelMetric.PM_IndicatorHeight,
        ):
            return 15
        return super().pixelMetric(metric, option, widget)

    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802
        if element != QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck:
            super().drawPrimitive(element, option, painter, widget)
            return

        rect = option.rect
        size = 15
        box = rect.adjusted(
            (rect.width() - size) // 2,
            (rect.height() - size) // 2,
            -((rect.width() - size + 1) // 2),
            -((rect.height() - size + 1) // 2),
        )

        checked = bool(option.state & QStyle.StateFlag.State_On)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)

        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)

        if checked:
            border = QColor("#d8d8d8")
            bg = QColor("#2a2a2a")
        elif hover:
            border = QColor("#e8e8e8")
            bg = QColor("#1f1f1f")
        else:
            border = QColor("#b8b8b8")
            bg = QColor("#151515")

        painter.setPen(QPen(border, 1.6))
        painter.setBrush(bg)
        painter.drawRoundedRect(box, 3, 3)

        if checked:
            check_pen = QPen(QColor("#ffffff"), 2.4)
            check_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            check_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(check_pen)

            x1 = box.left() + 3
            y1 = box.center().y()
            x2 = box.left() + 6
            y2 = box.bottom() - 4
            x3 = box.right() - 3
            y3 = box.top() + 4

            painter.drawLine(x1, y1, x2, y2)
            painter.drawLine(x2, y2, x3, y3)

        painter.restore()



def _is_checked_state(value) -> bool:
    if value == Qt.CheckState.Checked:
        return True

    # PySide環境によっては CheckStateRole が int で返る場合がある。
    try:
        return int(value) == int(Qt.CheckState.Checked.value)
    except Exception:
        return False


# Qt標準の選択状態は一時選択に使うため、現在プレビュー中の画像は別Roleで保持する。
PREVIEW_ROLE = int(Qt.ItemDataRole.UserRole) + 1
# Tagger実行後の対象外/結果なし表示用。
TAGGER_STATUS_ROLE = int(Qt.ItemDataRole.UserRole) + 2
# サムネイル欄が現在どちらの操作文脈を表示しているか。
# "edit" は既存の黄色チェック、"tagger" はTagger専用対象選択を表す。
SELECTION_CONTEXT_ROLE = int(Qt.ItemDataRole.UserRole) + 3
# サムネイル内のファイル名検索で、ハイライトする元文字列位置。
FILENAME_SEARCH_SPAN_ROLE = int(Qt.ItemDataRole.UserRole) + 4


def _filename_match_span(filename: str, query: str, exact: bool) -> tuple[int, int] | None:
    """一致した元ファイル名の範囲。拡張子を含め、大文字小文字は区別しない。"""
    if not query:
        return None
    if exact:
        return (0, len(filename)) if filename.casefold() == query.casefold() else None

    # casefoldにより文字数が変化する言語でも、元文字列の正しい範囲を返す。
    folded_parts = [character.casefold() for character in filename]
    folded_name = "".join(folded_parts)
    start_folded = folded_name.find(query.casefold())
    if start_folded < 0:
        return None
    end_folded = start_folded + len(query.casefold())
    cursor = 0
    first = None
    last = None
    for pos, part in enumerate(folded_parts):
        following = cursor + len(part)
        if first is None and following > start_folded:
            first = pos
        if first is not None and cursor < end_folded:
            last = pos + 1
        cursor = following
        if cursor >= end_folded:
            break
    return (first, last) if first is not None and last is not None else None

# サムネイル生成の同期並列化実験用。
# 1 / 2 / 4 / 8 に変更して PERF_FOLDER_LOAD の thumbnail_icon_create を比較できる。
THUMBNAIL_WORKER_COUNT = 8


class CheckOperationListWidget(QListWidget):
    """
    サムネイル欄のショートカット操作を扱う QListWidget。

    - 通常クリック: Qt 標準処理に任せ、ThumbnailPanel 側でプレビュー移動にする。
    - Ctrl + 左クリック: クリック画像のチェックON/OFF操作として通知する。
    - Shift + 左クリック: 範囲チェック操作として通知する。
    - Space: 現在プレビュー中画像のチェックON/OFF操作として通知する。

    Ctrl/Shift を Qt 標準の複数選択へ渡さないことで、
    一時選択用だった紫枠を完全に廃止する。
    """

    modifier_item_clicked = Signal(object, str)
    space_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._consume_modifier_release = False
        self._wheel_scroll_px = DEFAULT_THUMBNAIL_WHEEL_SCROLL_PX

    def set_wheel_scroll_px(self, value: int) -> None:
        """マウスホイール1ノッチあたりのスクロール量をpx単位で設定する。"""
        self._wheel_scroll_px = normalize_thumbnail_wheel_scroll_px(value)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            modifiers = event.modifiers()
            item = self.itemAt(event.position().toPoint())

            if item is not None:
                if modifiers & Qt.KeyboardModifier.ShiftModifier:
                    self._consume_modifier_release = True
                    self.setFocus(Qt.FocusReason.MouseFocusReason)
                    self.modifier_item_clicked.emit(item, "shift")
                    event.accept()
                    return

                if modifiers & Qt.KeyboardModifier.ControlModifier:
                    self._consume_modifier_release = True
                    self.setFocus(Qt.FocusReason.MouseFocusReason)
                    self.modifier_item_clicked.emit(item, "ctrl")
                    event.accept()
                    return

        self._consume_modifier_release = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._consume_modifier_release and event.button() == Qt.MouseButton.LeftButton:
            self._consume_modifier_release = False
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        """
        サムネイル欄のホイールスクロール量を画像枚数に依存しない固定pxにする。

        通常のマウスホイールでは angleDelta 120 が1ノッチ相当。
        タッチパッド等で pixelDelta が来る場合は、OS側の細かいスクロール量を尊重する。
        """
        vertical_bar = self.verticalScrollBar()
        pixel_delta_y = event.pixelDelta().y()

        if pixel_delta_y:
            vertical_bar.setValue(vertical_bar.value() - pixel_delta_y)
            event.accept()
            return

        angle_delta_y = event.angleDelta().y()
        if not angle_delta_y:
            super().wheelEvent(event)
            return

        steps = angle_delta_y / 120
        scroll_amount = int(round(steps * self._wheel_scroll_px))

        if scroll_amount:
            vertical_bar.setValue(vertical_bar.value() - scroll_amount)

        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Space:
            self.space_requested.emit()
            event.accept()
            return

        super().keyPressEvent(event)


class ThumbnailItemDelegate(QStyledItemDelegate):
    """
    サムネイルの2種類の状態を描き分ける。

    - 黄色背景 + 黄色発光:
        チェック中画像。タグ編集・画像削除などの操作対象。
    - 青い太枠:
        現在プレビュー表示中の画像。

    チェック中かつ表示中の場合は、黄色背景を残したまま青枠を描く。
    """

    CHECKED_BG_COLOR = QColor("#4a3d20")
    CHECKED_BORDER_COLOR = QColor("#e0b84a")
    CHECKED_OVERLAY_COLOR = QColor("#e0b84a")

    # Taggerタブでだけ使う専用対象選択の色。
    # 編集用の黄色チェックと混同しないよう、青緑系に分ける。
    TAGGER_SELECTED_BG_COLOR = QColor("#1d4148")
    TAGGER_SELECTED_BORDER_COLOR = QColor("#57d8c4")
    TAGGER_SELECTED_OVERLAY_COLOR = QColor("#57d8c4")

    CURRENT_BORDER_COLOR = QColor("#3f8cff")
    CURRENT_BORDER_WIDTH = 2.5
    CHECKED_BORDER_WIDTH = 1.0

    def paint(self, painter, option, index) -> None:
        is_checked = _is_checked_state(index.data(Qt.ItemDataRole.CheckStateRole))
        is_current = bool(index.data(PREVIEW_ROLE))
        tagger_status = index.data(TAGGER_STATUS_ROLE)
        selection_context = str(index.data(SELECTION_CONTEXT_ROLE) or "edit")

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        # Qt 標準の selected 表現は使わない。
        # 現在プレビュー中の青枠は PREVIEW_ROLE で自前描画する。
        opt.state &= ~QStyle.StateFlag.State_Selected
        opt.state &= ~QStyle.StateFlag.State_HasFocus

        if is_checked:
            # チェック済み画像では hover 色より黄色表示を優先する。
            opt.state &= ~QStyle.StateFlag.State_MouseOver

        rect = option.rect.adjusted(1, 1, -2, -2)

        if is_checked:
            checked_bg = (
                self.TAGGER_SELECTED_BG_COLOR
                if selection_context == "tagger"
                else self.CHECKED_BG_COLOR
            )
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(checked_bg)
            painter.drawRoundedRect(rect, 8, 8)
            painter.restore()

        # 通常時はQtの標準描画をそのまま使う。
        # QStyledItemDelegate.paint() は内部でも initStyleOption() を呼ぶため、
        # opt.text = "" を設定してから super().paint() しても文字が復活し、
        # 検索ハイライト用の文字と二重に描かれてしまう。
        # 一致時だけ、生成済みのオプションでStyleへ直接描画を依頼する。
        # これでアイコン・チェックボックスの描画はQtに任せつつ、
        # ファイル名だけを一回描画できる。
        search_span = index.data(FILENAME_SEARCH_SPAN_ROLE)
        if search_span is None:
            super().paint(painter, opt, index)
        else:
            filename = opt.text
            widget = self.parent()
            style = widget.style() if widget is not None else QApplication.style()
            name_rect = style.subElementRect(
                QStyle.SubElement.SE_ItemViewItemText, opt, widget
            )
            opt.text = ""
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
            self._paint_highlighted_filename(
                painter, opt, filename, search_span, name_rect, is_current
            )

        if (
            selection_context == "tagger"
            and tagger_status in {"no_result", "pending", "error", "cancelled", "skipped"}
        ):
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            overlay = QColor(0, 0, 0, 150)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(overlay)
            painter.drawRoundedRect(rect, 8, 8)
            painter.setPen(QColor("#cfcfcf"))
            status_map = {
                "pending": "Tagger\n実行中",
                "no_result": "Tagger\n候補なし",
                "error": "Tagger\nエラー",
                "cancelled": "Tagger\n中止",
                "skipped": "Tagger\n今回対象外",
            }
            status_text = status_map.get(str(tagger_status), "Tagger")
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, status_text)
            painter.restore()

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if is_checked:
            checked_overlay = (
                self.TAGGER_SELECTED_OVERLAY_COLOR
                if selection_context == "tagger"
                else self.CHECKED_OVERLAY_COLOR
            )
            overlay = QColor(checked_overlay)
            overlay.setAlpha(32)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(overlay)
            painter.drawRoundedRect(rect, 8, 8)

        if is_current:
            border_color = self.CURRENT_BORDER_COLOR
            border_width = self.CURRENT_BORDER_WIDTH
        elif is_checked:
            border_color = (
                self.TAGGER_SELECTED_BORDER_COLOR
                if selection_context == "tagger"
                else self.CHECKED_BORDER_COLOR
            )
            border_width = self.CHECKED_BORDER_WIDTH
        else:
            border_color = None
            border_width = 0.0

        if border_color is not None:
            border_pen = QPen(border_color, border_width)
            border_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(border_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, 8, 8)

        painter.restore()

    @staticmethod
    def _paint_highlighted_filename(painter, option, filename, span, text_rect, current):
        """文字列の一致箇所だけを着色。幅不足時もサムネイルの幅を広げない。"""
        if not text_rect.isValid() or text_rect.width() <= 3:
            return
        painter.save()
        painter.setFont(option.font)
        fm = painter.fontMetrics()
        available = max(1, text_rect.width() - 4)
        shown = fm.elidedText(filename, Qt.TextElideMode.ElideRight, available)
        # 一致箇所が省略記号の先にある場合は、その箇所からの断片を表示する。
        # 長いファイル名でも、見えていない文字を「一致」とするだけで終わらせない。
        visible_origin = 0
        if shown != filename and span[0] >= max(0, len(shown) - 1):
            visible_origin = span[0]
            remaining_width = max(1, available - fm.horizontalAdvance("…"))
            shown = "…" + fm.elidedText(filename[visible_origin:], Qt.TextElideMode.ElideRight, remaining_width)
        width = fm.horizontalAdvance(shown)
        x = text_rect.left() + (text_rect.width() - width) // 2
        baseline = text_rect.top() + (text_rect.height() + fm.ascent() - fm.descent()) // 2
        painter.setClipRect(text_rect)

        # 省略記号が表示されても検索範囲を元のファイル名で算出する。
        # 隠れている文字列は描かず、見えている範囲だけを着色する。
        if visible_origin:
            # 表示文字列の頭が省略記号になる分だけ描画座標を1文字ずらす。
            prefix_len = visible_origin + max(0, len(shown) - 2)
            start = min(1 + span[0] - visible_origin, len(shown))
            end = min(1 + span[1] - visible_origin, 1 + max(0, len(shown) - 2))
        else:
            prefix_len = len(shown) if shown == filename else max(0, len(shown) - 1)
            start = min(span[0], prefix_len)
            end = min(span[1], prefix_len)
        if start >= end:
            painter.setPen(QColor('#e8e8e8'))
            painter.drawText(x, baseline, shown)
        else:
            before, marked, after = shown[:start], shown[start:end], shown[end:]
            before_width = fm.horizontalAdvance(before)
            marked_width = fm.horizontalAdvance(marked)
            bg = QColor('#35d878') if current else QColor('#ff4ff3')
            fg = QColor('#07130b') if current else QColor('#ffffff')
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(bg)
            painter.drawRoundedRect(
                QRect(x + before_width, baseline - fm.ascent() - 1,
                      max(1, marked_width), fm.height() + 2), 2, 2
            )
            painter.setPen(QColor('#e8e8e8'))
            painter.drawText(x, baseline, before)
            painter.setPen(fg)
            painter.drawText(x + before_width, baseline, marked)
            painter.setPen(QColor('#e8e8e8'))
            painter.drawText(x + before_width + marked_width, baseline, after)
        painter.restore()


class FolderTreeCheckBox(QCheckBox):
    """
    フォルダ指定チェック用のチェックボックス。

    表示としては PartiallyChecked を使うが、
    ユーザークリックでは Unchecked <-> Checked の2状態だけを切り替える。
    これにより、未チェックからのクリックで半チェックを経由してしまう問題を避ける。
    """

    def nextCheckState(self) -> None:  # noqa: N802
        if self.checkState() == Qt.CheckState.Checked:
            self.setCheckState(Qt.CheckState.Unchecked)
        else:
            self.setCheckState(Qt.CheckState.Checked)


class FolderCheckDialog(QDialog):
    """
    読み込み済み画像の親フォルダをツリー表示し、チェック対象フォルダを選ぶダイアログ。

    QTreeWidgetItem標準のチェック機能は使わず、QCheckBoxを埋め込む。
    Qt側の自動親子チェック挙動に巻き込まれないよう、親子連動はここで完全に自前制御する。
    """

    def __init__(self, root_folder: Path, image_paths: list[Path], parent=None) -> None:
        super().__init__(parent)
        self.root_folder = root_folder
        self.image_paths = image_paths
        self._syncing_checks = False
        self._items_by_path: dict[Path, QTreeWidgetItem] = {}
        self._checkboxes_by_item_id: dict[int, QCheckBox] = {}

        self.setWindowTitle("フォルダ指定チェック")
        self.setModal(True)
        self.resize(420, 360)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setMinimumHeight(240)
        self.tree.setIndentation(18)

        self._build_tree()

        hint_label = QLabel("チェックしたフォルダ配下の画像にチェックを入れます。")
        hint_label.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(hint_label)
        layout.addWidget(self.tree, 1)
        layout.addWidget(buttons)

    def _folder_label(self, folder: Path) -> str:
        try:
            relative = folder.relative_to(self.root_folder)
        except ValueError:
            return str(folder)

        if str(relative) == ".":
            return f"{self.root_folder.name} / すべて"

        return folder.name

    def _build_tree(self) -> None:
        folder_paths: set[Path] = {self.root_folder}

        for image_path in self.image_paths:
            parent = image_path.parent
            folder_paths.add(parent)

            try:
                relative_parent = parent.relative_to(self.root_folder)
            except ValueError:
                continue

            current = self.root_folder
            for part in relative_parent.parts:
                current = current / part
                folder_paths.add(current)

        for folder in sorted(folder_paths, key=lambda p: str(p).casefold()):
            item = QTreeWidgetItem()
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            item.setData(0, Qt.ItemDataRole.UserRole, folder)
            self._items_by_path[folder] = item

        for folder, item in self._items_by_path.items():
            if folder == self.root_folder:
                self.tree.addTopLevelItem(item)
                continue

            parent_item = self._items_by_path.get(folder.parent)
            if parent_item is None:
                self.tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)

        for folder, item in self._items_by_path.items():
            checkbox = FolderTreeCheckBox(self._folder_label(folder))
            checkbox.setTristate(False)
            checkbox.setCheckState(Qt.CheckState.Unchecked)
            checkbox.stateChanged.connect(
                lambda state, tree_item=item: self._on_checkbox_changed(tree_item, state)
            )
            self._checkboxes_by_item_id[id(item)] = checkbox
            self.tree.setItemWidget(item, 0, checkbox)

        self.tree.expandAll()

    def _checkbox_for_item(self, item: QTreeWidgetItem) -> QCheckBox | None:
        return self._checkboxes_by_item_id.get(id(item))

    def _on_checkbox_changed(self, item: QTreeWidgetItem, state: int) -> None:
        if self._syncing_checks:
            return

        self._syncing_checks = True
        try:
            check_state = Qt.CheckState(state)

            # FolderTreeCheckBox により通常クリックでは半チェックを経由しない。
            # ただし環境差やプログラム更新で入ってきた場合の保険として Checked へ寄せる。
            if check_state == Qt.CheckState.PartiallyChecked:
                check_state = Qt.CheckState.Checked
                checkbox = self._checkbox_for_item(item)
                if checkbox is not None:
                    checkbox.setCheckState(check_state)

            self._set_children_check_state(item, check_state)
            self._update_parent_check_state(item.parent())
        finally:
            self._syncing_checks = False

    def _set_children_check_state(self, item: QTreeWidgetItem, state: Qt.CheckState) -> None:
        for child_index in range(item.childCount()):
            child = item.child(child_index)
            checkbox = self._checkbox_for_item(child)
            if checkbox is not None:
                checkbox.setCheckState(state)
            self._set_children_check_state(child, state)

    def _update_parent_check_state(self, item: QTreeWidgetItem | None) -> None:
        """
        子/孫フォルダのON/OFFを、親フォルダの見た目へは伝播させない。

        親フォルダを対象にしたい場合は、ユーザーが親フォルダ自体を直接チェックする。
        親フォルダ配下から一部の子/孫フォルダを除外したい場合は、
        親をチェックしたあとに子/孫を外す。
        """
        while item is not None:
            parent_folder = item.data(0, Qt.ItemDataRole.UserRole)
            parent_checkbox = self._checkbox_for_item(item)
            if dev_log_enabled("FOLDER_CHECK_DETAIL"):
                log.debug(
                    "skip parent visual update folder=%s state=%s",
                    parent_folder,
                    parent_checkbox.checkState().name if parent_checkbox is not None else None,
                )
            item = item.parent()

    def checked_folders(self) -> list[Path]:
        folders: list[Path] = []

        def walk(item: QTreeWidgetItem) -> None:
            checkbox = self._checkbox_for_item(item)
            if checkbox is not None and checkbox.checkState() == Qt.CheckState.Checked:
                folder = item.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(folder, Path):
                    folders.append(folder)

            for child_index in range(item.childCount()):
                walk(item.child(child_index))

        for top_index in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(top_index))

        return folders


    def is_image_path_checked(self, image_path: Path) -> bool:
        """
        画像パスが、現在のフォルダツリー状態でチェック対象になるか判定する。

        ルートから画像の親フォルダまでのチェック状態を順に見て、
        Checked なら対象ON、Unchecked なら対象OFFとして上書きする。
        これにより、親をONにしたあと子/孫をOFFにした場合、その配下だけ除外できる。
        """
        return self._is_folder_effectively_checked(image_path.parent)

    def _is_folder_effectively_checked(self, folder: Path) -> bool:
        try:
            relative = folder.relative_to(self.root_folder)
        except ValueError:
            checkbox = self._checkbox_for_item(self._items_by_path.get(folder)) if folder in self._items_by_path else None
            return checkbox is not None and checkbox.checkState() == Qt.CheckState.Checked

        path_chain = [self.root_folder]
        current = self.root_folder

        for part in relative.parts:
            current = current / part
            path_chain.append(current)

        effective = False

        for path in path_chain:
            item = self._items_by_path.get(path)
            if item is None:
                continue

            checkbox = self._checkbox_for_item(item)
            if checkbox is None:
                continue

            state = checkbox.checkState()
            if state == Qt.CheckState.Checked:
                effective = True
            elif state == Qt.CheckState.Unchecked:
                effective = False

        if dev_log_enabled("FOLDER_CHECK_DETAIL"):
            log.debug("effective folder check folder=%s effective=%s", folder, effective)
        return effective


def _build_thumbnail_image(image_path: Path) -> QImage:
    """
    ワーカースレッドで実行可能な一覧用サムネイル生成処理。

    QImageReaderで縮小読み込みし、QImage上へ132x96の完成サムネイルを描画する。
    QPixmap / QIcon / Widget類はここでは作らず、GUIスレッド側へ残す。
    """
    thumb_size = QSize(132, 96)

    reader = QImageReader(str(image_path))
    source_size = reader.size()

    if source_size.isValid() and not source_size.isEmpty():
        scaled_size = source_size.scaled(
            thumb_size,
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        reader.setScaledSize(scaled_size)

    image = reader.read()
    if image.isNull():
        return QImage()

    if image.width() > thumb_size.width() or image.height() > thumb_size.height():
        image = image.scaled(
            thumb_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    canvas = QImage(thumb_size, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(QColor("#101010"))

    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    x = (thumb_size.width() - image.width()) // 2
    y = (thumb_size.height() - image.height()) // 2
    painter.drawImage(x, y, image)

    painter.end()
    return canvas


class ThumbnailPanel(QFrame):
    image_selected = Signal(int)
    # Taggerタブでの現在画像移動。編集用の image_selected と分ける。
    tagger_image_selected = Signal(int)
    checks_changed = Signal()
    tagger_selection_changed = Signal()
    delete_requested = Signal()
    open_tag_file_requested = Signal(int)
    open_image_folder_requested = Signal(int)
    checked_work_mode_changed = Signal(bool, object)
    tag_filter_view_changed = Signal(bool, object, int, int)
    visible_view_changed = Signal()
    recursive_load_settings_changed = Signal(bool, int)

    def __init__(self) -> None:
        super().__init__()
        self.items: list[ImageItem] = []
        self.folder: Path | None = None
        self._recursive_load = False
        self._recursive_depth = 0
        self._updating_recursive_controls = False
        # 編集用とTagger用で「チェック中画像のみ表示」の状態を分ける。
        # タブを切り替えても、片方の表示モードがもう片方へ漏れないようにする。
        self._show_checked_only = False
        self._tagger_show_checked_only = False
        self._include_filter_tags: set[str] = set()
        self._exclude_filter_tags: set[str] = set()
        self._normal_scroll_before_checked_only: int | None = None
        self._normal_current_row_before_checked_only: int | None = None
        self._tagger_normal_scroll_before_checked_only: int | None = None
        self._tagger_normal_current_row_before_checked_only: int | None = None
        # 編集タブ側の現在画像（既存の青枠）。
        self._preview_index: int = -1
        # Taggerタブ側の現在画像と実行対象。
        self._tagger_preview_index: int = -1
        self._tagger_selected_paths: set[Path] = set()
        self._selection_context: str = "edit"
        self._checked_work_mode = False
        self._work_set_paths: set[Path] = set()
        # Tagger実行中は対象選択・フォルダ再読込に関わる操作だけを止める。
        # サムネイルのスクロールと通常クリックによる現在画像移動は残す。
        self._tagger_execution_locked = False
        # 可視化要求を世代番号で管理し、連続したフィルタ操作の古い要求を無効化する。
        self._ensure_visible_request_id = 0
        self._filename_search_matches: list[int] = []
        self._filename_search_query = ""
        self._filename_search_exact = False

        self.setObjectName("ThumbnailPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)

        self.toggle_checked_button = QPushButton("チェック中画像のみ表示")
        self.folder_check_button = QPushButton("フォルダ指定チェック")
        self.work_checked_button = QPushButton("作業セット作成")
        self.check_all_button = QPushButton("全チェック")
        self.clear_checks_button = QPushButton("全チェック解除")
        self.delete_images_button = QPushButton("削除")
        self.delete_images_button.setToolTip("チェック中の画像と対応するtxtをゴミ箱へ移動します")
        self.work_checked_button.setToolTip(
            "現在チェック中の画像から作業セットを作成し、\n"
            "その画像群だけを表示・タグ整理の対象にします"
        )

        self.toggle_checked_button.setCheckable(True)
        self.toggle_checked_button.clicked.connect(self._on_toggle_checked_only)
        self.folder_check_button.clicked.connect(self.check_by_folders)
        self.work_checked_button.setCheckable(True)
        self.work_checked_button.clicked.connect(self._on_toggle_checked_work_mode)
        self.check_all_button.clicked.connect(self.check_all)
        self.clear_checks_button.clicked.connect(self.clear_checks)
        self.delete_images_button.clicked.connect(self.delete_requested.emit)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.toggle_checked_button)
        button_layout.addWidget(self.folder_check_button)
        button_layout.addWidget(self.work_checked_button)
        button_layout.addWidget(self.check_all_button)
        button_layout.addWidget(self.clear_checks_button)
        button_layout.addStretch(1)

        # 既存タグ検索と同じ色・操作感。狭い上部ツールバー向けに
        # [完全一致] の直下へ [＜][＞][×] を収める。
        self.filename_search_bar = QFrame()
        self.filename_search_bar.setObjectName("TagSearchBar")
        self.filename_search_bar.setStyleSheet(TAG_SEARCH_BAR_QSS)
        self.filename_search_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.filename_search_bar.setMinimumWidth(245)
        self.filename_search_bar.setMaximumWidth(360)
        # 通常操作ボタンの高さを基準に検索バーの高さを固定する。
        # 一致件数の有無で行全体が伸縮しないようにする。
        self._filename_search_bar_height = max(34, self.clear_checks_button.sizeHint().height())
        self.filename_search_bar.setFixedHeight(self._filename_search_bar_height)

        self.filename_search_line = QLineEdit()
        self.filename_search_line.setPlaceholderText("ファイル名検索")
        self.filename_search_line.setToolTip(
            "表示中画像のファイル名を検索。検索しても一覧を絞り込まず、チェック状態も変えません。"
        )
        self.filename_search_line.setMinimumWidth(70)
        # 2段で操作ボタン1行分に収める。上段の入力欄を広めに取る。
        self.filename_search_line.setFixedHeight(self._filename_search_bar_height - 12)
        self.filename_search_line.setStyleSheet(
            "QLineEdit { padding: 1px 7px; font-size: 11px; }"
        )
        self.filename_search_line.textChanged.connect(self._set_filename_search_query)

        self.filename_search_count_label = QLabel("")
        self.filename_search_count_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.filename_search_count_label.setStyleSheet(
            "color: #ffe58a; font-size: 9px; font-weight: bold; background: transparent;"
        )
        self.filename_search_count_label.setFixedHeight(10)
        # 検索していない場合も行を確保する。表示は空文字にして位置を固定。
        self.filename_search_count_label.setVisible(True)

        self.filename_search_exact_check = QCheckBox("完全一致")
        self.filename_search_exact_check.setToolTip("ON時は拡張子を含むファイル名全体と一致した画像だけが対象です。")
        self.filename_search_exact_check.setFixedWidth(84)
        self.filename_search_exact_check.setFixedHeight(14)
        self.filename_search_exact_check.setStyleSheet(
            "QCheckBox { font-size: 10px; spacing: 3px; background: transparent; }"
            "QCheckBox::indicator { width: 12px; height: 12px; }"
        )
        self.filename_search_exact_check.toggled.connect(self._set_filename_search_exact)

        self.filename_search_prev_button = QPushButton("＜")
        self.filename_search_next_button = QPushButton("＞")
        self.filename_search_clear_button = QPushButton("×")
        for button in (self.filename_search_prev_button, self.filename_search_next_button,
                       self.filename_search_clear_button):
            button.setFixedSize(19, 17)
            button.setStyleSheet(
                "QPushButton { padding: 0px; font-size: 10px; border-radius: 4px; }"
            )
        self.filename_search_prev_button.setToolTip("前の一致画像へ")
        self.filename_search_next_button.setToolTip("次の一致画像へ")
        self.filename_search_clear_button.setToolTip("ファイル名検索をクリア")
        self.filename_search_prev_button.clicked.connect(lambda: self._jump_filename_search_match(-1))
        self.filename_search_next_button.clicked.connect(lambda: self._jump_filename_search_match(1))
        self.filename_search_clear_button.clicked.connect(self.filename_search_line.clear)
        self.filename_search_line.returnPressed.connect(lambda: self._jump_filename_search_match(1))

        filename_field_column = QVBoxLayout()
        filename_field_column.setContentsMargins(0, 0, 0, 0)
        filename_field_column.setSpacing(0)
        filename_field_column.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        filename_field_column.addWidget(self.filename_search_line)
        filename_field_column.addWidget(self.filename_search_count_label)

        filename_nav_row = QHBoxLayout()
        filename_nav_row.setContentsMargins(0, 0, 0, 0)
        filename_nav_row.setSpacing(3)
        filename_nav_row.addStretch(1)
        for button in (self.filename_search_prev_button, self.filename_search_next_button,
                       self.filename_search_clear_button):
            filename_nav_row.addWidget(button)
        filename_nav_row.addStretch(1)
        filename_controls_column = QVBoxLayout()
        filename_controls_column.setContentsMargins(0, 0, 0, 0)
        filename_controls_column.setSpacing(1)
        filename_controls_column.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        filename_controls_column.addWidget(self.filename_search_exact_check)
        filename_controls_column.addLayout(filename_nav_row)

        filename_bar_layout = QHBoxLayout(self.filename_search_bar)
        filename_bar_layout.setContentsMargins(5, 0, 5, 0)
        filename_bar_layout.setSpacing(4)
        filename_bar_layout.addLayout(filename_field_column, 1)
        filename_bar_layout.addLayout(filename_controls_column)
        button_layout.addWidget(self.filename_search_bar, 1)
        button_layout.addWidget(self.delete_images_button)
        self._update_filename_search_controls()

        # サムネイル欄側のフィルタ結果表示バー。
        # 条件の確認・解除操作は右側タグ一覧パネル側へ集約し、
        # こちらは「現在サムネイル表示が絞られている」ことだけを示す。
        self.filter_bar = QFrame()
        self.filter_bar.setObjectName("ThumbnailFilterBar")
        self.filter_bar.setStyleSheet(
            """
            QFrame#ThumbnailFilterBar {
                background: #162131;
                border: 1px solid #36577c;
                border-radius: 7px;
            }
            QLabel#FilterStateLabel {
                color: #a8d2ff;
                font-weight: bold;
            }
            QLabel#FilterCountLabel {
                color: #e8e8e8;
                font-weight: bold;
            }
            """
        )

        self.filter_state_label = QLabel("フィルタ中")
        self.filter_state_label.setObjectName("FilterStateLabel")

        self.filter_count_label = QLabel("表示: 0 / 0 枚")
        self.filter_count_label.setObjectName("FilterCountLabel")

        filter_bar_layout = QHBoxLayout(self.filter_bar)
        filter_bar_layout.setContentsMargins(10, 5, 10, 5)
        filter_bar_layout.setSpacing(10)
        filter_bar_layout.addWidget(self.filter_state_label)
        filter_bar_layout.addStretch(1)
        filter_bar_layout.addWidget(self.filter_count_label)

        self.filter_bar.setVisible(False)

        self.list_widget = CheckOperationListWidget()
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setMovement(QListWidget.Movement.Static)
        self.list_widget.setIconSize(QSize(132, 96))
        self.list_widget.setGridSize(QSize(180, 140))
        self.list_widget.setSpacing(8)
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.modifier_item_clicked.connect(self._on_modifier_item_clicked)
        self.list_widget.space_requested.connect(self._toggle_current_check)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        self.list_widget.itemChanged.connect(self._on_item_changed)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._on_thumbnail_context_menu)

        self.list_widget.setStyleSheet(
            """
            QListWidget {
                background: #101010;
                border: 1px solid #4a4a4a;
                border-radius: 8px;
                color: #e8e8e8;
                outline: none;
            }

            QListWidget::item {
                padding: 6px;
                border: 1px solid transparent;
                border-radius: 8px;
                margin: 0px;
            }

            QListWidget::item:selected {
                background: #23324a;
                border: 1px solid #7fb2ff;
            }

            QListWidget::item:hover {
                background: #1f2937;
            }
            """
        )

        self._check_style = ThumbnailCheckStyle(self.list_widget.style())
        self.list_widget.setStyle(self._check_style)
        self.list_widget.setItemDelegate(ThumbnailItemDelegate(self.list_widget))

        self._footer_full_text = "開いてるフォルダパス　画像数"
        self.footer_label = QLabel("")
        self.footer_label.setStyleSheet(THUMBNAIL_FOOTER_QSS)
        self.footer_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.footer_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.footer_label.setMinimumWidth(0)
        self.footer_label.setWordWrap(False)

        self.recursive_load_check = QCheckBox("サブフォルダも含める")
        self.recursive_load_check.setToolTip(
            "ONにすると、現在フォルダのサブフォルダ配下も読み込みます。"
        )
        self.recursive_load_check.setStyleSheet(
            """
            QCheckBox {
                color: #e8e8e8;
                spacing: 6px;
            }
            QCheckBox:disabled {
                color: #747474;
            }
            QCheckBox::indicator {
                width: 15px;
                height: 15px;
            }
            """
        )
        self.recursive_load_check.toggled.connect(self._on_recursive_load_controls_changed)

        self.recursive_depth_label = QLabel("深さ")
        self.recursive_depth_label.setToolTip("0 は全階層、1以上は指定階層まで読み込みます。")
        self.recursive_depth_label.setStyleSheet(
            """
            QLabel {
                color: #e8e8e8;
            }
            QLabel:disabled {
                color: #747474;
            }
            """
        )

        self.recursive_depth_spin = QSpinBox()
        self.recursive_depth_spin.setRange(0, 99)
        self.recursive_depth_spin.setValue(0)
        self.recursive_depth_spin.setFixedWidth(58)
        self.recursive_depth_spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.recursive_depth_spin.setToolTip(
            "サブフォルダを何階層下まで読むかを指定します。0 は全階層です。"
        )
        self.recursive_depth_spin.setStyleSheet(
            """
            QSpinBox {
                color: #e8e8e8;
                background: #101010;
                border: 1px solid #4a4a4a;
                border-radius: 6px;
                padding: 2px 6px;
            }
            QSpinBox:disabled {
                color: #747474;
                background: #1a1a1a;
                border-color: #333333;
            }
            QSpinBox::up-button,
            QSpinBox::down-button {
                width: 0px;
                border: none;
            }
            """
        )
        self.recursive_depth_spin.valueChanged.connect(self._on_recursive_load_controls_changed)

        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(8)
        footer_layout.addWidget(self.footer_label, 1)
        footer_layout.addWidget(self.recursive_load_check)
        footer_layout.addWidget(self.recursive_depth_label)
        footer_layout.addWidget(self.recursive_depth_spin)

        layout = QVBoxLayout(self)
        layout.addLayout(button_layout)
        layout.addWidget(self.filter_bar)
        layout.addWidget(self.list_widget, 1)
        layout.addLayout(footer_layout)

        self.set_recursive_load_settings(False, 0, emit=False)


    def interaction_context(self) -> str:
        # 現在サムネイル欄で表示・操作している選択文脈を返す。
        return self._selection_context

    def _checked_only_enabled_for_active_context(self) -> bool:
        if self._selection_context == "tagger":
            return self._tagger_show_checked_only
        return self._show_checked_only

    def _set_checked_only_enabled_for_active_context(self, enabled: bool) -> None:
        if self._selection_context == "tagger":
            self._tagger_show_checked_only = bool(enabled)
        else:
            self._show_checked_only = bool(enabled)

    def _sync_checked_only_button(self) -> None:
        """現在タブ側のチェック中画像のみ表示状態をボタンへ反映する。"""
        was_blocked = self.toggle_checked_button.blockSignals(True)
        try:
            self.toggle_checked_button.setChecked(self._checked_only_enabled_for_active_context())
            # 作業セット中も「作業セット内のチェック中画像のみ表示」として使える。
            # Tagger実行中の無効化は _sync_tagger_execution_control_state() が上書きする。
            self.toggle_checked_button.setEnabled(True)
        finally:
            self.toggle_checked_button.blockSignals(was_blocked)

    def set_tagger_execution_locked(self, locked: bool) -> None:
        """Tagger実行中の対象変更・フォルダ変更操作を無効化する。"""
        locked = bool(locked)
        if self._tagger_execution_locked == locked:
            return

        self._tagger_execution_locked = locked
        if not locked:
            self.recursive_load_check.setEnabled(True)
            self.recursive_load_check.setToolTip(
                "ONにすると、現在フォルダのサブフォルダ配下も読み込みます。"
            )
            self.recursive_depth_label.setEnabled(True)
            self.recursive_depth_spin.setEnabled(True)
            self.recursive_depth_spin.setToolTip(
                "サブフォルダを何階層下まで読むかを指定します。0 は全階層です。"
            )

        # 文脈ごとの通常状態をいったん再計算した後、実行中だけ上書きで無効化する。
        self.set_interaction_context(self._selection_context)

    def _sync_tagger_execution_control_state(self) -> None:
        """現在の選択文脈とTagger実行ロックをボタン・読込設定へ反映する。"""
        if not self._tagger_execution_locked:
            return

        locked_tooltip = "Tagger実行中は変更できません。完了するまでお待ちください。"
        for widget in (
            self.toggle_checked_button,
            self.folder_check_button,
            self.work_checked_button,
            self.check_all_button,
            self.clear_checks_button,
            self.delete_images_button,
            self.recursive_load_check,
            self.recursive_depth_spin,
        ):
            widget.setEnabled(False)
            widget.setToolTip(locked_tooltip)

        self.recursive_depth_label.setEnabled(False)

    def set_interaction_context(self, context: str) -> None:
        # 編集用チェックとTagger用対象選択の表示・操作先を切り替える。
        if context not in {"edit", "tagger"}:
            raise ValueError(f"unknown thumbnail interaction context: {context}")

        self._selection_context = context
        is_tagger = context == "tagger"

        # Tagger実行中に上書きしたツールチップを、ロック解除後は
        # 現在の操作文脈に合う通常文言へ必ず戻す。
        self.toggle_checked_button.setToolTip(
            "Tagger実行対象としてチェックした画像だけを表示します。"
            if is_tagger
            else "チェック中の画像だけを表示します。"
        )
        self.folder_check_button.setToolTip(
            "フォルダを指定して、Tagger実行対象にする画像をまとめてチェックします。"
            if is_tagger
            else "フォルダを指定して、画像をまとめてチェックします。"
        )

        # 作業セットと削除だけは編集用の操作なので、Taggerタブでは無効化する。
        # チェック中画像のみ表示、フォルダ指定チェック、全チェック／全解除は
        # Tagger専用チェック状態に対しても使える。
        self.folder_check_button.setEnabled(True)
        self.work_checked_button.setEnabled(not is_tagger)
        self.delete_images_button.setEnabled(not is_tagger)
        # Tagger実行ロック解除後も、全チェック／全チェック解除は
        # 文脈切替時に通常状態へ戻す必要がある。
        self.check_all_button.setEnabled(True)
        self.clear_checks_button.setEnabled(True)
        self.check_all_button.setText("全チェック")
        self.clear_checks_button.setText("全チェック解除")
        self.check_all_button.setToolTip(
            "表示中の画像をTagger実行対象としてチェックします。"
            if is_tagger
            else ""
        )
        self.clear_checks_button.setToolTip(
            "表示中の画像のTagger実行対象チェックを解除します。"
            if is_tagger
            else ""
        )
        self.work_checked_button.setToolTip(
            "Taggerタブでは作業セットを作成できません。\n"
            "タグ編集タブで使用してください。"
            if is_tagger
            else (
                "現在チェック中の画像から作業セットを作成し、\n"
                "その画像群だけを表示・タグ整理の対象にします"
            )
        )
        self.delete_images_button.setToolTip(
            "Taggerタブでは画像を削除できません。\n"
            "タグ編集タブで使用してください。"
            if is_tagger
            else "チェック中の画像と対応するtxtをゴミ箱へ移動します"
        )
        self._sync_checked_only_button()
        self._sync_tagger_execution_control_state()
        self._apply_checked_visibility()
        self._sync_active_context_visuals()

    def select_tagger_index(self, index: int) -> None:
        # Taggerタブ用の現在画像だけを移動する。編集用の青枠は変更しない。
        if not (0 <= index < self.list_widget.count()):
            return

        self._tagger_preview_index = index
        self._update_filename_search_controls()
        if self._selection_context == "tagger":
            self._sync_active_context_visuals()
            self.list_widget.scrollToItem(self.list_widget.item(index))

    def tagger_selected_indexes(self) -> list[int]:
        # Tagger実行対象として選ばれている画像indexを、表示順で返す。
        return [
            index
            for index, item in enumerate(self.items)
            if item.image_path in self._tagger_selected_paths
        ]

    def clear_tagger_selection(self) -> None:
        # Tagger実行対象だけを解除する。編集用黄色チェックには触れない。
        if self._tagger_execution_locked:
            return
        if not self._tagger_selected_paths:
            return

        self._tagger_selected_paths.clear()
        if self._selection_context == "tagger":
            self._apply_checked_visibility()
            self._sync_active_context_visuals()
        self.tagger_selection_changed.emit()

    def _active_preview_index(self) -> int:
        return (
            self._tagger_preview_index
            if self._selection_context == "tagger"
            else self._preview_index
        )

    def _is_tagger_selected_index(self, index: int) -> bool:
        return (
            0 <= index < len(self.items)
            and self.items[index].image_path in self._tagger_selected_paths
        )

    def _is_active_selected_index(self, index: int) -> bool:
        if self._selection_context == "tagger":
            return self._is_tagger_selected_index(index)
        return 0 <= index < len(self.items) and self.items[index].checked

    def _sync_active_context_visuals(self) -> None:
        # 現在タブの選択状態だけを、サムネイルのチェック・枠表示へ反映する。
        was_blocked = self.list_widget.blockSignals(True)
        try:
            active_preview_index = self._active_preview_index()
            for index in range(self.list_widget.count()):
                widget_item = self.list_widget.item(index)
                is_selected = self._is_active_selected_index(index)
                widget_item.setData(SELECTION_CONTEXT_ROLE, self._selection_context)
                widget_item.setCheckState(
                    Qt.CheckState.Checked if is_selected else Qt.CheckState.Unchecked
                )
                widget_item.setData(PREVIEW_ROLE, index == active_preview_index)
                self._apply_thumbnail_item_style(widget_item)
        finally:
            self.list_widget.blockSignals(was_blocked)

        self.list_widget.clearSelection()
        self._update_filename_search_controls()
        self.list_widget.viewport().update()

    def _set_tagger_selected_indexes(self, indexes: list[int], selected: bool) -> None:
        # Tagger専用の実行対象だけをON/OFFする。
        if self._tagger_execution_locked:
            return
        valid_indexes = [
            index
            for index in indexes
            if isinstance(index, int) and 0 <= index < len(self.items)
        ]
        if not valid_indexes:
            return

        changed = False
        for index in valid_indexes:
            path = self.items[index].image_path
            if selected:
                if path not in self._tagger_selected_paths:
                    self._tagger_selected_paths.add(path)
                    changed = True
            elif path in self._tagger_selected_paths:
                self._tagger_selected_paths.remove(path)
                changed = True

        if not changed:
            return

        if self._selection_context == "tagger":
            self._apply_checked_visibility()
            self._sync_active_context_visuals()
        self.tagger_selection_changed.emit()

    def set_wheel_scroll_px(self, value: int) -> None:
        """サムネイル欄のホイールスクロール量を反映する。"""
        self.list_widget.set_wheel_scroll_px(value)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_footer_elided_text()

    def _set_footer_text(self, text: str) -> None:
        """フッターのフルテキストを保持し、表示幅に合わせて省略表示する。"""
        self._footer_full_text = text
        self.footer_label.setToolTip(text)
        self._update_footer_elided_text()
        # set_items直後はレイアウト幅がまだ確定していない場合があるため、
        # 次のイベントループでもう一度省略幅を合わせる。
        QTimer.singleShot(0, self._update_footer_elided_text)

    def _update_footer_elided_text(self) -> None:
        if not hasattr(self, "footer_label"):
            return

        full_text = getattr(self, "_footer_full_text", "")
        available_width = self.footer_label.width()
        if available_width <= 8:
            self.footer_label.setText(full_text)
            return

        metrics = self.footer_label.fontMetrics()
        elided = metrics.elidedText(
            full_text,
            Qt.TextElideMode.ElideMiddle,
            max(8, available_width - 4),
        )
        self.footer_label.setText(elided)

    def set_recursive_load_settings(self, enabled: bool, depth: object, *, emit: bool = False) -> None:
        """サブフォルダ読み込みON/OFFと深さUIを更新する。"""
        normalized_depth = normalize_recursive_load_depth(depth)
        self._recursive_load = bool(enabled)
        self._recursive_depth = normalized_depth

        self._updating_recursive_controls = True
        try:
            self.recursive_load_check.setChecked(self._recursive_load)
            self.recursive_depth_spin.setValue(normalized_depth)
            self.recursive_depth_label.setEnabled(not self._tagger_execution_locked)
            self.recursive_depth_spin.setEnabled(not self._tagger_execution_locked)
        finally:
            self._updating_recursive_controls = False

        if emit:
            self.recursive_load_settings_changed.emit(self._recursive_load, self._recursive_depth)

    def recursive_load_enabled(self) -> bool:
        return bool(self._recursive_load)

    def recursive_load_depth(self) -> int:
        return normalize_recursive_load_depth(self._recursive_depth)

    def _on_recursive_load_controls_changed(self, *args) -> None:
        if self._updating_recursive_controls or self._tagger_execution_locked:
            return

        previous_recursive_load = self._recursive_load
        self._recursive_load = self.recursive_load_check.isChecked()
        self._recursive_depth = normalize_recursive_load_depth(self.recursive_depth_spin.value())
        self.recursive_depth_label.setEnabled(not self._tagger_execution_locked)
        self.recursive_depth_spin.setEnabled(not self._tagger_execution_locked)

        # チェックOFFのまま深さだけを変えた場合は、次にONにするための
        # 事前設定として内部値だけ保持し、フォルダ再読み込みは発生させない。
        if not previous_recursive_load and not self._recursive_load:
            return

        self.recursive_load_settings_changed.emit(self._recursive_load, self._recursive_depth)

    def _set_filename_search_query(self, value: str) -> None:
        self._filename_search_query = value.strip()
        # 検索語を入力・変更したら、現在画像が非一致の場合だけ先頭候補へ移動する。
        # 検索をクリアした場合は画像選択を変更しない。
        self._refresh_filename_search_matches(auto_select=True)

    def _set_filename_search_exact(self, enabled: bool) -> None:
        self._filename_search_exact = bool(enabled)
        # 完全一致の切替も新しい検索条件として扱う。
        self._refresh_filename_search_matches(auto_select=True)

    def _refresh_filename_search_matches(self, *, auto_select: bool = False) -> None:
        """可視画像だけを検索・ハイライトし、明示的な検索変更時だけ自動ジャンプする。"""
        matches: list[int] = []
        query = self._filename_search_query
        was_blocked = self.list_widget.blockSignals(True)
        try:
            for index, image in enumerate(self.items):
                if index >= self.list_widget.count():
                    break
                widget_item = self.list_widget.item(index)
                span = (
                    _filename_match_span(image.image_path.name, query, self._filename_search_exact)
                    if query and not widget_item.isHidden() else None
                )
                if span is not None:
                    matches.append(index)
                if widget_item.data(FILENAME_SEARCH_SPAN_ROLE) != span:
                    widget_item.setData(FILENAME_SEARCH_SPAN_ROLE, span)
        finally:
            self.list_widget.blockSignals(was_blocked)
        self._filename_search_matches = matches
        # タグフィルタや作業セットの表示再計算では自動ジャンプしない。
        # ユーザーが検索語・完全一致を変更したときだけ、現在の画像が
        # 一致しなければ最初の候補へ移動して、0 / N表示を避ける。
        if auto_select and query and matches and self._active_preview_index() not in matches:
            self._activate_filename_search_index(matches[0])
        self._update_filename_search_controls()
        self.list_widget.viewport().update()

    def _update_filename_search_controls(self) -> None:
        active = bool(self._filename_search_query)
        has_matches = active and bool(self._filename_search_matches)
        self.filename_search_prev_button.setEnabled(has_matches)
        self.filename_search_next_button.setEnabled(has_matches)
        self.filename_search_clear_button.setEnabled(active)
        if not active:
            label = ""
        elif not self._filename_search_matches:
            label = "結果なし"
        else:
            # 通常はタグ検索と同じ「現在位置 / 一致総数」。
            # 検索後に手動で非一致画像を表示した場合も、ヒットなしと誤解される
            # 0 / N にはせず、検索結果の総件数だけを表示する。
            current = self._active_preview_index()
            try:
                ordinal = self._filename_search_matches.index(current) + 1
            except ValueError:
                label = f"{len(self._filename_search_matches)}件"
            else:
                label = f"{ordinal} / {len(self._filename_search_matches)}"
        self.filename_search_count_label.setText(label)
        # 件数欄は常に確保する。非表示にすると検索時に行の高さが変わる。
        # active=False 時は上で設定した空文字だけを表示する。

    def _jump_filename_search_match(self, direction: int) -> None:
        matches = self._filename_search_matches
        if not matches:
            return
        current = self._tagger_preview_index if self._selection_context == "tagger" else self._preview_index
        if direction > 0:
            pos = bisect_right(matches, current) % len(matches)
        else:
            pos = (bisect_left(matches, current) - 1) % len(matches)
        self._activate_filename_search_index(matches[pos])

    def _activate_filename_search_index(self, index: int) -> None:
        """検索結果への移動は通常の画像選択シグナルを使い、既存UIへ反映する。"""
        if self._selection_context == "tagger":
            self.tagger_image_selected.emit(index)
        else:
            self.image_selected.emit(index)
        # AppWindowの選択同期を優先。サムネイルが画面外ならスクロールする。
        self.ensure_index_visible(index)

    def _on_thumbnail_context_menu(self, pos) -> None:
        """サムネイル右クリックメニュー。"""
        widget_item = self.list_widget.itemAt(pos)
        if widget_item is None:
            return

        index = widget_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(index, int) or not (0 <= index < len(self.items)):
            return

        # 右クリックしたサムネイルも、左クリック時と同じく現在プレビュー対象にする。
        # 現在のタブ文脈に応じて、編集用またはTagger用の青枠だけを動かす。
        if self._selection_context == "tagger":
            self.tagger_image_selected.emit(index)
        else:
            self.image_selected.emit(index)
        self.list_widget.clearSelection()
        self.list_widget.viewport().update()

        menu = QMenu(self)
        open_txt_action = menu.addAction("対応txtを開く")
        open_folder_action = menu.addAction("画像フォルダを開く")
        copy_filename_action = menu.addAction("ファイル名をコピーする")

        # カーソル直下にメニューが重なると項目が見づらいため、少し右下へずらして表示する。
        menu_pos = self.list_widget.viewport().mapToGlobal(pos + QPoint(24, 4))
        selected_action = menu.exec(menu_pos)
        if selected_action == open_txt_action:
            self.open_tag_file_requested.emit(index)
        elif selected_action == open_folder_action:
            self.open_image_folder_requested.emit(index)
        elif selected_action == copy_filename_action:
            # 選択中プレビューではなく、右クリックされた画像の実ファイル名。
            QApplication.clipboard().setText(self.items[index].image_path.name)

    def _make_thumbnail_icon(self, image_path: Path) -> QIcon:
        """単体再生成用。画像生成はQImage経路、QIcon化はGUIスレッドで行う。"""
        return self._thumbnail_image_to_icon(_build_thumbnail_image(image_path))

    def _thumbnail_image_to_icon(self, image: QImage) -> QIcon:
        """ワーカーが生成した完成サムネイルQImageを表示用QIconへ変換する。"""
        if image.isNull():
            return QIcon()
        return QIcon(QPixmap.fromImage(image))

    def _build_thumbnail_images(self, items: list[ImageItem]) -> list[QImage]:
        """
        一覧初期構築用のサムネイル画像を同期並列生成する。

        全件完成後に従来どおり一覧へ登録するため、UIの表示タイミングや
        フィルタ・チェック処理の状態管理は変更しない。
        """
        image_paths = [item.image_path for item in items]
        if THUMBNAIL_WORKER_COUNT <= 1 or len(image_paths) <= 1:
            return [_build_thumbnail_image(path) for path in image_paths]

        with ThreadPoolExecutor(
            max_workers=THUMBNAIL_WORKER_COUNT,
            thread_name_prefix="thumbnail",
        ) as executor:
            return list(executor.map(_build_thumbnail_image, image_paths))

    def set_items(
        self,
        items: list[ImageItem],
        folder: Path | None = None,
        recursive: bool | None = None,
        recursive_depth: int | None = None,
    ) -> None:
        perf_enabled = dev_log_enabled("PERF_FOLDER_LOAD")
        if perf_enabled:
            perf_started = time.perf_counter()

        previous_folder = self.folder
        folder_changed = previous_folder is not None and folder != previous_folder

        self.items = items
        self.folder = folder
        if folder_changed:
            # 別フォルダへ前の検索語を引き継がず、既存のチェック・表示範囲のみ復元する。
            blocked = self.filename_search_line.blockSignals(True)
            try:
                self.filename_search_line.clear()
            finally:
                self.filename_search_line.blockSignals(blocked)
            self._filename_search_query = ""
        if recursive is not None or recursive_depth is not None:
            self.set_recursive_load_settings(
                self._recursive_load if recursive is None else bool(recursive),
                self._recursive_depth if recursive_depth is None else recursive_depth,
                emit=False,
            )
        self._include_filter_tags.clear()
        self._exclude_filter_tags.clear()
        self._normal_scroll_before_checked_only = None
        self._normal_current_row_before_checked_only = None
        self._tagger_normal_scroll_before_checked_only = None
        self._tagger_normal_current_row_before_checked_only = None
        self._preview_index = -1
        self._tagger_preview_index = -1
        # 画像削除など同一フォルダ内の再構築では、残存画像のTagger選択だけを保険として残す。
        # フォルダ再読込時の明示的な全解除は AppWindow が行う。
        self._tagger_selected_paths.intersection_update(
            {item.image_path for item in self.items}
        )

        if folder_changed:
            # 新しいフォルダへ切り替えた場合、以前の作業セットは引き継がない。
            self._checked_work_mode = False
            self._work_set_paths.clear()
            self.work_checked_button.blockSignals(True)
            self.work_checked_button.setChecked(False)
            self.work_checked_button.blockSignals(False)
            self._show_checked_only = False
            self._tagger_show_checked_only = False
        elif self._checked_work_mode:
            # 削除後の再構築など、同じフォルダでの更新時は残存画像だけを作業セットに残す。
            existing_paths = {item.image_path for item in self.items}
            self._work_set_paths.intersection_update(existing_paths)

        if perf_enabled:
            clear_started = time.perf_counter()

        self.list_widget.blockSignals(True)
        self.list_widget.clear()

        if perf_enabled:
            clear_ms = (time.perf_counter() - clear_started) * 1000
            thumbnail_generate_started = time.perf_counter()

        thumbnail_images = self._build_thumbnail_images(items)

        if perf_enabled:
            thumbnail_generate_ms = (time.perf_counter() - thumbnail_generate_started) * 1000
            thumbnail_icon_wrap_ms = 0.0
            list_item_ms = 0.0

        for index, (item, thumbnail_image) in enumerate(zip(items, thumbnail_images)):
            if perf_enabled:
                icon_started = time.perf_counter()
            icon = self._thumbnail_image_to_icon(thumbnail_image)
            if perf_enabled:
                thumbnail_icon_wrap_ms += (time.perf_counter() - icon_started) * 1000
                list_item_started = time.perf_counter()

            widget_item = QListWidgetItem(icon, item.image_path.name)
            widget_item.setSizeHint(QSize(180, 140))
            widget_item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            widget_item.setData(Qt.ItemDataRole.UserRole, index)
            widget_item.setData(PREVIEW_ROLE, False)
            widget_item.setData(TAGGER_STATUS_ROLE, None)
            widget_item.setData(SELECTION_CONTEXT_ROLE, self._selection_context)
            widget_item.setData(FILENAME_SEARCH_SPAN_ROLE, None)
            widget_item.setFlags(
                widget_item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsEnabled
            )
            widget_item.setCheckState(
                Qt.CheckState.Checked
                if self._is_active_selected_index(index)
                else Qt.CheckState.Unchecked
            )
            self._apply_thumbnail_item_style(widget_item)
            self.list_widget.addItem(widget_item)

            if perf_enabled:
                list_item_ms += (time.perf_counter() - list_item_started) * 1000

        self.list_widget.blockSignals(False)

        self._sync_checked_only_button()
        if perf_enabled:
            visibility_started = time.perf_counter()
        self._apply_checked_visibility()
        if perf_enabled:
            visibility_ms = (time.perf_counter() - visibility_started) * 1000
            footer_started = time.perf_counter()

        folder_text = str(folder) if folder else "未選択"
        if self._recursive_load:
            folder_count = len({item.image_path.parent for item in items})
            footer_text = f"{folder_text}　サブフォルダ込み: {folder_count}フォルダ / {len(items)}画像"
        else:
            footer_text = f"{folder_text}　画像数: {len(items)}"

        self._set_footer_text(footer_text)

        if perf_enabled:
            footer_ms = (time.perf_counter() - footer_started) * 1000
            thumbnail_icon_total_ms = thumbnail_generate_ms + thumbnail_icon_wrap_ms
            log.info(
                "[PERF_LOAD] thumbnail.set_items images=%d folder_changed=%s workers=%d "
                "clear_list=%.2fms thumbnail_generate_parallel=%.2fms "
                "icon_wrap=%.2fms thumbnail_icon_create=%.2fms "
                "list_item_insert=%.2fms visibility=%.2fms footer=%.2fms total=%.2fms",
                len(items),
                folder_changed,
                THUMBNAIL_WORKER_COUNT,
                clear_ms,
                thumbnail_generate_ms,
                thumbnail_icon_wrap_ms,
                thumbnail_icon_total_ms,
                list_item_ms,
                visibility_ms,
                footer_ms,
                (time.perf_counter() - perf_started) * 1000,
            )

    def select_index(self, index: int) -> None:
        if not (0 <= index < self.list_widget.count()):
            return

        # PREVIEW_ROLE の変更で itemChanged が発火し、チェック変更と誤認されないようにする。
        was_blocked = self.list_widget.blockSignals(True)
        try:
            if 0 <= self._preview_index < self.list_widget.count():
                previous_item = self.list_widget.item(self._preview_index)
                previous_item.setData(PREVIEW_ROLE, False)

            self._preview_index = index
            current_item = self.list_widget.item(index)
            current_item.setData(PREVIEW_ROLE, True)

            # current item は更新するが、チェック状態は変更しない。
            self.list_widget.setCurrentItem(
                current_item,
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )
        finally:
            self.list_widget.blockSignals(was_blocked)

        self.list_widget.scrollToItem(current_item)
        self._update_filename_search_controls()
        self.list_widget.viewport().update()

    def ensure_index_visible(self, index: int) -> None:
        """フィルタ反映後に、指定サムネイルが見える位置までだけスクロールする。"""
        if not (0 <= index < self.list_widget.count()):
            return

        self._ensure_visible_request_id += 1
        request_id = self._ensure_visible_request_id

        def apply_scroll() -> None:
            if request_id != self._ensure_visible_request_id:
                return
            if not (0 <= index < self.list_widget.count()):
                return

            item = self.list_widget.item(index)
            if item is None or item.isHidden():
                return

            self.list_widget.scrollToItem(
                item,
                QAbstractItemView.ScrollHint.EnsureVisible,
            )

        # setHidden()直後はQListWidgetの行レイアウトとスクロール範囲が
        # まだ更新途中の場合があるため、2イベントループ待ってから位置を合わせる。
        QTimer.singleShot(0, lambda: QTimer.singleShot(0, apply_scroll))

    def set_tag_filters(self, include_tags: set[str], exclude_tags: set[str]) -> int:
        """
        緑/赤のタグフィルタを適用する。

        黄色チェックは現在画面上で操作対象にしている画像として扱うため、
        タグ条件から外れて非表示になるチェック中画像は自動解除する。

        ただし、「チェック中画像のみ表示」や作業セット外であることによる
        非表示は、タグフィルタによる除外ではないため解除対象にしない。
        """
        perf_started = time.perf_counter()

        self._include_filter_tags = set(include_tags)
        self._exclude_filter_tags = set(exclude_tags)

        scan_started = time.perf_counter()
        # Taggerタブでは編集用フィルタを可視条件として使わない。
        # 万一この文脈で編集用フィルタ状態が通知されても、黄色チェックを
        # 自動解除せず、タグ編集タブへ戻った時の状態を保持する。
        unchecked_indexes = (
            [
                index
                for index, item in enumerate(self.items)
                if item.checked and not self._item_matches_tag_filter(item)
            ]
            if self._selection_context == "edit"
            else []
        )
        scan_ms = (time.perf_counter() - scan_started) * 1000

        uncheck_started = time.perf_counter()
        if unchecked_indexes:
            self.list_widget.blockSignals(True)
            try:
                for index in unchecked_indexes:
                    self.items[index].checked = False
                    widget_item = self.list_widget.item(index)
                    widget_item.setCheckState(Qt.CheckState.Unchecked)
                    self._apply_thumbnail_item_style(widget_item)
            finally:
                self.list_widget.blockSignals(False)
        uncheck_ms = (time.perf_counter() - uncheck_started) * 1000

        visibility_started = time.perf_counter()
        visible_count = self._apply_checked_visibility()
        visibility_ms = (time.perf_counter() - visibility_started) * 1000

        viewport_started = time.perf_counter()
        self.list_widget.viewport().update()
        viewport_ms = (time.perf_counter() - viewport_started) * 1000

        checks_emit_ms = 0.0
        if unchecked_indexes:
            if dev_log_enabled("FILTER_DETAIL"):
                log.info(
                    "unchecked images hidden by tag filter count=%d indexes=%s",
                    len(unchecked_indexes),
                    unchecked_indexes,
                )
            checks_emit_started = time.perf_counter()
            self.checks_changed.emit()
            checks_emit_ms = (time.perf_counter() - checks_emit_started) * 1000

        total_ms = (time.perf_counter() - perf_started) * 1000
        if dev_log_enabled("PERF_FILTER"):
            log.info(
                "[PERF] thumbnail.set_tag_filters images=%d checked_auto_removed=%d "
                "scan=%.2fms uncheck_apply=%.2fms visibility_and_notify=%.2fms "
                "viewport_update=%.2fms checks_emit=%.2fms total=%.2fms",
                len(self.items),
                len(unchecked_indexes),
                scan_ms,
                uncheck_ms,
                visibility_ms,
                viewport_ms,
                checks_emit_ms,
                total_ms,
            )
        log.info(
            "thumbnail tag filter applied include=%s exclude=%s visible_count=%d",
            sorted(self._include_filter_tags),
            sorted(self._exclude_filter_tags),
            visible_count,
        )
        return visible_count

    def visible_indexes(self) -> list[int]:
        started = time.perf_counter()
        indexes = [index for index in range(len(self.items)) if self._is_index_visible(index)]
        if dev_log_enabled("PERF_FILTER"):
            log.info(
                "[PERF] thumbnail.visible_indexes images=%d visible=%d total=%.2fms",
                len(self.items),
                len(indexes),
                (time.perf_counter() - started) * 1000,
            )
        return indexes

    def is_index_visible(self, index: int) -> bool:
        return self._is_index_visible(index)

    def clear_selection(self) -> None:
        was_blocked = self.list_widget.blockSignals(True)
        try:
            if 0 <= self._preview_index < self.list_widget.count():
                current_item = self.list_widget.item(self._preview_index)
                current_item.setData(PREVIEW_ROLE, False)

            self._preview_index = -1
            self.list_widget.clearSelection()
            self.list_widget.setCurrentRow(-1)
        finally:
            self.list_widget.blockSignals(was_blocked)

        self.list_widget.viewport().update()


    def set_tagger_statuses(self, statuses: dict[int, str | None] | None) -> None:
        """Tagger結果の有無をサムネイル表示へ反映する。"""
        statuses = statuses or {}
        was_blocked = self.list_widget.blockSignals(True)
        try:
            for index in range(self.list_widget.count()):
                widget_item = self.list_widget.item(index)
                widget_item.setData(TAGGER_STATUS_ROLE, statuses.get(index))
        finally:
            self.list_widget.blockSignals(was_blocked)

        self.list_widget.viewport().update()

    def set_tagger_statuses_for_indexes(self, statuses: dict[int, str | None]) -> None:
        """指定画像だけのTagger状態を差分更新する。"""
        if not statuses:
            return

        changed_count = 0
        was_blocked = self.list_widget.blockSignals(True)
        try:
            for index, status in statuses.items():
                if not (0 <= index < self.list_widget.count()):
                    continue

                widget_item = self.list_widget.item(index)
                if widget_item.data(TAGGER_STATUS_ROLE) == status:
                    continue

                widget_item.setData(TAGGER_STATUS_ROLE, status)
                changed_count += 1
        finally:
            self.list_widget.blockSignals(was_blocked)

        if changed_count:
            self.list_widget.viewport().update()

        if dev_log_enabled("TAGGER_PERF"):
            log.info(
                "[TAGGER_PERF] thumbnail_status_partial requested=%d changed=%d",
                len(statuses),
                changed_count,
            )

    def checked_indexes(self) -> list[int]:
        return [i for i, item in enumerate(self.items) if item.checked]


    def tagger_selected_count(self) -> int:
        return len(self.tagger_selected_indexes())

    def checked_thumbnail_entries(self) -> list[tuple[Path, QPixmap]]:
        """
        チェック中画像の一時プレビュー一覧向けに、既存サムネイルを返す。

        サムネイル欄の QListWidgetItem は、フォルダ読込時に作成済みの
        QIcon を保持しているため、ここでは元画像を再読み込みしない。
        大量チェック時の一時プレビュー表示を軽くするための経路。
        """
        entries: list[tuple[Path, QPixmap]] = []
        icon_size = self.list_widget.iconSize()

        for index in self.checked_indexes():
            if not (0 <= index < self.list_widget.count()):
                continue

            widget_item = self.list_widget.item(index)
            pixmap = widget_item.icon().pixmap(icon_size)
            entries.append((self.items[index].image_path, pixmap))

        return entries

    def work_set_indexes(self) -> list[int]:
        """作業モード中に表示・右側タグ操作の母集団となる画像indexを返す。"""
        if not self._checked_work_mode:
            return []
        return [
            index
            for index, item in enumerate(self.items)
            if item.image_path in self._work_set_paths
        ]

    def _bulk_check_target_indexes(self) -> list[int]:
        """
        全チェック／全チェック解除の対象は、現在サムネイル欄に表示されている画像だけ。

        これにより、
        - タグフィルタ中はフィルタ結果だけ
        - 作業モード中は作業セット内の表示中画像だけ
        - チェック中画像のみ表示中は現在表示されている画像だけ
        を安全に操作対象にする。
        """
        return self.visible_indexes()

    def check_all(self) -> None:
        if self._tagger_execution_locked:
            return
        target_indexes = self._bulk_check_target_indexes()
        if self._selection_context == "tagger":
            self._set_tagger_selected_indexes(target_indexes, True)
            return
        self._set_checked_indexes(target_indexes, True)

    def clear_checks(self) -> None:
        if self._tagger_execution_locked:
            return
        target_indexes = self._bulk_check_target_indexes()
        if self._selection_context == "tagger":
            self._set_tagger_selected_indexes(target_indexes, False)
            return
        self._set_checked_indexes(target_indexes, False)

    def check_by_folders(self) -> None:
        if self._tagger_execution_locked:
            return
        if not self.items or self.folder is None:
            return

        is_tagger = self._selection_context == "tagger"
        # Taggerタブでは編集用の作業セットに縛られず、読み込み済み全画像から選ぶ。
        candidate_indexes = (
            list(range(len(self.items)))
            if is_tagger
            else (self.work_set_indexes() if self._checked_work_mode else list(range(len(self.items))))
        )
        candidate_paths = [self.items[index].image_path for index in candidate_indexes]

        dialog = FolderCheckDialog(
            self.folder,
            candidate_paths,
            self,
        )
        self._move_folder_check_dialog_near_button(dialog)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        selected_folders = dialog.checked_folders()
        log.info("folder check accepted context=%s checked_folders=%s", self._selection_context, selected_folders)

        # OKした場合は、選択フォルダが空でも現在文脈側のチェックを全解除する。
        # 実際の対象判定は、親ON→子OFFのような除外を扱えるように、
        # フォルダリストの単純一致ではなくツリー状態から画像ごとに判定する。
        candidate_path_set = set(candidate_paths)

        if is_tagger:
            next_selected_paths = {
                item.image_path
                for item in self.items
                if (
                    item.image_path in candidate_path_set
                    and dialog.is_image_path_checked(item.image_path)
                )
            }
            changed = next_selected_paths != self._tagger_selected_paths
            self._tagger_selected_paths = next_selected_paths
            log.info("folder check applied context=tagger checked_count=%d", len(next_selected_paths))

            self._apply_checked_visibility()
            self._sync_active_context_visuals()
            if changed:
                self.tagger_selection_changed.emit()
            return

        checked_count = 0
        for item in self.items:
            item.checked = (
                item.image_path in candidate_path_set
                and dialog.is_image_path_checked(item.image_path)
            )
            if item.checked:
                checked_count += 1

        log.info("folder check applied context=edit checked_count=%d", checked_count)

        self.list_widget.blockSignals(True)
        for row in range(self.list_widget.count()):
            widget_item = self.list_widget.item(row)
            index = widget_item.data(Qt.ItemDataRole.UserRole)
            widget_item.setCheckState(
                Qt.CheckState.Checked if self.items[index].checked else Qt.CheckState.Unchecked
            )
            self._apply_thumbnail_item_style(widget_item)
        self.list_widget.blockSignals(False)

        self._apply_checked_visibility()
        self.checks_changed.emit()

    def _move_folder_check_dialog_near_button(self, dialog: QDialog) -> None:
        dialog.adjustSize()

        button_global = self.folder_check_button.mapToGlobal(self.folder_check_button.rect().topLeft())
        x = button_global.x()
        y = button_global.y() - dialog.height() - 8

        screen = self.folder_check_button.screen()
        if screen is not None:
            available = screen.availableGeometry()
            x = max(available.left(), min(x, available.right() - dialog.width()))
            y = max(available.top(), min(y, available.bottom() - dialog.height()))

        dialog.move(x, y)

    def _is_same_or_child_path(self, path: Path, folder: Path) -> bool:
        if path == folder:
            return True

        try:
            path.relative_to(folder)
            return True
        except ValueError:
            return False

    def _on_toggle_checked_work_mode(self) -> None:
        """
        現在のチェック中画像を固定の作業セットとして取り込み、
        その画像群だけで表示・タグ整理を行うモードを切り替える。

        モード開始時に黄色チェックは解除する。
        モード中の黄色チェックは、作業セット内での部分操作・削除対象として使う。
        """
        if self._tagger_execution_locked:
            return

        enabled = self.work_checked_button.isChecked()

        if enabled:
            work_set_paths = {
                item.image_path for item in self.items if item.checked
            }
            if not work_set_paths:
                QMessageBox.information(
                    self,
                    "作業セット作成",
                    "作業セットにする画像をチェックしてください。",
                )
                self.work_checked_button.blockSignals(True)
                self.work_checked_button.setChecked(False)
                self.work_checked_button.blockSignals(False)
                return

            self._checked_work_mode = True
            self._work_set_paths = work_set_paths

            # 作業モードは既存の「チェック中画像のみ表示」と重ねて使える。
            # 作業セット作成直後だけは、作成元になった黄色チェックを解除するため
            # 表示をOFFへ戻す。その後は「作業セット内のチェック中画像のみ表示」として使う。
            self._show_checked_only = False
            self._normal_scroll_before_checked_only = None
            self._normal_current_row_before_checked_only = None
            self._sync_checked_only_button()

            # 開始時のチェックは、作業セットを作るための入力だったので解除する。
            self.list_widget.blockSignals(True)
            try:
                for index, model_item in enumerate(self.items):
                    model_item.checked = False
                    widget_item = self.list_widget.item(index)
                    widget_item.setCheckState(Qt.CheckState.Unchecked)
                    self._apply_thumbnail_item_style(widget_item)
            finally:
                self.list_widget.blockSignals(False)

            self._apply_checked_visibility()
            self.list_widget.viewport().update()
            self.checked_work_mode_changed.emit(True, set(self._work_set_paths))
            self.checks_changed.emit()
            return

        self._checked_work_mode = False
        self._work_set_paths.clear()
        # 作業セット終了時は従来どおり通常表示へ戻す。
        self._show_checked_only = False
        self._normal_scroll_before_checked_only = None
        self._normal_current_row_before_checked_only = None
        self._sync_checked_only_button()
        self._apply_checked_visibility()
        self.checked_work_mode_changed.emit(False, set())

    def _on_toggle_checked_only(self) -> None:
        if self._tagger_execution_locked:
            return
        is_tagger = self._selection_context == "tagger"
        # 編集タブの作業セット中も、このトグルは
        # 「作業セット内のチェック中画像のみ表示」として利用できる。
        enabled = self.toggle_checked_button.isChecked()

        if enabled:
            # 現在の文脈ごとに、通常表示へ戻すためのスクロール位置を覚える。
            if is_tagger:
                self._tagger_normal_scroll_before_checked_only = self.list_widget.verticalScrollBar().value()
                self._tagger_normal_current_row_before_checked_only = self.list_widget.currentRow()
            else:
                self._normal_scroll_before_checked_only = self.list_widget.verticalScrollBar().value()
                self._normal_current_row_before_checked_only = self.list_widget.currentRow()

            self._set_checked_only_enabled_for_active_context(True)
            self._apply_checked_visibility()
            return

        # 通常表示へ戻す。
        if is_tagger:
            restore_scroll = self._tagger_normal_scroll_before_checked_only
            restore_row = self._tagger_normal_current_row_before_checked_only
        else:
            restore_scroll = self._normal_scroll_before_checked_only
            restore_row = self._normal_current_row_before_checked_only

        self._set_checked_only_enabled_for_active_context(False)
        self._apply_checked_visibility()

        if restore_row is not None and 0 <= restore_row < self.list_widget.count():
            self.list_widget.setCurrentItem(
                self.list_widget.item(restore_row),
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )

        def restore_scroll_position() -> None:
            if restore_scroll is not None:
                scroll_bar = self.list_widget.verticalScrollBar()
                scroll_bar.setValue(
                    max(scroll_bar.minimum(), min(restore_scroll, scroll_bar.maximum()))
                )

            if is_tagger:
                self._tagger_normal_scroll_before_checked_only = None
                self._tagger_normal_current_row_before_checked_only = None
            else:
                self._normal_scroll_before_checked_only = None
                self._normal_current_row_before_checked_only = None

        # setHidden解除直後はレイアウト更新がまだ終わっていないことがあるので、
        # 次のイベントループでスクロール位置を戻す。
        QTimer.singleShot(0, restore_scroll_position)

    def _tag_filter_is_active(self) -> bool:
        """
        編集用のinclude / exclude条件が、現在のサムネイル表示へ効くかを返す。

        条件そのものはタグ編集タブ側の状態として保持するが、Taggerタブでは
        Tagger専用の対象選択と「チェック中画像のみ表示」だけを使う。
        """
        return (
            self._selection_context == "edit"
            and bool(self._include_filter_tags or self._exclude_filter_tags)
        )

    def _filter_base_count(self) -> int:
        """フィルタ状態バーの分母となる、現在の母集団画像数を返す。"""
        # Taggerタブでは編集用の作業セットを操作対象にしない。
        if self._selection_context != "tagger" and self._checked_work_mode:
            return len(self.work_set_indexes())
        return len(self.items)

    def _update_filter_bar(self, visible_count: int | None = None) -> None:
        """
        緑/赤タグフィルタが有効な場合だけ、サムネイル欄へ結果表示バーを出す。

        条件の確認・個別解除・全解除は右側タグ一覧パネルが担当する。
        右側タグ一覧を表示中画像へ追従させるため、フィルタ有無と
        現在表示中の画像indexも同時に通知する。
        """
        started = time.perf_counter()
        base_count = self._filter_base_count()
        filter_active = self._tag_filter_is_active()

        if not filter_active:
            self.filter_bar.setVisible(False)
            emit_started = time.perf_counter()
            self.tag_filter_view_changed.emit(False, [], 0, base_count)
            emit_ms = (time.perf_counter() - emit_started) * 1000
            if dev_log_enabled("PERF_FILTER"):
                log.info(
                    "[PERF] thumbnail.update_filter_bar active=False base=%d "
                    "signal_to_tag_panel=%.2fms total=%.2fms",
                    base_count,
                    emit_ms,
                    (time.perf_counter() - started) * 1000,
                )
            return

        index_started = time.perf_counter()
        visible_indexes = self.visible_indexes()
        index_ms = (time.perf_counter() - index_started) * 1000
        if visible_count is None:
            visible_count = len(visible_indexes)

        self.filter_count_label.setText(f"表示: {visible_count} / {base_count} 枚")
        self.filter_bar.setVisible(True)

        emit_started = time.perf_counter()
        self.tag_filter_view_changed.emit(
            True,
            visible_indexes,
            visible_count,
            base_count,
        )
        emit_ms = (time.perf_counter() - emit_started) * 1000
        if dev_log_enabled("PERF_FILTER"):
            log.info(
                "[PERF] thumbnail.update_filter_bar active=True visible=%d base=%d "
                "visible_indexes=%.2fms signal_to_tag_panel=%.2fms total=%.2fms",
                visible_count,
                base_count,
                index_ms,
                emit_ms,
                (time.perf_counter() - started) * 1000,
            )

    def _apply_checked_visibility(self) -> int:
        started = time.perf_counter()
        visible_count = 0
        changed_hidden_count = 0

        loop_started = time.perf_counter()
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            index = item.data(Qt.ItemDataRole.UserRole)
            visible = self._is_index_visible(index)
            hidden = not visible
            if item.isHidden() != hidden:
                changed_hidden_count += 1
            item.setHidden(hidden)

            if visible:
                visible_count += 1
        loop_ms = (time.perf_counter() - loop_started) * 1000

        notify_started = time.perf_counter()
        self._update_filter_bar(visible_count)
        self._refresh_filename_search_matches()
        if changed_hidden_count > 0:
            self.visible_view_changed.emit()
        notify_ms = (time.perf_counter() - notify_started) * 1000

        if dev_log_enabled("PERF_FILTER"):
            log.info(
                "[PERF] thumbnail.apply_visibility rows=%d visible=%d hidden_changed=%d "
                "set_hidden_loop=%.2fms bar_and_tagpanel_notify=%.2fms total=%.2fms",
                self.list_widget.count(),
                visible_count,
                changed_hidden_count,
                loop_ms,
                notify_ms,
                (time.perf_counter() - started) * 1000,
            )
        return visible_count

    def _is_index_visible(self, index: int) -> bool:
        if not (0 <= index < len(self.items)):
            return False

        item = self.items[index]

        if self._selection_context == "tagger":
            # Taggerは編集用の作業セット・タグフィルタとは独立。
            # Tagger専用の選択と「チェック中画像のみ表示」だけを可視条件にする。
            if self._tagger_show_checked_only and not self._is_tagger_selected_index(index):
                return False
            return True

        # 作業セットは編集側の表示母集団。チェック中画像のみ表示がONなら、
        # その作業セットの中から黄色チェック中画像だけへさらに絞り込む。
        if self._checked_work_mode and item.image_path not in self._work_set_paths:
            return False
        if self._show_checked_only and not item.checked:
            return False

        return self._item_matches_tag_filter(item)

    def _item_matches_tag_filter(self, item: ImageItem) -> bool:
        tags = set(item.tags)

        if self._include_filter_tags and not self._include_filter_tags.issubset(tags):
            return False

        if self._exclude_filter_tags and self._exclude_filter_tags.intersection(tags):
            return False

        return True

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        # 通常クリックはチェック状態を変えず、現在画像だけを切り替える。
        # Taggerタブでは編集用の青枠を動かさない。
        self.list_widget.clearSelection()
        index = item.data(Qt.ItemDataRole.UserRole)
        if self._selection_context == "tagger":
            self.tagger_image_selected.emit(index)
        else:
            self.image_selected.emit(index)
        self.list_widget.viewport().update()

    def _on_modifier_item_clicked(self, item: QListWidgetItem, operation: str) -> None:
        """
        Ctrl/Shift + 左クリックを、黄色チェックの高速操作として扱う。

        Ctrl:
            チェック中画像が0枚の状態から操作を開始した場合は、
            直前にプレビュー表示していた青枠画像もチェック対象へ追加する。
            すでにチェック中画像がある場合は、クリックした1枚だけをON/OFF反転する。

        Shift:
            現在プレビュー中の画像からクリック画像までを範囲操作。
            クリック先が未チェックなら範囲をすべてON、
            チェック済みなら範囲をすべてOFFにする。
        """
        if self._tagger_execution_locked:
            return

        index = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(index, int) or not (0 <= index < len(self.items)):
            return

        if self._selection_context == "tagger":
            previous_preview_index = self._tagger_preview_index
            had_selected_items = bool(self.tagger_selected_indexes())
            should_select = not self._is_tagger_selected_index(index)

            # Tagger用の修飾クリックも、Tagger用の現在画像だけを移動する。
            self.tagger_image_selected.emit(index)
            self.list_widget.clearSelection()

            if operation == "shift":
                indexes = self._visible_range_indexes(previous_preview_index, index)
            elif operation == "ctrl":
                indexes = [index]
                # 編集側と同じ操作感として、Tagger対象が0件の最初のCtrl操作だけは
                # 直前のTagger用青枠も対象へ加える。
                if (
                    not had_selected_items
                    and should_select
                    and 0 <= previous_preview_index < len(self.items)
                    and previous_preview_index != index
                    and self._is_index_visible(previous_preview_index)
                ):
                    indexes.insert(0, previous_preview_index)
            else:
                return

            self._set_tagger_selected_indexes(indexes, should_select)
            return

        previous_preview_index = self._preview_index
        had_checked_items = bool(self.checked_indexes())
        should_check = not self.items[index].checked

        # 修飾クリックした画像も現在プレビューへ反映する。
        # emit 後は _preview_index がクリック先へ移動するため、
        # 起点判定には上で退避した previous_preview_index を使う。
        self.image_selected.emit(index)
        self.list_widget.clearSelection()

        if operation == "shift":
            indexes = self._visible_range_indexes(previous_preview_index, index)

        elif operation == "ctrl":
            indexes = [index]

            # チェック対象を作り始める最初の Ctrl 操作だけ、
            # 直前の青枠画像も黄色チェックへ昇格させる。
            #
            # 例:
            #   A: 青枠、チェック0枚
            #   Ctrl + B
            #   -> A: 黄色 / B: 黄色 + 青枠
            if (
                not had_checked_items
                and should_check
                and 0 <= previous_preview_index < len(self.items)
                and previous_preview_index != index
                and self._is_index_visible(previous_preview_index)
            ):
                indexes.insert(0, previous_preview_index)

        else:
            return

        self._set_checked_indexes(indexes, should_check)

    def _visible_range_indexes(self, start_index: int, end_index: int) -> list[int]:
        """Shift 範囲操作では、現在画面に表示されている画像だけを対象にする。"""
        visible_indexes = self.visible_indexes()
        if start_index not in visible_indexes or end_index not in visible_indexes:
            return [end_index]

        start_position = visible_indexes.index(start_index)
        end_position = visible_indexes.index(end_index)
        low, high = sorted((start_position, end_position))
        return visible_indexes[low : high + 1]

    def _toggle_current_check(self) -> None:
        """Space キーで、現在タブの現在画像を選択ON/OFFする。"""
        if self._tagger_execution_locked:
            return
        if self._selection_context == "tagger":
            index = self._tagger_preview_index
            if not (0 <= index < len(self.items)) or not self._is_index_visible(index):
                return
            self._set_tagger_selected_indexes(
                [index],
                not self._is_tagger_selected_index(index),
            )
            return

        if not (0 <= self._preview_index < len(self.items)):
            return
        if not self._is_index_visible(self._preview_index):
            return

        should_check = not self.items[self._preview_index].checked
        self._set_checked_indexes([self._preview_index], should_check)

    def _set_checked_indexes(self, indexes: list[int], checked: bool) -> None:
        if self._tagger_execution_locked:
            return
        valid_indexes = [
            index for index in indexes if isinstance(index, int) and 0 <= index < len(self.items)
        ]
        if not valid_indexes:
            return

        next_state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked

        self.list_widget.blockSignals(True)
        try:
            for index in valid_indexes:
                self.items[index].checked = checked
                widget_item = self.list_widget.item(index)
                widget_item.setCheckState(next_state)
                self._apply_thumbnail_item_style(widget_item)
        finally:
            self.list_widget.blockSignals(False)

        self._apply_checked_visibility()
        self.list_widget.viewport().update()
        self.checks_changed.emit()

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        index = item.data(Qt.ItemDataRole.UserRole)
        if not (0 <= index < len(self.items)):
            return

        if self._tagger_execution_locked:
            expected_state = (
                Qt.CheckState.Checked
                if self._is_active_selected_index(index)
                else Qt.CheckState.Unchecked
            )
            if item.checkState() != expected_state:
                was_blocked = self.list_widget.blockSignals(True)
                try:
                    item.setCheckState(expected_state)
                finally:
                    self.list_widget.blockSignals(was_blocked)
            self._apply_thumbnail_item_style(item)
            return

        if self._selection_context == "tagger":
            self._set_tagger_selected_indexes(
                [index],
                item.checkState() == Qt.CheckState.Checked,
            )
            return

        if 0 <= index < len(self.items):
            self.items[index].checked = item.checkState() == Qt.CheckState.Checked
            self._apply_thumbnail_item_style(item)
            self._apply_checked_visibility()

            # 黄色枠はアイコンを書き換えず、delegateの再描画だけで反映する。
            # itemChanged内でsetIconすると、環境によってitemChangedが再発火し続けて重くなる。
            self.list_widget.viewport().update()

            self.checks_changed.emit()

    def _apply_thumbnail_item_style(self, widget_item: QListWidgetItem) -> None:
        index = widget_item.data(Qt.ItemDataRole.UserRole)
        if not (0 <= index < len(self.items)):
            return

        if self._is_active_selected_index(index):
            if self._selection_context == "tagger":
                widget_item.setBackground(QColor("#1d4148"))
            else:
                widget_item.setBackground(QColor("#4a3d20"))
            widget_item.setForeground(QColor("#ffffff"))
        else:
            widget_item.setBackground(QColor("#101010"))
            widget_item.setForeground(QColor("#e8e8e8"))
