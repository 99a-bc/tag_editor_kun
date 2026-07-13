from __future__ import annotations

from collections import Counter
from pathlib import Path
import time
from urllib.parse import quote_plus

from logger import get_logger
from dev_config import dev_log_enabled

from PySide6.QtCore import QEvent, QMimeData, QPoint, QRect, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QDrag
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QMessageBox,
    QMenu,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from theme import (
    ADD_TAG_DIALOG_QSS,
    ALL_TAG_BUTTON_QSS,
    ALL_TAG_BUTTON_SEARCH_CURRENT_QSS,
    ALL_TAG_BUTTON_SEARCH_HIGHLIGHT_QSS,
    ALL_TAG_CHIP_QSS,
    ALL_TAG_CHIP_SEARCH_CURRENT_QSS,
    ALL_TAG_CHIP_SEARCH_HIGHLIGHT_QSS,
    CHIP_SCROLL_QSS,
    GREEN,
    RED,
    TAG_CHIP_DRAGGING_QSS,
    TAG_CHIP_QSS,
    TAG_CHIP_SEARCH_CURRENT_QSS,
    TAG_CHIP_SEARCH_HIGHLIGHT_QSS,
    TAG_REORDER_MARKER_QSS,
    TAG_SEARCH_BAR_QSS,
    TAG_SORT_SEARCH_POPUP_QSS,
    TAG_SUB_TABS_QSS,
    TAG_TABS_QSS,
    TARGET_LABEL_QSS,
)

from models import ImageItem
from tag_history import MAX_TAG_HISTORY_ENTRIES, TagHistoryEntry
from history_dialog import OperationHistoryDialog
from tag_io import parse_tags
from danbooru_tag_dictionary import DanbooruTagDictionary
from tag_completion import TagCompletionController
from settings import (
    DEFAULT_TAG_COMPLETION_ENABLED,
    DEFAULT_TAG_COMPLETION_LIMIT,
    normalize_tag_completion_limit,
)
from tag_color_dictionary import (
    TagColorDictionary,
    all_tag_button_qss,
    all_tag_chip_qss,
    tag_chip_qss,
)


log = get_logger(__name__)
log.info("tag_panel.py loaded from: %s", Path(__file__).resolve())

CURRENT_TAG_REORDER_MIME = "application/x-tag-editor-current-tag-reorder"

# 右上に浮かせる保存ボタンの見た目調整用。
# 大きさを変えたい時は WIDTH / HEIGHT、色を変えたい時は下の色定数を触る。
FLOATING_SAVE_BUTTON_WIDTH = 100
FLOATING_SAVE_BUTTON_HEIGHT = 45
FLOATING_SAVE_BUTTON_MARGIN = 12
FLOATING_SAVE_BUTTON_BG = "#284761"
FLOATING_SAVE_BUTTON_BG_HOVER = "#315872"
FLOATING_SAVE_BUTTON_BG_PRESSED = "#223a50"
FLOATING_SAVE_BUTTON_BORDER = "#35566f"
FLOATING_SAVE_BUTTON_BORDER_HOVER = "#4a718c"
FLOATING_SAVE_BUTTON_BORDER_PRESSED = "#5f839f"
FLOATING_SAVE_BUTTON_TEXT = "#e8f3ff"

# Qtの実質的な無制限幅。右側AllTagChipは、FlowLayoutの実効幅に合わせて
# 表示文字列だけを事前調整し、配置中に自身のgeometryを変更しない。
_ALL_TAG_CHIP_UNBOUNDED_WIDTH = 16_777_215


def shorten_text(text: str, max_length: int = 80) -> str:
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."


SEARCH_MATCH_BG = QColor("#ff4ff3")
SEARCH_MATCH_TEXT = QColor("#ffffff")
SEARCH_CURRENT_MATCH_BG = QColor("#35d878")
SEARCH_CURRENT_MATCH_TEXT = QColor("#07130b")

def _normalized_search_index_map(text: str) -> tuple[str, list[int]]:
    """
    検索ハイライト描画用に、空白/underscoreをゆるく同一視した文字列と
    元文字位置の対応を作る。
    """
    normalized_chars: list[str] = []
    index_map: list[int] = []
    previous_space = False

    for index, char in enumerate(text):
        normalized_char = " " if char == "_" or char.isspace() else char.casefold()

        if normalized_char == " ":
            if previous_space:
                continue
            previous_space = True
        else:
            previous_space = False

        normalized_chars.append(normalized_char)
        index_map.append(index)

    # 先頭/末尾空白を落としながら index_map も揃える。
    while normalized_chars and normalized_chars[0] == " ":
        normalized_chars.pop(0)
        index_map.pop(0)
    while normalized_chars and normalized_chars[-1] == " ":
        normalized_chars.pop()
        index_map.pop()

    return "".join(normalized_chars), index_map


def _search_highlight_span(display_text: str, query_text: str) -> tuple[int, int] | None:
    """表示中テキスト内で、検索語に対応する範囲を返す。"""
    query = query_text.strip()
    if not display_text or not query:
        return None

    display_folded = display_text.casefold()
    query_folded = query.casefold()

    # まずは見たまま一致を優先する。
    direct_index = display_folded.find(query_folded)
    if direct_index >= 0:
        return direct_index, direct_index + len(query)

    # 空白/underscoreの違いを吸収した一致も試す。
    normalized_text, index_map = _normalized_search_index_map(display_text)
    normalized_query = " ".join(query.casefold().replace("_", " ").split())
    if not normalized_text or not normalized_query:
        return None

    normalized_index = normalized_text.find(normalized_query)
    if normalized_index < 0:
        return None

    start = index_map[normalized_index]
    end_map_index = normalized_index + len(normalized_query) - 1
    if end_map_index >= len(index_map):
        return None

    end = index_map[end_map_index] + 1
    return start, end


class SearchHighlightButton(QPushButton):
    """
    タグ名ボタン用の検索ハイライト描画ボタン。

    チップ全体のQSSは通常/カテゴリ色/選択破線を維持し、
    検索一致部分の文字背景だけを自前描画で反転表示する。
    """

    def __init__(self, display_text: str) -> None:
        super().__init__(display_text)
        self._display_text = display_text
        self._search_query_text = ""
        self._search_current = False
        self._search_exact_match = False

    def set_display_text(self, display_text: str) -> bool:
        if self._display_text == display_text:
            return False

        self._display_text = display_text
        self.setText(display_text)
        self.updateGeometry()
        self.update()
        return True

    def set_search_marker(
        self,
        query_text: str,
        *,
        current: bool = False,
        exact_match: bool = False,
    ) -> bool:
        next_query = query_text if query_text else ""
        next_current = bool(current and next_query)
        next_exact_match = bool(exact_match and next_query)

        if (
            self._search_query_text == next_query
            and self._search_current == next_current
            and self._search_exact_match == next_exact_match
        ):
            return False

        self._search_query_text = next_query
        self._search_current = next_current
        self._search_exact_match = next_exact_match
        self.update()
        return True

    def paintEvent(self, event) -> None:  # noqa: N802
        """
        通常時はQt標準のQPushButton描画へ任せる。

        タグチップのpadding・border・選択状態はQSSが決めているため、通常文字まで
        tightBoundingRect() 基準で自前描画すると、字形ごとの左右ベアリング差が
        「余白が不揃い」に見える原因になる。検索一致がある時だけ、背景ハイライトを
        重ねるために文字描画を自前で行う。
        """
        text = self._display_text
        if not text:
            super().paintEvent(event)
            return

        if self._search_exact_match and self._search_query_text:
            span = (0, len(text))
        else:
            span = _search_highlight_span(text, self._search_query_text)

        if span is None:
            super().paintEvent(event)
            return

        layout_option = QStyleOptionButton()
        self.initStyleOption(layout_option)

        content_rect = self.style().subElementRect(
            QStyle.SubElement.SE_PushButtonContents,
            layout_option,
            self,
        )
        if not content_rect.isValid() or content_rect.width() <= 0:
            content_rect = self.rect().adjusted(4, 2, -4, -2)

        # 枠・背景だけは標準描画へ任せ、文字と検索マーカーだけを重ねる。
        draw_option = QStyleOptionButton(layout_option)
        draw_option.text = ""

        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, draw_option)

        # QStylePainter(self) はウィジェットのフォントを既に使う。ここで改めて
        # setFont(self.font()) を呼ぶと、pointSize=-1（pixelSize指定）の継承フォントを
        # Qtが再設定しようとして警告を出す場合があるため、フォント設定は触らない。
        metrics = self.fontMetrics()

        # Qt標準の中央寄せと同じくadvance幅を基準にする。
        # tightBoundingRect() は字形の左右ベアリングを含まないため、通常時のQSS
        # paddingと別の基準で中央寄せされ、タグごとに余白が揺れて見える。
        text_width = metrics.horizontalAdvance(text)
        text_x = content_rect.left() + max(
            0,
            (content_rect.width() - text_width) // 2,
        )
        baseline_y = content_rect.top() + (
            content_rect.height() + metrics.ascent() - metrics.descent()
        ) // 2

        text_color = layout_option.palette.buttonText().color()
        if not text_color.isValid():
            text_color = QColor("#e8e8e8")

        # ハイライト文字もQPushButtonの内容領域から外へ出さない。
        safe_clip_rect = content_rect.intersected(self.rect())
        painter.save()
        painter.setClipRect(safe_clip_rect)

        start, end = span
        start = max(0, min(start, len(text)))
        end = max(start, min(end, len(text)))

        before_text = text[:start]
        matched_text = text[start:end]
        after_text = text[end:]

        before_width = metrics.horizontalAdvance(before_text)
        matched_width = metrics.horizontalAdvance(matched_text)

        marker_bg = SEARCH_CURRENT_MATCH_BG if self._search_current else SEARCH_MATCH_BG
        marker_text = SEARCH_CURRENT_MATCH_TEXT if self._search_current else SEARCH_MATCH_TEXT

        marker_rect = QRect(
            text_x + before_width,
            baseline_y - metrics.ascent() - 2,
            max(1, matched_width),
            metrics.height() + 3,
        ).intersected(safe_clip_rect)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(marker_bg)
        painter.drawRoundedRect(marker_rect, 3, 3)

        painter.setPen(text_color)
        painter.drawText(text_x, baseline_y, before_text)

        painter.setPen(marker_text)
        painter.drawText(text_x + before_width, baseline_y, matched_text)

        painter.setPen(text_color)
        painter.drawText(
            text_x + before_width + matched_width,
            baseline_y,
            after_text,
        )
        painter.restore()
        painter.end()


class TagChip(QFrame):
    context_menu_requested = Signal(str, QPoint)
    content_width_changed = Signal()

    """
    選択中画像のタグ用チップ。
    クリックで選択/解除。

    badge_text を指定すると、タグ本体の外側に "×2" や "3/10" のような
    件数バッジを表示する。
    内部タグ名は self.tag に保持し、選択状態は内部の tag_button が保持する。
    """

    def __init__(
        self,
        tag: str,
        badge_text: str | None = None,
        badge_kind: str = "duplicate",
    ) -> None:
        super().__init__()
        self.tag = tag
        self._drag_enabled = False
        self._drag_start_pos: QPoint | None = None
        # 大量の左側チェック中タグチップ再利用時に、同じ状態へ何度も
        # setText / setStyleSheet / setChecked / cursor変更をかけると重くなるため、
        # 現在状態を保持して差分だけ更新する。
        self._badge_text: str | None = badge_text
        self._search_highlight_state: str = "normal"
        self._normal_qss = TAG_CHIP_QSS
        self._content_width_sync_pending = False
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        display_tag = shorten_text(tag, 44)

        self.tag_button = SearchHighlightButton(display_tag)
        self.tag_button.setToolTip(tag)
        self.tag_button.setCheckable(True)
        self.tag_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tag_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.tag_button.setStyleSheet(self._normal_qss)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addWidget(self.tag_button)

        self._install_context_menu(self)
        self._install_context_menu(self.tag_button)
        self.tag_button.installEventFilter(self)

        if badge_text:
            self.count_badge = QLabel(badge_text)
            self.count_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.count_badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

            if badge_kind == "ratio":
                # チェック画像の n/チェック枚数 用。
                # タグ本体より控えめに見えるよう、小さめ・薄めにする。
                self.count_badge.setStyleSheet(
                    """
                    QLabel {
                        color: #d7bb73;
                        background: transparent;
                        font-size: 10px;
                        font-weight: bold;
                        padding-left: 0px;
                        padding-right: 2px;
                        padding-top: 0px;
                        padding-bottom: 0px;
                    }
                    """
                )
            else:
                # 表示中画像の重複表示 "×2" 用。
                self.count_badge.setToolTip("この画像内でタグが重複しています")
                self.count_badge.setStyleSheet(
                    """
                    QLabel {
                        color: #f0c96a;
                        background: transparent;
                        font-weight: bold;
                        padding-left: 1px;
                        padding-right: 3px;
                    }
                    """
                )

            layout.addWidget(self.count_badge)
            self._install_context_menu(self.count_badge)
        else:
            self.count_badge = None

        # ChipWrapAreaの行レイアウトに入る前に、子ボタンとバッジから
        # 自然な幅を確定する。行レイアウト側に余った伸縮余地があっても、
        # タグチップ自体を希望幅より縮めない。
        self.sync_content_width()
        self._schedule_content_width_sync()

    def sync_content_width(self) -> bool:
        """
        タグ名ボタンと任意の件数バッジから、TagChipの自然な横幅を確定する。

        QSS適用前の一時的なsizeHintを固定値として持ち越さないよう、内側ボタンは
        固定幅にしない。最終的に確定した各子Widgetの必要幅を合計して、外側TagChip
        だけを固定する。
        """
        layout = self.layout()
        if layout is None:
            return False

        self.tag_button.ensurePolished()
        if self.count_badge is not None:
            self.count_badge.ensurePolished()

        # 以前の実験で設定された古い固定幅を解除する。カテゴリ色QSSのfont-weight
        # や件数バッジのfont-sizeが確定した後は、子Widget自身のsizeHintを使う。
        self.tag_button.setMinimumWidth(0)
        self.tag_button.setMaximumWidth(_ALL_TAG_CHIP_UNBOUNDED_WIDTH)
        self.tag_button.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )

        button_width = max(
            1,
            self.tag_button.sizeHint().width(),
            self.tag_button.minimumSizeHint().width(),
        )

        badge_width = 0
        has_badge = self.count_badge is not None and not self.count_badge.isHidden()
        if has_badge and self.count_badge is not None:
            badge_width = max(
                1,
                self.count_badge.sizeHint().width(),
                self.count_badge.minimumSizeHint().width(),
            )

        margins = layout.contentsMargins()
        natural_width = (
            margins.left()
            + button_width
            + (layout.spacing() + badge_width if has_badge else 0)
            + margins.right()
        )

        # Style実装差で手計算より大きい最小サイズが返る場合だけは、それも尊重する。
        layout.invalidate()
        natural_width = max(
            1,
            natural_width,
            layout.minimumSize().width(),
        )

        if (
            self.minimumWidth() == natural_width
            and self.maximumWidth() == natural_width
        ):
            return False

        self.setFixedWidth(natural_width)
        self.updateGeometry()
        self.content_width_changed.emit()
        return True

    def _schedule_content_width_sync(self) -> None:
        """QSSのfont/padding確定後に、1回だけ幅を再測定する。"""
        if self._content_width_sync_pending:
            return

        self._content_width_sync_pending = True
        QTimer.singleShot(0, self._sync_content_width_after_style)

    def _sync_content_width_after_style(self) -> None:
        self._content_width_sync_pending = False
        self.sync_content_width()

    def _install_context_menu(self, widget: QWidget) -> None:
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        widget.customContextMenuRequested.connect(
            lambda pos, source=widget: self.context_menu_requested.emit(
                self.tag,
                source.mapToGlobal(pos),
            )
        )

    def set_drag_enabled(self, enabled: bool) -> bool:
        """
        左側通常状態のタグ順ドラッグ並び替えを有効/無効にする。

        戻り値は実際にカーソル更新したかどうか。
        """
        if self._drag_enabled == enabled:
            return False

        self._drag_enabled = enabled
        self.tag_button.setCursor(
            Qt.CursorShape.OpenHandCursor if enabled else Qt.CursorShape.PointingHandCursor
        )
        return True

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.tag_button and self._drag_enabled:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_start_pos = event.position().toPoint()
            elif event.type() == QEvent.Type.MouseMove and self._drag_start_pos is not None:
                distance = (event.position().toPoint() - self._drag_start_pos).manhattanLength()
                if distance >= QApplication.startDragDistance():
                    self._start_drag()
                    self._drag_start_pos = None
                    return True
            elif event.type() in (
                QEvent.Type.MouseButtonRelease,
                QEvent.Type.MouseButtonDblClick,
                QEvent.Type.Leave,
            ):
                self._drag_start_pos = None

        return super().eventFilter(watched, event)

    def _start_drag(self) -> None:
        mime_data = QMimeData()
        mime_data.setData(CURRENT_TAG_REORDER_MIME, self.tag.encode("utf-8"))

        drag = QDrag(self)
        drag.setMimeData(mime_data)
        drag.setHotSpot(QPoint(self.width() // 2, self.height() // 2))

        previous_checked = self.tag_button.isChecked()
        self.tag_button.setStyleSheet(TAG_CHIP_DRAGGING_QSS)
        drag.exec(Qt.DropAction.MoveAction)
        self.tag_button.setChecked(previous_checked)
        self.tag_button.setStyleSheet(self._normal_qss)

    def update_badge_text(self, badge_text: str | None) -> bool:
        """
        キャッシュ再利用時にバッジ表示だけを更新する。

        チェック中画像の集計では、同じタグチップを使い回しながら
        "123/436" のような件数表示だけを更新する。
        戻り値は実際に見た目を更新したかどうか。
        """
        if self.count_badge is None:
            self._badge_text = badge_text
            return False

        if self._badge_text == badge_text:
            return False

        self._badge_text = badge_text

        if badge_text:
            self.count_badge.setText(badge_text)
            if not self.count_badge.isVisible():
                self.count_badge.show()
        else:
            if self.count_badge.isVisible():
                self.count_badge.hide()

        self.sync_content_width()
        self._schedule_content_width_sync()
        return True

    def set_search_highlight(
        self,
        highlighted: bool,
        current: bool = False,
        query_text: str = "",
        exact_match: bool = False,
    ) -> bool:
        """
        左側タグ一覧の検索一致ハイライトを切り替える。

        チップ全体の色は変えず、検索一致部分の文字背景だけを反転表示する。
        """
        if current:
            next_state = "current"
        elif highlighted:
            next_state = "highlight"
        else:
            next_state = "normal"

        state_changed = self._search_highlight_state != next_state
        self._search_highlight_state = next_state

        marker_changed = self.tag_button.set_search_marker(
            query_text if highlighted or current else "",
            current=current,
            exact_match=exact_match,
        )
        return state_changed or marker_changed

    def set_category_style(self, qss: str | None) -> bool:
        """通常状態のカテゴリ色QSSを更新する。"""
        next_qss = qss or TAG_CHIP_QSS
        if self._normal_qss == next_qss:
            return False

        self._normal_qss = next_qss
        self.tag_button.setStyleSheet(self._normal_qss)
        self.sync_content_width()
        self._schedule_content_width_sync()
        return True

    def isChecked(self) -> bool:  # noqa: N802
        return self.tag_button.isChecked()

    def setChecked(self, checked: bool) -> bool:  # noqa: N802
        """選択状態を変更する。戻り値は実際に変更したかどうか。"""
        if self.tag_button.isChecked() == checked:
            return False

        self.tag_button.setChecked(checked)
        self._schedule_content_width_sync()
        return True


class AllTagChip(QFrame):
    context_menu_requested = Signal(str, QPoint)

    """
    全てのタグ一覧用チップ。

    左の緑ボタンは含める条件、右の赤ボタンは除外条件、中央のタグ本体は選択状態を
    それぞれ独立したクリック領域として扱う。
    使用数はタグチップ本体の外側に小さめのバッジとして表示する。
    """

    clicked = Signal(str)
    include_clicked = Signal(str)
    exclude_clicked = Signal(str)

    def __init__(self, tag: str, count: int) -> None:
        super().__init__()
        self.tag = tag
        # 検索ハイライトのstyleSheet再適用は大量タグで重いため、
        # 現在状態を持って同じ状態なら何もしない。
        self._search_highlight_state: str = "normal"
        self._normal_chip_qss = ALL_TAG_CHIP_QSS
        self._normal_button_qss = ALL_TAG_BUTTON_QSS
        self._display_width_budget: int | None = None
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setObjectName("AllTagChipWrapper")

        self.chip_frame = QFrame()
        self.chip_frame.setObjectName("AllTagChip")
        self.chip_frame.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.include_button = QPushButton("○")
        self.include_button.setCheckable(True)
        self.include_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.include_button.setFixedWidth(28)
        self.include_button.setToolTip("このタグを含む画像を表示")
        self.include_button.clicked.connect(self._on_include_clicked)
        self.include_button.setStyleSheet(
            f"""
            QPushButton {{
                color: {GREEN};
                background: transparent;
                border: none;
                font-size: 18px;
                font-weight: bold;
                padding: 0px;
            }}
            QPushButton:hover {{
                background: rgba(34, 197, 94, 0.10);
            }}
            QPushButton:checked {{
                color: #8dffae;
                background: rgba(34, 197, 94, 0.22);
                border: 1px solid #3fbf75;
                border-radius: 6px;
            }}
            QPushButton:checked:hover {{
                color: #b2ffc8;
                background: rgba(34, 197, 94, 0.30);
                border: 1px solid #72ff9c;
                border-radius: 6px;
            }}
            """
        )

        self.exclude_button = QPushButton("○")
        self.exclude_button.setCheckable(True)
        self.exclude_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.exclude_button.setFixedWidth(28)
        self.exclude_button.setToolTip("このタグを含まない画像を表示")
        self.exclude_button.clicked.connect(self._on_exclude_clicked)
        self.exclude_button.setStyleSheet(
            f"""
            QPushButton {{
                color: {RED};
                background: transparent;
                border: none;
                font-size: 18px;
                font-weight: bold;
                padding: 0px;
            }}
            QPushButton:hover {{
                background: rgba(239, 68, 68, 0.10);
            }}
            QPushButton:checked {{
                color: #ff9a9a;
                background: rgba(239, 68, 68, 0.22);
                border: 1px solid #d35b5b;
                border-radius: 6px;
            }}
            QPushButton:checked:hover {{
                color: #ffb8b8;
                background: rgba(239, 68, 68, 0.30);
                border: 1px solid #ff8a8a;
                border-radius: 6px;
            }}
            """
        )

        # 初期状態では全文を持たせる。右側一覧へ並べる直前に、
        # チップ全体の利用可能幅で必要な場合だけ省略する。
        display_tag = tag

        self.tag_button = SearchHighlightButton(display_tag)
        self.tag_button.setCheckable(True)
        self.tag_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tag_button.setToolTip(f"{tag}\n使用数: {count}")
        self.tag_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.tag_button.clicked.connect(lambda: self.clicked.emit(self.tag))
        self.tag_button.setStyleSheet(self._normal_button_qss)

        chip_layout = QHBoxLayout(self.chip_frame)
        chip_layout.setContentsMargins(2, 2, 2, 2)
        chip_layout.setSpacing(0)
        chip_layout.addWidget(self.include_button)
        chip_layout.addWidget(self._make_separator())
        chip_layout.addWidget(self.tag_button)
        chip_layout.addWidget(self._make_separator())
        chip_layout.addWidget(self.exclude_button)

        self.chip_frame.setStyleSheet(self._normal_chip_qss)

        self.count_badge = QLabel(str(count))
        self.count_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count_badge.setToolTip(f"使用数: {count}")
        self.count_badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.count_badge.setStyleSheet(
            """
            QLabel {
                color: #d7bb73;
                background: transparent;
                font-size: 10px;
                font-weight: bold;
                padding-left: 1px;
                padding-right: 2px;
                padding-top: 0px;
                padding-bottom: 0px;
            }
            """
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addWidget(self.chip_frame)
        layout.addWidget(self.count_badge)

        self._install_context_menu(self)
        self._install_context_menu(self.chip_frame)
        self._install_context_menu(self.include_button)
        self._install_context_menu(self.tag_button)
        self._install_context_menu(self.exclude_button)
        self._install_context_menu(self.count_badge)

    def _invalidate_display_width_budget(self) -> None:
        """件数やカテゴリ色が変わった後、次の一覧再構築で再測定させる。"""
        self._display_width_budget: int | None = None

    def _refresh_chip_size_hint(self) -> None:
        """子ボタンの表示文字列を変えた後、親レイアウトのsizeHintを更新する。"""
        self.tag_button.ensurePolished()
        self.count_badge.ensurePolished()

        chip_layout = self.chip_frame.layout()
        outer_layout = self.layout()
        if chip_layout is not None:
            chip_layout.invalidate()
            chip_layout.activate()
        if outer_layout is not None:
            outer_layout.invalidate()
            outer_layout.activate()

    def _total_width_for_display_text(self, display_text: str) -> int:
        """指定表示文字列での、使用数バッジ込みチップ全体の自然幅を返す。"""
        self.tag_button.set_display_text(display_text)
        self._refresh_chip_size_hint()
        return max(1, self.sizeHint().width())

    def fit_display_to_total_width(self, max_total_width: int) -> bool:
        """チップ全体が指定幅に収まるよう、必要な時だけタグ名を省略する。

        幅の判定には実際のQHBoxLayoutのsizeHintを使う。つまり緑/赤ボタン、
        区切り、内側余白、使用数バッジをすべて含めた幅で判定できる。
        ここでは固定幅を設定しないため、FlowLayoutのsetGeometry中に新たな
        レイアウト要求を出さず、再配置ループを起こさない。
        """
        next_budget = max(1, int(max_total_width))
        if self._display_width_budget == next_budget:
            return False

        self._display_width_budget = next_budget

        # 前回の状態に左右されず、まず全文表示の自然幅を測る。
        self.setMinimumWidth(0)
        self.setMaximumWidth(_ALL_TAG_CHIP_UNBOUNDED_WIDTH)
        self.tag_button.setMinimumWidth(0)
        self.tag_button.setMaximumWidth(_ALL_TAG_CHIP_UNBOUNDED_WIDTH)
        self.tag_button.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )

        full_width = self._total_width_for_display_text(self.tag)
        if full_width <= next_budget:
            self.updateGeometry()
            return True

        # 全文が入らない時だけ、最大で入る接頭辞を二分探索する。
        # 省略文字列そのものも同じsizeHint経路で測るので、文字数やQStyleの
        # 内側余白を推測する必要がない。
        suffix = "..."
        best_display = suffix
        low = 0
        high = len(self.tag)

        if self._total_width_for_display_text(suffix) <= next_budget:
            while low <= high:
                middle = (low + high) // 2
                candidate = self.tag[:middle] + suffix
                candidate_width = self._total_width_for_display_text(candidate)
                if candidate_width <= next_budget:
                    best_display = candidate
                    low = middle + 1
                else:
                    high = middle - 1

        self._total_width_for_display_text(best_display)
        self.updateGeometry()
        return True

    def _install_context_menu(self, widget: QWidget) -> None:
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        widget.customContextMenuRequested.connect(
            lambda pos, source=widget: self.context_menu_requested.emit(
                self.tag,
                source.mapToGlobal(pos),
            )
        )

    def _make_separator(self) -> QFrame:
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFrameShadow(QFrame.Shadow.Plain)
        separator.setFixedWidth(1)
        separator.setStyleSheet("color: #46505f; background: #46505f;")
        return separator

    def update_count(self, count: int) -> None:
        """キャッシュ再利用時に、現在の対象画像群での使用数だけを更新する。"""
        self.count_badge.setText(str(count))
        self.count_badge.setToolTip(f"使用数: {count}")
        self.tag_button.setToolTip(f"{self.tag}\n使用数: {count}")
        self._invalidate_display_width_budget()

    def set_search_highlight(
        self,
        highlighted: bool,
        current: bool = False,
        query_text: str = "",
        exact_match: bool = False,
    ) -> bool:
        """
        右側タグ一覧の検索一致ハイライトを切り替える。

        チップ全体の色は変えず、検索一致部分の文字背景だけを反転表示する。
        """
        if current:
            next_state = "current"
        elif highlighted:
            next_state = "highlight"
        else:
            next_state = "normal"

        state_changed = self._search_highlight_state != next_state
        self._search_highlight_state = next_state

        marker_changed = self.tag_button.set_search_marker(
            query_text if highlighted or current else "",
            current=current,
            exact_match=exact_match,
        )
        return state_changed or marker_changed

    def set_category_style(self, chip_qss: str | None, button_qss: str | None) -> bool:
        """通常状態のカテゴリ色QSSを更新する。"""
        next_chip_qss = chip_qss or ALL_TAG_CHIP_QSS
        next_button_qss = button_qss or ALL_TAG_BUTTON_QSS
        if self._normal_chip_qss == next_chip_qss and self._normal_button_qss == next_button_qss:
            return False

        self._normal_chip_qss = next_chip_qss
        self._normal_button_qss = next_button_qss
        self.chip_frame.setStyleSheet(self._normal_chip_qss)
        self.tag_button.setStyleSheet(self._normal_button_qss)
        self._invalidate_display_width_budget()
        return True

    def reset_operation_selection(self) -> None:
        """
        従来は右側一覧の再生成でタグ本体の選択状態が消えていた。
        チップ再利用後も同じ操作感を保つため、一覧更新時にOFFへ戻す。
        """
        self.tag_button.blockSignals(True)
        self.tag_button.setChecked(False)
        self.tag_button.blockSignals(False)

    def set_filter_mode(self, mode: str | None) -> None:
        """
        TagPanel側で保持しているフィルタ状態を、チップ再生成時に見た目へ復元する。
        mode:
            "include" -> 緑ON
            "exclude" -> 赤ON
            None      -> どちらもOFF
        """
        include_on = mode == "include"
        exclude_on = mode == "exclude"

        self.include_button.blockSignals(True)
        self.exclude_button.blockSignals(True)
        self.include_button.setChecked(include_on)
        self.exclude_button.setChecked(exclude_on)
        self.include_button.setText("●" if include_on else "○")
        self.exclude_button.setText("●" if exclude_on else "○")
        self.include_button.blockSignals(False)
        self.exclude_button.blockSignals(False)

    def _on_include_clicked(self, checked: bool) -> None:
        self.include_button.setText("●" if checked else "○")

        if checked:
            self.exclude_button.setChecked(False)
            self.exclude_button.setText("○")

        self.include_clicked.emit(self.tag)

    def _on_exclude_clicked(self, checked: bool) -> None:
        self.exclude_button.setText("●" if checked else "○")

        if checked:
            self.include_button.setChecked(False)
            self.include_button.setText("○")

        self.exclude_clicked.emit(self.tag)


class AddTagDialog(QDialog):
    """
    タグ追加用の小ダイアログ。
    入力はカンマ区切り対応。

    追加予定タグのうち1つを選択し、LoRA用トリガータグとして
    対象txtの先頭へ配置できる。
    """

    def __init__(
        self,
        title: str,
        parent: QWidget | None = None,
        *,
        tag_dictionary: DanbooruTagDictionary | None = None,
        tag_color_dictionary: TagColorDictionary | None = None,
        tag_color_enabled: bool = True,
        tag_completion_enabled: bool = DEFAULT_TAG_COMPLETION_ENABLED,
        tag_completion_limit: int = DEFAULT_TAG_COMPLETION_LIMIT,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setStyleSheet(ADD_TAG_DIALOG_QSS)

        self._selected_front_tag: str | None = None
        self._preview_buttons: dict[str, QPushButton] = {}
        self._tag_color_dictionary = tag_color_dictionary
        self._tag_color_enabled = tag_color_enabled
        self._tag_completion_enabled = bool(tag_completion_enabled)
        self._tag_completion_limit = normalize_tag_completion_limit(
            tag_completion_limit
        )

        card = QFrame()
        card.setObjectName("DialogCard")

        self.title_label = QLabel(title)
        self.title_label.setObjectName("DialogTitle")

        self.input_line = QLineEdit()
        self.input_line.setPlaceholderText("例: 1girl, solo, smile")
        self.input_line.textChanged.connect(self._refresh_preview)
        self.input_line.returnPressed.connect(self.accept)

        self._tag_completion = TagCompletionController(
            self.input_line,
            tag_dictionary if self._tag_completion_enabled else None,
            tag_color_dictionary=tag_color_dictionary,
            tag_color_enabled=tag_color_enabled,
            limit=self._tag_completion_limit,
        )

        hint = QLabel("複数タグはカンマ区切りで入力できます。")
        hint.setObjectName("DialogHint")

        self.dictionary_status_label = QLabel("")
        self.dictionary_status_label.setObjectName("DialogHint")
        self.dictionary_status_label.setStyleSheet("color: #e7ba66;")
        if self._tag_completion_enabled and not self._tag_completion.is_available():
            status_text = (
                tag_dictionary.status_message()
                if tag_dictionary is not None
                else "Danbooru補完辞書を利用できません。"
            )
            self.dictionary_status_label.setText(status_text)
            self.dictionary_status_label.setWordWrap(True)

        preview_label = QLabel("追加予定タグ")
        preview_label.setObjectName("DialogHint")

        self.preview_area = ChipWrapArea()
        self.preview_area.setMinimumHeight(88)

        self.front_tag_checkbox = QCheckBox("選択タグをトリガータグとして先頭に配置")
        self.front_tag_checkbox.setObjectName("DialogHint")
        self.front_tag_checkbox.setStyleSheet(
            """
            QCheckBox {
                color: #d8e6ff;
                font-weight: bold;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
            }
            """
        )

        self.warning_label = QLabel("")
        self.warning_label.setObjectName("DialogHint")
        self.warning_label.setStyleSheet("color: #ff8a8a; font-weight: bold;")

        reflect_button = QPushButton("反映")
        cancel_button = QPushButton("取消")
        reflect_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(reflect_button)
        button_layout.addSpacing(24)
        button_layout.addWidget(cancel_button)
        button_layout.addStretch(1)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(12)
        card_layout.addWidget(self.title_label)
        card_layout.addWidget(self.input_line)
        card_layout.addWidget(hint)
        if self.dictionary_status_label.text():
            card_layout.addWidget(self.dictionary_status_label)
        card_layout.addWidget(preview_label)
        card_layout.addWidget(self.preview_area)
        card_layout.addWidget(self.front_tag_checkbox)
        card_layout.addWidget(self.warning_label)
        card_layout.addLayout(button_layout)

        layout = QVBoxLayout(self)
        layout.addWidget(card)

        self._refresh_preview()

    def tags(self) -> list[str]:
        return parse_tags(self.input_line.text())

    def front_tag(self) -> str | None:
        if not self.front_tag_checkbox.isChecked():
            return None

        tags = self._unique_preview_tags()
        if self._selected_front_tag in tags:
            return self._selected_front_tag

        return None

    def accept(self) -> None:
        if self.front_tag_checkbox.isChecked() and not self.front_tag():
            self.warning_label.setText("先頭に配置するタグを選択してください。")
            return

        super().accept()

    def done(self, result: int) -> None:
        """ダイアログを閉じる前に、補完UIの全体イベント監視を解除する。"""
        self._tag_completion.dispose()
        super().done(result)

    def _unique_preview_tags(self) -> list[str]:
        tags: list[str] = []
        seen: set[str] = set()

        for tag in parse_tags(self.input_line.text()):
            if not tag or tag in seen:
                continue
            tags.append(tag)
            seen.add(tag)

        return tags

    def _refresh_preview(self) -> None:
        tags = self._unique_preview_tags()

        if self._selected_front_tag not in tags:
            self._selected_front_tag = None

        self._preview_buttons.clear()

        if not tags:
            empty_label = QLabel("タグなし")
            empty_label.setStyleSheet("color: #999999; padding: 4px;")
            self.preview_area.set_chips([empty_label])
            return

        chips: list[QWidget] = []
        for tag in tags:
            display_tag = shorten_text(tag, 44)

            chip_frame = QFrame()
            chip_frame.setObjectName("PendingTagChip")
            chip_frame.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

            chip = QPushButton(display_tag)
            chip.setToolTip(tag)
            chip.setCheckable(True)
            chip.setChecked(tag == self._selected_front_tag)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            chip.setStyleSheet(self._preview_tag_chip_qss(tag))
            chip.clicked.connect(
                lambda checked, selected_tag=tag: self._select_front_tag(
                    selected_tag if checked else None
                )
            )

            remove_button = QPushButton("×")
            remove_button.setToolTip(f"「{tag}」を追加予定タグから外す")
            remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
            remove_button.setFixedSize(22, 22)
            remove_button.setStyleSheet(
                """
                QPushButton {
                    color: #d7e2ee;
                    background: #1a2029;
                    border: 1px solid #465463;
                    border-radius: 8px;
                    font-size: 15px;
                    font-weight: bold;
                    padding: 0px;
                }
                QPushButton:hover {
                    color: #ffffff;
                    background: #69303a;
                    border-color: #e06a7c;
                }
                QPushButton:pressed {
                    color: #ffffff;
                    background: #4e252d;
                    border-color: #ff8ca0;
                }
                """
            )
            remove_button.clicked.connect(
                lambda _checked=False, target_tag=tag: self._remove_preview_tag(
                    target_tag
                )
            )

            chip_layout = QHBoxLayout(chip_frame)
            chip_layout.setContentsMargins(0, 0, 0, 0)
            chip_layout.setSpacing(3)
            chip_layout.addWidget(chip)
            chip_layout.addWidget(
                remove_button,
                0,
                Qt.AlignmentFlag.AlignVCenter,
            )

            chips.append(chip_frame)
            self._preview_buttons[tag] = chip

        self.preview_area.set_chips(chips)

    def _preview_tag_chip_qss(self, tag: str) -> str:
        """追加予定タグ用に、通常タグチップと同じカテゴリ色を返す。"""
        if self._tag_color_enabled and self._tag_color_dictionary is not None:
            style = self._tag_color_dictionary.style_for_tag(tag)
            return tag_chip_qss(style) or TAG_CHIP_QSS
        return TAG_CHIP_QSS

    def _remove_preview_tag(self, tag: str) -> None:
        """追加予定タグチップの×から、そのタグを入力欄ごと削除する。"""
        remaining_tags = [
            current_tag
            for current_tag in parse_tags(self.input_line.text())
            if current_tag != tag
        ]
        self.input_line.setText(", ".join(remaining_tags))
        self.input_line.setCursorPosition(len(self.input_line.text()))
        self.input_line.setFocus(Qt.FocusReason.OtherFocusReason)
        # setText() に伴う補完更新タイマーも止め、×操作直後に候補が
        # 再表示されないようにする。
        self._tag_completion.hide_popup()

    def _select_front_tag(self, tag: str | None) -> None:
        self._selected_front_tag = tag

        for button_tag, button in self._preview_buttons.items():
            button.blockSignals(True)
            button.setChecked(button_tag == tag)
            button.blockSignals(False)

        if tag:
            self.warning_label.setText("")

class EditTagDialog(QDialog):
    """
    タグ編集/置換用のダイアログ。

    選択した元タグごとに、置換後タグを1つずつ指定する。
    置換後タグを空欄にした場合は、その元タグを削除する。
    """

    def __init__(
        self,
        title: str,
        selected_tags: list[str],
        tag_counts: Counter[str] | None = None,
        target_count: int = 1,
        parent: QWidget | None = None,
        *,
        tag_dictionary: DanbooruTagDictionary | None = None,
        tag_color_dictionary: TagColorDictionary | None = None,
        tag_color_enabled: bool = True,
        tag_completion_enabled: bool = DEFAULT_TAG_COMPLETION_ENABLED,
        tag_completion_limit: int = DEFAULT_TAG_COMPLETION_LIMIT,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(720)
        self.setStyleSheet(ADD_TAG_DIALOG_QSS)

        self.selected_tags = selected_tags
        self.tag_counts = tag_counts or Counter()
        self.target_count = max(1, target_count)
        self._replacement_inputs: dict[str, QLineEdit] = {}
        self._tag_completion_controllers: list[TagCompletionController] = []
        self._tag_dictionary = tag_dictionary
        self._tag_color_dictionary = tag_color_dictionary
        self._tag_color_enabled = tag_color_enabled
        self._tag_completion_enabled = bool(tag_completion_enabled)
        self._tag_completion_limit = normalize_tag_completion_limit(
            tag_completion_limit
        )

        card = QFrame()
        card.setObjectName("DialogCard")

        self.title_label = QLabel(title)
        self.title_label.setObjectName("DialogTitle")

        original_tags_text = ", ".join(selected_tags) if selected_tags else "タグなし"

        target_title_label = QLabel("編集対象：")
        target_title_label.setObjectName("DialogHint")

        self.original_tags_label = QLabel(original_tags_text)
        self.original_tags_label.setObjectName("DialogHint")
        self.original_tags_label.setToolTip(original_tags_text)
        self.original_tags_label.setWordWrap(True)
        self.original_tags_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.original_tags_label.setStyleSheet(
            """
            QLabel {
                color: #d8e6ff;
                font-weight: bold;
            }
            """
        )

        target_layout = QHBoxLayout()
        target_layout.setContentsMargins(0, 0, 0, 0)
        target_layout.setSpacing(4)
        target_layout.addWidget(target_title_label)
        target_layout.addWidget(self.original_tags_label, 1)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        source_header = QLabel("元タグ")
        source_header.setObjectName("DialogHint")
        replace_header = QLabel("置換後タグ")
        replace_header.setObjectName("DialogHint")

        header_layout.addWidget(source_header, 3)
        header_layout.addWidget(QLabel(""), 0)
        header_layout.addWidget(replace_header, 4)

        rows_container = QWidget()
        rows_layout = QVBoxLayout(rows_container)
        rows_layout.setContentsMargins(8, 8, 8, 8)
        rows_layout.setSpacing(8)

        for tag in selected_tags:
            row_layout = QHBoxLayout()
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)

            count = self.tag_counts.get(tag, self.target_count)
            if self.target_count > 1:
                source_text = f"{tag}  {count}/{self.target_count}"
            else:
                source_text = tag

            source_label = QLabel(source_text)
            source_label.setObjectName("DialogHint")
            source_label.setToolTip(tag)
            source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            source_label.setMinimumWidth(220)
            source_label.setMaximumWidth(420)
            source_label.setStyleSheet(
                """
                QLabel {
                    color: #d8e6ff;
                    font-weight: bold;
                }
                """
            )

            arrow_label = QLabel("→")
            arrow_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            arrow_label.setStyleSheet(
                """
                QLabel {
                    color: #e8e8e8;
                    font-size: 18px;
                    font-weight: bold;
                }
                """
            )

            replacement_input = QLineEdit()
            replacement_input.setText(tag)
            replacement_input.setPlaceholderText("空欄にすると削除")
            replacement_input.setMinimumWidth(260)
            replacement_input.setToolTip(tag)
            replacement_input.textChanged.connect(
                lambda text, line_edit=replacement_input: line_edit.setToolTip(text)
            )
            replacement_input.returnPressed.connect(self.accept)
            self._tag_completion_controllers.append(
                TagCompletionController(
                    replacement_input,
                    self._tag_dictionary if self._tag_completion_enabled else None,
                    tag_color_dictionary=self._tag_color_dictionary,
                    tag_color_enabled=self._tag_color_enabled,
                    limit=self._tag_completion_limit,
                )
            )

            self._replacement_inputs[tag] = replacement_input

            row_layout.addWidget(source_label, 3)
            row_layout.addWidget(arrow_label, 0)
            row_layout.addWidget(replacement_input, 4)

            rows_layout.addLayout(row_layout)

        rows_layout.addStretch(1)

        rows_scroll = QScrollArea()
        rows_scroll.setWidgetResizable(True)
        rows_scroll.setWidget(rows_container)
        rows_scroll.setMinimumHeight(180)
        rows_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rows_scroll.setStyleSheet(CHIP_SCROLL_QSS)

        self.warning_label = QLabel("")
        self.warning_label.setObjectName("DialogHint")
        self.warning_label.setStyleSheet("color: #ff8a8a; font-weight: bold;")

        hint = QLabel("置換後タグを空欄にすると、その元タグを削除します。1行には1タグだけ入力できます。")
        hint.setObjectName("DialogHint")

        self.dictionary_status_label = QLabel("")
        self.dictionary_status_label.setObjectName("DialogHint")
        self.dictionary_status_label.setStyleSheet("color: #e7ba66;")
        if not self._tag_completion_controllers or not self._tag_completion_controllers[0].is_available():
            status_text = (
                self._tag_dictionary.status_message()
                if self._tag_dictionary is not None
                else "Danbooru補完辞書を利用できません。"
            )
            self.dictionary_status_label.setText(status_text)
            self.dictionary_status_label.setWordWrap(True)

        reflect_button = QPushButton("反映")
        cancel_button = QPushButton("取消")
        reflect_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(reflect_button)
        button_layout.addSpacing(24)
        button_layout.addWidget(cancel_button)
        button_layout.addStretch(1)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(10)
        card_layout.addWidget(self.title_label)
        card_layout.addLayout(target_layout)
        card_layout.addLayout(header_layout)
        card_layout.addWidget(rows_scroll)
        card_layout.addWidget(self.warning_label)
        card_layout.addWidget(hint)
        if self.dictionary_status_label.text():
            card_layout.addWidget(self.dictionary_status_label)
        card_layout.addLayout(button_layout)

        layout = QVBoxLayout(self)
        layout.addWidget(card)

    def replacements(self) -> dict[str, str | None]:
        replacements = self._build_replacements(validate=False)
        return replacements or {}

    def accept(self) -> None:
        replacements = self._build_replacements(validate=True)
        if replacements is None:
            return
        super().accept()

    def done(self, result: int) -> None:
        """ダイアログを閉じる前に、各入力欄の補完UI監視をすべて解除する。"""
        for controller in self._tag_completion_controllers:
            controller.dispose()
        super().done(result)

    def _build_replacements(self, *, validate: bool) -> dict[str, str | None] | None:
        replacements: dict[str, str | None] = {}

        for source_tag, line_edit in self._replacement_inputs.items():
            raw_text = line_edit.text().strip()

            if not raw_text:
                replacements[source_tag] = None
                continue

            parsed_tags = parse_tags(raw_text)

            if len(parsed_tags) > 1:
                if validate:
                    self.warning_label.setText("1行には1タグだけ入力してください。複数タグの追加は「追加」ボタンを使ってください。")
                return None

            if not parsed_tags:
                replacements[source_tag] = None
                continue

            replacements[source_tag] = parsed_tags[0]

        self.warning_label.setText("")
        return replacements


class ChipWrapArea(QScrollArea):
    """
    タグチップを手動で折り返して並べるスクロールエリア。
    FlowLayoutを使わず、行ごとにQHBoxLayoutを作る。
    """

    layout_rebuilt = Signal()
    tag_reorder_requested = Signal(str, int)

    def __init__(self) -> None:
        super().__init__()
        self._chips: list[QWidget] = []
        self._last_width = -1
        self._rebuild_pending = False
        self._rebuilding = False
        # 再構築中に内容更新が来た場合、終了後に最新状態だけをもう一度反映する。
        self._rebuild_requested_while_busy = False
        self._tag_reorder_enabled = False
        self._reorder_marker_index: int | None = None
        self._post_show_width_sync_needed = False

        self.container = QWidget()
        self._reorder_marker = QFrame(self.container)
        self._reorder_marker.setObjectName("TagReorderMarker")
        self._reorder_marker.setFixedWidth(5)
        self._reorder_marker.setStyleSheet(TAG_REORDER_MARKER_QSS)
        self._reorder_marker.hide()

        self.root_layout = QVBoxLayout(self.container)
        self.root_layout.setContentsMargins(8, 8, 8, 8)
        self.root_layout.setSpacing(6)

        self.setWidgetResizable(False)
        self.setWidget(self.container)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.container.setAcceptDrops(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(CHIP_SCROLL_QSS)

    def set_chips(self, chips: list[QWidget]) -> None:
        self._chips = chips
        self._post_show_width_sync_needed = True
        for chip in chips:
            if isinstance(chip, TagChip) and not getattr(
                chip,
                "_chip_wrap_width_sync_connected",
                False,
            ):
                chip.content_width_changed.connect(self._schedule_rebuild)
                chip._chip_wrap_width_sync_connected = True
        self._reorder_marker_index = None
        self._hide_reorder_marker()
        self._schedule_rebuild(force=True)

    def set_tag_reorder_enabled(self, enabled: bool) -> None:
        """この領域でタグチップのドラッグ並び替えを受け付けるかを切り替える。"""
        self._tag_reorder_enabled = enabled
        if not enabled:
            self._reorder_marker_index = None
            self._hide_reorder_marker()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._can_accept_tag_reorder(event):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if not self._can_accept_tag_reorder(event):
            event.ignore()
            return

        insert_index = self._insert_index_from_event(event)
        if insert_index != self._reorder_marker_index:
            self._reorder_marker_index = insert_index
        self._update_reorder_marker_geometry()

        event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._reorder_marker_index = None
        self._hide_reorder_marker()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        if not self._can_accept_tag_reorder(event):
            event.ignore()
            return

        source_tag = bytes(event.mimeData().data(CURRENT_TAG_REORDER_MIME)).decode("utf-8")
        insert_index = self._insert_index_from_event(event)
        self._reorder_marker_index = None
        self._hide_reorder_marker()
        self.tag_reorder_requested.emit(source_tag, insert_index)
        event.acceptProposedAction()

    def _can_accept_tag_reorder(self, event) -> bool:
        return (
            self._tag_reorder_enabled
            and event.mimeData().hasFormat(CURRENT_TAG_REORDER_MIME)
        )

    def _insert_index_from_event(self, event) -> int:
        """
        ドロップ先の挿入位置を、ドラッグイベントの座標から決める。

        QDragMoveEvent / QDropEvent は環境によって globalPosition() を持たないため、
        position() をイベント受信Widget基準の座標として扱い、mapToGlobal() を経由して
        container座標へ変換する。
        """
        local_pos = event.position().toPoint()

        # drag/dropイベントは QScrollArea 自身・viewport・container のどこに届くかで
        # position() の基準が変わるため、sender() には頼らず、座標が含まれる領域から推定する。
        if self.viewport().rect().contains(local_pos):
            global_pos = self.viewport().mapToGlobal(local_pos)
        elif self.container.rect().contains(local_pos):
            global_pos = self.container.mapToGlobal(local_pos)
        else:
            global_pos = self.mapToGlobal(local_pos)

        container_pos = self.container.mapFromGlobal(global_pos)
        chips = [chip for chip in self._chips if isinstance(chip, TagChip)]
        if not chips:
            return 0

        # 同じ行にいるチップを優先して、左右どちらに落としたかで before/after を決める。
        row_candidates: list[tuple[int, QRect]] = []
        for index, chip in enumerate(chips):
            chip_pos = chip.mapTo(self.container, QPoint(0, 0))
            chip_rect = QRect(chip_pos, chip.size())
            vertical_margin = max(4, chip_rect.height() // 4)
            if chip_rect.top() - vertical_margin <= container_pos.y() <= chip_rect.bottom() + vertical_margin:
                row_candidates.append((index, chip_rect))

        if row_candidates:
            row_candidates.sort(key=lambda pair: pair[1].left())

            first_index, first_rect = row_candidates[0]
            if container_pos.x() <= first_rect.center().x():
                return first_index

            for index, chip_rect in row_candidates:
                if container_pos.x() <= chip_rect.center().x():
                    return index

            last_index, _last_rect = row_candidates[-1]
            return last_index + 1

        # 行間や上下余白にいる場合は、もっとも近いチップの前後へ入れる。
        closest_index = 0
        closest_distance = 10**12
        after = False

        for index, chip in enumerate(chips):
            chip_pos = chip.mapTo(self.container, QPoint(0, 0))
            chip_rect = QRect(chip_pos, chip.size())
            dy = 0
            if container_pos.y() < chip_rect.top():
                dy = chip_rect.top() - container_pos.y()
            elif container_pos.y() > chip_rect.bottom():
                dy = container_pos.y() - chip_rect.bottom()

            dx = abs(chip_rect.center().x() - container_pos.x())
            distance = dy * 10000 + dx

            if distance < closest_distance:
                closest_distance = distance
                closest_index = index
                after = container_pos.y() > chip_rect.center().y()

        return max(0, min(closest_index + (1 if after else 0), len(chips)))

    def _hide_reorder_marker(self) -> None:
        if hasattr(self, "_reorder_marker"):
            self._reorder_marker.hide()

    def _update_reorder_marker_geometry(self) -> None:
        """レイアウトを変えずに、挿入位置マーカーだけをオーバーレイ表示する。"""
        index = self._reorder_marker_index
        chips = [chip for chip in self._chips if isinstance(chip, TagChip)]

        if index is None or not chips:
            self._hide_reorder_marker()
            return

        index = max(0, min(index, len(chips)))

        if index < len(chips):
            target = chips[index]
            target_pos = target.mapTo(self.container, QPoint(0, 0))
            x = target_pos.x() - 4
            y = target_pos.y()
            height = max(30, target.height())
        else:
            target = chips[-1]
            target_pos = target.mapTo(self.container, QPoint(0, 0))
            x = target_pos.x() + target.width() + 4
            y = target_pos.y()
            height = max(30, target.height())

        self._reorder_marker.setParent(self.container)
        self._reorder_marker.setGeometry(x, y, 5, height)
        self._reorder_marker.raise_()
        self._reorder_marker.show()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)

        width = self.viewport().width()
        if width <= 0:
            return
        if abs(width - self._last_width) < 8:
            return

        self._last_width = width
        self._schedule_rebuild()

    def _schedule_rebuild(self, *, force: bool = False) -> None:
        """
        次イベントループでの再構築を予約する。

        force は「幅が変わっていなくても再構築したい」意味だけに使う。
        既に予約済みなら self._chips は最新値へ更新されているため、
        同じ再構築を複数予約しない。
        """
        if self._rebuilding:
            self._rebuild_requested_while_busy = True
            return

        if self._rebuild_pending:
            return

        self._rebuild_pending = True
        QTimer.singleShot(0, self._rebuild_rows)

    def refresh_after_show(self) -> None:
        """
        非表示タブから再表示された後、実際のviewport幅で折り返しを作り直す。
        """
        self._last_width = -1
        self._schedule_rebuild(force=True)

    def _clear_rows(self) -> tuple[int, int]:
        """
        前回の行Widgetをまとめて破棄する。

        CurrentTagArea の通常表示は、画像を切り替えるたびに新しい TagChip を作る。
        そのため、旧行から各チップを ``setParent(None)`` で1個ずつ外す必要はない。
        旧行を非表示にして破棄すれば、子チップも行と一緒に安全に破棄される。

        以前のチップ単位の親解除は、50〜100個程度でも数百msかかり、
        次の選択枠の描画を待たせていた。
        """
        removed_rows = 0
        removed_chips = 0

        while self.root_layout.count():
            item = self.root_layout.takeAt(0)
            if item is None:
                continue

            row_widget = item.widget()
            if row_widget is None:
                continue

            removed_rows += 1
            row_layout = row_widget.layout()
            if row_layout is not None:
                for index in range(row_layout.count()):
                    child_item = row_layout.itemAt(index)
                    if child_item is not None and child_item.widget() is not None:
                        removed_chips += 1

            # イベントループへ戻るまでは古い行を見せず、子チップを個別に
            # 親解除せずに行Widgetごと破棄する。
            row_widget.hide()
            row_widget.deleteLater()

        return removed_rows, removed_chips

    def _sync_chip_widths_after_show(self) -> None:
        """実際に表示された後のQSSサイズで、必要なら行を再構築する。"""
        changed = False
        for chip in self._chips:
            if isinstance(chip, TagChip):
                changed = chip.sync_content_width() or changed

        if changed:
            self._schedule_rebuild(force=True)

    def _rebuild_rows(self) -> None:
        if self._rebuilding:
            return

        perf_started = time.perf_counter()
        perf_chip_count = len(self._chips)
        perf_area_name = self.objectName() or self.__class__.__name__
        perf_row_count = 0
        perf_enabled = dev_log_enabled("TAG_UI_LAYOUT")

        self._rebuild_pending = False
        self._rebuilding = True

        try:
            old_rows, old_chips = self._clear_rows()

            viewport_width = max(160, self.viewport().width())
            content_width = max(120, viewport_width - 18)
            self.container.setFixedWidth(content_width)

            margins = self.root_layout.contentsMargins()
            usable_width = max(80, content_width - margins.left() - margins.right())

            row_layout: QHBoxLayout | None = None
            row_width = 0
            spacing = 6
            total_height = margins.top() + margins.bottom()

            def new_row() -> QHBoxLayout:
                nonlocal perf_row_count
                perf_row_count += 1
                row_widget = QWidget()
                layout = QHBoxLayout(row_widget)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(spacing)
                layout.addStretch(1)
                self.root_layout.addWidget(row_widget)
                return layout

            for chip in self._chips:
                # 新規チップは行レイアウトへ直接追加する。
                # container へ一度 setParent() してから行へ移す二段階の親変更を避ける。
                chip.ensurePolished()
                if isinstance(chip, TagChip):
                    # QSS反映直後でも子ボタンの自然幅を行の折り返し計算へ反映する。
                    chip.sync_content_width()
                chip_hint = chip.sizeHint()
                chip_width = chip_hint.width()

                if row_layout is None:
                    row_layout = new_row()
                    row_width = 0
                    total_height += chip_hint.height()

                if row_width > 0 and row_width + chip_width + spacing > usable_width:
                    row_layout = new_row()
                    row_width = 0
                    total_height += chip_hint.height() + self.root_layout.spacing()

                row_layout.insertWidget(max(0, row_layout.count() - 1), chip)
                row_width += chip_width + spacing

            self.root_layout.addStretch(1)
            final_height = max(total_height + 16, self.viewport().height())
            self.container.setMinimumHeight(final_height)
            self.container.resize(content_width, final_height)

            if self._reorder_marker_index is not None:
                self._update_reorder_marker_geometry()

        finally:
            self._rebuilding = False
            if perf_enabled:
                log.info(
                    "[PERF] tag_panel.chip_wrap_rebuild area=%s chips=%d rows=%d "
                    "discarded_rows=%d discarded_chips=%d total=%.2fms",
                    perf_area_name,
                    perf_chip_count,
                    perf_row_count,
                    old_rows if "old_rows" in locals() else 0,
                    old_chips if "old_chips" in locals() else 0,
                    (time.perf_counter() - perf_started) * 1000,
                )
            self.layout_rebuilt.emit()

            if self._post_show_width_sync_needed:
                self._post_show_width_sync_needed = False
                QTimer.singleShot(0, self._sync_chip_widths_after_show)

            if self._rebuild_requested_while_busy:
                self._rebuild_requested_while_busy = False
                self._schedule_rebuild(force=True)


class VisibleWidgetFlowLayout(QLayout):
    """
    右側タグ一覧専用の軽量 FlowLayout。

    全タグチップは一度だけこのレイアウトへ登録し、フィルタ変更時は
    active_widgets に含まれる表示対象だけを折り返し配置する。
    行 QWidget / 行 QHBoxLayout の生成・破棄や、行間の付け替えを行わない。
    """

    def __init__(self, parent: QWidget | None = None, *, margin: int = 8, spacing: int = 6) -> None:
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)
        self._items: list[QLayoutItem] = []
        self._item_by_widget: dict[QWidget, QLayoutItem] = {}
        self._active_widgets: list[QWidget] = []
        self._last_row_count: int = 0
        # 大量タグ表示の診断用。通常時は空のままで、FlowChipWrapArea側が
        # PERF_FILTER / TAG_UI_LAYOUT 有効時だけリセットして内訳を読む。
        self._debug_metrics: dict[str, float | int] = {}
        # 大量の hide/show をまとめて行う間、Qtから届く中間の geometry 要求では
        # 全チップ再配置をしない。最後に呼び出し側が明示的に1回だけ配置する。
        self._geometry_suspended = False
        self._suspended_height_hint = 0

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802
        widget = item.widget()
        if widget is not None and widget in self._item_by_widget:
            return
        self._items.append(item)
        if widget is not None:
            self._item_by_widget[widget] = item

    def add_widget_once(self, widget: QWidget) -> bool:
        """未登録チップだけをレイアウトへ追加し、新規登録だったかを返す。"""
        if widget in self._item_by_widget:
            return False
        self.addWidget(widget)
        return True

    def contains_widget(self, widget: QWidget) -> bool:
        return widget in self._item_by_widget

    def known_widget_count(self) -> int:
        return len(self._item_by_widget)

    def active_widgets(self) -> list[QWidget]:
        return list(self._active_widgets)

    def reset_debug_metrics(self) -> None:
        """FlowLayoutの今回の再構築に関する診断値を初期化する。"""
        self._debug_metrics = {
            "height_for_width_calls": 0,
            "height_for_width_ms": 0.0,
            "set_geometry_calls": 0,
            "set_geometry_ms": 0.0,
            "layout_test_calls": 0,
            "layout_apply_calls": 0,
            "layout_test_ms": 0.0,
            "layout_apply_ms": 0.0,
            "size_hint_calls": 0,
            "size_hint_ms": 0.0,
            "item_geometry_calls": 0,
            "item_geometry_ms": 0.0,
        }

    def debug_metrics(self) -> dict[str, float | int]:
        """現在の診断値のコピーを返す。"""
        return dict(self._debug_metrics)

    def _debug_enabled(self) -> bool:
        return bool(self._debug_metrics)

    def set_geometry_suspended(self, suspended: bool) -> None:
        """
        大量チップの表示状態を一括変更する間だけ、中間geometry要求を軽量化する。

        show()/hide() の途中で親レイアウトから geometry 要求が来ても、
        active_widgets 全件の再配置を繰り返さない。解除後は呼び出し側が
        heightForWidth() と setGeometry() を明示的に1回ずつ呼ぶ。
        """
        suspended = bool(suspended)
        if self._geometry_suspended == suspended:
            return

        self._geometry_suspended = suspended
        if suspended:
            parent = self.parentWidget()
            parent_height = parent.height() if parent is not None else 0
            self._suspended_height_hint = max(
                self._suspended_height_hint,
                self.geometry().height(),
                parent_height,
            )
        else:
            self.invalidate()

    def set_active_widgets(self, widgets: list[QWidget]) -> None:
        self._active_widgets = list(widgets)
        self.invalidate()

    def clear_registered_widgets(self) -> None:
        """
        FlowLayout に登録済みの全チップを安全に解除する。

        フォルダ切替では TagPanel 側の AllTagChip キャッシュが破棄されるため、
        レイアウト側も古い QWidget / QLayoutItem 参照を保持してはいけない。
        先にレイアウトから外し、その後で呼び出し側が deleteLater() する。
        """
        self._active_widgets.clear()

        while self._items:
            item = self.takeAt(len(self._items) - 1)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.setParent(None)

        self._item_by_widget.clear()
        self._last_row_count = 0
        self.invalidate()

    @property
    def last_row_count(self) -> int:
        return self._last_row_count

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        if not (0 <= index < len(self._items)):
            return None
        item = self._items.pop(index)
        widget = item.widget()
        if widget is not None:
            self._item_by_widget.pop(widget, None)
            if widget in self._active_widgets:
                self._active_widgets.remove(widget)
        return item

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        if self._geometry_suspended:
            return max(0, self._suspended_height_hint)

        started = time.perf_counter() if self._debug_enabled() else 0.0
        height, _rows = self._do_layout(QRect(0, 0, width, 0), test_only=True)
        if self._debug_enabled():
            self._debug_metrics["height_for_width_calls"] += 1
            self._debug_metrics["height_for_width_ms"] += (
                time.perf_counter() - started
            ) * 1000
        return height

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        started = time.perf_counter() if self._debug_enabled() else 0.0
        super().setGeometry(rect)
        if self._geometry_suspended:
            return

        _height, rows = self._do_layout(rect, test_only=False)
        self._last_row_count = rows
        if self._debug_enabled():
            self._debug_metrics["set_geometry_calls"] += 1
            self._debug_metrics["set_geometry_ms"] += (
                time.perf_counter() - started
            ) * 1000

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for widget in self._active_widgets:
            item = self._item_by_widget.get(widget)
            if item is not None:
                size = size.expandedTo(item.minimumSize())

        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def prepare_all_tag_chip_display_widths(self, available_width: int) -> bool:
        """右側一覧の実効幅を使い、AllTagChipの表示文字列を配置前に確定する。

        このメソッドはFlowChipWrapAreaのバッチ再構築中に呼ばれる。
        _do_layout()/setGeometry()ではサイズを一切変更しないため、無限再配置を
        起こさず、既存のFlowLayoutの折り返しロジックをそのまま使える。
        """
        changed = False
        for widget in self._active_widgets:
            if isinstance(widget, AllTagChip):
                changed = (
                    widget.fit_display_to_total_width(available_width) or changed
                )

        if changed:
            self.invalidate()
        return changed

    def _do_layout(self, rect: QRect, *, test_only: bool) -> tuple[int, int]:
        debug_enabled = self._debug_enabled()
        started = time.perf_counter() if debug_enabled else 0.0
        size_hint_ms = 0.0
        item_geometry_ms = 0.0
        size_hint_calls = 0
        item_geometry_calls = 0

        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(),
            margins.top(),
            -margins.right(),
            -margins.bottom(),
        )

        x = effective.x()
        y = effective.y()
        line_height = 0
        row_count = 0
        spacing = self.spacing()

        for widget in self._active_widgets:
            item = self._item_by_widget.get(widget)
            if item is None:
                continue

            if debug_enabled:
                operation_started = time.perf_counter()
            hint = item.sizeHint()
            if debug_enabled:
                size_hint_ms += (time.perf_counter() - operation_started) * 1000
                size_hint_calls += 1

            next_x = x + hint.width()

            if row_count > 0 and line_height > 0 and next_x > effective.right() + 1:
                x = effective.x()
                y += line_height + spacing
                next_x = x + hint.width()
                line_height = 0

            if line_height == 0:
                row_count += 1

            if not test_only:
                if debug_enabled:
                    operation_started = time.perf_counter()
                item.setGeometry(QRect(QPoint(x, y), hint))
                if debug_enabled:
                    item_geometry_ms += (
                        time.perf_counter() - operation_started
                    ) * 1000
                    item_geometry_calls += 1

            x = next_x + spacing
            line_height = max(line_height, hint.height())

        height = (
            margins.top() + margins.bottom()
            if not self._active_widgets
            else y + line_height - rect.y() + margins.bottom()
        )

        if debug_enabled:
            elapsed_ms = (time.perf_counter() - started) * 1000
            if test_only:
                self._debug_metrics["layout_test_calls"] += 1
                self._debug_metrics["layout_test_ms"] += elapsed_ms
            else:
                self._debug_metrics["layout_apply_calls"] += 1
                self._debug_metrics["layout_apply_ms"] += elapsed_ms

            self._debug_metrics["size_hint_calls"] += size_hint_calls
            self._debug_metrics["size_hint_ms"] += size_hint_ms
            self._debug_metrics["item_geometry_calls"] += item_geometry_calls
            self._debug_metrics["item_geometry_ms"] += item_geometry_ms

        self._suspended_height_hint = height
        return height, row_count


class FlowChipWrapArea(QScrollArea):
    """
    右側の大量タグ一覧専用領域。

    チップはタグ名ごとのキャッシュと同様に一度だけ FlowLayout に登録する。
    フィルタ変更時は show/hide と active_widgets の差替えだけ行い、
    数百行の QWidget / QHBoxLayout を作り直さない。
    """

    layout_rebuilt = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._chips: list[QWidget] = []
        self._last_width = -1
        self._rebuild_pending = False
        self._rebuilding = False
        # set_chips() が連続しても、最後の状態だけを再配置する。
        self._rebuild_requested_while_busy = False
        self._post_show_width_sync_needed = False

        self.container = QWidget()
        self.flow_layout = VisibleWidgetFlowLayout(self.container, margin=8, spacing=6)

        self.setWidgetResizable(False)
        self.setWidget(self.container)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(CHIP_SCROLL_QSS)

    def set_chips(self, chips: list[QWidget]) -> None:
        self._chips = list(chips)
        self._post_show_width_sync_needed = True
        for chip in self._chips:
            if isinstance(chip, TagChip) and not getattr(
                chip,
                "_flow_chip_wrap_width_sync_connected",
                False,
            ):
                chip.content_width_changed.connect(self._schedule_rebuild)
                chip._flow_chip_wrap_width_sync_connected = True
        self._schedule_rebuild(force=True)

    def clear_registered_chips(self) -> None:
        """フォルダ切替時に、前フォルダ由来のチップ登録を完全に破棄する。"""
        previous_known = self.flow_layout.known_widget_count()
        self._chips.clear()
        self.flow_layout.clear_registered_widgets()

        viewport_width = max(160, self.viewport().width())
        content_width = max(120, viewport_width - 18)
        self.container.setFixedWidth(content_width)
        self.container.setMinimumHeight(self.viewport().height())
        self.container.resize(content_width, self.viewport().height())
        self.viewport().update()

        if (
            dev_log_enabled("PERF_FILTER")
            or dev_log_enabled("FOLDER_LOAD_DETAIL")
            or dev_log_enabled("PERF_FOLDER_LOAD")
        ):
            log.info(
                "[PERF] tag_panel.flow_clear_registered area=%s cleared_widgets=%d known_widgets_after=%d",
                self.objectName() or self.__class__.__name__,
                previous_known,
                self.flow_layout.known_widget_count(),
            )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)

        width = self.viewport().width()
        if width <= 0:
            return
        if abs(width - self._last_width) < 8:
            return

        self._last_width = width
        self._schedule_rebuild()

    def _schedule_rebuild(self, *, force: bool = False) -> None:
        """
        次イベントループでの再配置を予約する。

        force は表示後の幅再取得用であり、予約済みの再配置を重複させない。
        予約済みでも self._chips は最新の内容に置き換わるため、1回で十分。
        """
        if self._rebuilding:
            self._rebuild_requested_while_busy = True
            return

        if self._rebuild_pending:
            return

        self._rebuild_pending = True
        QTimer.singleShot(0, self._rebuild_flow)

    def refresh_after_show(self) -> None:
        """
        非表示タブから戻った後、実際のviewport幅で最終geometryを確定する。
        """
        self._last_width = -1
        self._schedule_rebuild(force=True)

    def _sync_chip_widths_after_show(self) -> None:
        """表示確定後のQSSサイズでTagChip幅を再測定し、必要なら再配置する。"""
        changed = False
        for chip in self._chips:
            if isinstance(chip, TagChip):
                changed = chip.sync_content_width() or changed

        if changed:
            self._schedule_rebuild(force=True)

    def _rebuild_flow(self) -> None:
        if self._rebuilding:
            return

        perf_started = time.perf_counter()
        area_name = self.objectName() or self.__class__.__name__
        previous_active = self.flow_layout.active_widgets()
        previous_set = set(previous_active)
        next_set = set(self._chips)
        newly_registered = 0
        parent_changed_count = 0
        shown_count = 0
        hidden_count = 0
        perf_enabled = (
            dev_log_enabled("PERF_FILTER")
            or dev_log_enabled("TAG_UI_LAYOUT")
            or dev_log_enabled("PERF_FOLDER_LOAD")
        )
        phase_ms: dict[str, float] = {}

        self._rebuild_pending = False
        self._rebuilding = True
        if perf_enabled:
            self.flow_layout.reset_debug_metrics()

        layout_was_enabled = self.flow_layout.isEnabled()
        layout_batch_active = False

        phase_started = time.perf_counter()
        # _rebuild_flow はイベントループ内で一続きに動くため、ここで個別に
        # 描画停止をかけなくても中間状態が画面へ出ることはない。
        #
        # QScrollArea / container の updates を止めると、非表示化したチップの
        # 旧描画が消えず、復帰後の配置と重なって見える場合がある。
        # レイアウトの中間geometryだけを止める。
        self.flow_layout.set_geometry_suspended(True)
        self.flow_layout.setEnabled(False)
        layout_batch_active = True
        phase_ms["begin_batch"] = (time.perf_counter() - phase_started) * 1000

        try:
            # 表示対象から外れたチップは Layout 内に残したまま隠す。
            # すでに非表示なら余計な hide() を行わない。
            phase_started = time.perf_counter()
            for widget in previous_set - next_set:
                # 親タブが非表示でも、明示的にshow状態のチップは isVisible() では
                # false になる。ここでは祖先の可視状態ではなく、チップ自身が
                # 明示的に隠されているかだけを見る。
                if not widget.isHidden():
                    widget.hide()
                    hidden_count += 1
            phase_ms["hide_removed"] = (time.perf_counter() - phase_started) * 1000

            parent_ms = 0.0
            register_ms = 0.0
            show_ms = 0.0
            for widget in self._chips:
                # キャッシュ再利用チップは通常すでに同じcontainer配下にある。
                # setParent() を繰り返すとQtのレイアウト要求を増やすため、新規だけ行う。
                if widget.parentWidget() is not self.container:
                    operation_started = time.perf_counter() if perf_enabled else 0.0
                    widget.setParent(self.container)
                    parent_changed_count += 1
                    if perf_enabled:
                        parent_ms += (time.perf_counter() - operation_started) * 1000

                operation_started = time.perf_counter() if perf_enabled else 0.0
                if self.flow_layout.add_widget_once(widget):
                    newly_registered += 1
                if perf_enabled:
                    register_ms += (time.perf_counter() - operation_started) * 1000

                # Layout無効化・geometry停止中にまとめて復帰させる。
                # すでに表示中のチップへのshow()も避ける。
                if widget.isHidden():
                    operation_started = time.perf_counter() if perf_enabled else 0.0
                    widget.show()
                    shown_count += 1
                    if perf_enabled:
                        show_ms += (time.perf_counter() - operation_started) * 1000

            phase_ms["set_parent"] = parent_ms
            phase_ms["register"] = register_ms
            phase_ms["show"] = show_ms

            phase_started = time.perf_counter()
            self.flow_layout.set_active_widgets(self._chips)
            phase_ms["set_active"] = (time.perf_counter() - phase_started) * 1000

            viewport_width = max(160, self.viewport().width())
            content_width = max(120, viewport_width - 18)

            phase_started = time.perf_counter()
            self.container.setFixedWidth(content_width)
            phase_ms["set_width"] = (time.perf_counter() - phase_started) * 1000

            # geometryを再開する前に、緑/赤ボタン・使用数バッジ込みで
            # 各AllTagChipの表示文字列を確定する。ここでは固定幅を設定しない。
            flow_margins = self.flow_layout.contentsMargins()
            available_chip_width = max(
                1,
                content_width - flow_margins.left() - flow_margins.right(),
            )
            phase_started = time.perf_counter()
            self.flow_layout.prepare_all_tag_chip_display_widths(
                available_chip_width
            )
            phase_ms["prepare_chip_display_widths"] = (
                time.perf_counter() - phase_started
            ) * 1000

            # ここまでの中間状態ではFlowLayoutの全件配置を止めている。
            # 有効化後、最終状態に対してだけ高さ計算とgeometry確定を行う。
            phase_started = time.perf_counter()
            self.flow_layout.setEnabled(layout_was_enabled)
            self.flow_layout.set_geometry_suspended(False)
            layout_batch_active = False
            phase_ms["end_batch"] = (time.perf_counter() - phase_started) * 1000

            phase_started = time.perf_counter()
            content_height = self.flow_layout.heightForWidth(content_width)
            phase_ms["height_for_width_call"] = (
                time.perf_counter() - phase_started
            ) * 1000

            actual_height = max(content_height, self.viewport().height())
            phase_started = time.perf_counter()
            self.container.setMinimumHeight(actual_height)
            self.container.resize(content_width, actual_height)
            phase_ms["container_resize"] = (time.perf_counter() - phase_started) * 1000

            # geometry をここで1回だけ確定させ、スクロール位置復元が直後に参照できるようにする。
            phase_started = time.perf_counter()
            self.flow_layout.setGeometry(QRect(0, 0, content_width, actual_height))
            phase_ms["explicit_set_geometry"] = (
                time.perf_counter() - phase_started
            ) * 1000

        finally:
            # 例外時でも、次回以降のレイアウトが停止状態に残らないよう復帰する。
            if layout_batch_active:
                self.flow_layout.setEnabled(layout_was_enabled)
                self.flow_layout.set_geometry_suspended(False)

            phase_started = time.perf_counter()
            # hide() 済みチップの旧描画を確実に消すため、container と viewport の
            # 両方へ再描画要求を出す。実際のペイントはこの処理終了後にまとめて行われる。
            self.container.update()
            self.viewport().update()
            phase_ms["refresh_container_and_viewport"] = (
                time.perf_counter() - phase_started
            ) * 1000

            self._rebuilding = False
            if perf_enabled:
                debug = self.flow_layout.debug_metrics()
                log.info(
                    "[PERF] tag_panel.flow_rebuild area=%s chips=%d rows=%d "
                    "active_before=%d hidden=%d hidden_applied=%d shown=%d "
                    "parent_changed=%d newly_registered=%d known_widgets=%d "
                    "begin_batch=%.2fms hide_removed=%.2fms set_parent=%.2fms "
                    "register=%.2fms show=%.2fms set_active=%.2fms set_width=%.2fms "
                    "end_batch=%.2fms height_for_width_call=%.2fms "
                    "container_resize=%.2fms explicit_set_geometry=%.2fms "
                    "refresh_container_and_viewport=%.2fms "
                    "layout_hfw=%d/%.2fms layout_geometry=%d/%.2fms "
                    "layout_test=%d/%.2fms layout_apply=%d/%.2fms "
                    "size_hint=%d/%.2fms item_geometry=%d/%.2fms total=%.2fms",
                    area_name,
                    len(self._chips),
                    self.flow_layout.last_row_count,
                    len(previous_active),
                    len(previous_set - next_set),
                    hidden_count,
                    shown_count,
                    parent_changed_count,
                    newly_registered,
                    self.flow_layout.known_widget_count(),
                    phase_ms.get("begin_batch", 0.0),
                    phase_ms.get("hide_removed", 0.0),
                    phase_ms.get("set_parent", 0.0),
                    phase_ms.get("register", 0.0),
                    phase_ms.get("show", 0.0),
                    phase_ms.get("set_active", 0.0),
                    phase_ms.get("set_width", 0.0),
                    phase_ms.get("end_batch", 0.0),
                    phase_ms.get("height_for_width_call", 0.0),
                    phase_ms.get("container_resize", 0.0),
                    phase_ms.get("explicit_set_geometry", 0.0),
                    phase_ms.get("refresh_container_and_viewport", 0.0),
                    debug.get("height_for_width_calls", 0),
                    debug.get("height_for_width_ms", 0.0),
                    debug.get("set_geometry_calls", 0),
                    debug.get("set_geometry_ms", 0.0),
                    debug.get("layout_test_calls", 0),
                    debug.get("layout_test_ms", 0.0),
                    debug.get("layout_apply_calls", 0),
                    debug.get("layout_apply_ms", 0.0),
                    debug.get("size_hint_calls", 0),
                    debug.get("size_hint_ms", 0.0),
                    debug.get("item_geometry_calls", 0),
                    debug.get("item_geometry_ms", 0.0),
                    (time.perf_counter() - perf_started) * 1000,
                )
            self.layout_rebuilt.emit()

            if self._post_show_width_sync_needed:
                self._post_show_width_sync_needed = False
                QTimer.singleShot(0, self._sync_chip_widths_after_show)

            if self._rebuild_requested_while_busy:
                self._rebuild_requested_while_busy = False
                self._schedule_rebuild(force=True)


class ActiveFilterChip(QPushButton):
    """右側フィルタ管理エリアに表示する、クリックで解除可能な条件チップ。"""

    remove_requested = Signal(str)
    context_menu_requested = Signal(str, QPoint)

    def __init__(self, tag: str, mode: str) -> None:
        super().__init__()
        self.tag = tag
        self.mode = mode

        display_tag = shorten_text(tag, 24)
        self.setText(f"{display_tag}  ×")
        self.setToolTip(f"{tag}\nクリックでこのフィルタ条件を解除")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        if mode == "include":
            border = "#31945a"
            color = "#9be9b5"
            hover = "rgba(34, 197, 94, 0.20)"
        else:
            border = "#a64a4a"
            color = "#ffabab"
            hover = "rgba(239, 68, 68, 0.20)"

        self.setStyleSheet(
            f"""
            QPushButton {{
                color: {color};
                background: #17202b;
                border: 1px solid {border};
                border-radius: 11px;
                padding: 3px 8px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: {hover};
            }}
            """
        )
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self.context_menu_requested.emit(self.tag, self.mapToGlobal(pos))
        )
        self.clicked.connect(lambda: self.remove_requested.emit(self.tag))


class CompactChipWrapArea(QWidget):
    """フィルタ条件チップを、利用可能な幅に合わせて折り返し表示する小型領域。"""

    def __init__(self) -> None:
        super().__init__()
        self._chips: list[QWidget] = []
        self._last_width = -1
        self._rebuild_pending = False
        self._rebuilding = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.root_layout = QVBoxLayout(self)
        self.root_layout.setContentsMargins(0, 0, 0, 0)
        self.root_layout.setSpacing(4)

    def set_chips(self, chips: list[QWidget]) -> None:
        self._chips = chips
        self._schedule_rebuild(force=True)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        width = self.width()
        if width <= 0:
            return
        if abs(width - self._last_width) < 6:
            return
        self._last_width = width
        self._schedule_rebuild()

    def _schedule_rebuild(self, *, force: bool = False) -> None:
        if self._rebuilding:
            return
        if self._rebuild_pending and not force:
            return
        self._rebuild_pending = True
        QTimer.singleShot(0, self._rebuild_rows)

    def _clear_rows(self) -> None:
        while self.root_layout.count():
            item = self.root_layout.takeAt(0)
            row_widget = item.widget() if item is not None else None
            if row_widget is None:
                continue

            row_layout = row_widget.layout()
            if row_layout is not None:
                while row_layout.count():
                    child_item = row_layout.takeAt(0)
                    child_widget = child_item.widget() if child_item else None
                    if child_widget is not None:
                        child_widget.setParent(None)
            row_widget.deleteLater()

    def _rebuild_rows(self) -> None:
        if self._rebuilding:
            return

        perf_started = time.perf_counter()
        perf_chip_count = len(self._chips)
        perf_area_name = self.objectName() or self.__class__.__name__

        self._rebuild_pending = False
        self._rebuilding = True

        try:
            self._clear_rows()

            if not self._chips:
                self.setFixedHeight(0)
                return

            usable_width = max(80, self.width())
            spacing = 5
            row_layout: QHBoxLayout | None = None
            row_width = 0
            row_height = 0
            total_height = 0
            row_count = 0

            def new_row() -> QHBoxLayout:
                nonlocal row_count
                row_widget = QWidget()
                layout = QHBoxLayout(row_widget)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(spacing)
                layout.addStretch(1)
                self.root_layout.addWidget(row_widget)
                row_count += 1
                return layout

            for chip in self._chips:
                chip.setParent(self)
                chip.ensurePolished()
                chip_width = chip.sizeHint().width()
                chip_height = chip.sizeHint().height()

                if row_layout is None or (row_width > 0 and row_width + spacing + chip_width > usable_width):
                    row_layout = new_row()
                    row_width = 0
                    total_height += chip_height
                    row_height = chip_height
                else:
                    row_height = max(row_height, chip_height)

                row_layout.insertWidget(max(0, row_layout.count() - 1), chip)
                row_width += chip_width + (spacing if row_width else 0)

            if row_count > 1:
                total_height += self.root_layout.spacing() * (row_count - 1)

            self.setFixedHeight(max(24, total_height))
        finally:
            self._rebuilding = False
            if dev_log_enabled("TAG_UI_LAYOUT"):
                log.info(
                    "[PERF] tag_panel.compact_chip_rebuild area=%s chips=%d total=%.2fms",
                    perf_area_name,
                    perf_chip_count,
                    (time.perf_counter() - perf_started) * 1000,
                )



class TagSortSearchPopup(QFrame):
    """タグ一覧の並べ替えを行うフローティングパネル。"""

    sort_applied = Signal(str)
    close_requested = Signal()

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TagSortSearchPopup")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(TAG_SORT_SEARCH_POPUP_QSS)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._pending_sort_mode = "name_asc"

        self.title_label = QLabel(title)
        self.title_label.setObjectName("TagSortSearchTitle")

        sort_label = QLabel("並べ替え")
        sort_label.setObjectName("TagSortSearchHint")

        self.sort_group = QButtonGroup(self)
        self.sort_buttons: dict[str, QRadioButton] = {}

        sort_items = [
            ("name_asc", "名前昇順"),
            ("name_desc", "名前降順"),
            ("count_desc", "使用数多い順"),
            ("count_asc", "使用数少ない順"),
            ("color_group", "色分け順"),
        ]

        sort_layout = QVBoxLayout()
        sort_layout.setContentsMargins(0, 0, 0, 0)
        sort_layout.setSpacing(2)

        for mode, label in sort_items:
            button = QRadioButton(label)
            self.sort_group.addButton(button)
            self.sort_buttons[mode] = button
            button.clicked.connect(
                lambda checked, selected_mode=mode: self._set_pending_sort_mode(
                    selected_mode,
                    checked,
                )
            )
            sort_layout.addWidget(button)

        self.ok_button = QPushButton("OK")
        self.cancel_button = QPushButton("キャンセル")
        self.ok_button.clicked.connect(self._apply)
        self.cancel_button.clicked.connect(self.close_requested.emit)

        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.addStretch(1)
        button_layout.addWidget(self.ok_button)
        button_layout.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(sort_label)
        layout.addLayout(sort_layout)
        layout.addLayout(button_layout)

        self.hide()

    def set_state(self, *, sort_mode: str) -> None:
        self._pending_sort_mode = sort_mode

        for mode, button in self.sort_buttons.items():
            button.blockSignals(True)
            button.setChecked(mode == sort_mode)
            button.blockSignals(False)

    def _set_pending_sort_mode(self, mode: str, checked: bool) -> None:
        if checked:
            self._pending_sort_mode = mode

    def _apply(self) -> None:
        self.sort_applied.emit(self._pending_sort_mode)


class CurrentTagHoverArea(QFrame):
    """左側タグ操作欄だけをホバー対象として扱う透明なラッパー。"""

    hover_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMouseTracking(True)

    def enterEvent(self, event) -> None:  # noqa: N802
        super().enterEvent(event)
        self.hover_changed.emit(True)

    def leaveEvent(self, event) -> None:  # noqa: N802
        super().leaveEvent(event)
        self.hover_changed.emit(False)


class TagPanel(QFrame):
    tags_changed = Signal()
    save_requested = Signal()
    # 左側の操作対象タグ欄への出入り。操作対象プレビューの新規発動に使う。
    hover_changed = Signal(bool)
    # 左右を含むタグパネル全体への出入り。発動済みプレビューの維持に使う。
    panel_hover_changed = Signal(bool)
    tag_filter_changed = Signal(object, object)
    # 黄色チェックを維持したまま、左側だけ現在の青枠1枚を操作対象にする状態。
    single_target_override_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.items: list[ImageItem] = []
        self.current_index: int = -1
        # 黄色チェック群を保持したまま、左側タグ欄だけ現在の青枠1枚へ切り替える。
        # フォルダ切替・アプリ再起動では保持しない。
        self._single_target_override_active: bool = False
        self._current_tag_buttons: dict[str, TagChip] = {}
        # チェック中画像の左側タグ欄では、チェック変更のたびに数百個のTagChipを
        # 作り直すと重いため、タグ名ごとにチップを再利用する。
        self._current_checked_tag_chip_cache: dict[str, TagChip] = {}
        # 現在、右側一覧に表示されているタグチップ。
        self._all_tag_rows: dict[str, AllTagChip] = {}
        # 同一フォルダ／作業中は AllTagChip を使い回し、毎回の大量生成を避ける。
        self._all_tag_chip_cache: dict[str, AllTagChip] = {}
        self._include_filter_tags: set[str] = set()
        self._exclude_filter_tags: set[str] = set()
        # 条件チップは各行でフィルタを追加した順に表示する。
        self._include_filter_order: list[str] = []
        self._exclude_filter_order: list[str] = []
        # [フィルタ解除] の基準に使うため、含む/除外をまたいだ追加順も保持する。
        self._filter_condition_order: list[str] = []
        # 右側タグ一覧を再構築した後に、操作前の周辺位置へ戻すためのアンカー。
        # Y座標は all_area 内ではなく TagPanel 全体基準で保持し、
        # フィルタ管理エリアが出現・増減・消滅した分のずれも補正する。
        self._pending_all_scroll_anchor_tags: list[str] = []
        self._pending_all_scroll_anchor_panel_y: int | None = None
        # 条件管理欄の出現/消滅だけで発生する中間レイアウト変更では復元せず、
        # タグ一覧本体を再生成した後の復元だけを受け付ける。
        self._pending_all_scroll_restore_armed: bool = False
        self._filter_visible_count: int = 0
        self._filter_base_count: int = 0
        self._filter_view_active: bool = False
        self._filter_visible_indexes: list[int] = []
        # 右側タグ一覧パネルの基礎対象範囲。"all" または "work_set"。
        # タグフィルタ有効時は、この範囲内の現在表示中画像だけを操作対象にする。
        self._all_tags_scope: str = "all"
        self._work_set_paths: set[Path] = set()
        # 右側タグ一覧の「並べ替えと検索」状態。
        # 検索はタグチップを非表示にせず、一致チップをハイライトする。
        self._all_tag_sort_mode: str = "name_asc"
        self._all_tag_search_text: str = ""
        self._all_tag_search_exact_match: bool = False
        self._all_tag_search_match_tags: list[str] = []
        self._all_tag_current_match_index: int = -1
        # 右側検索ハイライト状態。空検索時に全チップへ通常styleを再適用しないために保持する。
        self._all_tag_highlighted_tags: set[str] = set()
        self._all_tag_current_highlight_tag: str | None = None
        # 左側タグ一覧の検索/並べ替え状態。
        # 並べ替えはチェック中画像がある時だけ有効。通常時はtxt上のタグ順を維持する。
        self._current_tag_sort_mode: str = "name_asc"
        self._current_tag_search_text: str = ""
        self._current_tag_search_exact_match: bool = False
        self._current_tag_search_match_tags: list[str] = []
        self._current_tag_current_match_index: int = -1
        self._current_tag_sort_enabled: bool = False
        self._current_tag_reorder_enabled: bool = False
        # 左側タグ欄の現在表示モード。
        # チェック中画像がある場合、画像選択だけでは集計内容が変わらないため、
        # set_current_index() では再集計をスキップできる。
        self._current_tags_view_mode: str | None = None
        self._current_tags_checked_count: int = 0
        # フィルタ操作中は、管理エリア表示・タグ一覧再配置・スクロール復元が
        # 複数イベントループに分かれて走る。途中状態が1フレームだけ見えないよう、
        # 緑/赤フィルタ操作の間だけTagPanel全体の再描画を遅延させる。
        self._filter_ui_update_generation: int = 0
        # 操作履歴は現在読み込んでいるフォルダ・アプリ起動中だけ保持する。
        # 履歴は新しい順に積み、フォルダ切替時にクリアする。
        self._tag_history: list[TagHistoryEntry] = []
        self._tag_color_enabled: bool = True
        self._tag_completion_enabled: bool = DEFAULT_TAG_COMPLETION_ENABLED
        self._tag_completion_limit: int = DEFAULT_TAG_COMPLETION_LIMIT
        self._tag_color_dictionary = TagColorDictionary.load()
        # Danbooru補完辞書は固定CSVとして起動時に一度だけ読む。
        # 未配置でも通常のタグ編集は止めず、補完だけ無効にする。
        self._danbooru_tag_dictionary = DanbooruTagDictionary.load()
        # Tagger実行中はTaggerタブに留め、保存や通常タグ編集と競合しないようにする。
        self._tagger_execution_locked = False
        self._tab_enabled_before_tagger_lock: dict[int, bool] = {}
        self._tag_chip_qss_cache: dict[str, str] = {}
        self._all_tag_chip_qss_cache: dict[str, tuple[str, str]] = {}

        self.setObjectName("TagPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMouseTracking(True)

        self.undo_button = QPushButton("戻す")
        self.history_button = QPushButton("タグ操作履歴")
        self.save_button = QPushButton("保存", self)
        # 取り消し操作は操作履歴ダイアログ内へ集約するため、単独の戻すボタンは非表示にする。
        self.undo_button.setEnabled(False)
        self.undo_button.hide()
        self.history_button.setEnabled(True)
        self.history_button.clicked.connect(self.show_history_dialog)
        self.save_button.setObjectName("FloatingSaveButton")
        self.save_button.setToolTip("現在のタグ内容をtxtへ保存します")
        self.save_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.save_button.setFixedSize(FLOATING_SAVE_BUTTON_WIDTH, FLOATING_SAVE_BUTTON_HEIGHT)
        self.save_button.setStyleSheet(
            f"""
            QPushButton#FloatingSaveButton {{
                color: {FLOATING_SAVE_BUTTON_TEXT};
                background: {FLOATING_SAVE_BUTTON_BG};
                border: 1px solid {FLOATING_SAVE_BUTTON_BORDER};
                border-radius: 10px;
                font-weight: bold;
                font-size: 14px;
                padding: 8px 12px;
            }}
            QPushButton#FloatingSaveButton:hover {{
                background: {FLOATING_SAVE_BUTTON_BG_HOVER};
                border-color: {FLOATING_SAVE_BUTTON_BORDER_HOVER};
            }}
            QPushButton#FloatingSaveButton:pressed {{
                background: {FLOATING_SAVE_BUTTON_BG_PRESSED};
                border-color: {FLOATING_SAVE_BUTTON_BORDER_PRESSED};
            }}
            """
        )
        self.save_button.clicked.connect(self.save_requested.emit)

        top_layout = QHBoxLayout()
        top_layout.addWidget(self.undo_button)
        top_layout.addWidget(self.history_button)
        top_layout.addStretch(1)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("TagTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setMovable(False)
        self.tabs.setTabsClosable(False)
        self.tabs.setStyleSheet(TAG_TABS_QSS)

        self.editor_page = QWidget()
        self.danbooru_page = self._make_placeholder_page("danbooruタグ機能は v0.02 以降で対応予定です")
        self._add_tab_page("タグ編集", self.editor_page)
        self._add_tab_page("danbooruタグ", self.danbooru_page, enabled=False, tooltip="v0.02以降で対応予定")

        # ローカルLLMは未実装のため、v1.0.0ではGUIタブを表示しない。
        # 実装予定はREADMEの「今後の予定」で案内する。

        self.target_label = QLabel("操作対象：表示中の1枚")
        self.target_label.setStyleSheet(TARGET_LABEL_QSS)

        self.single_target_toggle_button = QPushButton("対象切替")
        self.single_target_toggle_button.setToolTip(
            "黄色チェックを維持したまま、左側タグ欄の操作対象だけを現在の青枠画像へ切り替えます"
        )
        self.single_target_toggle_button.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        # 操作対象ラベルと同じ行へコンパクトに置き、
        # ボタン出現時も下のタグパネルを押し下げない高さに固定する。
        self.single_target_toggle_button.setObjectName(
            "SingleTargetToggleButton"
        )
        self.single_target_toggle_button.setFixedSize(72, 24)
        self.single_target_toggle_button.setProperty(
            "singleTargetActive",
            False,
        )
        self.single_target_toggle_button.setStyleSheet(
            """
            QPushButton#SingleTargetToggleButton {
                color: #e8e8e8;
                background: #242424;
                border: 1px solid #555555;
                border-radius: 6px;
                padding: 1px 8px;
            }
            QPushButton#SingleTargetToggleButton:hover {
                background: #303030;
                border-color: #777777;
            }
            QPushButton#SingleTargetToggleButton:pressed {
                background: #1d1d1d;
            }
            QPushButton#SingleTargetToggleButton:disabled {
                color: #777777;
                background: #1b1b1b;
                border-color: #3a3a3a;
            }
            QPushButton#SingleTargetToggleButton[singleTargetActive="true"] {
                color: #211800;
                background: #e5b93f;
                border: 1px solid #ffd866;
                font-weight: bold;
            }
            QPushButton#SingleTargetToggleButton[singleTargetActive="true"]:hover {
                background: #f0c94f;
                border-color: #ffe58a;
            }
            QPushButton#SingleTargetToggleButton[singleTargetActive="true"]:pressed {
                background: #c99d2f;
            }
            """
        )
        # ボタンは常時表示し、黄色チェックがない時だけ無効化する。
        # 出現/消失させず位置を固定し、既存UIの「条件未成立時は無効」へ揃える。
        self.single_target_toggle_button.setEnabled(False)
        self.single_target_toggle_button.setToolTip(
            "黄色チェックがある時に、左側タグ欄の操作対象を切り替えられます"
        )
        self.single_target_toggle_button.clicked.connect(
            self._toggle_single_target_override
        )

        self.current_tags_label = QLabel("選択中画像のタグ")
        self.current_tags_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.current_tags_label.setObjectName("PanelHeader")

        self.all_tags_label = QLabel("全画像のタグ一覧")
        self.all_tags_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.all_tags_label.setObjectName("PanelHeader")

        self.current_tag_count_label = QLabel("タグ数: 0")
        self.current_tag_count_label.setObjectName("TagCountLabel")
        self.current_tag_count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.current_tag_count_label.setStyleSheet(
            "color: #ffe58a; font-size: 11px; font-weight: bold; padding: 1px 4px;"
        )
        self.current_tag_count_label.setToolTip("左側パネルに現在表示されているタグチップ数です。")

        self.all_tag_count_label = QLabel("タグ数: 0")
        self.all_tag_count_label.setObjectName("TagCountLabel")
        self.all_tag_count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.all_tag_count_label.setStyleSheet(
            "color: #ffe58a; font-size: 11px; font-weight: bold; padding: 1px 4px;"
        )
        self.all_tag_count_label.setToolTip("右側パネルに現在表示されているタグチップ数です。")

        self.current_toolbar = self._make_toolbar(
            add_callback=self.add_tags_to_current_targets,
            delete_callback=self.delete_current_selected_tags,
            dedupe_callback=self.dedupe_current_targets,
            edit_callback=self.edit_current_selected_tags,
            move_to_front_callback=self.move_current_selected_tag_to_front,
            sort_callback=self._toggle_current_sort_search_popup,
        )
        self.all_toolbar = self._make_toolbar(
            add_callback=self.add_tags_to_all_items,
            delete_callback=self.delete_all_selected_tags,
            dedupe_callback=self.dedupe_all_items,
            edit_callback=self.edit_all_selected_tags,
            sort_callback=self._toggle_all_sort_search_popup,
        )

        # 左側タグ欄は用途で表示エリアを分ける。
        # 通常1枚表示はタグ順ドラッグ並び替えが必要なので従来の ChipWrapArea を使う。
        self.current_area = ChipWrapArea()
        self.current_area.setObjectName("CurrentTagArea")
        self.current_area.tag_reorder_requested.connect(self._reorder_current_image_tags)

        # チェック中画像表示は大量タグになりやすく、ChipWrapArea の行 QWidget 再生成が重いため、
        # 右側タグ一覧と同じ FlowChipWrapArea で軽量表示する。ドラッグ並び替えは行わない。
        self.current_checked_area = FlowChipWrapArea()
        self.current_checked_area.setObjectName("CurrentCheckedTagArea")
        self.current_checked_area.hide()

        self.current_tag_search_bar = QFrame()
        self.current_tag_search_bar.setObjectName("TagSearchBar")
        self.current_tag_search_bar.setStyleSheet(TAG_SEARCH_BAR_QSS)
        self.current_tag_search_line = QLineEdit()
        self.current_tag_search_line.setPlaceholderText("タグ検索（ハイライト）")
        self.current_tag_search_line.setToolTip("_ と空白を同一視して、一致するタグをハイライトします。タグ一覧は非表示になりません。")
        self.current_tag_search_line.textChanged.connect(self._set_current_tag_search_text)

        self.current_tag_search_exact_check = QCheckBox("完全一致")
        self.current_tag_search_exact_check.setToolTip("ONにすると、検索語と完全に一致するタグだけをハイライトします。")
        self.current_tag_search_exact_check.setStyleSheet(
            """
            QCheckBox {
                color: #e8e8e8;
                spacing: 3px;
                font-size: 10px;
                font-weight: bold;
            }
            QCheckBox::indicator {
                width: 12px;
                height: 12px;
            }
            """
        )
        self.current_tag_search_exact_check.toggled.connect(
            self._set_current_tag_search_exact_match
        )

        self.current_tag_search_exact_check.setFixedWidth(82)

        self.current_tag_search_count_label = QLabel("")
        self.current_tag_search_count_label.setObjectName("TagSearchCount")
        # 4桁件数（9999 / 9999）まで想定し、検索結果数で横幅が揺れないよう固定する。
        self.current_tag_search_count_label.setFixedWidth(82)
        self.current_tag_search_count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.current_tag_search_count_label.setStyleSheet(
            "color: #ffe58a; font-size: 10px; font-weight: bold;"
        )

        current_tag_search_info_layout = QVBoxLayout()
        current_tag_search_info_layout.setContentsMargins(0, 0, 0, 0)
        current_tag_search_info_layout.setSpacing(0)
        current_tag_search_info_layout.addWidget(self.current_tag_search_exact_check)
        current_tag_search_info_layout.addWidget(self.current_tag_search_count_label)

        self.current_tag_search_prev_button = QPushButton("＜")
        self.current_tag_search_prev_button.setFixedWidth(34)
        self.current_tag_search_prev_button.setToolTip("前の一致タグへ移動")
        self.current_tag_search_prev_button.clicked.connect(lambda: self._jump_current_tag_search_match(-1))

        self.current_tag_search_next_button = QPushButton("＞")
        self.current_tag_search_next_button.setFixedWidth(34)
        self.current_tag_search_next_button.setToolTip("次の一致タグへ移動")
        self.current_tag_search_next_button.clicked.connect(lambda: self._jump_current_tag_search_match(1))

        self.current_tag_search_clear_button = QPushButton("×")
        self.current_tag_search_clear_button.setFixedWidth(32)
        self.current_tag_search_clear_button.setToolTip("検索をクリア")
        self.current_tag_search_clear_button.clicked.connect(self._clear_current_tag_search)

        current_tag_search_layout = QHBoxLayout(self.current_tag_search_bar)
        current_tag_search_layout.setContentsMargins(6, 5, 6, 5)
        current_tag_search_layout.setSpacing(5)
        current_tag_search_layout.addWidget(self.current_tag_search_line, 1)
        current_tag_search_layout.addLayout(current_tag_search_info_layout)
        current_tag_search_layout.addWidget(self.current_tag_search_prev_button)
        current_tag_search_layout.addWidget(self.current_tag_search_next_button)
        current_tag_search_layout.addWidget(self.current_tag_search_clear_button)

        # 右側一覧は大量タグで再構築がボトルネックになるため、
        # 単一のFlowLayoutへチップを保持し、表示対象だけ再配置する。
        self.all_area = FlowChipWrapArea()
        self.all_area.setObjectName("AllTagArea")
        self.all_area.layout_rebuilt.connect(self._on_all_area_layout_rebuilt)

        self.all_tag_search_bar = QFrame()
        self.all_tag_search_bar.setObjectName("TagSearchBar")
        self.all_tag_search_bar.setStyleSheet(TAG_SEARCH_BAR_QSS)
        self.all_tag_search_line = QLineEdit()
        self.all_tag_search_line.setPlaceholderText("タグ検索（ハイライト）")
        self.all_tag_search_line.setToolTip("_ と空白を同一視して、一致するタグをハイライトします。タグ一覧は非表示になりません。")
        self.all_tag_search_line.textChanged.connect(self._set_all_tag_search_text)

        self.all_tag_search_exact_check = QCheckBox("完全一致")
        self.all_tag_search_exact_check.setToolTip("ONにすると、検索語と完全に一致するタグだけをハイライトします。")
        self.all_tag_search_exact_check.setStyleSheet(
            """
            QCheckBox {
                color: #e8e8e8;
                spacing: 3px;
                font-size: 10px;
                font-weight: bold;
            }
            QCheckBox::indicator {
                width: 12px;
                height: 12px;
            }
            """
        )
        self.all_tag_search_exact_check.toggled.connect(
            self._set_all_tag_search_exact_match
        )

        self.all_tag_search_exact_check.setFixedWidth(82)

        self.all_tag_search_count_label = QLabel("")
        self.all_tag_search_count_label.setObjectName("TagSearchCount")
        # 4桁件数（9999 / 9999）まで想定し、検索結果数で横幅が揺れないよう固定する。
        self.all_tag_search_count_label.setFixedWidth(82)
        self.all_tag_search_count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.all_tag_search_count_label.setStyleSheet(
            "color: #ffe58a; font-size: 10px; font-weight: bold;"
        )

        all_tag_search_info_layout = QVBoxLayout()
        all_tag_search_info_layout.setContentsMargins(0, 0, 0, 0)
        all_tag_search_info_layout.setSpacing(0)
        all_tag_search_info_layout.addWidget(self.all_tag_search_exact_check)
        all_tag_search_info_layout.addWidget(self.all_tag_search_count_label)

        self.all_tag_search_prev_button = QPushButton("＜")
        self.all_tag_search_prev_button.setFixedWidth(34)
        self.all_tag_search_prev_button.setToolTip("前の一致タグへ移動")
        self.all_tag_search_prev_button.clicked.connect(lambda: self._jump_all_tag_search_match(-1))

        self.all_tag_search_next_button = QPushButton("＞")
        self.all_tag_search_next_button.setFixedWidth(34)
        self.all_tag_search_next_button.setToolTip("次の一致タグへ移動")
        self.all_tag_search_next_button.clicked.connect(lambda: self._jump_all_tag_search_match(1))

        self.all_tag_search_clear_button = QPushButton("×")
        self.all_tag_search_clear_button.setFixedWidth(32)
        self.all_tag_search_clear_button.setToolTip("検索をクリア")
        self.all_tag_search_clear_button.clicked.connect(self._clear_all_tag_search)

        all_tag_search_layout = QHBoxLayout(self.all_tag_search_bar)
        all_tag_search_layout.setContentsMargins(6, 5, 6, 5)
        all_tag_search_layout.setSpacing(5)
        all_tag_search_layout.addWidget(self.all_tag_search_line, 1)
        all_tag_search_layout.addLayout(all_tag_search_info_layout)
        all_tag_search_layout.addWidget(self.all_tag_search_prev_button)
        all_tag_search_layout.addWidget(self.all_tag_search_next_button)
        all_tag_search_layout.addWidget(self.all_tag_search_clear_button)

        # 右側タグ一覧に掛かっている include / exclude フィルタの管理エリア。
        # タグ操作ボタンの下、タグチップ一覧の上へ表示する。
        self.active_filter_panel = QFrame()
        self.active_filter_panel.setObjectName("ActiveFilterPanel")
        self.active_filter_panel.setStyleSheet(
            """
            QFrame#ActiveFilterPanel {
                background: #162131;
                border: 1px solid #36577c;
                border-radius: 7px;
            }
            QLabel#ActiveFilterTitle {
                color: #a8d2ff;
                font-weight: bold;
            }
            QLabel#ActiveFilterCount {
                color: #e8e8e8;
                font-weight: bold;
            }
            QLabel#IncludeFilterRowTitle {
                color: #72d991;
                font-weight: bold;
            }
            QLabel#ExcludeFilterRowTitle {
                color: #ff8f8f;
                font-weight: bold;
            }
            """
        )

        self.active_filter_title = QLabel("フィルタ中")
        self.active_filter_title.setObjectName("ActiveFilterTitle")
        self.active_filter_count = QLabel("表示: 0 / 0 枚")
        self.active_filter_count.setObjectName("ActiveFilterCount")
        self.active_filter_clear_button = QPushButton("フィルタ解除")
        self.active_filter_clear_button.setToolTip("含む／除外フィルタをすべて解除します")
        self.active_filter_clear_button.clicked.connect(self.clear_tag_filters)

        active_filter_header = QHBoxLayout()
        active_filter_header.setContentsMargins(0, 0, 0, 0)
        active_filter_header.setSpacing(6)
        active_filter_header.addWidget(self.active_filter_title)
        active_filter_header.addStretch(1)
        active_filter_header.addWidget(self.active_filter_count)
        active_filter_header.addWidget(self.active_filter_clear_button)

        self.include_filter_row = QWidget()
        include_row_layout = QHBoxLayout(self.include_filter_row)
        include_row_layout.setContentsMargins(0, 0, 0, 0)
        include_row_layout.setSpacing(6)
        self.include_filter_title = QLabel("含む")
        self.include_filter_title.setObjectName("IncludeFilterRowTitle")
        self.include_filter_title.setFixedWidth(32)
        self.include_filter_chip_area = CompactChipWrapArea()
        self.include_filter_chip_area.setObjectName("IncludeFilterChipArea")
        include_row_layout.addWidget(self.include_filter_title)
        include_row_layout.addWidget(self.include_filter_chip_area, 1)

        self.exclude_filter_row = QWidget()
        exclude_row_layout = QHBoxLayout(self.exclude_filter_row)
        exclude_row_layout.setContentsMargins(0, 0, 0, 0)
        exclude_row_layout.setSpacing(6)
        self.exclude_filter_title = QLabel("除外")
        self.exclude_filter_title.setObjectName("ExcludeFilterRowTitle")
        self.exclude_filter_title.setFixedWidth(32)
        self.exclude_filter_chip_area = CompactChipWrapArea()
        self.exclude_filter_chip_area.setObjectName("ExcludeFilterChipArea")
        exclude_row_layout.addWidget(self.exclude_filter_title)
        exclude_row_layout.addWidget(self.exclude_filter_chip_area, 1)

        active_filter_layout = QVBoxLayout(self.active_filter_panel)
        active_filter_layout.setContentsMargins(8, 6, 8, 7)
        active_filter_layout.setSpacing(5)
        active_filter_layout.addLayout(active_filter_header)
        active_filter_layout.addWidget(self.include_filter_row)
        active_filter_layout.addWidget(self.exclude_filter_row)

        self.active_filter_panel.setVisible(False)

        # 操作対象プレビューの「新規発動」は左側のタグ操作欄だけで行う。
        # いったん発動した後は、右側を含む TagPanel 全体で表示を維持する。
        self.current_hover_area = CurrentTagHoverArea()
        self.current_hover_area.hover_changed.connect(self.hover_changed.emit)

        # タグ一覧はスタック領域として下まで伸ばし、タグ数はスクロール枠の外側に
        # 薄いフッターとして置く。オーバーレイにするとタグチップ欄の中に見えてしまうため、
        # ラベルは独立行に戻しつつ、高さと余白を最小限にする。
        self.current_tag_count_label.setFixedHeight(16)
        self.all_tag_count_label.setFixedHeight(16)

        current_tag_area_stack = QWidget()
        current_tag_area_stack_layout = QGridLayout(current_tag_area_stack)
        current_tag_area_stack_layout.setContentsMargins(0, 0, 0, 0)
        current_tag_area_stack_layout.setSpacing(0)
        current_tag_area_stack_layout.addWidget(self.current_area, 0, 0)
        current_tag_area_stack_layout.addWidget(self.current_checked_area, 0, 0)

        all_tag_area_stack = QWidget()
        all_tag_area_stack_layout = QGridLayout(all_tag_area_stack)
        all_tag_area_stack_layout.setContentsMargins(0, 0, 0, 0)
        all_tag_area_stack_layout.setSpacing(0)
        all_tag_area_stack_layout.addWidget(self.all_area, 0, 0)

        current_tag_count_layout = QHBoxLayout()
        current_tag_count_layout.setContentsMargins(0, 0, 2, 0)
        current_tag_count_layout.setSpacing(0)
        current_tag_count_layout.addStretch(1)
        current_tag_count_layout.addWidget(self.current_tag_count_label)

        all_tag_count_layout = QHBoxLayout()
        all_tag_count_layout.setContentsMargins(0, 0, 2, 0)
        all_tag_count_layout.setSpacing(0)
        all_tag_count_layout.addStretch(1)
        all_tag_count_layout.addWidget(self.all_tag_count_label)

        current_panel = QVBoxLayout(self.current_hover_area)
        current_panel.setContentsMargins(0, 0, 0, 0)
        current_panel.setSpacing(4)
        current_panel.addWidget(self.current_tags_label)
        current_panel.addWidget(self.current_toolbar)
        current_panel.addWidget(self.current_tag_search_bar)
        current_panel.addWidget(current_tag_area_stack, 1)
        current_panel.addLayout(current_tag_count_layout)

        all_panel = QVBoxLayout()
        # 左側は CurrentTagHoverArea 内のレイアウトで余白0にしているため、
        # 右側も同じ余白・間隔に揃える。デフォルト余白が残ると検索欄以下が
        # 右側だけ少し下へずれて見える。
        all_panel.setContentsMargins(0, 0, 0, 0)
        all_panel.setSpacing(current_panel.spacing())
        all_panel.addWidget(self.all_tags_label)
        all_panel.addWidget(self.all_toolbar)
        all_panel.addWidget(self.all_tag_search_bar)
        all_panel.addWidget(self.active_filter_panel)
        all_panel.addWidget(all_tag_area_stack, 1)
        all_panel.addLayout(all_tag_count_layout)

        lists_layout = QHBoxLayout()
        lists_layout.addWidget(self.current_hover_area, 1)
        lists_layout.addLayout(all_panel, 1)

        self.format_tabs = QTabWidget()
        self.format_tabs.setObjectName("FormatTabs")
        self.format_tabs.setDocumentMode(True)
        self.format_tabs.setMovable(False)
        self.format_tabs.setTabsClosable(False)
        self.format_tabs.setStyleSheet(TAG_SUB_TABS_QSS)

        self.tag_format_page = QWidget()

        tag_format_layout = QVBoxLayout(self.tag_format_page)
        # 下側の余白を詰め、左右タグ一覧の表示領域を少し縦に広げる。
        # 右下のタグ数表示も、タグ形式ページの下端へ寄せる。
        tag_format_layout.setContentsMargins(8, 8, 8, 0)
        tag_format_layout.setSpacing(8)

        # 操作対象行も下の左右タグパネルと同じ2列比率にする。
        # 左列内ではラベルを左、切替ボタンを右へ置くことで、
        # 文言の長さに関係なくボタン右端を左側緑見出しの右端へ揃える。
        target_row_layout = QHBoxLayout()
        target_row_layout.setContentsMargins(0, 0, 0, 0)
        target_row_layout.setSpacing(lists_layout.spacing())

        target_left_widget = QWidget()
        target_left_layout = QHBoxLayout(target_left_widget)
        target_left_layout.setContentsMargins(0, 0, 0, 0)
        target_left_layout.setSpacing(8)
        target_left_layout.addWidget(self.target_label)
        target_left_layout.addStretch(1)
        target_left_layout.addWidget(self.single_target_toggle_button)

        target_right_spacer = QWidget()
        target_right_spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )

        target_row_layout.addWidget(target_left_widget, 1)
        target_row_layout.addWidget(target_right_spacer, 1)

        tag_format_layout.addLayout(target_row_layout)
        tag_format_layout.addLayout(lists_layout, 1)

        self.format_tabs.addTab(self.tag_format_page, "タグ形式")

        # 自然文形式は未実装のため、v1.0.0ではGUIタブを表示しない。
        # 実装予定はREADMEの「今後の予定」で案内する。

        editor_layout = QVBoxLayout(self.editor_page)
        # タグ形式タブ下端の余白を詰め、タグ数表示をオレンジ枠寄りにする。
        editor_layout.setContentsMargins(8, 8, 8, 0)
        editor_layout.setSpacing(8)
        editor_layout.addWidget(self.format_tabs, 1)

        main_layout = QVBoxLayout(self)
        # TagPanel外周の下余白を小さくし、タグ数表示を枠下端へ寄せる。
        # 上左右は従来相当を維持し、下だけ詰める。
        main_layout.setContentsMargins(8, 8, 8, 2)
        main_layout.addLayout(top_layout)
        main_layout.addWidget(self.tabs, 1)
        QTimer.singleShot(0, self._place_floating_save_button)

        self.current_sort_search_popup = TagSortSearchPopup("左側タグ一覧：並べ替え", self)
        self.current_sort_search_popup.sort_applied.connect(self._apply_current_tag_sort_mode)
        self.current_sort_search_popup.close_requested.connect(self.current_sort_search_popup.hide)

        self.all_sort_search_popup = TagSortSearchPopup("右側タグ一覧：並べ替え", self)
        self.all_sort_search_popup.sort_applied.connect(self._apply_all_tag_sort_mode)
        self.all_sort_search_popup.close_requested.connect(self.all_sort_search_popup.hide)
        self._refresh_current_tag_search_state()
        self._refresh_all_tag_search_state()

    def enterEvent(self, event) -> None:  # noqa: N802
        super().enterEvent(event)
        self.panel_hover_changed.emit(True)

    def leaveEvent(self, event) -> None:  # noqa: N802
        super().leaveEvent(event)
        self.panel_hover_changed.emit(False)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_floating_save_button()
        if hasattr(self, "current_sort_search_popup") and self.current_sort_search_popup.isVisible():
            self._place_current_sort_search_popup()
        if hasattr(self, "all_sort_search_popup") and self.all_sort_search_popup.isVisible():
            self._place_all_sort_search_popup()

    def _place_floating_save_button(self) -> None:
        """
        saveボタンは右上に浮かせて配置する。

        通常レイアウトへ入れて大きくすると、下のタグ編集エリアを押し下げてしまう。
        そのため、TagPanel直下の子Widgetとして右上に重ね、タブ行付近まで跨ぐ大きめの
        ボタンとして見せる。
        """
        if not hasattr(self, "save_button"):
            return

        margin = FLOATING_SAVE_BUTTON_MARGIN
        x = max(margin, self.width() - self.save_button.width() - margin)
        y = margin
        self.save_button.move(x, y)
        self.save_button.raise_()

    def _add_tab_page(self, title: str, page: QWidget, *, enabled: bool = True, tooltip: str = "") -> int:
        index = self.tabs.addTab(page, title)
        self.tabs.setTabEnabled(index, enabled)
        if tooltip:
            self.tabs.setTabToolTip(index, tooltip)
        return index

    def _make_placeholder_page(self, text: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(
            """
            QLabel {
                color: #777777;
                padding: 32px;
                font-weight: bold;
            }
            """
        )
        layout.addWidget(label, 1)
        return page

    def _make_toolbar(
        self,
        *,
        add_callback,
        delete_callback,
        dedupe_callback,
        edit_callback,
        move_to_front_callback=None,
        sort_callback=None,
    ) -> QWidget:
        """
        左右タグパネルで位置と幅を揃えた4列ツールバーを作成する。

        左側:
            [並べ替え] [編集] [追加] [削除]
            [重複削除] [先頭へ] [      ] [      ]

        右側:
            [並べ替え] [編集] [追加] [削除]
            [重複削除] [      ] [      ] [      ]

        検索ボタンは、右側タグ一覧に常設検索バーを追加したため廃止する。
        """
        frame = QWidget()
        grid = QGridLayout(frame)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)

        # 4列を均等に伸縮させ、左右パネルで共通機能の位置と幅を揃える。
        for column in range(4):
            grid.setColumnStretch(column, 1)

        def configure_button(button: QPushButton) -> QPushButton:
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            return button

        sort_button = configure_button(QPushButton("並べ替え"))
        edit_button = configure_button(QPushButton("編集"))
        add_button = configure_button(QPushButton("追加"))
        delete_button = configure_button(QPushButton("削除"))
        dedupe_button = configure_button(QPushButton("重複削除"))

        if sort_callback is None:
            sort_button.setEnabled(False)
            sort_button.setToolTip("並べ替えは今後対応予定")
        else:
            if move_to_front_callback is not None:
                self.current_sort_button = sort_button
                sort_button.setToolTip("チェック中画像がある時だけ、左側タグ一覧を並べ替えできます")
            else:
                self.all_sort_button = sort_button
                sort_button.setToolTip("右側タグ一覧の並べ替えを開きます")
            sort_button.clicked.connect(sort_callback)

        edit_button.clicked.connect(edit_callback)
        add_button.clicked.connect(add_callback)
        delete_button.clicked.connect(delete_callback)
        dedupe_button.clicked.connect(dedupe_callback)

        # 検索ボタンは廃止し、右側タグ一覧は常設検索バーを使う。
        # 左右共通の主要操作は同じ位置に置き、左側だけの「先頭へ」は2段目へ逃がす。
        grid.addWidget(sort_button, 0, 0)
        grid.addWidget(edit_button, 0, 1)
        grid.addWidget(add_button, 0, 2)
        grid.addWidget(delete_button, 0, 3)

        grid.addWidget(dedupe_button, 1, 0)

        if move_to_front_callback is not None:
            move_to_front_button = configure_button(QPushButton("先頭へ"))
            move_to_front_button.clicked.connect(move_to_front_callback)
            move_to_front_button.setToolTip("選択した既存タグをtxtの先頭へ移動します")
            grid.addWidget(move_to_front_button, 1, 1)
        else:
            spacer = QWidget()
            spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            grid.addWidget(spacer, 1, 1)

        for column in (2, 3):
            spacer = QWidget()
            spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            grid.addWidget(spacer, 1, column)

        return frame

    def _normalize_tag_search_text(self, text: str) -> str:
        """検索比較用に、大文字小文字・空白・underscoreの揺れを吸収する。"""
        return " ".join(text.lower().replace("_", " ").split())

    def _tag_search_matches(self, tag: str, query_text: str, *, exact_match: bool) -> bool:
        query = self._normalize_tag_search_text(query_text)
        if not query:
            return False

        normalized_tag = self._normalize_tag_search_text(tag)
        if exact_match:
            return normalized_tag == query

        return query in normalized_tag

    def _all_tag_search_matches(self, tag: str) -> bool:
        return self._tag_search_matches(
            tag,
            self._all_tag_search_text,
            exact_match=self._all_tag_search_exact_match,
        )

    def _sort_current_tag_counts(self, entries: list[tuple[str, int]]) -> list[tuple[str, int]]:
        """左側チェック中画像タグの表示順を現在の並べ替えモードに従って返す。"""
        if self._current_tag_sort_mode == "name_desc":
            return sorted(entries, key=lambda pair: pair[0].lower(), reverse=True)

        if self._current_tag_sort_mode == "count_desc":
            return sorted(entries, key=lambda pair: (-pair[1], pair[0].lower()))

        if self._current_tag_sort_mode == "count_asc":
            return sorted(entries, key=lambda pair: (pair[1], pair[0].lower()))

        if self._current_tag_sort_mode == "color_group":
            return sorted(
                entries,
                key=lambda pair: (
                    self._tag_color_dictionary.sort_key_for_tag(pair[0]),
                    pair[0].casefold(),
                ),
            )

        return sorted(entries, key=lambda pair: pair[0].lower())

    def _refresh_current_sort_button_state(self) -> None:
        """左側並べ替えボタンを、チェック中画像がある時だけ有効にする。"""
        if not hasattr(self, "current_sort_button"):
            return

        self.current_sort_button.setEnabled(self._current_tag_sort_enabled)
        if self._current_tag_sort_enabled:
            self.current_sort_button.setToolTip("チェック中画像に含まれるタグ一覧を並べ替えます")
        else:
            self.current_sort_button.setToolTip("チェック中画像がある時だけ並べ替えできます")

    def _current_tag_search_matches(self, tag: str) -> bool:
        return self._tag_search_matches(
            tag,
            self._current_tag_search_text,
            exact_match=self._current_tag_search_exact_match,
        )

    def _refresh_current_tag_search_state(
        self,
        *,
        auto_jump: bool = False,
        scroll_current: bool = False,
    ) -> None:
        """左側タグ一覧の検索一致・現在位置・ボタン状態を更新する。"""
        query_active = bool(self._normalize_tag_search_text(self._current_tag_search_text))
        previous_current_tag: str | None = None

        if (
            0 <= self._current_tag_current_match_index < len(self._current_tag_search_match_tags)
        ):
            previous_current_tag = self._current_tag_search_match_tags[
                self._current_tag_current_match_index
            ]

        if query_active:
            matches = [
                tag
                for tag in self._current_tag_buttons.keys()
                if self._current_tag_search_matches(tag)
            ]
        else:
            matches = []

        self._current_tag_search_match_tags = matches

        if not query_active or not matches:
            self._current_tag_current_match_index = -1
        elif auto_jump or previous_current_tag not in matches:
            self._current_tag_current_match_index = 0
        else:
            self._current_tag_current_match_index = matches.index(previous_current_tag)

        current_tag = (
            matches[self._current_tag_current_match_index]
            if 0 <= self._current_tag_current_match_index < len(matches)
            else None
        )

        match_set = set(matches)
        for tag, chip in self._current_tag_buttons.items():
            chip.set_search_highlight(
                tag in match_set,
                current=(tag == current_tag),
                query_text=self._current_tag_search_text if query_active else "",
                exact_match=self._current_tag_search_exact_match,
            )

        self._refresh_current_tag_search_controls(query_active=query_active)

        if scroll_current and current_tag is not None:
            self._schedule_scroll_to_current_tag_match()

    def _refresh_current_tag_search_controls(self, *, query_active: bool) -> None:
        """左側検索バーの件数表示・ボタン状態を更新する。"""
        if not hasattr(self, "current_tag_search_count_label"):
            return

        matched = len(self._current_tag_search_match_tags)

        if not query_active:
            self.current_tag_search_count_label.setText("")
        elif matched == 0:
            self.current_tag_search_count_label.setText("結果なし")
        else:
            self.current_tag_search_count_label.setText(
                f"{self._current_tag_current_match_index + 1} / {matched}"
            )

        self.current_tag_search_prev_button.setEnabled(matched > 0)
        self.current_tag_search_next_button.setEnabled(matched > 0)
        self.current_tag_search_clear_button.setEnabled(query_active)

    def _toggle_current_sort_search_popup(self) -> None:
        """左側タグ一覧の並べ替えポップアップを開閉する。"""
        if not self._current_tag_sort_enabled:
            return

        if self.current_sort_search_popup.isVisible():
            self.current_sort_search_popup.hide()
            return

        self.current_sort_search_popup.set_state(sort_mode=self._current_tag_sort_mode)
        self._place_current_sort_search_popup()
        self.current_sort_search_popup.show()
        self.current_sort_search_popup.raise_()

    def _place_current_sort_search_popup(self) -> None:
        """左側タグ一覧用の並べ替えポップアップを、並べ替えボタンの下に配置する。"""
        if not hasattr(self, "current_sort_search_popup") or not hasattr(self, "current_sort_button"):
            return

        button_top = self.current_sort_button.mapTo(self, QPoint(0, 0))
        popup_width = max(220, self.current_sort_button.width() + 72)

        self.current_sort_search_popup.setFixedWidth(popup_width)
        self.current_sort_search_popup.adjustSize()

        x = button_top.x()
        y = button_top.y() + self.current_sort_button.height() + 4

        max_x = max(0, self.width() - self.current_sort_search_popup.width() - 8)
        max_y = max(0, self.height() - self.current_sort_search_popup.height() - 8)
        self.current_sort_search_popup.move(max(0, min(x, max_x)), max(0, min(y, max_y)))

    def _set_current_tag_search_text(self, text: str) -> None:
        """左側タグ一覧検索をリアルタイムにハイライトへ反映する。"""
        self._current_tag_search_text = text
        self._refresh_current_tag_search_state(auto_jump=True, scroll_current=True)

    def _set_current_tag_search_exact_match(self, checked: bool) -> None:
        """左側タグ一覧検索の完全一致モードを切り替える。"""
        self._current_tag_search_exact_match = bool(checked)
        self._refresh_current_tag_search_state(auto_jump=True, scroll_current=True)

    def _clear_current_tag_search(self) -> None:
        """左側タグ一覧検索をクリアする。"""
        if not self._current_tag_search_text and not self.current_tag_search_line.text():
            return

        self._current_tag_search_text = ""
        self.current_tag_search_line.blockSignals(True)
        self.current_tag_search_line.clear()
        self.current_tag_search_line.blockSignals(False)
        self._refresh_current_tag_search_state()

    def _jump_current_tag_search_match(self, direction: int) -> None:
        """左側検索一致タグの前後へ移動する。"""
        if not self._current_tag_search_match_tags:
            return

        if self._current_tag_current_match_index < 0:
            self._current_tag_current_match_index = 0
        else:
            self._current_tag_current_match_index = (
                self._current_tag_current_match_index + direction
            ) % len(self._current_tag_search_match_tags)

        self._refresh_current_tag_search_state(scroll_current=True)

    def _schedule_scroll_to_current_tag_match(self) -> None:
        """ChipWrapArea更新後のgeometry確定を待って、現在一致タグへスクロールする。"""
        QTimer.singleShot(0, self._scroll_to_current_tag_match)
        QTimer.singleShot(20, self._scroll_to_current_tag_match)

    def _active_current_tag_area(self):
        """左側タグ欄で現在表示しているスクロールエリアを返す。"""
        if (
            self._current_tags_view_mode == "checked"
            and hasattr(self, "current_checked_area")
            and self.current_checked_area.isVisible()
        ):
            return self.current_checked_area

        return self.current_area

    def _scroll_to_current_tag_match(self) -> None:
        if not (
            0 <= self._current_tag_current_match_index < len(self._current_tag_search_match_tags)
        ):
            return

        tag = self._current_tag_search_match_tags[self._current_tag_current_match_index]
        chip = self._current_tag_buttons.get(tag)
        if chip is None:
            return

        own_layout = self.layout()
        if own_layout is not None:
            own_layout.activate()

        area = self._active_current_tag_area()
        area.updateGeometry()
        chip_y = chip.mapTo(area.container, QPoint(0, 0)).y()
        bar = area.verticalScrollBar()
        margin = max(20, area.viewport().height() // 4)
        bar.setValue(max(0, chip_y - margin))

    def _apply_current_tag_sort_mode(self, mode: str) -> None:
        """左側並べ替えポップアップのOKで、チェック中画像タグの並び順を反映する。"""
        self.current_sort_search_popup.hide()

        if not self._current_tag_sort_enabled:
            return

        if mode == self._current_tag_sort_mode:
            return

        self._current_tag_sort_mode = mode
        self.refresh_current_tags(reason="set_items")

        if self._current_tag_search_match_tags:
            self._schedule_scroll_to_current_tag_match()

    def _sort_all_tag_entries(self, entries: list[tuple[str, int]]) -> list[tuple[str, int]]:
        """右側タグ一覧の表示順を現在の並べ替えモードに従って返す。"""
        if self._all_tag_sort_mode == "name_desc":
            return sorted(entries, key=lambda pair: pair[0].lower(), reverse=True)

        if self._all_tag_sort_mode == "count_desc":
            return sorted(entries, key=lambda pair: (-pair[1], pair[0].lower()))

        if self._all_tag_sort_mode == "count_asc":
            return sorted(entries, key=lambda pair: (pair[1], pair[0].lower()))

        if self._all_tag_sort_mode == "color_group":
            return sorted(
                entries,
                key=lambda pair: (
                    self._tag_color_dictionary.sort_key_for_tag(pair[0]),
                    pair[0].casefold(),
                ),
            )

        return sorted(entries, key=lambda pair: pair[0].lower())

    def _update_all_tag_search_highlights(self) -> None:
        """互換用。右側タグ一覧の検索状態を再反映する。"""
        self._refresh_all_tag_search_state()

    def _refresh_all_tag_search_state(
        self,
        *,
        auto_jump: bool = False,
        scroll_current: bool = False,
    ) -> None:
        """右側タグ一覧の検索一致・現在位置・ボタン状態を差分更新する。"""
        perf_enabled = dev_log_enabled("PERF_FILTER")
        perf_started = time.perf_counter() if perf_enabled else 0.0

        query_started = time.perf_counter() if perf_enabled else 0.0
        query_active = bool(self._normalize_tag_search_text(self._all_tag_search_text))
        query_ms = (time.perf_counter() - query_started) * 1000 if perf_enabled else 0.0

        previous_started = time.perf_counter() if perf_enabled else 0.0
        previous_current_tag: str | None = None

        if (
            0 <= self._all_tag_current_match_index < len(self._all_tag_search_match_tags)
        ):
            previous_current_tag = self._all_tag_search_match_tags[
                self._all_tag_current_match_index
            ]
        previous_ms = (time.perf_counter() - previous_started) * 1000 if perf_enabled else 0.0

        matches_started = time.perf_counter() if perf_enabled else 0.0
        if query_active:
            matches = [
                tag
                for tag in self._all_tag_rows.keys()
                if self._all_tag_search_matches(tag)
            ]
        else:
            matches = []
        matches_ms = (time.perf_counter() - matches_started) * 1000 if perf_enabled else 0.0

        state_started = time.perf_counter() if perf_enabled else 0.0
        self._all_tag_search_match_tags = matches

        if not query_active or not matches:
            self._all_tag_current_match_index = -1
        elif auto_jump or previous_current_tag not in matches:
            self._all_tag_current_match_index = 0
        else:
            self._all_tag_current_match_index = matches.index(previous_current_tag)

        current_tag = (
            matches[self._all_tag_current_match_index]
            if 0 <= self._all_tag_current_match_index < len(matches)
            else None
        )
        match_set = set(matches)
        state_ms = (time.perf_counter() - state_started) * 1000 if perf_enabled else 0.0

        highlight_started = time.perf_counter() if perf_enabled else 0.0
        previous_highlighted = set(self._all_tag_highlighted_tags)
        previous_current = self._all_tag_current_highlight_tag

        if query_active:
            candidate_tags = previous_highlighted | match_set
            if previous_current is not None:
                candidate_tags.add(previous_current)
            if current_tag is not None:
                candidate_tags.add(current_tag)
        else:
            # 検索欄が空なら、前回ハイライトされていたタグだけ通常状態へ戻す。
            candidate_tags = set(previous_highlighted)
            if previous_current is not None:
                candidate_tags.add(previous_current)

        highlight_checked = 0
        highlight_changed = 0
        missing_tags = 0

        for tag in candidate_tags:
            chip = self._all_tag_rows.get(tag)
            if chip is None:
                # フィルタ等で現在の右側一覧から外れていても、AllTagChip自体は
                # キャッシュに残っている。非表示中に検索語を変更・クリアした場合も
                # 古い検索ハイライトを解除できるよう、キャッシュ側も確認する。
                chip = self._all_tag_chip_cache.get(tag)
            if chip is None:
                missing_tags += 1
                continue

            highlight_checked += 1
            changed = chip.set_search_highlight(
                tag in match_set,
                current=(tag == current_tag),
                query_text=self._all_tag_search_text if query_active else "",
                exact_match=self._all_tag_search_exact_match,
            )
            if changed:
                highlight_changed += 1

        self._all_tag_highlighted_tags = match_set if query_active else set()
        self._all_tag_current_highlight_tag = current_tag if query_active else None
        highlight_ms = (time.perf_counter() - highlight_started) * 1000 if perf_enabled else 0.0

        controls_started = time.perf_counter() if perf_enabled else 0.0
        self._refresh_all_tag_search_controls(query_active=query_active)
        controls_ms = (time.perf_counter() - controls_started) * 1000 if perf_enabled else 0.0

        scroll_ms = 0.0
        if scroll_current and current_tag is not None:
            scroll_started = time.perf_counter() if perf_enabled else 0.0
            self._schedule_scroll_to_current_all_tag_match()
            scroll_ms = (time.perf_counter() - scroll_started) * 1000 if perf_enabled else 0.0

        if perf_enabled:
            log.info(
                "[PERF] tag_panel.refresh_all_tag_search_state rows=%d query_active=%s "
                "matches=%d auto_jump=%s scroll_current=%s query=%.2fms previous=%.2fms "
                "match_build=%.2fms state=%.2fms highlight_checked=%d highlight_changed=%d "
                "missing_tags=%d highlight_apply=%.2fms controls=%.2fms "
                "scroll_schedule=%.2fms total=%.2fms",
                len(self._all_tag_rows),
                query_active,
                len(matches),
                auto_jump,
                scroll_current,
                query_ms,
                previous_ms,
                matches_ms,
                state_ms,
                highlight_checked,
                highlight_changed,
                missing_tags,
                highlight_ms,
                controls_ms,
                scroll_ms,
                (time.perf_counter() - perf_started) * 1000,
            )

    def _refresh_all_tag_search_controls(self, *, query_active: bool) -> None:
        """常設検索バーの件数表示・ボタン状態を更新する。"""
        if not hasattr(self, "all_tag_search_count_label"):
            return

        matched = len(self._all_tag_search_match_tags)

        if not query_active:
            self.all_tag_search_count_label.setText("")
        elif matched == 0:
            self.all_tag_search_count_label.setText("結果なし")
        else:
            self.all_tag_search_count_label.setText(
                f"{self._all_tag_current_match_index + 1} / {matched}"
            )

        self.all_tag_search_prev_button.setEnabled(matched > 0)
        self.all_tag_search_next_button.setEnabled(matched > 0)
        self.all_tag_search_clear_button.setEnabled(query_active)

    def _toggle_all_sort_search_popup(self) -> None:
        """右側タグ一覧の並べ替えポップアップを開閉する。"""
        if self.all_sort_search_popup.isVisible():
            self.all_sort_search_popup.hide()
            return

        self.all_sort_search_popup.set_state(sort_mode=self._all_tag_sort_mode)
        self._place_all_sort_search_popup()
        self.all_sort_search_popup.show()
        self.all_sort_search_popup.raise_()

    def _place_all_sort_search_popup(self) -> None:
        """右側タグ一覧用の並べ替えポップアップを、並べ替えボタンの下に配置する。"""
        if not hasattr(self, "all_sort_search_popup") or not hasattr(self, "all_sort_button"):
            return

        button_top = self.all_sort_button.mapTo(self, QPoint(0, 0))
        popup_width = max(220, self.all_sort_button.width() + 72)

        self.all_sort_search_popup.setFixedWidth(popup_width)
        self.all_sort_search_popup.adjustSize()

        x = button_top.x()
        y = button_top.y() + self.all_sort_button.height() + 4

        max_x = max(0, self.width() - self.all_sort_search_popup.width() - 8)
        max_y = max(0, self.height() - self.all_sort_search_popup.height() - 8)
        self.all_sort_search_popup.move(max(0, min(x, max_x)), max(0, min(y, max_y)))

    def _set_all_tag_search_text(self, text: str) -> None:
        """右側タグ一覧検索をリアルタイムにハイライトへ反映する。"""
        self._all_tag_search_text = text
        self._refresh_all_tag_search_state(auto_jump=True, scroll_current=True)

    def _set_all_tag_search_exact_match(self, checked: bool) -> None:
        """右側タグ一覧検索の完全一致モードを切り替える。"""
        self._all_tag_search_exact_match = bool(checked)
        self._refresh_all_tag_search_state(auto_jump=True, scroll_current=True)

    def _clear_all_tag_search(self) -> None:
        """右側タグ一覧検索をクリアする。"""
        if not self._all_tag_search_text and not self.all_tag_search_line.text():
            return

        self._all_tag_search_text = ""
        self.all_tag_search_line.blockSignals(True)
        self.all_tag_search_line.clear()
        self.all_tag_search_line.blockSignals(False)
        self._refresh_all_tag_search_state()

    def _jump_all_tag_search_match(self, direction: int) -> None:
        """検索一致タグの前後へ移動する。"""
        if not self._all_tag_search_match_tags:
            return

        if self._all_tag_current_match_index < 0:
            self._all_tag_current_match_index = 0
        else:
            self._all_tag_current_match_index = (
                self._all_tag_current_match_index + direction
            ) % len(self._all_tag_search_match_tags)

        self._refresh_all_tag_search_state(scroll_current=True)

    def _schedule_scroll_to_current_all_tag_match(self) -> None:
        """FlowLayout更新後のgeometry確定を待って、現在一致タグへスクロールする。"""
        QTimer.singleShot(0, self._scroll_to_current_all_tag_match)
        QTimer.singleShot(20, self._scroll_to_current_all_tag_match)

    def _scroll_to_current_all_tag_match(self) -> None:
        if not (
            0 <= self._all_tag_current_match_index < len(self._all_tag_search_match_tags)
        ):
            return

        tag = self._all_tag_search_match_tags[self._all_tag_current_match_index]
        chip = self._all_tag_rows.get(tag)
        if chip is None:
            return

        own_layout = self.layout()
        if own_layout is not None:
            own_layout.activate()

        self.all_area.updateGeometry()
        self.all_area.flow_layout.setGeometry(
            QRect(
                0,
                0,
                self.all_area.container.width(),
                self.all_area.container.height(),
            )
        )

        chip_y = chip.mapTo(self.all_area.container, QPoint(0, 0)).y()
        bar = self.all_area.verticalScrollBar()
        margin = max(20, self.all_area.viewport().height() // 4)
        bar.setValue(max(0, chip_y - margin))

    def _apply_all_tag_sort_mode(self, mode: str) -> None:
        """並べ替えポップアップのOKで、右側タグ一覧の並び順を反映する。"""
        self.all_sort_search_popup.hide()

        if mode == self._all_tag_sort_mode:
            return

        self._all_tag_sort_mode = mode
        self.refresh_all_tags()

        if self._all_tag_search_match_tags:
            self._schedule_scroll_to_current_all_tag_match()

    def _booru_query_tag(self, tag: str) -> str:
        """
        Danbooru / Gelbooru 用にタグ検索文字列を正規化する。

        caption内では "closed eyes" のように空白区切りで入っている場合があるが、
        booru系のタグ検索では "closed_eyes" のように underscore 区切りが必要になる。
        既に underscore が入っているタグはそのまま維持する。
        """
        return "_".join(tag.strip().split())

    def _show_tag_context_menu(self, tag: str, global_pos: QPoint) -> None:
        """タグチップ共通の右クリックメニューを表示する。"""
        menu = QMenu(self)

        copy_action = menu.addAction("コピー")
        google_action = menu.addAction("Googleで検索")
        danbooru_action = menu.addAction("Danbooruで検索")
        danbooru_wiki_action = menu.addAction("Danbooru Wikiを開く")
        gelbooru_action = menu.addAction("Gelbooruで検索")
        gelbooru_wiki_action = menu.addAction("Gelbooru Wikiで検索")

        selected_action = menu.exec(global_pos)
        if selected_action == copy_action:
            QApplication.clipboard().setText(tag)
            return

        if selected_action == google_action:
            query = quote_plus(tag)
            QDesktopServices.openUrl(QUrl(f"https://www.google.com/search?q={query}"))
            return

        if selected_action == danbooru_action:
            query = quote_plus(self._booru_query_tag(tag))
            QDesktopServices.openUrl(QUrl(f"https://danbooru.donmai.us/posts?tags={query}"))
            return

        if selected_action == danbooru_wiki_action:
            query = quote_plus(self._booru_query_tag(tag))
            QDesktopServices.openUrl(QUrl(f"https://danbooru.donmai.us/wiki_pages/{query}"))
            return

        if selected_action == gelbooru_action:
            query = quote_plus(self._booru_query_tag(tag))
            QDesktopServices.openUrl(QUrl(f"https://gelbooru.com/index.php?page=post&s=list&tags={query}"))
            return

        if selected_action == gelbooru_wiki_action:
            query = quote_plus(self._booru_query_tag(tag))
            QDesktopServices.openUrl(QUrl(f"https://gelbooru.com/index.php?page=wiki&s=list&search={query}"))
            return

    def _connect_tag_context_menu(self, chip: QWidget) -> None:
        """context_menu_requested を持つタグチップを共通メニューへ接続する。"""
        if hasattr(chip, "context_menu_requested"):
            chip.context_menu_requested.connect(self._show_tag_context_menu)

    def set_tag_completion_settings(self, *, enabled: bool, limit: int) -> None:
        """新規に開く追加・置換ダイアログへDanbooru候補設定を渡す。"""
        self._tag_completion_enabled = bool(enabled)
        self._tag_completion_limit = normalize_tag_completion_limit(limit)

    def set_tag_color_enabled(self, enabled: bool) -> None:
        """タグカテゴリ色分けのON/OFFを切り替える。"""
        enabled = bool(enabled)
        if self._tag_color_enabled == enabled:
            return

        self._tag_color_enabled = enabled
        self._apply_tag_color_styles_to_existing_chips()

    def reload_tag_color_dictionary(self) -> None:
        """タグ色分け辞書を再読み込みし、既存チップへ反映する。"""
        self._tag_color_dictionary = TagColorDictionary.load()
        self._tag_chip_qss_cache.clear()
        self._all_tag_chip_qss_cache.clear()
        self._apply_tag_color_styles_to_existing_chips()

    def _tag_chip_category_qss(self, tag: str) -> str | None:
        if not self._tag_color_enabled:
            return None

        category_id = self._tag_color_dictionary.category_id_for_tag(tag)
        cached = self._tag_chip_qss_cache.get(category_id)
        if cached is not None:
            return cached

        style = self._tag_color_dictionary.style_for_tag(tag)
        qss = tag_chip_qss(style)
        self._tag_chip_qss_cache[category_id] = qss
        return qss

    def _all_tag_category_qss(self, tag: str) -> tuple[str | None, str | None]:
        if not self._tag_color_enabled:
            return None, None

        category_id = self._tag_color_dictionary.category_id_for_tag(tag)
        cached = self._all_tag_chip_qss_cache.get(category_id)
        if cached is not None:
            return cached

        style = self._tag_color_dictionary.style_for_tag(tag)
        qss_pair = (all_tag_chip_qss(style), all_tag_button_qss(style))
        self._all_tag_chip_qss_cache[category_id] = qss_pair
        return qss_pair

    def _apply_tag_color_style(self, chip: TagChip) -> None:
        chip.set_category_style(self._tag_chip_category_qss(chip.tag))

    def _apply_all_tag_color_style(self, chip: AllTagChip) -> None:
        chip_qss, button_qss = self._all_tag_category_qss(chip.tag)
        chip.set_category_style(chip_qss, button_qss)

    def _apply_tag_color_styles_to_existing_chips(self) -> None:
        for chip in self._current_tag_buttons.values():
            self._apply_tag_color_style(chip)
        for chip in self._current_checked_tag_chip_cache.values():
            self._apply_tag_color_style(chip)
        for chip in self._all_tag_chip_cache.values():
            self._apply_all_tag_color_style(chip)


    def set_tagger_execution_locked(self, locked: bool) -> None:
        """Tagger実行中はTagger以外のタブと保存操作を一時的に無効化する。"""
        locked = bool(locked)
        if self._tagger_execution_locked == locked:
            return

        self._tagger_execution_locked = locked
        self.save_button.setEnabled(not locked)
        self.history_button.setEnabled(not locked)

        if locked:
            self._tab_enabled_before_tagger_lock = {
                index: self.tabs.isTabEnabled(index)
                for index in range(self.tabs.count())
            }
            for index in range(self.tabs.count()):
                if self.tabs.tabText(index) != "Tagger":
                    self.tabs.setTabEnabled(index, False)
            return

        for index, enabled in self._tab_enabled_before_tagger_lock.items():
            if 0 <= index < self.tabs.count():
                self.tabs.setTabEnabled(index, enabled)
        self._tab_enabled_before_tagger_lock.clear()

    def set_items(self, items: list[ImageItem]) -> None:
        perf_enabled = dev_log_enabled("PERF_FOLDER_LOAD")
        if perf_enabled:
            perf_started = time.perf_counter()

        self.items = items
        single_target_override_was_active = self._single_target_override_active
        self._single_target_override_active = False

        # 操作履歴は読み込んでいるフォルダに対してだけ有効。
        # 新しいフォルダを読み込んだ場合は、過去フォルダの履歴を破棄する。
        self._tag_history.clear()
        self._current_tag_search_text = ""
        # 左側チェック中タグ一覧の並び替え設定は、アプリ起動中は維持する。
        # 通常1枚表示ではtxt上のタグ順を使うため並べ替えボタンは無効だが、
        # 次にチェック中画像表示へ戻った時は前回選んだ並び替えを再利用する。
        self._current_tag_search_match_tags = []
        self._current_tag_current_match_index = -1
        self._current_tag_sort_enabled = False
        self._current_tag_reorder_enabled = False
        self._current_tags_view_mode = None
        self._current_tags_checked_count = 0
        self._all_tag_search_text = ""
        self._all_tag_sort_mode = "name_asc"
        self._all_tag_search_match_tags = []
        self._all_tag_current_match_index = -1
        self._all_tag_highlighted_tags.clear()
        self._all_tag_current_highlight_tag = None
        if hasattr(self, "current_tag_search_line"):
            self.current_tag_search_line.blockSignals(True)
            self.current_tag_search_line.clear()
            self.current_tag_search_line.blockSignals(False)
            self._refresh_current_tag_search_state()
            self._refresh_current_sort_button_state()
        if hasattr(self, "current_sort_search_popup"):
            self.current_sort_search_popup.hide()
            self.current_sort_search_popup.set_state(sort_mode=self._current_tag_sort_mode)
        if hasattr(self, "all_tag_search_line"):
            self.all_tag_search_line.blockSignals(True)
            self.all_tag_search_line.clear()
            self.all_tag_search_line.blockSignals(False)
            self._refresh_all_tag_search_state()
        if hasattr(self, "all_sort_search_popup"):
            self.all_sort_search_popup.hide()
            self.all_sort_search_popup.set_state(sort_mode=self._all_tag_sort_mode)

        # フォルダが切り替わった時だけキャッシュを破棄する。
        # FlowLayout 化後は、Widgetを deleteLater() する前にレイアウト側の
        # 登録参照も解除しないと、前フォルダの破棄済みチップを後から触って
        # クラッシュする可能性がある。
        if perf_enabled:
            cache_clear_started = time.perf_counter()

        if hasattr(self, "current_checked_area"):
            self.current_checked_area.clear_registered_chips()
            self.current_checked_area.hide()
        if hasattr(self, "current_area"):
            self.current_area.show()

        for cached_chip in self._current_checked_tag_chip_cache.values():
            cached_chip.deleteLater()
        self._current_checked_tag_chip_cache.clear()

        self.all_area.clear_registered_chips()
        for cached_chip in self._all_tag_chip_cache.values():
            cached_chip.deleteLater()
        self._all_tag_chip_cache.clear()
        self._all_tag_rows.clear()
        if perf_enabled:
            cache_clear_ms = (time.perf_counter() - cache_clear_started) * 1000

        self._include_filter_tags.clear()
        self._exclude_filter_tags.clear()
        self._include_filter_order.clear()
        self._exclude_filter_order.clear()
        self._filter_condition_order.clear()
        self._clear_pending_all_scroll_anchor()
        self._filter_view_active = False
        self._filter_visible_indexes = []
        self._filter_visible_count = 0
        self._filter_base_count = len(items)
        # 新しいフォルダを読み込んだ場合は、作業セットを引き継がない。
        # 削除後は set_items を通さず共有 items の中身が更新されるため、
        # 作業モード中の削除ではこのリセットは走らない。
        self._all_tags_scope = "all"
        self._work_set_paths.clear()

        if perf_enabled:
            current_started = time.perf_counter()
        self.refresh_current_tags()
        if perf_enabled:
            current_ms = (time.perf_counter() - current_started) * 1000
            all_started = time.perf_counter()

        self.refresh_all_tags()

        if perf_enabled:
            all_ms = (time.perf_counter() - all_started) * 1000
            label_started = time.perf_counter()

        self.refresh_target_label()
        self._refresh_active_filter_panel()

        if single_target_override_was_active:
            self.single_target_override_changed.emit(False)

        if perf_enabled:
            label_and_filter_ui_ms = (time.perf_counter() - label_started) * 1000
            emit_started = time.perf_counter()

        self._emit_tag_filter_changed()

        if perf_enabled:
            emit_ms = (time.perf_counter() - emit_started) * 1000
            log.info(
                "[PERF_LOAD] tag_panel.set_items images=%d cache_clear=%.2fms "
                "refresh_current=%.2fms refresh_all_sync=%.2fms "
                "label_and_filter_ui=%.2fms emit_filter_reset=%.2fms total=%.2fms",
                len(items),
                cache_clear_ms,
                current_ms,
                all_ms,
                label_and_filter_ui_ms,
                emit_ms,
                (time.perf_counter() - perf_started) * 1000,
            )

    def refresh_editor_layouts_after_show(self) -> None:
        """
        Taggerタブからタグ編集タブへ戻った直後に、実際の表示幅でタグ領域を再配置する。

        Tagger反映時はタグ編集ページが非表示のまま内容だけ更新されることがある。
        非表示中のviewport幅や祖先の可視状態に依存した古いgeometryを残さないよう、
        現在表示中の左側領域と右側一覧を最後に一度だけ再レイアウトする。
        """
        if self.tabs.currentWidget() is not self.editor_page:
            return

        if self._current_tags_view_mode == "checked":
            self.current_checked_area.refresh_after_show()
        else:
            self.current_area.refresh_after_show()

        self.all_area.refresh_after_show()

    def set_current_index(self, index: int, *, defer_checked_refresh: bool = False) -> None:
        perf_enabled = dev_log_enabled("PERF_FILTER") or dev_log_enabled("UI_INTERACTION_DETAIL")
        perf_started = time.perf_counter() if perf_enabled else 0.0

        previous_index = self.current_index
        self.current_index = index

        checked_count = sum(1 for item in self.items if item.checked)
        skip_current_tags_refresh = (
            checked_count > 0
            and self._current_tags_view_mode == "checked"
            and self._current_tags_checked_count == checked_count
        )
        deferred_current_tags_refresh = (
            checked_count > 0
            and defer_checked_refresh
            and not self._single_target_override_active
        )

        refresh_ms = 0.0
        if deferred_current_tags_refresh:
            # チェック変更処理がQTimerで保留中の間は、画像選択側で重い再集計を先取りしない。
            # 青枠・プレビューは即時更新し、左側のチェック中タグ集計は
            # AppWindow._process_checks_changed() 側の1回更新に任せる。
            self._current_tag_sort_enabled = True
            self._current_tag_reorder_enabled = False
            if perf_enabled:
                log.info(
                    "[PERF] tag_panel.set_current_index defer_checked_refresh "
                    "index=%d previous=%d checked_count=%d view_mode=%s cached_checked_count=%d total=%.2fms",
                    index,
                    previous_index,
                    checked_count,
                    self._current_tags_view_mode,
                    self._current_tags_checked_count,
                    (time.perf_counter() - perf_started) * 1000,
                )
        elif skip_current_tags_refresh:
            # チェック中画像の集合が変わっていない限り、
            # 左側の「チェック中画像に含まれるタグ」集計は画像選択だけでは変わらない。
            self._current_tag_sort_enabled = True
            self._current_tag_reorder_enabled = False
            if perf_enabled:
                log.info(
                    "[PERF] tag_panel.set_current_index skip_current_tags_refresh "
                    "index=%d previous=%d checked_count=%d view_mode=%s total=%.2fms",
                    index,
                    previous_index,
                    checked_count,
                    self._current_tags_view_mode,
                    (time.perf_counter() - perf_started) * 1000,
                )
        else:
            if perf_enabled:
                refresh_started = time.perf_counter()
            self.refresh_current_tags(reason="set_current_index")
            if perf_enabled:
                refresh_ms = (time.perf_counter() - refresh_started) * 1000

        self.refresh_target_label()

        if perf_enabled and not skip_current_tags_refresh and not deferred_current_tags_refresh:
            log.info(
                "[PERF] tag_panel.set_current_index refreshed "
                "index=%d previous=%d checked_count=%d view_mode=%s refresh_current=%.2fms total=%.2fms",
                index,
                previous_index,
                checked_count,
                self._current_tags_view_mode,
                refresh_ms,
                (time.perf_counter() - perf_started) * 1000,
            )

    def is_single_target_override_active(self) -> bool:
        """黄色チェックを保持した一時単体編集モードが有効か返す。"""
        return (
            self._single_target_override_active
            and any(item.checked for item in self.items)
        )

    def _toggle_single_target_override(self) -> None:
        """左側タグ欄の操作対象を、チェック群と現在の青枠1枚で切り替える。"""
        checked_count = sum(1 for item in self.items if item.checked)
        if checked_count <= 0:
            self._single_target_override_active = False
            self.refresh_target_label()
            return

        self._single_target_override_active = (
            not self._single_target_override_active
        )
        self.refresh_current_tags(reason="single_target_override")
        self.refresh_target_label()
        self.single_target_override_changed.emit(
            self._single_target_override_active
        )

    def _set_single_target_toggle_button_active(
        self,
        active: bool,
    ) -> None:
        """単体編集モードの有効色を切替ボタンへ反映する。"""
        if not hasattr(self, "single_target_toggle_button"):
            return

        button = self.single_target_toggle_button
        active = bool(active)

        if bool(button.property("singleTargetActive")) == active:
            return

        button.setProperty("singleTargetActive", active)
        style = button.style()
        style.unpolish(button)
        style.polish(button)
        button.update()

    def refresh_target_label(self) -> None:
        checked_count = sum(1 for item in self.items if item.checked)
        override_deactivated = False

        if checked_count <= 0 and self._single_target_override_active:
            self._single_target_override_active = False
            override_deactivated = True

        self.single_target_toggle_button.setEnabled(
            checked_count > 0
        )

        if checked_count > 0:
            if self._single_target_override_active:
                self.target_label.setText(
                    f"操作対象：表示中の1枚（チェック{checked_count}件保持中）"
                )
                self.single_target_toggle_button.setText("戻す")
                self.single_target_toggle_button.setToolTip(
                    "左側タグ欄の操作対象を、保持中の黄色チェック画像へ戻します"
                )
            else:
                self.target_label.setText(
                    f"操作対象：チェック中画像 {checked_count}件"
                )
                self.single_target_toggle_button.setText(
                    "対象切替"
                )
                self.single_target_toggle_button.setToolTip(
                    "黄色チェックを維持したまま、左側タグ欄の操作対象だけを現在の青枠画像へ切り替えます"
                )
        else:
            self.target_label.setText("操作対象：表示中の1枚")
            self.single_target_toggle_button.setText("対象切替")
            self.single_target_toggle_button.setToolTip(
                "黄色チェックがある時に、左側タグ欄の操作対象を切り替えられます"
            )

        self._set_single_target_toggle_button_active(
            checked_count > 0
            and self._single_target_override_active
        )

        if override_deactivated:
            self.single_target_override_changed.emit(False)

    def _set_current_tag_count_label(self, count: int) -> None:
        """左側パネルに現在表示しているタグチップ数を表示する。"""
        if not hasattr(self, "current_tag_count_label"):
            return
        self.current_tag_count_label.setText(f"タグ数: {max(0, count)}")

    def _set_all_tag_count_label(
        self,
        count: int,
        *,
        full_count: int | None = None,
        work_count: int | None = None,
    ) -> None:
        """右側パネルに現在表示しているタグチップ数を表示する。"""
        if not hasattr(self, "all_tag_count_label"):
            return

        visible_count = max(0, count)
        details: list[str] = []

        if self._all_tags_scope == "work_set":
            # 作業セット中にさらにタグフィルタしている時だけ、
            # 作業セット内の母数も併記する。
            if self._filter_view_active and work_count is not None:
                details.append(f"作業{max(0, work_count)}")

            # 「全N」は常にフォルダ全体のユニークタグ数。
            if full_count is not None and full_count != visible_count:
                details.append(f"全{max(0, full_count)}")
        else:
            if full_count is not None and full_count != visible_count:
                details.append(f"全{max(0, full_count)}")

        if details:
            self.all_tag_count_label.setText(f"タグ数: {visible_count} ({' / '.join(details)})")
        else:
            self.all_tag_count_label.setText(f"タグ数: {visible_count}")

    def _unique_tag_count_for_indexes(self, indexes: list[int]) -> int:
        """指定画像群に含まれるユニークタグ数を返す。"""
        tags: set[str] = set()
        for index in indexes:
            if 0 <= index < len(self.items):
                tags.update(self.items[index].tags)
        return len(tags)

    def _target_indexes(self) -> list[int]:
        if self._single_target_override_active:
            if 0 <= self.current_index < len(self.items):
                return [self.current_index]
            return []

        checked = [i for i, item in enumerate(self.items) if item.checked]
        if checked:
            return checked
        if 0 <= self.current_index < len(self.items):
            return [self.current_index]
        return []

    def refresh_current_tags(self, *, reason: str = "manual") -> None:
        perf_started = time.perf_counter()
        perf_enabled = dev_log_enabled("TAG_UI_LAYOUT") or dev_log_enabled("PERF_FILTER")

        self._current_tag_buttons.clear()
        chips: list[QWidget] = []

        checked_scan_started = time.perf_counter() if perf_enabled else 0.0
        checked_items = [item for item in self.items if item.checked]
        checked_scan_ms = (time.perf_counter() - checked_scan_started) * 1000 if perf_enabled else 0.0
        use_checked_targets = (
            bool(checked_items)
            and not self._single_target_override_active
        )

        tag_entries: list[tuple[str, str | None, str]] = []
        self._current_tag_sort_enabled = use_checked_targets
        self._current_tag_reorder_enabled = (
            not use_checked_targets
            and 0 <= self.current_index < len(self.items)
        )

        counter_ms = 0.0
        sort_ms = 0.0
        entry_prepare_ms = 0.0
        chip_prepare_ms = 0.0
        set_chips_ms = 0.0
        current_badge_update_ms = 0.0
        current_checked_update_ms = 0.0
        current_drag_update_ms = 0.0
        current_search_update_ms = 0.0
        current_append_ms = 0.0
        current_badge_changed = 0
        current_checked_changed = 0
        current_drag_changed = 0
        current_search_changed = 0
        new_chips = 0
        reused_chips = 0

        if use_checked_targets:
            self._current_tags_view_mode = "checked"
            self.current_tags_label.setText("チェック中画像に含まれるタグ")
            counter = Counter()
            checked_count = len(checked_items)
            self._current_tags_checked_count = checked_count

            counter_started = time.perf_counter() if perf_enabled else 0.0
            # チェック画像では、1枚の画像内に同じタグが重複していても
            # 「その画像に含まれる」扱いで1カウントにする。
            for item in checked_items:
                counter.update(set(item.tags))
            counter_ms = (time.perf_counter() - counter_started) * 1000 if perf_enabled else 0.0

            sort_started = time.perf_counter() if perf_enabled else 0.0
            sorted_counts = self._sort_current_tag_counts(list(counter.items()))
            sort_ms = (time.perf_counter() - sort_started) * 1000 if perf_enabled else 0.0

            entry_started = time.perf_counter() if perf_enabled else 0.0
            tag_entries = [
                (tag, f"{count}/{checked_count}", "ratio")
                for tag, count in sorted_counts
            ]
            entry_prepare_ms = (time.perf_counter() - entry_started) * 1000 if perf_enabled else 0.0

            chip_started = time.perf_counter() if perf_enabled else 0.0
            visible_tags: set[str] = set()

            for tag, badge_text, _badge_kind in tag_entries:
                visible_tags.add(tag)
                chip = self._current_checked_tag_chip_cache.get(tag)

                if chip is None:
                    chip = TagChip(tag, badge_text, "ratio")
                    self._connect_tag_context_menu(chip)
                    self._current_checked_tag_chip_cache[tag] = chip
                    new_chips += 1
                else:
                    badge_started = time.perf_counter() if perf_enabled else 0.0
                    if chip.update_badge_text(badge_text):
                        current_badge_changed += 1
                    current_badge_update_ms += (
                        (time.perf_counter() - badge_started) * 1000
                        if perf_enabled
                        else 0.0
                    )
                    reused_chips += 1

                self._apply_tag_color_style(chip)

                # 以前の選択/検索/ドラッグ状態を引きずらないよう、表示前に通常状態へ戻す。
                checked_started = time.perf_counter() if perf_enabled else 0.0
                if chip.setChecked(False):
                    current_checked_changed += 1
                current_checked_update_ms += (
                    (time.perf_counter() - checked_started) * 1000
                    if perf_enabled
                    else 0.0
                )

                drag_started = time.perf_counter() if perf_enabled else 0.0
                if chip.set_drag_enabled(False):
                    current_drag_changed += 1
                current_drag_update_ms += (
                    (time.perf_counter() - drag_started) * 1000
                    if perf_enabled
                    else 0.0
                )

                search_started = time.perf_counter() if perf_enabled else 0.0
                if chip.set_search_highlight(False):
                    current_search_changed += 1
                current_search_update_ms += (
                    (time.perf_counter() - search_started) * 1000
                    if perf_enabled
                    else 0.0
                )

                append_started = time.perf_counter() if perf_enabled else 0.0
                chips.append(chip)
                self._current_tag_buttons[tag] = chip
                current_append_ms += (
                    (time.perf_counter() - append_started) * 1000
                    if perf_enabled
                    else 0.0
                )

            # FlowChipWrapArea は一度登録した QWidget をレイアウト内部に保持する。
            # そのため、チェック対象から一時的に消えたタグチップをここで deleteLater() すると、
            # 次回 rebuild 時に「Internal C++ object already deleted」で落ちる。
            #
            # 右側 AllTagArea と同じく、チェック中タグ用チップもフォルダ切替までは保持し、
            # 表示対象から外れたチップは FlowChipWrapArea 側で hide する。
            # フォルダ切替時は set_items() で current_checked_area.clear_registered_chips()
            # → _current_checked_tag_chip_cache の deleteLater() の順に破棄する。
            chip_prepare_ms = (time.perf_counter() - chip_started) * 1000 if perf_enabled else 0.0
        else:
            self._current_tags_view_mode = "single"
            self._current_tags_checked_count = 0
            self.current_tags_label.setText("表示中画像のタグ")
            if hasattr(self, "current_sort_search_popup"):
                self.current_sort_search_popup.hide()
                self.current_sort_search_popup.set_state(sort_mode=self._current_tag_sort_mode)

            entry_started = time.perf_counter() if perf_enabled else 0.0
            if 0 <= self.current_index < len(self.items):
                counter = Counter(self.items[self.current_index].tags)
                seen: set[str] = set()

                # 表示中画像ではタグ順を維持しつつ、同名タグは1チップへまとめる。
                # 重複しているタグだけ、タグ本体の外側に "×2" のようなバッジを出す。
                for tag in self.items[self.current_index].tags:
                    if tag in seen:
                        continue

                    seen.add(tag)
                    count = counter[tag]
                    badge_text = f"×{count}" if count > 1 else None
                    tag_entries.append((tag, badge_text, "duplicate"))
            entry_prepare_ms = (time.perf_counter() - entry_started) * 1000 if perf_enabled else 0.0

            chip_started = time.perf_counter() if perf_enabled else 0.0
            for tag, badge_text, badge_kind in tag_entries:
                chip = TagChip(tag, badge_text, badge_kind)
                self._apply_tag_color_style(chip)
                chip.set_drag_enabled(self._current_tag_reorder_enabled)
                self._connect_tag_context_menu(chip)
                chips.append(chip)
                self._current_tag_buttons[tag] = chip
                new_chips += 1
            chip_prepare_ms = (time.perf_counter() - chip_started) * 1000 if perf_enabled else 0.0

        set_chips_started = time.perf_counter() if perf_enabled else 0.0
        self._set_current_tag_count_label(len(chips))
        if use_checked_targets:
            # チェック中画像のタグ欄は大量タグになりやすいので、軽量Flow表示へ切り替える。
            self.current_area.set_tag_reorder_enabled(False)
            self.current_area.hide()
            self.current_checked_area.show()
            self.current_checked_area.set_chips(chips)
        else:
            # 通常1枚表示はtxt上のタグ順・ドラッグ並び替えが必要なので従来表示を使う。
            self.current_checked_area.hide()
            self.current_area.show()
            self.current_area.set_tag_reorder_enabled(self._current_tag_reorder_enabled)
            self.current_area.set_chips(chips)
        set_chips_ms = (time.perf_counter() - set_chips_started) * 1000 if perf_enabled else 0.0

        self._refresh_current_sort_button_state()
        self._refresh_current_tag_search_state()

        if perf_enabled:
            log.info(
                "[PERF] tag_panel.refresh_current_tags reason=%s view_mode=%s "
                "checked_images=%d chips=%d new_chips=%d reused_chips=%d cache_size=%d "
                "sort_enabled=%s checked_scan=%.2fms counter=%.2fms sort=%.2fms "
                "entry_prepare=%.2fms chip_prepare=%.2fms set_chips=%.2fms schedule_rebuild_total=%.2fms",
                reason,
                self._current_tags_view_mode,
                len(checked_items),
                len(chips),
                new_chips,
                reused_chips,
                len(self._current_checked_tag_chip_cache),
                self._current_tag_sort_enabled,
                checked_scan_ms,
                counter_ms,
                sort_ms,
                entry_prepare_ms,
                chip_prepare_ms,
                set_chips_ms,
                (time.perf_counter() - perf_started) * 1000,
            )
            if self._current_tags_view_mode == "checked":
                log.info(
                    "[PERF] tag_panel.refresh_current_tags_checked_detail reason=%s "
                    "chips=%d new_chips=%d reused_chips=%d "
                    "badge_update=%.2fms badge_changed=%d checked_update=%.2fms checked_changed=%d "
                    "drag_update=%.2fms drag_changed=%d search_update=%.2fms search_changed=%d "
                    "append=%.2fms",
                    reason,
                    len(chips),
                    new_chips,
                    reused_chips,
                    current_badge_update_ms,
                    current_badge_changed,
                    current_checked_update_ms,
                    current_checked_changed,
                    current_drag_update_ms,
                    current_drag_changed,
                    current_search_update_ms,
                    current_search_changed,
                    current_append_ms,
                )

    def _unique_visible_tags_for_current_image(self) -> list[str]:
        """左側通常状態の表示チップ順に対応する、重複をまとめたタグ一覧を返す。"""
        if not (0 <= self.current_index < len(self.items)):
            return []

        tags: list[str] = []
        seen: set[str] = set()
        for tag in self.items[self.current_index].tags:
            if tag in seen:
                continue
            tags.append(tag)
            seen.add(tag)
        return tags

    def _reorder_current_image_tags(self, moved_tag: str, insert_index: int) -> None:
        """
        左側通常状態で、表示チップのドラッグ順に合わせて現在画像のタグ順を更新する。

        同名タグが重複している場合は、表示チップ1個に対応する同名タグ群をまとめて移動する。
        """
        if not self._current_tag_reorder_enabled:
            return
        if not (0 <= self.current_index < len(self.items)):
            return
        if any(item.checked for item in self.items):
            return

        item = self.items[self.current_index]
        before_history = self._snapshot_tags_for_indexes([self.current_index])
        if moved_tag not in item.tags:
            return

        visible_order = self._unique_visible_tags_for_current_image()
        if moved_tag not in visible_order:
            return

        old_index = visible_order.index(moved_tag)
        remaining_order = [tag for tag in visible_order if tag != moved_tag]

        target_index = max(0, min(insert_index, len(visible_order)))
        if target_index > old_index:
            target_index -= 1
        target_index = max(0, min(target_index, len(remaining_order)))

        if target_index == old_index:
            return

        next_order = remaining_order[:target_index] + [moved_tag] + remaining_order[target_index:]
        grouped: dict[str, list[str]] = {tag: [] for tag in next_order}
        extra_tags: list[str] = []

        for tag in item.tags:
            if tag in grouped:
                grouped[tag].append(tag)
            else:
                extra_tags.append(tag)

        next_tags: list[str] = []
        for tag in next_order:
            next_tags.extend(grouped[tag])
        next_tags.extend(extra_tags)

        if next_tags == item.tags:
            return

        item.tags = next_tags
        item.refresh_dirty()
        if self._record_tag_history_from_snapshot(
            operation_type="並び替え",
            summary=None,
            detail=f"左側タグチップのドラッグでタグ順を変更しました。\n\n移動タグ: {moved_tag}",
            before_tags_by_path=before_history,
            history_labels=[f'"{moved_tag}"'],
            history_count_tags=[moved_tag],
        ):
            self._refresh_after_change()
        self._refresh_current_tag_search_state(scroll_current=True)

    def set_checked_work_mode(
        self,
        enabled: bool,
        work_set_paths: set[Path] | None = None,
        *,
        refresh: bool = True,
        clear_filters: bool = True,
    ) -> None:
        """
        サムネイル欄で固定された作業セットに追従し、
        右側タグ一覧の集計と右側タグ操作対象を切り替える。

        enabled=False:
            フォルダ内の全画像を対象にする。
        enabled=True:
            モード開始時に固定された作業セットだけを対象にする。
            モード中に黄色チェックを変更しても、この対象範囲は変わらない。
        """
        scope = "work_set" if enabled else "all"
        scope_changed = self._all_tags_scope != scope
        self._all_tags_scope = scope
        self._work_set_paths = set(work_set_paths or set()) if enabled else set()

        if scope_changed and clear_filters:
            self._include_filter_tags.clear()
            self._exclude_filter_tags.clear()
            self._include_filter_order.clear()
            self._exclude_filter_order.clear()
            self._filter_condition_order.clear()
            self._clear_pending_all_scroll_anchor()
            self._filter_view_active = False
            self._filter_visible_indexes = []
            self._refresh_active_filter_panel()
            self._emit_tag_filter_changed()

        if refresh:
            self.refresh_all_tags()

    def _base_all_target_indexes(self) -> list[int]:
        """タグフィルタがない場合の、右側タグ一覧・操作の基礎対象範囲。"""
        if self._all_tags_scope == "work_set":
            return [
                index
                for index, item in enumerate(self.items)
                if item.image_path in self._work_set_paths
            ]
        return list(range(len(self.items)))

    def _all_target_indexes(self) -> list[int]:
        """
        右側タグ一覧と右側タグ操作の実対象を返す。

        タグフィルタ有効時は、サムネイル欄で現在表示されている画像だけを対象にする。
        フィルタなしでは、全画像または固定作業セット全体を対象にする。
        """
        if self._filter_view_active:
            return [
                index
                for index in self._filter_visible_indexes
                if 0 <= index < len(self.items)
            ]
        return self._base_all_target_indexes()

    def _all_target_label(self) -> str:
        if self._filter_view_active:
            return "表示中画像"
        if self._all_tags_scope == "work_set":
            return "作業セット"
        return "全画像"

    def _all_target_indexes_or_message(self) -> list[int]:
        indexes = self._all_target_indexes()
        if indexes:
            return indexes

        if self._filter_view_active:
            QMessageBox.information(
                self,
                "表示中画像",
                "フィルタ結果に画像がありません。",
            )
        elif self._all_tags_scope == "work_set":
            QMessageBox.information(
                self,
                "作業セット",
                "作業セットに画像がありません。",
            )
        return indexes

    def refresh_all_tags_for_check_change(self) -> None:
        """
        作業セットは固定対象なので、黄色チェック変更だけでは右側タグ一覧を変えない。
        ただし、サムネイル側の表示範囲変更が必要な場合は、
        ThumbnailPanelからset_tag_filter_view()経由で再集計される。
        """
        return

    def _clear_pending_all_scroll_anchor(self) -> None:
        self._pending_all_scroll_anchor_tags = []
        self._pending_all_scroll_anchor_panel_y = None
        self._pending_all_scroll_restore_armed = False

    def _arm_pending_all_scroll_restore(self) -> None:
        self._pending_all_scroll_restore_armed = bool(
            self._pending_all_scroll_anchor_tags
            and self._pending_all_scroll_anchor_panel_y is not None
        )

    def _set_all_area_chips(self, chips: list[QWidget]) -> None:
        """
        右側タグ一覧の内容を差し替える時点でのみ、保留中のスクロール復元を有効化する。
        これにより、フィルタ管理欄の表示/非表示変更で先にアンカーが消費されるのを防ぐ。
        """
        self._arm_pending_all_scroll_restore()
        self.all_area.set_chips(chips)

    def _remember_all_scroll_anchor_for_clicked_tag(self, tag: str) -> None:
        """
        緑/赤フィルタを押したタグと、その近傍タグをスクロール復元候補として記録する。
        includeでは押したタグ自身、excludeではフィルタ後も残る近傍タグが使われる。
        """
        order = list(self._all_tag_rows.keys())
        chip = self._all_tag_rows.get(tag)
        if chip is None or tag not in order:
            self._remember_current_all_scroll_anchor()
            return

        position = order.index(tag)
        candidates = [tag]
        distance = 1
        while position + distance < len(order) or position - distance >= 0:
            if position + distance < len(order):
                candidates.append(order[position + distance])
            if position - distance >= 0:
                candidates.append(order[position - distance])
            distance += 1

        self._pending_all_scroll_anchor_tags = candidates
        self._pending_all_scroll_anchor_panel_y = chip.mapTo(
            self, QPoint(0, 0)
        ).y()

    def _remember_filter_condition_anchor(self, tag: str) -> None:
        """
        解除しようとしているフィルタ条件タグをアンカーにする。

        include 条件のようにタグが現在の一覧にも表示されている場合は、
        緑/赤ボタンで解除した時と同じくそのタグ自身を基準に戻す。
        exclude 条件のようにタグが現在の一覧から消えている場合は、
        解除前に見えている一覧位置を基準にする。
        """
        if tag in self._all_tag_rows:
            self._remember_all_scroll_anchor_for_clicked_tag(tag)
        else:
            self._remember_current_all_scroll_anchor()

    def _last_active_filter_condition_tag(self) -> str | None:
        active = self._include_filter_tags | self._exclude_filter_tags
        for tag in reversed(self._filter_condition_order):
            if tag in active:
                return tag
        return None

    def _remember_current_all_scroll_anchor(self) -> None:
        """解除前に現在見えているタグ周辺の位置を可能な限り維持する。"""
        if not self._all_tag_rows:
            self._clear_pending_all_scroll_anchor()
            return

        viewport_height = self.all_area.viewport().height()
        visible: list[tuple[int, str, QWidget]] = []
        for tag, chip in self._all_tag_rows.items():
            y = chip.mapTo(self.all_area.viewport(), QPoint(0, 0)).y()
            height = chip.height() or chip.sizeHint().height()
            if y + height >= 0 and y <= viewport_height:
                visible.append((abs(y), tag, chip))

        if visible:
            _, tag, chip = min(visible, key=lambda entry: entry[0])
        else:
            tag, chip = next(iter(self._all_tag_rows.items()))

        order = list(self._all_tag_rows.keys())
        position = order.index(tag)
        candidates = [tag]
        distance = 1
        while position + distance < len(order) or position - distance >= 0:
            if position + distance < len(order):
                candidates.append(order[position + distance])
            if position - distance >= 0:
                candidates.append(order[position - distance])
            distance += 1

        self._pending_all_scroll_anchor_tags = candidates
        self._pending_all_scroll_anchor_panel_y = chip.mapTo(
            self, QPoint(0, 0)
        ).y()

    def _on_all_area_layout_rebuilt(self) -> None:
        """
        右側タグ一覧の本命差替え後だけ、保留中のアンカー復元を開始する。

        フィルタ解除では、管理欄の消滅と全タグ一覧への再生成が連続して起きるため、
        armed 前の中間レイアウト変更ではアンカーを消費しない。
        """
        if (
            not self._pending_all_scroll_restore_armed
            or not self._pending_all_scroll_anchor_tags
            or self._pending_all_scroll_anchor_panel_y is None
        ):
            return

        self._pending_all_scroll_restore_armed = False
        # ChipWrapArea と親レイアウトの geometry が落ち着くまで待つ。
        QTimer.singleShot(0, self._restore_all_scroll_anchor_first_pass)

    def _minimum_all_scroll_anchor_panel_y(self) -> int:
        """右側タグ一覧のアンカーを最低限このY座標より下へ出す。

        フィルタ条件パネルが出現した直後に、押したタグを「操作前と同じ
        TagPanel内Y座標」へ戻すだけだと、そのY座標が新しく表示された
        フィルタ条件パネルの領域内になり、タグが上端に隠れて見えることがある。
        そのため、フィルタ条件パネルとタグ一覧viewportの下端側を安全ラインにする。
        """
        margin = 6
        minimum_y = self.all_area.viewport().mapTo(self, QPoint(0, 0)).y() + margin

        if self.active_filter_panel.isVisible():
            filter_bottom_y = self.active_filter_panel.mapTo(
                self,
                QPoint(0, self.active_filter_panel.height()),
            ).y()
            minimum_y = max(minimum_y, filter_bottom_y + margin)

        return minimum_y

    def _apply_pending_all_scroll_anchor(self) -> bool:
        candidates = list(self._pending_all_scroll_anchor_tags)
        target_y = self._pending_all_scroll_anchor_panel_y

        if not candidates or target_y is None:
            return False

        anchor_chip = next(
            (self._all_tag_rows[tag] for tag in candidates if tag in self._all_tag_rows),
            None,
        )
        if anchor_chip is None:
            return False

        own_layout = self.layout()
        if own_layout is not None:
            own_layout.activate()
        self.active_filter_panel.updateGeometry()
        self.all_area.updateGeometry()
        area_layout = self.all_area.container.layout()
        if area_layout is not None:
            area_layout.activate()
        anchor_parent = anchor_chip.parentWidget()
        if anchor_parent is not None and anchor_parent.layout() is not None:
            anchor_parent.layout().activate()

        # TagPanel 全体基準の座標で補正するため、管理欄が消えた高さも吸収する。
        # ただし、フィルタ条件パネルが新しく出た場合は、以前のY座標が
        # パネルの領域内になることがある。最低限、条件パネルの下に
        # アンカーが見える位置まで押し下げる。
        target_y = max(target_y, self._minimum_all_scroll_anchor_panel_y())

        current_y = anchor_chip.mapTo(self, QPoint(0, 0)).y()
        bar = self.all_area.verticalScrollBar()
        bar.setValue(bar.value() + current_y - target_y)
        return True

    def _restore_all_scroll_anchor_first_pass(self) -> None:
        if not self._apply_pending_all_scroll_anchor():
            self._clear_pending_all_scroll_anchor()
            return

        # スクロールバー更新による最終geometry変化を待ち、同じ基準でもう一度合わせる。
        QTimer.singleShot(0, self._restore_all_scroll_anchor_final_pass)

    def _restore_all_scroll_anchor_final_pass(self) -> None:
        self._apply_pending_all_scroll_anchor()
        self._clear_pending_all_scroll_anchor()

    def _ordered_filter_tags(self, mode: str) -> list[str]:
        active = self._include_filter_tags if mode == "include" else self._exclude_filter_tags
        order = self._include_filter_order if mode == "include" else self._exclude_filter_order
        result = [tag for tag in order if tag in active]
        result.extend(sorted((tag for tag in active if tag not in result), key=str.casefold))
        return result

    def refresh_all_tags(self) -> None:
        perf_enabled = dev_log_enabled("PERF_FILTER")
        perf_started = time.perf_counter()

        clear_started = time.perf_counter() if perf_enabled else 0.0
        self._all_tag_rows.clear()
        clear_ms = (time.perf_counter() - clear_started) * 1000 if perf_enabled else 0.0

        target_started = time.perf_counter() if perf_enabled else 0.0
        target_indexes = self._all_target_indexes()
        target_ms = (time.perf_counter() - target_started) * 1000 if perf_enabled else 0.0

        # 右側の「全N」は、作業セットやタグフィルタをかける前の
        # フォルダ全体のユニークタグ数として表示する。
        # 作業セット中にさらにタグフィルタしている場合だけ、
        # 作業セット内の母数も「作業N」として併記する。
        all_tag_full_count = (
            self._unique_tag_count_for_indexes(list(range(len(self.items))))
            if self._filter_view_active or self._all_tags_scope == "work_set"
            else None
        )
        work_set_tag_count = (
            self._unique_tag_count_for_indexes(self._base_all_target_indexes())
            if self._all_tags_scope == "work_set"
            else None
        )

        label_started = time.perf_counter() if perf_enabled else 0.0
        if self._all_tags_scope == "work_set":
            all_tags_label_text = "作業セットのタグ一覧"
        else:
            all_tags_label_text = "全画像のタグ一覧"
        if self._filter_view_active:
            all_tags_label_text += "（フィルタ中）"
        self.all_tags_label.setText(all_tags_label_text)
        label_ms = (time.perf_counter() - label_started) * 1000 if perf_enabled else 0.0

        chips: list[QWidget] = []

        if self._filter_view_active and not target_indexes:
            empty_label = QLabel("フィルタ結果に画像がありません")
            empty_label.setStyleSheet("color: #999999; padding: 4px;")
            self._set_all_area_chips([empty_label])
            self._clear_pending_all_scroll_anchor()
            self._update_all_tag_search_highlights()
            self._set_all_tag_count_label(0, full_count=all_tag_full_count, work_count=work_set_tag_count)
            if perf_enabled:
                log.info(
                    "[PERF] tag_panel.refresh_all_tags_detail target_images=0 unique_tags=0 "
                    "state=filter_empty clear=%.2fms target_indexes=%.2fms label=%.2fms "
                    "sync_total=%.2fms",
                    clear_ms,
                    target_ms,
                    label_ms,
                    (time.perf_counter() - perf_started) * 1000,
                )
            return

        if self._all_tags_scope == "work_set" and not target_indexes:
            empty_label = QLabel("作業セットに画像がありません")
            empty_label.setStyleSheet("color: #999999; padding: 4px;")
            self._set_all_area_chips([empty_label])
            self._clear_pending_all_scroll_anchor()
            self._update_all_tag_search_highlights()
            self._set_all_tag_count_label(0, full_count=all_tag_full_count, work_count=work_set_tag_count)
            if perf_enabled:
                log.info(
                    "[PERF] tag_panel.refresh_all_tags_detail target_images=0 unique_tags=0 "
                    "state=work_set_empty clear=%.2fms target_indexes=%.2fms label=%.2fms "
                    "sync_total=%.2fms",
                    clear_ms,
                    target_ms,
                    label_ms,
                    (time.perf_counter() - perf_started) * 1000,
                )
            return

        recount_started = time.perf_counter()
        counter = Counter()
        for index in target_indexes:
            if 0 <= index < len(self.items):
                counter.update(self.items[index].tags)
        recount_ms = (time.perf_counter() - recount_started) * 1000

        if not counter:
            empty_label = QLabel("タグなし")
            empty_label.setStyleSheet("color: #999999; padding: 4px;")
            self._set_all_area_chips([empty_label])
            self._clear_pending_all_scroll_anchor()
            self._update_all_tag_search_highlights()
            self._set_all_tag_count_label(0, full_count=all_tag_full_count, work_count=work_set_tag_count)
            if perf_enabled:
                log.info(
                    "[PERF] tag_panel.refresh_all_tags_detail target_images=%d unique_tags=0 "
                    "state=no_tags clear=%.2fms target_indexes=%.2fms label=%.2fms "
                    "recount=%.2fms sync_total=%.2fms",
                    len(target_indexes),
                    clear_ms,
                    target_ms,
                    label_ms,
                    recount_ms,
                    (time.perf_counter() - perf_started) * 1000,
                )
            return

        sort_started = time.perf_counter() if perf_enabled else 0.0
        sorted_entries = self._sort_all_tag_entries(list(counter.items()))
        sort_ms = (time.perf_counter() - sort_started) * 1000 if perf_enabled else 0.0

        self._set_all_tag_count_label(len(counter), full_count=all_tag_full_count, work_count=work_set_tag_count)

        chips_started = time.perf_counter()
        new_chips = 0
        reused_chips = 0

        create_ms = 0.0
        connect_ms = 0.0
        update_count_ms = 0.0
        reset_selection_ms = 0.0
        filter_mode_ms = 0.0
        inline_search_highlight_ms = 0.0
        append_ms = 0.0

        for tag, count in sorted_entries:
            chip = self._all_tag_chip_cache.get(tag)

            if chip is None:
                create_started = time.perf_counter() if perf_enabled else 0.0
                chip = AllTagChip(tag, count)
                create_ms += (time.perf_counter() - create_started) * 1000 if perf_enabled else 0.0

                connect_started = time.perf_counter() if perf_enabled else 0.0
                self._connect_tag_context_menu(chip)
                chip.clicked.connect(self._toggle_all_tag_selection)
                chip.include_clicked.connect(self._toggle_include_filter)
                chip.exclude_clicked.connect(self._toggle_exclude_filter)
                self._all_tag_chip_cache[tag] = chip
                connect_ms += (time.perf_counter() - connect_started) * 1000 if perf_enabled else 0.0

                new_chips += 1
            else:
                update_started = time.perf_counter() if perf_enabled else 0.0
                chip.update_count(count)
                update_count_ms += (time.perf_counter() - update_started) * 1000 if perf_enabled else 0.0
                reused_chips += 1

            self._apply_all_tag_color_style(chip)

            # 現行仕様では右側一覧再集計時にタグ本体の操作選択は解除される。
            reset_started = time.perf_counter() if perf_enabled else 0.0
            chip.reset_operation_selection()
            reset_selection_ms += (time.perf_counter() - reset_started) * 1000 if perf_enabled else 0.0

            filter_started = time.perf_counter() if perf_enabled else 0.0
            if tag in self._include_filter_tags:
                chip.set_filter_mode("include")
            elif tag in self._exclude_filter_tags:
                chip.set_filter_mode("exclude")
            else:
                chip.set_filter_mode(None)
            filter_mode_ms += (time.perf_counter() - filter_started) * 1000 if perf_enabled else 0.0

            # 検索ハイライトは、ループ内で全チップへ毎回styleSheetを再適用すると重い。
            # ここでは触らず、最後に _update_all_tag_search_highlights() の差分更新へ一本化する。
            search_started = time.perf_counter() if perf_enabled else 0.0
            inline_search_highlight_ms += (time.perf_counter() - search_started) * 1000 if perf_enabled else 0.0

            append_started = time.perf_counter() if perf_enabled else 0.0
            chips.append(chip)
            self._all_tag_rows[tag] = chip
            append_ms += (time.perf_counter() - append_started) * 1000 if perf_enabled else 0.0

        chips_ms = (time.perf_counter() - chips_started) * 1000

        schedule_started = time.perf_counter()
        self._set_all_area_chips(chips)
        schedule_ms = (time.perf_counter() - schedule_started) * 1000

        search_refresh_started = time.perf_counter() if perf_enabled else 0.0
        self._update_all_tag_search_highlights()
        search_refresh_ms = (time.perf_counter() - search_refresh_started) * 1000 if perf_enabled else 0.0

        if perf_enabled:
            log.info(
                "[PERF] tag_panel.refresh_all_tags_detail target_images=%d unique_tags=%d "
                "clear=%.2fms target_indexes=%.2fms label=%.2fms recount=%.2fms "
                "sort=%.2fms chip_prepare=%.2fms create=%.2fms connect=%.2fms "
                "update_count=%.2fms reset_selection=%.2fms filter_mode=%.2fms "
                "inline_search_highlight=%.2fms append_rows=%.2fms schedule_rebuild=%.2fms "
                "search_refresh=%.2fms new_chips=%d reused_chips=%d cache_size=%d sync_total=%.2fms",
                len(target_indexes),
                len(counter),
                clear_ms,
                target_ms,
                label_ms,
                recount_ms,
                sort_ms,
                chips_ms,
                create_ms,
                connect_ms,
                update_count_ms,
                reset_selection_ms,
                filter_mode_ms,
                inline_search_highlight_ms,
                append_ms,
                schedule_ms,
                search_refresh_ms,
                new_chips,
                reused_chips,
                len(self._all_tag_chip_cache),
                (time.perf_counter() - perf_started) * 1000,
            )

        if perf_enabled:
            log.info(
                "[PERF] tag_panel.refresh_all_tags target_images=%d unique_tags=%d "
                "recount=%.2fms chip_prepare=%.2fms new_chips=%d reused_chips=%d "
                "cache_size=%d schedule_rebuild=%.2fms sync_total=%.2fms",
                len(target_indexes),
                len(counter),
                recount_ms,
                chips_ms,
                new_chips,
                reused_chips,
                len(self._all_tag_chip_cache),
                schedule_ms,
                (time.perf_counter() - perf_started) * 1000,
            )

    def _begin_deferred_filter_ui_update(self) -> int:
        """フィルタ操作中の中間描画を隠すため、TagPanelの更新を一時停止する。"""
        self._filter_ui_update_generation += 1
        generation = self._filter_ui_update_generation
        self.setUpdatesEnabled(False)
        return generation

    def _finish_deferred_filter_ui_update_later(self, generation: int) -> None:
        """
        FlowLayout再配置とスクロール位置復元がQTimer経由で終わるまで待ってから再描画を戻す。

        refresh_all_tags() -> FlowChipWrapArea._rebuild_flow() -> スクロール復元2段、
        という順に複数のsingleShot(0)が積まれるため、ここでも数イベント分だけ遅らせる。
        """

        def finish_if_current() -> None:
            if generation != self._filter_ui_update_generation:
                return
            self.setUpdatesEnabled(True)
            self.update()
            self.all_area.viewport().update()

        QTimer.singleShot(
            0,
            lambda: QTimer.singleShot(
                0,
                lambda: QTimer.singleShot(0, finish_if_current),
            ),
        )

    def _toggle_all_tag_selection(self, tag: str) -> None:
        # チェック状態は AllTagChip.tag_button が保持している。
        # タグ本体の選択は見た目の状態保持だけで、タグフィルタは変更しない。
        pass

    def _toggle_include_filter(self, tag: str) -> None:
        generation = self._begin_deferred_filter_ui_update()
        try:
            self._remember_all_scroll_anchor_for_clicked_tag(tag)
            row = self._all_tag_rows.get(tag)
            include_on = row.include_button.isChecked() if row is not None else False

            if include_on:
                self._include_filter_tags.add(tag)
                self._exclude_filter_tags.discard(tag)
                if tag in self._exclude_filter_order:
                    self._exclude_filter_order.remove(tag)
                if tag not in self._include_filter_order:
                    self._include_filter_order.append(tag)
                if tag in self._filter_condition_order:
                    self._filter_condition_order.remove(tag)
                self._filter_condition_order.append(tag)
            else:
                self._include_filter_tags.discard(tag)
                if tag in self._include_filter_order:
                    self._include_filter_order.remove(tag)
                if tag in self._filter_condition_order:
                    self._filter_condition_order.remove(tag)

            self._refresh_active_filter_panel()
            self._emit_tag_filter_changed()
        finally:
            self._finish_deferred_filter_ui_update_later(generation)

    def _toggle_exclude_filter(self, tag: str) -> None:
        generation = self._begin_deferred_filter_ui_update()
        try:
            self._remember_all_scroll_anchor_for_clicked_tag(tag)
            row = self._all_tag_rows.get(tag)
            exclude_on = row.exclude_button.isChecked() if row is not None else False

            if exclude_on:
                self._exclude_filter_tags.add(tag)
                self._include_filter_tags.discard(tag)
                if tag in self._include_filter_order:
                    self._include_filter_order.remove(tag)
                if tag not in self._exclude_filter_order:
                    self._exclude_filter_order.append(tag)
                if tag in self._filter_condition_order:
                    self._filter_condition_order.remove(tag)
                self._filter_condition_order.append(tag)
            else:
                self._exclude_filter_tags.discard(tag)
                if tag in self._exclude_filter_order:
                    self._exclude_filter_order.remove(tag)
                if tag in self._filter_condition_order:
                    self._filter_condition_order.remove(tag)

            self._refresh_active_filter_panel()
            self._emit_tag_filter_changed()
        finally:
            self._finish_deferred_filter_ui_update_later(generation)

    def current_tag_filters(self) -> tuple[set[str], set[str]]:
        return set(self._include_filter_tags), set(self._exclude_filter_tags)

    def set_tag_filter_view(
        self,
        active: bool,
        visible_indexes: list[int],
        visible_count: int,
        base_count: int,
    ) -> None:
        """
        サムネイル側のタグフィルタ結果を受け取り、
        右側タグ一覧と右側タグ操作対象を現在表示中画像へ追従させる。
        """
        perf_started = time.perf_counter()
        previous_active = self._filter_view_active
        previous_indexes = list(self._filter_visible_indexes)

        self._filter_view_active = bool(active)
        self._filter_visible_indexes = [
            int(index)
            for index in visible_indexes
            if isinstance(index, int) and 0 <= index < len(self.items)
        ]
        self._filter_visible_count = max(0, visible_count)
        self._filter_base_count = max(0, base_count)

        manage_started = time.perf_counter()
        self._refresh_active_filter_panel()
        manage_ms = (time.perf_counter() - manage_started) * 1000

        needs_refresh = (
            self._filter_view_active != previous_active
            or (
                self._filter_view_active
                and self._filter_visible_indexes != previous_indexes
            )
        )
        refresh_ms = 0.0
        if needs_refresh:
            refresh_started = time.perf_counter()
            self.refresh_all_tags()
            refresh_ms = (time.perf_counter() - refresh_started) * 1000

        if dev_log_enabled("PERF_FILTER"):
            log.info(
                "[PERF] tag_panel.set_tag_filter_view active=%s visible=%d base=%d "
                "conditions_panel=%.2fms refresh_needed=%s refresh_all_sync=%.2fms total=%.2fms",
                self._filter_view_active,
                self._filter_visible_count,
                self._filter_base_count,
                manage_ms,
                needs_refresh,
                refresh_ms,
                (time.perf_counter() - perf_started) * 1000,
            )

    def _remove_include_filter(self, tag: str) -> None:
        if tag not in self._include_filter_tags:
            return

        generation = self._begin_deferred_filter_ui_update()
        try:
            self._remember_filter_condition_anchor(tag)
            self._include_filter_tags.discard(tag)
            if tag in self._include_filter_order:
                self._include_filter_order.remove(tag)
            if tag in self._filter_condition_order:
                self._filter_condition_order.remove(tag)

            row = self._all_tag_rows.get(tag)
            if row is not None:
                row.set_filter_mode(None)

            self._refresh_active_filter_panel()
            self._emit_tag_filter_changed()
        finally:
            self._finish_deferred_filter_ui_update_later(generation)

    def _remove_exclude_filter(self, tag: str) -> None:
        if tag not in self._exclude_filter_tags:
            return

        generation = self._begin_deferred_filter_ui_update()
        try:
            self._remember_filter_condition_anchor(tag)
            self._exclude_filter_tags.discard(tag)
            if tag in self._exclude_filter_order:
                self._exclude_filter_order.remove(tag)
            if tag in self._filter_condition_order:
                self._filter_condition_order.remove(tag)

            row = self._all_tag_rows.get(tag)
            if row is not None:
                row.set_filter_mode(None)

            self._refresh_active_filter_panel()
            self._emit_tag_filter_changed()
        finally:
            self._finish_deferred_filter_ui_update_later(generation)

    def _refresh_active_filter_panel(self) -> None:
        active = bool(self._include_filter_tags or self._exclude_filter_tags)
        self.active_filter_panel.setVisible(active)

        if not active:
            self.include_filter_chip_area.set_chips([])
            self.exclude_filter_chip_area.set_chips([])
            self.include_filter_row.setVisible(False)
            self.exclude_filter_row.setVisible(False)
            return

        self.active_filter_count.setText(
            f"表示: {self._filter_visible_count} / {self._filter_base_count} 枚"
        )

        include_chips: list[QWidget] = []
        for tag in self._ordered_filter_tags("include"):
            chip = ActiveFilterChip(tag, "include")
            chip.remove_requested.connect(self._remove_include_filter)
            self._connect_tag_context_menu(chip)
            include_chips.append(chip)

        exclude_chips: list[QWidget] = []
        for tag in self._ordered_filter_tags("exclude"):
            chip = ActiveFilterChip(tag, "exclude")
            chip.remove_requested.connect(self._remove_exclude_filter)
            self._connect_tag_context_menu(chip)
            exclude_chips.append(chip)

        self.include_filter_row.setVisible(bool(include_chips))
        self.exclude_filter_row.setVisible(bool(exclude_chips))
        self.include_filter_chip_area.set_chips(include_chips)
        self.exclude_filter_chip_area.set_chips(exclude_chips)

    def clear_tag_filters(self) -> None:
        """右側タグ一覧で有効になっている include / exclude フィルタをすべて解除する。"""
        if not self._include_filter_tags and not self._exclude_filter_tags:
            return

        generation = self._begin_deferred_filter_ui_update()
        try:
            anchor_tag = self._last_active_filter_condition_tag()
            if anchor_tag is not None:
                self._remember_filter_condition_anchor(anchor_tag)
            else:
                self._remember_current_all_scroll_anchor()

            self._include_filter_tags.clear()
            self._exclude_filter_tags.clear()
            self._include_filter_order.clear()
            self._exclude_filter_order.clear()
            self._filter_condition_order.clear()

            # 現在表示されているタグチップの緑/赤状態を直ちにOFF表示へ戻す。
            for row in self._all_tag_rows.values():
                row.set_filter_mode(None)

            self._refresh_active_filter_panel()
            self._emit_tag_filter_changed()
        finally:
            self._finish_deferred_filter_ui_update_later(generation)

    def _emit_tag_filter_changed(self) -> None:
        started = time.perf_counter()
        self.tag_filter_changed.emit(set(self._include_filter_tags), set(self._exclude_filter_tags))
        if dev_log_enabled("PERF_FILTER"):
            log.info(
                "[PERF] tag_panel.emit_tag_filter_changed downstream_sync=%.2fms include=%d exclude=%d",
                (time.perf_counter() - started) * 1000,
                len(self._include_filter_tags),
                len(self._exclude_filter_tags),
            )

    # ---- 操作履歴・Undo ----

    def show_history_dialog(self) -> None:
        """操作履歴ダイアログを開く。"""
        dialog = OperationHistoryDialog(self._tag_history, self)
        dialog.undo_latest_requested.connect(self.undo_latest_tag_history_entry)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def _latest_undoable_history_entry(self) -> TagHistoryEntry | None:
        """まだ取り消されていない最新の通常タグ編集操作を返す。"""
        for entry in self._tag_history:
            if not entry.is_undo_entry and not entry.undone and not entry.invalidated:
                return entry
        return None

    def undo_latest_tag_history_entry(self) -> None:
        """まだ取り消されていない最新の通常タグ編集操作を取り消す。"""
        latest = self._latest_undoable_history_entry()
        if latest is None:
            QMessageBox.information(self, "操作履歴", "取り消せるタグ編集操作はありません。")
            return

        restored_count = 0
        missing_names: list[str] = []

        for image_path, previous_tags in latest.before_tags_by_path.items():
            item = self._item_for_image_path(image_path)
            if item is None:
                missing_names.append(image_path.name)
                continue
            if item.tags == previous_tags:
                continue
            item.tags = list(previous_tags)
            item.refresh_dirty()
            restored_count += 1

        latest.undone = True

        if restored_count <= 0:
            message_lines = [
                "取り消し対象のタグ変更はありませんでした。",
                "この操作は取り消し済みとして扱います。",
            ]
            if missing_names:
                message_lines.extend(["", "見つからなかった画像:"])
                message_lines.extend(f"- {name}" for name in missing_names[:20])
                if len(missing_names) > 20:
                    message_lines.append(f"...ほか {len(missing_names) - 20}件")

            QMessageBox.information(self, "操作履歴", "\n".join(message_lines))
            return

        self._refresh_after_change()

        message_lines = [
            "タグ編集操作を取り消しました。",
            f"対象画像：{restored_count}件",
        ]
        if missing_names:
            message_lines.extend(["", "見つからなかった画像:"])
            message_lines.extend(f"- {name}" for name in missing_names[:20])
            if len(missing_names) > 20:
                message_lines.append(f"...ほか {len(missing_names) - 20}件")

        QMessageBox.information(self, "操作履歴", "\n".join(message_lines))

    def _snapshot_tags_for_indexes(self, indexes: list[int]) -> dict[Path, list[str]]:
        """指定index群の現在タグ状態を、画像パス基準で保存する。"""
        snapshots: dict[Path, list[str]] = {}
        for index in indexes:
            if not (0 <= index < len(self.items)):
                continue
            item = self.items[index]
            snapshots[item.image_path] = list(item.tags)
        return snapshots

    def _snapshot_tags_for_paths(self, image_paths: list[Path]) -> dict[Path, list[str]]:
        """指定画像パス群の現在タグ状態を保存する。"""
        snapshots: dict[Path, list[str]] = {}
        for image_path in image_paths:
            item = self._item_for_image_path(image_path)
            if item is None:
                continue
            snapshots[item.image_path] = list(item.tags)
        return snapshots

    def _item_for_image_path(self, image_path: Path) -> ImageItem | None:
        for item in self.items:
            if item.image_path == image_path:
                return item
        return None

    def _tag_history_changes_from_snapshot(
        self,
        before_tags_by_path: dict[Path, list[str]],
    ) -> tuple[dict[Path, list[str]], dict[Path, list[str]], list[Path], list[Path]]:
        after_tags_by_path = self._snapshot_tags_for_paths(list(before_tags_by_path.keys()))
        return self._tag_history_changes_from_snapshots(before_tags_by_path, after_tags_by_path)

    def _tag_history_changes_from_snapshots(
        self,
        before_tags_by_path: dict[Path, list[str]],
        after_tags_by_path: dict[Path, list[str]],
    ) -> tuple[dict[Path, list[str]], dict[Path, list[str]], list[Path], list[Path]]:
        changed_before: dict[Path, list[str]] = {}
        changed_after: dict[Path, list[str]] = {}
        target_image_paths: list[Path] = []
        target_tag_paths: list[Path] = []

        for image_path, before_tags in before_tags_by_path.items():
            after_tags = after_tags_by_path.get(image_path)
            if after_tags is None or before_tags == after_tags:
                continue

            item = self._item_for_image_path(image_path)
            if item is None:
                continue

            changed_before[image_path] = list(before_tags)
            changed_after[image_path] = list(after_tags)
            target_image_paths.append(item.image_path)
            target_tag_paths.append(item.tag_path)

        return changed_before, changed_after, target_image_paths, target_tag_paths

    def _push_tag_history_entry(
        self,
        *,
        operation_type: str,
        summary: str,
        detail: str,
        target_image_paths: list[Path],
        target_tag_paths: list[Path],
        before_tags_by_path: dict[Path, list[str]],
        after_tags_by_path: dict[Path, list[str]],
        tag_counts_by_tag: dict[str, int] | None = None,
        is_undo_entry: bool = False,
    ) -> None:
        if not before_tags_by_path:
            return

        entry = TagHistoryEntry(
            operation_type=operation_type,
            summary=summary,
            detail=detail,
            target_image_paths=list(target_image_paths),
            target_tag_paths=list(target_tag_paths),
            before_tags_by_path={path: list(tags) for path, tags in before_tags_by_path.items()},
            after_tags_by_path={path: list(tags) for path, tags in after_tags_by_path.items()},
            tag_counts_by_tag=dict(tag_counts_by_tag or {}),
            is_undo_entry=is_undo_entry,
        )
        self._tag_history.insert(0, entry)
        if len(self._tag_history) > MAX_TAG_HISTORY_ENTRIES:
            del self._tag_history[MAX_TAG_HISTORY_ENTRIES:]


    def _record_tag_history_from_snapshot(
        self,
        *,
        operation_type: str,
        summary: str | None,
        detail: str,
        before_tags_by_path: dict[Path, list[str]],
        history_labels: list[str] | None = None,
        history_count_tags: list[str] | None = None,
        history_tag_counts: dict[str, int] | None = None,
    ) -> bool:
        changed_before, changed_after, target_image_paths, target_tag_paths = self._tag_history_changes_from_snapshot(
            before_tags_by_path
        )
        if not changed_before:
            return False

        counts_by_label: dict[str, int] = {}
        if history_tag_counts is not None:
            counts_by_label = dict(history_tag_counts)
        elif history_labels:
            counts_by_label = self._history_tag_counts_for_labels(
                changed_before,
                changed_after,
                history_labels,
                history_count_tags,
            )

        if history_labels:
            summary = self._build_history_summary(
                changed_count=len(changed_before),
                labels=history_labels,
                counts_by_label=counts_by_label,
            )

        self._push_tag_history_entry(
            operation_type=operation_type,
            summary=summary or f"{len(changed_before)}枚",
            detail=detail,
            target_image_paths=target_image_paths,
            target_tag_paths=target_tag_paths,
            before_tags_by_path=changed_before,
            after_tags_by_path=changed_after,
            tag_counts_by_tag=counts_by_label,
        )
        return True

    def record_tagger_apply_history(
        self,
        before_tags_by_path: dict[Path, list[str]],
        *,
        apply_mode: str,
    ) -> bool:
        """Tagger反映を通常のタグ操作履歴として記録する。"""
        mode_texts = {
            "empty_only": "タグが空の場合のみ",
            "overwrite": "上書き",
            "append": "既存txtの末尾に追加",
        }
        mode_text = mode_texts.get(apply_mode, "不明な反映方法")
        changed_before, changed_after, target_image_paths, target_tag_paths = (
            self._tag_history_changes_from_snapshot(before_tags_by_path)
        )
        if not changed_before:
            return False

        detail = "\n".join(
            [
                "Tagger候補を反映しました。",
                "",
                f"反映方法: {mode_text}",
                f"反映画像: {len(changed_before)}件",
            ]
        )
        self._push_tag_history_entry(
            operation_type="Tagger反映",
            summary=f"{len(changed_before)}枚 / {mode_text}",
            detail=detail,
            target_image_paths=target_image_paths,
            target_tag_paths=target_tag_paths,
            before_tags_by_path=changed_before,
            after_tags_by_path=changed_after,
        )
        return True

    def _history_tag_counts_for_labels(
        self,
        changed_before: dict[Path, list[str]],
        changed_after: dict[Path, list[str]],
        labels: list[str],
        count_tags: list[str] | None = None,
    ) -> dict[str, int]:
        """
        履歴表示用に、操作タグごとの対象画像数を数える。

        labels は表示名、count_tags は実際に before/after 内で探すタグ。
        タグ編集では label が "old→new" になり、count_tag は元タグになる。
        """
        unique_labels: list[str] = []
        unique_count_tags: list[str] = []

        raw_count_tags = count_tags or labels
        for label, count_tag in zip(labels, raw_count_tags):
            if label in unique_labels:
                continue
            unique_labels.append(label)
            unique_count_tags.append(count_tag)

        counts: dict[str, int] = {}
        for label, count_tag in zip(unique_labels, unique_count_tags):
            count = 0
            for image_path, before_tags in changed_before.items():
                after_tags = changed_after.get(image_path, [])
                if before_tags == after_tags:
                    continue
                if count_tag in before_tags or count_tag in after_tags:
                    count += 1
            counts[label] = count

        return counts

    def _build_history_summary(
        self,
        *,
        changed_count: int,
        labels: list[str],
        counts_by_label: dict[str, int],
    ) -> str:
        unique_labels = self._unique_tags(labels)

        if not unique_labels:
            return f"{changed_count}枚"

        if len(unique_labels) == 1:
            label = unique_labels[0]
            count = counts_by_label.get(label, changed_count)
            return f"{count}枚 / {label}"

        count_values = [counts_by_label.get(label, changed_count) for label in unique_labels]
        if count_values and len(set(count_values)) == 1:
            return f"{count_values[0]}枚 / {len(unique_labels)}タグ"

        return f"対象{changed_count}枚 / {len(unique_labels)}タグ"

    def _append_history_tag_counts(self, detail: str, counts_by_label: dict[str, int]) -> str:
        if not counts_by_label:
            return detail

        lines = [detail, "", "タグ別内訳:"]
        for label, count in counts_by_label.items():
            lines.append(f"- {label}: {count}枚")
        return "\n".join(lines)

    def invalidate_history_for_tag_paths(
        self,
        tag_paths: list[Path] | set[Path],
        *,
        reason: str = "外部再読み込みにより無効",
    ) -> None:
        """外部再読み込みされたtxtを含む履歴を取り消し不可にする。"""
        target_paths = {Path(path) for path in tag_paths}
        if not target_paths:
            return

        for entry in self._tag_history:
            if entry.invalidated:
                continue
            if target_paths.intersection(entry.target_tag_paths):
                entry.invalidated = True
                entry.invalidated_reason = reason


    def _format_tags_for_history(self, tags: list[str], *, limit: int = 3) -> str:
        unique_tags = self._unique_tags(tags)
        if not unique_tags:
            return "タグなし"

        visible = unique_tags[:limit]
        text = ", ".join(f'"{tag}"' for tag in visible)
        hidden = len(unique_tags) - len(visible)
        if hidden > 0:
            text += f" ほか{hidden}件"
        return text

    def _format_replacements_for_history(self, replacements: dict[str, str | None], *, limit: int = 3) -> str:
        parts: list[str] = []
        for source_tag, replacement_tag in replacements.items():
            if replacement_tag == source_tag:
                continue
            if replacement_tag is None:
                parts.append(f'"{source_tag}"→削除')
            else:
                parts.append(f'"{source_tag}"→"{replacement_tag}"')

        if not parts:
            return "変更なし"

        visible = parts[:limit]
        text = ", ".join(visible)
        hidden = len(parts) - len(visible)
        if hidden > 0:
            text += f" ほか{hidden}件"
        return text

    def _replacement_labels_for_history(self, replacements: dict[str, str | None]) -> list[str]:
        labels: list[str] = []
        for source_tag, replacement_tag in replacements.items():
            if replacement_tag == source_tag:
                continue
            if replacement_tag is None:
                labels.append(f'"{source_tag}"→削除')
            else:
                labels.append(f'"{source_tag}"→"{replacement_tag}"')
        return labels

    def _replacement_count_tags_for_history(self, replacements: dict[str, str | None]) -> list[str]:
        count_tags: list[str] = []
        for source_tag, replacement_tag in replacements.items():
            if replacement_tag == source_tag:
                continue
            count_tags.append(source_tag)
        return count_tags


    def _add_tags_history_detail(self, tags: list[str], front_tag: str | None) -> str:
        lines = ["タグ追加を実行しました。", "", "追加タグ:"]
        for tag in self._unique_tags(tags):
            prefix = "- "
            suffix = ""
            if tag == front_tag:
                suffix = "（先頭配置）"
            lines.append(f"{prefix}{tag}{suffix}")
        if front_tag:
            lines.extend(["", f"先頭配置タグ: {front_tag}"])
        return "\n".join(lines)

    def _delete_tags_history_detail(self, tags: list[str]) -> str:
        lines = ["タグ削除を実行しました。", "", "削除タグ:"]
        lines.extend(f"- {tag}" for tag in self._unique_tags(tags))
        return "\n".join(lines)

    def _edit_tags_history_detail(self, replacements: dict[str, str | None]) -> str:
        lines = ["タグ編集を実行しました。", "", "変更内容:"]
        for source_tag, replacement_tag in replacements.items():
            if replacement_tag == source_tag:
                continue
            if replacement_tag is None:
                lines.append(f"- {source_tag} → 削除")
            else:
                lines.append(f"- {source_tag} → {replacement_tag}")
        return "\n".join(lines)

    def _dedupe_history_summary_and_detail(
        self,
        changed_before: dict[Path, list[str]],
        changed_after: dict[Path, list[str]],
    ) -> tuple[str, str]:
        removed_counter: Counter[str] = Counter()
        removed_total = 0

        for image_path, before_tags in changed_before.items():
            after_tags = changed_after.get(image_path, [])
            after_counter = Counter(after_tags)
            for tag in before_tags:
                if after_counter[tag] > 0:
                    after_counter[tag] -= 1
                    continue
                removed_counter[tag] += 1
                removed_total += 1

        summary = f"{len(changed_before)}枚 / {removed_total}個の重複タグを削除"
        lines = ["重複タグ削除を実行しました。", "", f"削除タグ数: {removed_total}個", "", "削除タグ:"]
        for tag, count in self._sorted_counter_items(removed_counter)[:50]:
            lines.append(f"- {tag}: {count}個")
        if len(removed_counter) > 50:
            lines.append(f"...ほか {len(removed_counter) - 50}種類")
        return summary, "\n".join(lines)

    def _selected_current_tags(self) -> list[str]:
        return [tag for tag, button in self._current_tag_buttons.items() if button.isChecked()]

    def _selected_all_tags(self) -> list[str]:
        return [tag for tag, row in self._all_tag_rows.items() if row.tag_button.isChecked()]

    def _ask_add_tags(self, title: str) -> tuple[list[str], str | None]:
        dialog = AddTagDialog(
            title,
            self,
            tag_dictionary=self._danbooru_tag_dictionary,
            tag_color_dictionary=self._tag_color_dictionary,
            tag_color_enabled=self._tag_color_enabled,
            tag_completion_enabled=self._tag_completion_enabled,
            tag_completion_limit=self._tag_completion_limit,
        )
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return [], None

            tags = self._unique_tags(dialog.tags())
            front_tag = dialog.front_tag()

            if front_tag not in tags:
                front_tag = None

            return tags, front_tag
        finally:
            dialog.deleteLater()

    def _confirm_global_action(self, message: str) -> bool:
        result = QMessageBox.question(
            self,
            "全txt対象の操作",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return result == QMessageBox.StandardButton.Yes

    def _format_tags_for_confirmation(self, tags: list[str], *, line_width: int = 72) -> str:
        """確認ダイアログ用に、タグ一覧を省略せず折り返して表示する。"""
        unique_tags = self._unique_tags(tags)
        if not unique_tags:
            return "タグなし"

        lines: list[str] = []
        current_line = ""

        for index, tag in enumerate(unique_tags):
            token = tag + ("," if index < len(unique_tags) - 1 else "")

            if not current_line:
                current_line = token
                continue

            candidate = f"{current_line} {token}"
            if len(candidate) > line_width:
                lines.append(current_line)
                current_line = token
            else:
                current_line = candidate

        if current_line:
            lines.append(current_line)

        return "\n".join(lines)

    # ---- 左パネル：選択中画像のタグ ----

    def add_tags_to_current_targets(self) -> None:
        tags, front_tag = self._ask_add_tags("選択中画像のタグ：タグ追加")
        if not tags:
            return

        indexes = self._target_indexes()
        if not indexes:
            return

        before_history = self._snapshot_tags_for_indexes(indexes)

        added_counter: Counter[str] = Counter()
        skipped_counter: Counter[str] = Counter()
        moved_counter: Counter[str] = Counter()
        already_front_counter: Counter[str] = Counter()

        for index in indexes:
            item = self.items[index]
            self._add_tags_to_item(
                item,
                tags,
                front_tag,
                added_counter,
                skipped_counter,
                moved_counter,
                already_front_counter,
            )

        if added_counter or moved_counter:
            changed = self._record_tag_history_from_snapshot(
                operation_type="タグ追加",
                summary=None,
                detail=self._add_tags_history_detail(tags, front_tag),
                before_tags_by_path=before_history,
                history_labels=[f'"{tag}"' for tag in self._unique_tags(tags)],
                history_count_tags=self._unique_tags(tags),
            )
            if changed:
                self._refresh_after_change()

        self._show_add_tags_result_if_needed(
            added_counter,
            skipped_counter,
            moved_counter,
            already_front_counter,
        )

    def delete_current_selected_tags(self) -> None:
        selected_tags = self._selected_current_tags()
        if not selected_tags:
            QMessageBox.information(self, "タグ削除", "削除するタグを選択してください。")
            return
        indexes = self._target_indexes()
        if not indexes:
            return

        before_history = self._snapshot_tags_for_indexes(indexes)
        for index in indexes:
            item = self.items[index]
            before = list(item.tags)
            item.tags = [tag for tag in item.tags if tag not in selected_tags]
            if item.tags != before:
                item.refresh_dirty()

        changed_count = sum(
            1
            for path, before_tags in before_history.items()
            if self._item_for_image_path(path) is not None and self._item_for_image_path(path).tags != before_tags
        )
        if self._record_tag_history_from_snapshot(
            operation_type="タグ削除",
            summary=None,
            detail=self._delete_tags_history_detail(selected_tags),
            before_tags_by_path=before_history,
            history_labels=[f'"{tag}"' for tag in self._unique_tags(selected_tags)],
            history_count_tags=self._unique_tags(selected_tags),
        ):
            self._refresh_after_change()

    def edit_current_selected_tags(self) -> None:
        selected_tags = self._selected_current_tags()
        if not selected_tags:
            QMessageBox.information(self, "タグ編集", "編集するタグを選択してください。")
            return

        indexes = self._target_indexes()
        if not indexes:
            return

        checked_count = len([item for item in self.items if item.checked])
        title = (
            f"チェック中画像 {checked_count}枚 のタグ：タグ編集"
            if checked_count > 0
            else "選択中画像のタグ：タグ編集"
        )

        replacements = self._ask_edit_tags(title, selected_tags, indexes)
        if not replacements:
            return

        before_history = self._snapshot_tags_for_indexes(indexes)
        self._replace_tags_in_indexes(indexes, replacements)
        changed_count = sum(
            1
            for path, before_tags in before_history.items()
            if self._item_for_image_path(path) is not None and self._item_for_image_path(path).tags != before_tags
        )
        if self._record_tag_history_from_snapshot(
            operation_type="タグ編集",
            summary=None,
            detail=self._edit_tags_history_detail(replacements),
            before_tags_by_path=before_history,
            history_labels=self._replacement_labels_for_history(replacements),
            history_count_tags=self._replacement_count_tags_for_history(replacements),
        ):
            self._refresh_after_change()

    def move_current_selected_tag_to_front(self) -> None:
        selected_tags = self._selected_current_tags()

        if not selected_tags:
            QMessageBox.information(self, "先頭へ", "先頭へ移動するタグを1つ選択してください。")
            return

        if len(selected_tags) > 1:
            QMessageBox.information(self, "先頭へ", "先頭へ移動できるタグは1つだけです。\n1つだけ選択してください。")
            return

        tag = selected_tags[0]
        indexes = self._target_indexes()

        if not indexes:
            return

        checked_count = len([item for item in self.items if item.checked])

        if checked_count > 0:
            missing_count = 0

            for index in indexes:
                if not (0 <= index < len(self.items)):
                    continue
                if tag not in self.items[index].tags:
                    missing_count += 1

            if missing_count > 0:
                existing_count = len(indexes) - missing_count
                QMessageBox.information(
                    self,
                    "先頭へ",
                    "選択タグはチェック中画像すべてに存在しないため、先頭へ移動できません。\n\n"
                    f"タグ：{tag}\n"
                    f"チェック中画像：{len(indexes)}枚\n"
                    f"存在する画像：{existing_count}枚\n"
                    f"存在しない画像：{missing_count}枚",
                )
                return

        before_history = self._snapshot_tags_for_indexes(indexes)
        moved_count = 0
        already_front_count = 0
        missing_count = 0

        for index in indexes:
            if not (0 <= index < len(self.items)):
                continue

            result = self._move_tag_to_front_in_item(self.items[index], tag)

            if result == "moved":
                moved_count += 1
            elif result == "already_front":
                already_front_count += 1
            elif result == "missing":
                missing_count += 1

        if moved_count > 0:
            if self._record_tag_history_from_snapshot(
                operation_type="先頭へ",
                summary=None,
                detail=f"選択タグをtxt先頭へ移動しました。\n\nタグ: {tag}",
                before_tags_by_path=before_history,
                history_labels=[f'"{tag}"'],
                history_count_tags=[tag],
            ):
                self._refresh_after_change()

            lines = [
                "選択タグをtxt先頭へ移動しました。",
                "",
                f"タグ：{tag}",
                f"先頭へ移動：{moved_count}件",
            ]

            if already_front_count > 0:
                lines.append(f"既に先頭：{already_front_count}件")

            QMessageBox.information(self, "先頭へ", "\n".join(lines))
            return

        if already_front_count > 0:
            QMessageBox.information(
                self,
                "先頭へ",
                f"選択タグは既に先頭です。\n\nタグ：{tag}",
            )
            return

        if missing_count > 0:
            QMessageBox.information(
                self,
                "先頭へ",
                f"選択タグが対象画像に存在しないため、先頭へ移動できません。\n\nタグ：{tag}",
            )

    def dedupe_current_targets(self) -> None:
        indexes = self._target_indexes()
        if not indexes:
            return

        if not self._confirm_dedupe(indexes):
            return

        before_history = self._snapshot_tags_for_indexes(indexes)
        self._dedupe_indexes(indexes)
        changed_before, changed_after, target_image_paths, target_tag_paths = self._tag_history_changes_from_snapshot(before_history)
        if changed_before:
            summary, detail = self._dedupe_history_summary_and_detail(changed_before, changed_after)
            self._push_tag_history_entry(
                operation_type="重複削除",
                summary=summary,
                detail=detail,
                target_image_paths=target_image_paths,
                target_tag_paths=target_tag_paths,
                before_tags_by_path=changed_before,
                after_tags_by_path=changed_after,
            )
            self._refresh_after_change()

    # ---- 右パネル：全てのタグ一覧 ----

    def add_tags_to_all_items(self) -> None:
        indexes = self._all_target_indexes_or_message()
        if not indexes:
            return

        target_label = self._all_target_label()
        tags, front_tag = self._ask_add_tags(f"{target_label}のタグ一覧：タグ追加")
        if not tags:
            return

        if self._filter_view_active:
            message = f"入力したタグを、現在表示中の画像 {len(indexes)}枚 のtxtに追加します。"
        elif self._all_tags_scope == "work_set":
            message = f"入力したタグを、作業セット {len(indexes)}枚 のtxtに追加します。"
        else:
            message = "入力したタグを、開いているフォルダ内の全txtに追加します。"

        if front_tag:
            message += f"\n\n先頭配置：\n{front_tag} を対象txtの先頭へ配置します。"

        message += "\n\nよろしいですか？"

        if not self._confirm_global_action(message):
            return

        before_history = self._snapshot_tags_for_indexes(indexes)

        added_counter: Counter[str] = Counter()
        skipped_counter: Counter[str] = Counter()
        moved_counter: Counter[str] = Counter()
        already_front_counter: Counter[str] = Counter()

        for index in indexes:
            self._add_tags_to_item(
                self.items[index],
                tags,
                front_tag,
                added_counter,
                skipped_counter,
                moved_counter,
                already_front_counter,
            )

        if added_counter or moved_counter:
            changed = self._record_tag_history_from_snapshot(
                operation_type="タグ追加",
                summary=None,
                detail=self._add_tags_history_detail(tags, front_tag),
                before_tags_by_path=before_history,
                history_labels=[f'"{tag}"' for tag in self._unique_tags(tags)],
                history_count_tags=self._unique_tags(tags),
            )
            if changed:
                self._refresh_after_change()

        self._show_add_tags_result_if_needed(
            added_counter,
            skipped_counter,
            moved_counter,
            already_front_counter,
        )

    def delete_all_selected_tags(self) -> None:
        selected_tags = self._selected_all_tags()
        if not selected_tags:
            QMessageBox.information(self, "タグ削除", "削除するタグを選択してください。")
            return

        indexes = self._all_target_indexes_or_message()
        if not indexes:
            return

        if self._filter_view_active:
            message = f"選択したタグを、現在表示中の画像 {len(indexes)}枚 のtxtから削除します。"
        elif self._all_tags_scope == "work_set":
            message = f"選択したタグを、作業セット {len(indexes)}枚 のtxtから削除します。"
        else:
            message = "選択したタグを、開いているフォルダ内の全txtから削除します。"

        message += (
            "\n\n"
            "削除対象タグ:\n"
            f"{self._format_tags_for_confirmation(selected_tags)}"
            "\n\nよろしいですか？"
        )

        if not self._confirm_global_action(message):
            return

        before_history = self._snapshot_tags_for_indexes(indexes)
        for index in indexes:
            item = self.items[index]
            before = list(item.tags)
            item.tags = [tag for tag in item.tags if tag not in selected_tags]
            if item.tags != before:
                item.refresh_dirty()

        changed_count = sum(
            1
            for path, before_tags in before_history.items()
            if self._item_for_image_path(path) is not None and self._item_for_image_path(path).tags != before_tags
        )
        if self._record_tag_history_from_snapshot(
            operation_type="タグ削除",
            summary=None,
            detail=self._delete_tags_history_detail(selected_tags),
            before_tags_by_path=before_history,
            history_labels=[f'"{tag}"' for tag in self._unique_tags(selected_tags)],
            history_count_tags=self._unique_tags(selected_tags),
        ):
            self._refresh_after_change()

    def edit_all_selected_tags(self) -> None:
        selected_tags = self._selected_all_tags()
        if not selected_tags:
            QMessageBox.information(self, "タグ編集", "編集するタグを選択してください。")
            return

        indexes = self._all_target_indexes_or_message()
        if not indexes:
            return

        target_label = self._all_target_label()
        replacements = self._ask_edit_tags(f"{target_label}のタグ一覧：タグ編集", selected_tags, indexes)
        if not replacements:
            return

        change_lines: list[str] = []
        for source_tag, replacement_tag in replacements.items():
            if replacement_tag == source_tag:
                continue
            if replacement_tag is None:
                change_lines.append(f"- {source_tag} → 削除")
            else:
                change_lines.append(f"- {source_tag} → {replacement_tag}")

        if not change_lines:
            return

        affected_count = self._count_items_containing_tags(indexes, selected_tags)

        if self._filter_view_active:
            action_message = f"選択したタグを、現在表示中の画像 {len(indexes)}枚 のtxtで一括置換します。"
        elif self._all_tags_scope == "work_set":
            action_message = f"選択したタグを、作業セット {len(indexes)}枚 のtxtで一括置換します。"
        else:
            action_message = "選択したタグを、開いているフォルダ内の全txtで一括置換します。"

        if not self._confirm_global_action(
            action_message + "\n"
            f"対象txt：{affected_count}件\n"
            "\n"
            "変更内容：\n"
            + "\n".join(change_lines)
            + "\n\nよろしいですか？"
        ):
            return

        before_history = self._snapshot_tags_for_indexes(indexes)
        self._replace_tags_in_indexes(indexes, replacements)
        changed_count = sum(
            1
            for path, before_tags in before_history.items()
            if self._item_for_image_path(path) is not None and self._item_for_image_path(path).tags != before_tags
        )
        if self._record_tag_history_from_snapshot(
            operation_type="タグ編集",
            summary=None,
            detail=self._edit_tags_history_detail(replacements),
            before_tags_by_path=before_history,
            history_labels=self._replacement_labels_for_history(replacements),
            history_count_tags=self._replacement_count_tags_for_history(replacements),
        ):
            self._refresh_after_change()

    def dedupe_all_items(self) -> None:
        indexes = self._all_target_indexes_or_message()
        if not indexes:
            return

        if not self._confirm_dedupe(indexes):
            return

        before_history = self._snapshot_tags_for_indexes(indexes)
        self._dedupe_indexes(indexes)
        changed_before, changed_after, target_image_paths, target_tag_paths = self._tag_history_changes_from_snapshot(before_history)
        if changed_before:
            summary, detail = self._dedupe_history_summary_and_detail(changed_before, changed_after)
            self._push_tag_history_entry(
                operation_type="重複削除",
                summary=summary,
                detail=detail,
                target_image_paths=target_image_paths,
                target_tag_paths=target_tag_paths,
                before_tags_by_path=changed_before,
                after_tags_by_path=changed_after,
            )
            self._refresh_after_change()

    # ---- 共通処理 ----

    def _unique_tags(self, tags: list[str]) -> list[str]:
        unique_tags: list[str] = []
        seen: set[str] = set()

        for tag in tags:
            if tag in seen:
                continue
            unique_tags.append(tag)
            seen.add(tag)

        return unique_tags

    def _add_tags_to_item(
        self,
        item: ImageItem,
        tags: list[str],
        front_tag: str | None,
        added_counter: Counter[str],
        skipped_counter: Counter[str],
        moved_counter: Counter[str],
        already_front_counter: Counter[str],
    ) -> None:
        before = list(item.tags)

        if front_tag:
            front_tag_exists = front_tag in item.tags

            item.tags = [tag for tag in item.tags if tag != front_tag]
            item.tags.insert(0, front_tag)

            if front_tag_exists:
                skipped_counter[front_tag] += 1

                if item.tags != before:
                    moved_counter[front_tag] += 1
                else:
                    already_front_counter[front_tag] += 1
            else:
                added_counter[front_tag] += 1

        for tag in tags:
            if tag == front_tag:
                continue

            if tag in item.tags:
                skipped_counter[tag] += 1
                continue

            item.tags.append(tag)
            added_counter[tag] += 1

        if item.tags != before:
            item.refresh_dirty()

    def _move_tag_to_front_in_item(self, item: ImageItem, tag: str) -> str:
        try:
            tag_index = item.tags.index(tag)
        except ValueError:
            return "missing"

        if tag_index == 0:
            return "already_front"

        item.tags.pop(tag_index)
        item.tags.insert(0, tag)
        item.refresh_dirty()
        return "moved"

    def _show_add_tags_result_if_needed(
        self,
        added_counter: Counter[str],
        skipped_counter: Counter[str],
        moved_counter: Counter[str] | None = None,
        already_front_counter: Counter[str] | None = None,
    ) -> None:
        moved_counter = moved_counter or Counter()
        already_front_counter = already_front_counter or Counter()

        if not added_counter and not skipped_counter and not moved_counter and not already_front_counter:
            return

        added_total = sum(added_counter.values())
        skipped_total = sum(skipped_counter.values())
        moved_total = sum(moved_counter.values())
        already_front_total = sum(already_front_counter.values())

        lines: list[str] = []

        if added_total > 0 and moved_total > 0:
            lines.append("タグ追加・先頭配置を実行しました。")
        elif added_total > 0:
            lines.append("タグを追加しました。")
        elif moved_total > 0:
            lines.append("既存タグを先頭配置しました。")
        elif already_front_total > 0:
            lines.append("指定タグは既に先頭に配置されていたため、先頭配置処理をスキップしました。")
        elif skipped_total > 0:
            lines.append("入力されたタグはすべて既に存在していたため、追加されませんでした。")

        lines.extend(["", "追加結果", ""])

        if added_total > 0:
            added_label = "追加（先頭配置）" if moved_total > 0 or already_front_total > 0 else "追加"
            lines.append(f"{added_label}：{added_total}件")
            for tag, count in self._sorted_counter_items(added_counter):
                lines.append(f"- {tag}：{count}件")
            lines.append("")

        if moved_total > 0:
            lines.append(f"既存タグを先頭配置：{moved_total}件")
            for tag, count in self._sorted_counter_items(moved_counter):
                lines.append(f"- {tag}：{count}件")
            lines.append("")

        if already_front_total > 0:
            lines.append(f"既に先頭のため処理スキップ：{already_front_total}件")
            for tag, count in self._sorted_counter_items(already_front_counter):
                lines.append(f"- {tag}：{count}件")
            lines.append("")

        if skipped_total > 0:
            lines.append(f"既存のため追加スキップ：{skipped_total}件")
            for tag, count in self._sorted_counter_items(skipped_counter):
                lines.append(f"- {tag}：{count}件")

        QMessageBox.information(self, "タグ追加", "\n".join(lines).rstrip())

    def _sorted_counter_items(self, counter: Counter[str]) -> list[tuple[str, int]]:
        return sorted(counter.items(), key=lambda pair: (-pair[1], pair[0].lower()))

    def _ask_edit_tags(
        self,
        title: str,
        selected_tags: list[str],
        indexes: list[int],
    ) -> dict[str, str | None]:
        tag_counts, target_count = self._tag_presence_counts(indexes, selected_tags)
        dialog = EditTagDialog(
            title,
            selected_tags,
            tag_counts,
            target_count,
            self,
            tag_dictionary=self._danbooru_tag_dictionary,
            tag_color_dictionary=self._tag_color_dictionary,
            tag_color_enabled=self._tag_color_enabled,
            tag_completion_enabled=self._tag_completion_enabled,
            tag_completion_limit=self._tag_completion_limit,
        )
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return {}
            return dialog.replacements()
        finally:
            dialog.deleteLater()

    def _tag_presence_counts(self, indexes: list[int], selected_tags: list[str]) -> tuple[Counter[str], int]:
        counts: Counter[str] = Counter()
        selected = set(selected_tags)
        valid_count = 0

        for index in indexes:
            if not (0 <= index < len(self.items)):
                continue

            valid_count += 1
            item_tags = set(self.items[index].tags)

            for tag in selected:
                if tag in item_tags:
                    counts[tag] += 1

        return counts, valid_count

    def _count_items_containing_tags(self, indexes: list[int], selected_tags: list[str]) -> int:
        count = 0
        selected = set(selected_tags)

        for index in indexes:
            if not (0 <= index < len(self.items)):
                continue
            if any(tag in selected for tag in self.items[index].tags):
                count += 1

        return count

    def _replace_tags_in_indexes(
        self,
        indexes: list[int],
        replacements: dict[str, str | None],
    ) -> None:
        if not indexes or not replacements:
            return

        effective_replacements = {
            source_tag: replacement_tag
            for source_tag, replacement_tag in replacements.items()
            if replacement_tag != source_tag
        }

        if not effective_replacements:
            return

        selected = set(effective_replacements.keys())

        for index in indexes:
            if not (0 <= index < len(self.items)):
                continue

            item = self.items[index]

            # この画像に対象タグがなければ何もしない。
            # 複数チェック時に、一部画像だけのタグを他画像へ追加しないため。
            if not any(tag in selected for tag in item.tags):
                continue

            next_tags: list[str] = []

            for tag in item.tags:
                if tag in effective_replacements:
                    replacement_tag = effective_replacements[tag]

                    # 空欄指定は削除扱い。
                    if replacement_tag is None:
                        continue

                    if replacement_tag not in next_tags:
                        next_tags.append(replacement_tag)

                    continue

                if tag not in next_tags:
                    next_tags.append(tag)

            if next_tags != item.tags:
                item.tags = next_tags
                item.refresh_dirty()

    def _find_duplicate_summary(self, indexes: list[int]) -> tuple[Counter[str], int, int]:
        """
        各txt内の重複タグを調べる。

        戻り値:
            duplicate_counter:
                削除予定の重複タグと削除個数。
                例: 同一txt内に 1girl が3回ある場合、1個残して2個削除予定として数える。
            duplicate_file_count:
                重複タグが見つかったtxt数。
            remove_count:
                削除予定のタグ総数。
        """
        duplicate_counter: Counter[str] = Counter()
        duplicate_file_count = 0
        remove_count = 0

        for index in indexes:
            if not (0 <= index < len(self.items)):
                continue

            item = self.items[index]
            per_file_counter = Counter(item.tags)
            file_has_duplicate = False

            for tag, count in per_file_counter.items():
                if count <= 1:
                    continue

                duplicate_amount = count - 1
                duplicate_counter[tag] += duplicate_amount
                remove_count += duplicate_amount
                file_has_duplicate = True

            if file_has_duplicate:
                duplicate_file_count += 1

        return duplicate_counter, duplicate_file_count, remove_count

    def _confirm_dedupe(self, indexes: list[int]) -> bool:
        duplicate_counter, duplicate_file_count, remove_count = self._find_duplicate_summary(indexes)

        if remove_count <= 0:
            QMessageBox.information(self, "重複削除", "重複タグはありません。")
            return False

        target_count = len([index for index in indexes if 0 <= index < len(self.items)])

        lines = [
            "以下の重複タグを削除します。",
            "",
            f"対象：{target_count}件",
            f"重複あり：{duplicate_file_count}件",
            f"削除予定：{remove_count}個",
            "",
            "重複タグ：",
        ]

        sorted_duplicates = sorted(
            duplicate_counter.items(),
            key=lambda pair: (-pair[1], pair[0].lower()),
        )

        max_visible = 30
        for tag, count in sorted_duplicates[:max_visible]:
            lines.append(f"- {tag}：{count}個削除")

        hidden_count = len(sorted_duplicates) - max_visible
        if hidden_count > 0:
            lines.append(f"...ほか {hidden_count}種類")

        lines.extend(["", "よろしいですか？"])

        result = QMessageBox.question(
            self,
            "重複タグの確認",
            "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return result == QMessageBox.StandardButton.Yes

    def _dedupe_indexes(self, indexes: list[int]) -> None:
        for index in indexes:
            if not (0 <= index < len(self.items)):
                continue

            item = self.items[index]
            seen: set[str] = set()
            deduped: list[str] = []

            # 最初に出現したタグを残し、2回目以降の同名タグを削除する。
            # 削除対象以外のタグ順は維持する。
            for tag in item.tags:
                if tag not in seen:
                    seen.add(tag)
                    deduped.append(tag)

            if deduped != item.tags:
                item.tags = deduped
                item.refresh_dirty()

    def _refresh_after_change(self) -> None:
        self.refresh_current_tags(reason="tag_change")
        self.refresh_all_tags()
        self.refresh_target_label()
        self.tags_changed.emit()
