from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCursor

from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from danbooru_tag_dictionary import DanbooruTagDictionary, TagCompletionCandidate
from tag_color_dictionary import DEFAULT_UNKNOWN_CATEGORY, TagColorDictionary


def format_post_count(post_count: int) -> str:
    """候補一覧向けに投稿数をコンパクト表示する。"""
    count = max(0, int(post_count))

    if count < 1_000:
        return str(count)

    if count < 1_000_000:
        value = count / 1_000
        if value < 10:
            return f"{value:.1f}".rstrip("0").rstrip(".") + "K"
        return f"{value:.0f}K"

    value = count / 1_000_000
    if value < 10:
        return f"{value:.1f}".rstrip("0").rstrip(".") + "M"
    return f"{value:.0f}M"


# ポップアップが候補1件だけの時に極端に細くなりすぎないための下限。
# 入力欄幅には追従させず、候補UIとして読みやすい独立した幅にする。
MIN_COMPLETION_LIST_WIDTH = 280
# 候補数を増やしてもポップアップが画面を埋めないよう、同時表示は最大12行にする。
MAX_VISIBLE_COMPLETION_ROWS = 12
_QT_MAX_WIDGET_SIZE = 16_777_215


class _CompletionRow(QWidget):
    def __init__(
        self,
        candidate: TagCompletionCandidate,
        *,
        tag_color_dictionary: TagColorDictionary | None,
        tag_color_enabled: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # QListWidget側で hover 行を追えるよう、行Widget自身と子Labelは
        # マウスイベントを受け取らず、イベントをviewportへ通す。
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.candidate = candidate

        # alias経由で見つかった候補は、別名と変換先を主表示へまとめる。
        # 例: "221galahad  →  jyuru"
        # 正規タグ名で見つかった候補は従来どおり正規タグ名だけを表示する。
        display_text = (
            f"{candidate.matched_alias}  →  {candidate.tag.name}"
            if candidate.matched_alias is not None
            else candidate.tag.name
        )
        category_style = None
        if tag_color_enabled and tag_color_dictionary is not None:
            category_style = tag_color_dictionary.style_for_tag(candidate.tag.name)

        # 補完候補のタグ名だけは、既存タグチップの枠線色へ揃える。
        # alias候補でも表示全体を「変換先の正規タグ」のカテゴリ色で表示する。
        tag_text_color = "#edf6ff"
        if (
            category_style is not None
            and category_style.category_id != DEFAULT_UNKNOWN_CATEGORY
        ):
            tag_text_color = category_style.border_color

        tag_label = QLabel(display_text)
        tag_label.setStyleSheet(
            f"font-weight: bold; color: {tag_text_color};"
        )
        tag_label.setWordWrap(False)
        tag_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        count_text = format_post_count(candidate.tag.post_count)
        count_label = QLabel(count_text)
        count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        count_label.setStyleSheet("color: #b8c6d8;")
        count_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        # スタイルシート由来の太字フォントでは、fontMetrics() がスタイル適用前の
        # 幅を返すことがある。実際のsizeHint()と比較して大きい方を使い、
        # 長い alias → 正規タグ が途中で切れないようにする。
        tag_label.ensurePolished()
        count_label.ensurePolished()
        tag_text_width = max(
            tag_label.fontMetrics().horizontalAdvance(display_text) + 4,
            tag_label.sizeHint().width(),
        )
        count_text_width = max(
            count_label.fontMetrics().horizontalAdvance(count_text) + 4,
            count_label.sizeHint().width(),
        )
        tag_label.setFixedWidth(max(1, tag_text_width))
        count_label.setFixedWidth(max(1, count_text_width))

        # 最終レイアウト後の幅再調整で使う。
        self._tag_label = tag_label
        self._count_label = count_label

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(12)
        top_row.addWidget(tag_label)
        top_row.addStretch(1)
        top_row.addWidget(count_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 6, 9, 6)
        layout.setSpacing(0)
        layout.addLayout(top_row)

        # popup幅はQListWidgetの縮小後sizeHintではなく、実際の文字幅から求める。
        # QListWidget::item の左右マージン分も余裕として加える。
        self._top_row = top_row
        self._row_layout = layout
        self._natural_width = (
            tag_text_width
            + count_text_width
            + top_row.spacing()
            + layout.contentsMargins().left()
            + layout.contentsMargins().right()
            + 8
        )
        self.setMinimumWidth(self._natural_width)

        if category_style is not None and category_style.category_id != DEFAULT_UNKNOWN_CATEGORY:
            self.setStyleSheet(
                f"""
                _CompletionRow {{
                    background: {category_style.color};
                    border: 1px solid {category_style.border_color};
                    border-radius: 6px;
                }}
                """
            )
        else:
            self.setStyleSheet(
                """
                _CompletionRow {
                    background: #1a1f27;
                    border: 1px solid #3f4752;
                    border-radius: 6px;
                }
                """
            )

    def natural_width(self) -> int:
        """候補文字列・投稿数・左右余白を含む、切れない最小横幅を返す。"""
        return self._natural_width

    def refresh_natural_width_from_final_hints(self) -> int:
        """
        Qtの最終スタイル適用後のsizeHintから、必要な幅を取り直す。

        初期化時点ではグローバルQSSの太字などがまだ反映されておらず、
        fontMetricsだけでは長い候補を短く見積もることがある。
        """
        self._tag_label.ensurePolished()
        self._count_label.ensurePolished()

        tag_width = max(
            self._tag_label.width(),
            self._tag_label.sizeHint().width(),
            self._tag_label.minimumSizeHint().width(),
        )
        count_width = max(
            self._count_label.width(),
            self._count_label.sizeHint().width(),
            self._count_label.minimumSizeHint().width(),
        )

        self._tag_label.setFixedWidth(max(1, tag_width))
        self._count_label.setFixedWidth(max(1, count_width))
        margins = self._row_layout.contentsMargins()
        self._natural_width = (
            tag_width
            + count_width
            + self._top_row.spacing()
            + margins.left()
            + margins.right()
            + 8
        )
        self.setMinimumWidth(self._natural_width)
        self.updateGeometry()
        return self._natural_width

class TagCompletionPopup(QFrame):
    candidate_chosen = Signal(object)

    def __init__(
        self,
        *,
        tag_color_dictionary: TagColorDictionary | None,
        tag_color_enabled: bool,
        parent: QWidget | None = None,
    ) -> None:
        # 通常の子Widgetにすると、親ダイアログの表示時に候補領域まで
        # 一緒に可視化され、入力欄やカードの裏へ重なってしまう。
        #
        # ToolTip型の独立小窓なら、表示中もQLineEditからキーボードフォーカスを
        # 奪わず、スクロール領域の外側にも正しく重ねられる。
        super().__init__(
            parent,
            Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint,
        )
        self.setObjectName("TagCompletionPopup")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.hide()
        self.setStyleSheet(
            """
            QFrame#TagCompletionPopup {
                background: #11161d;
                border: 1px solid #5a6b7d;
                border-radius: 8px;
            }
            QListWidget {
                background: transparent;
                border: none;
                outline: none;
            }
            QListWidget::item {
                border: none;
                margin: 1px 2px;
                padding: 0px;
            }
            QListWidget::item:selected {
                background: #2d5c87;
                border-radius: 6px;
            }
            """
        )
        self._tag_color_dictionary = tag_color_dictionary
        self._tag_color_enabled = tag_color_enabled
        self._candidates: list[TagCompletionCandidate] = []
        self._popup_anchor = QPoint()

        self.list_widget = QListWidget()
        self.list_widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list_widget.setMouseTracking(True)
        self.list_widget.viewport().setMouseTracking(True)
        self.list_widget.setFrameShape(QFrame.Shape.NoFrame)
        self.list_widget.setContentsMargins(0, 0, 0, 0)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 候補数が同時表示上限を超える時だけ、縦スクロールで続きを確認できる。
        self.list_widget.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list_widget.setSpacing(1)
        self.list_widget.itemActivated.connect(lambda _item: self._emit_current_candidate())
        self.list_widget.itemClicked.connect(self._emit_item_candidate)
        self.list_widget.itemEntered.connect(self._set_hovered_item)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.list_widget)

    def set_candidates(self, candidates: list[TagCompletionCandidate]) -> None:
        self._reset_size_constraints()
        self._candidates = list(candidates)
        self.list_widget.clear()

        for candidate in self._candidates:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, candidate)
            row = _CompletionRow(
                candidate,
                tag_color_dictionary=self._tag_color_dictionary,
                tag_color_enabled=self._tag_color_enabled,
            )
            row.ensurePolished()
            item.setSizeHint(
                QSize(
                    row.natural_width(),
                    max(1, row.sizeHint().height()),
                )
            )
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, row)

        if self._candidates:
            self.list_widget.setCurrentRow(0)

    def _reset_size_constraints(self) -> None:
        """
        前回の候補表示で設定した固定サイズを解除する。

        固定幅を残したまま次の候補を追加すると、投稿数ラベルが押し出され、
        sizeHintが不安定になるため、候補の作り直し前と実測前の両方で呼ぶ。
        """
        self.list_widget.setMinimumSize(0, 0)
        self.list_widget.setMaximumSize(_QT_MAX_WIDGET_SIZE, _QT_MAX_WIDGET_SIZE)
        self.setMinimumSize(0, 0)
        self.setMaximumSize(_QT_MAX_WIDGET_SIZE, _QT_MAX_WIDGET_SIZE)

    def show_for_line_edit(self, line_edit: QLineEdit) -> None:
        if not self._candidates:
            self.hide()
            return

        # 直前の検索で設定した固定幅・固定高を必ず外してから測る。
        # これをしないと、短い候補の表示後に次の候補が狭い幅でレイアウトされ、
        # 右端の投稿数ラベルが消えることがある。
        self._reset_size_constraints()

        item_count = self.list_widget.count()
        if item_count <= 0:
            self.hide()
            return

        item_hints = [
            self.list_widget.item(index).sizeHint()
            for index in range(item_count)
            if self.list_widget.item(index) is not None
        ]
        if not item_hints:
            self.hide()
            return

        # 表示内容に合わせて広がる。ただし候補1件だけでは細くなりすぎないよう、
        # 独立した候補UIとして280pxを下限にする。
        #
        # QListWidget側で一度縮められたsizeHintを使うと、長いalias候補が
        # "..." で切れた幅のまま確定してしまう。行Widget自身の自然幅を使う。
        natural_widths: list[int] = []
        for index in range(item_count):
            item = self.list_widget.item(index)
            row = self.list_widget.itemWidget(item) if item is not None else None
            if isinstance(row, _CompletionRow):
                natural_widths.append(row.natural_width())
            elif item is not None:
                natural_widths.append(item.sizeHint().width())

        content_width = max(
            MIN_COMPLETION_LIST_WIDTH,
            max(natural_widths, default=MIN_COMPLETION_LIST_WIDTH) + 12,
        )

        # 候補数を増やしてもポップアップが画面を埋めないよう、最大12行だけを
        # 同時表示する。残りはQListWidgetの縦スクロールで確認できる。
        visible_item_count = min(item_count, MAX_VISIBLE_COMPLETION_ROWS)
        fallback_height = (
            sum(hint.height() for hint in item_hints[:visible_item_count])
            + max(0, visible_item_count - 1) * self.list_widget.spacing()
            + 8
        )
        scrollbar_width = (
            self.list_widget.verticalScrollBar().sizeHint().width()
            if item_count > visible_item_count
            else 0
        )
        list_width_for_layout = content_width + scrollbar_width
        self.list_widget.resize(list_width_for_layout, max(1, fallback_height))
        self.list_widget.doItemsLayout()

        last_item = self.list_widget.item(visible_item_count - 1)
        last_rect = self.list_widget.visualItemRect(last_item)
        content_height = (
            last_rect.bottom() + 1
            if last_rect.isValid()
            else fallback_height
        )
        content_height = max(1, content_height)

        popup_layout = self.layout()
        margins = popup_layout.contentsMargins() if popup_layout is not None else None
        horizontal_margins = (margins.left() + margins.right()) if margins is not None else 0
        vertical_margins = (margins.top() + margins.bottom()) if margins is not None else 0
        frame_size = self.frameWidth() * 2

        desired_width = list_width_for_layout + horizontal_margins + frame_size
        desired_height = content_height + vertical_margins + frame_size

        # ToolTip型の独立小窓なので、画面座標で入力欄の真下へ置く。
        origin = line_edit.mapToGlobal(QPoint(0, line_edit.height() + 2))
        popup_x = origin.x()
        popup_y = origin.y()

        # 画面下に収まらない時は入力欄の上へ開く。
        # 幅が画面をはみ出す長い候補がある時だけ、画面幅へ収める。
        screen = line_edit.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            desired_width = min(desired_width, available.width())
            desired_height = min(desired_height, available.height())

            max_x = available.right() - desired_width + 1
            popup_x = max(available.left(), min(popup_x, max_x))

            if popup_y + desired_height > available.bottom() + 1:
                above_y = line_edit.mapToGlobal(QPoint(0, -desired_height - 2)).y()
                if above_y >= available.top():
                    popup_y = above_y
                else:
                    popup_y = max(available.top(), available.bottom() - desired_height + 1)

        # popupとQListWidgetを候補全件ぴったりの実寸へ固定する。
        list_width = max(1, desired_width - horizontal_margins - frame_size)
        list_height = max(1, desired_height - vertical_margins - frame_size)
        self.list_widget.setFixedSize(list_width, list_height)
        self.setFixedSize(desired_width, desired_height)
        self.list_widget.verticalScrollBar().setValue(0)
        self.move(QPoint(popup_x, popup_y))
        self.show()
        self.raise_()

        # show()後に最終スタイルが反映されるため、次のイベントループで
        # 文字列の実幅を取り直してポップアップを再調整する。
        self._popup_anchor = origin
        QTimer.singleShot(0, self._finalize_width_after_show)

    def _finalize_width_after_show(self) -> None:
        """最終スタイル適用後の候補文字幅で、ポップアップ幅を再調整する。"""
        if not self.isVisible() or self.list_widget.count() <= 0:
            return

        final_widths: list[int] = []
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            row = self.list_widget.itemWidget(item) if item is not None else None
            if isinstance(row, _CompletionRow):
                final_widths.append(row.refresh_natural_width_from_final_hints())
            elif item is not None:
                final_widths.append(item.sizeHint().width())

        content_width = max(
            MIN_COMPLETION_LIST_WIDTH,
            max(final_widths, default=MIN_COMPLETION_LIST_WIDTH) + 12,
        )

        popup_layout = self.layout()
        margins = popup_layout.contentsMargins() if popup_layout is not None else None
        horizontal_margins = (margins.left() + margins.right()) if margins is not None else 0
        vertical_margins = (margins.top() + margins.bottom()) if margins is not None else 0
        frame_size = self.frameWidth() * 2

        scrollbar_width = (
            self.list_widget.verticalScrollBar().sizeHint().width()
            if self.list_widget.count() > MAX_VISIBLE_COMPLETION_ROWS
            else 0
        )
        desired_width = content_width + scrollbar_width + horizontal_margins + frame_size
        desired_height = self.height()

        popup_x = self._popup_anchor.x()
        popup_y = self._popup_anchor.y()
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            desired_width = min(desired_width, available.width())
            max_x = available.right() - desired_width + 1
            popup_x = max(available.left(), min(popup_x, max_x))

            if popup_y + desired_height > available.bottom() + 1:
                above_y = self._popup_anchor.y() - desired_height - 2
                if above_y >= available.top():
                    popup_y = above_y
                else:
                    popup_y = max(available.top(), available.bottom() - desired_height + 1)

        list_width = max(1, desired_width - horizontal_margins - frame_size)
        list_height = max(1, desired_height - vertical_margins - frame_size)
        self.list_widget.setFixedSize(list_width, list_height)
        self.setFixedSize(desired_width, desired_height)
        self.list_widget.doItemsLayout()
        self.move(QPoint(popup_x, popup_y))

    def has_candidates(self) -> bool:
        return bool(self._candidates)

    def move_current_row(self, offset: int) -> None:
        if not self._candidates:
            return

        row = self.list_widget.currentRow()
        if row < 0:
            row = 0
        else:
            row = (row + offset) % len(self._candidates)
        self.list_widget.setCurrentRow(row)
        self.list_widget.scrollToItem(self.list_widget.currentItem())

    def current_candidate(self) -> TagCompletionCandidate | None:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        candidate = item.data(Qt.ItemDataRole.UserRole)
        return candidate if isinstance(candidate, TagCompletionCandidate) else None

    def _emit_current_candidate(self) -> None:
        candidate = self.current_candidate()
        if candidate is not None:
            self.candidate_chosen.emit(candidate)

    def _emit_item_candidate(self, item: QListWidgetItem) -> None:
        candidate = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(candidate, TagCompletionCandidate):
            self.candidate_chosen.emit(candidate)

    def _set_hovered_item(self, item: QListWidgetItem) -> None:
        """マウスカーソルが乗った候補を、キーボード確定対象にもする。"""
        if item is not None:
            self.list_widget.setCurrentItem(item)


class TagCompletionController(QObject):
    """
    QLineEditへTagComplete風の補完ポップアップを付与する。

    候補を確定した時だけaliasを正規タグへ変換する。
    手入力の文字列は一切変更しない。
    """

    def __init__(
        self,
        line_edit: QLineEdit,
        tag_dictionary: DanbooruTagDictionary | None,
        *,
        tag_color_dictionary: TagColorDictionary | None,
        tag_color_enabled: bool,
        limit: int = 12,
    ) -> None:
        super().__init__(line_edit)
        self._line_edit = line_edit
        self._host_window = line_edit.window()
        self._dictionary = tag_dictionary
        self._limit = max(1, limit)
        self._applying_candidate = False
        self._popup_host_frame_geometry: QRect | None = None

        # 入力中の補完更新を少し遅らせ、連続入力での候補作り直しをまとめる。
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(40)
        self._refresh_timer.timeout.connect(self._refresh)

        # Windowsのタイトルバー移動はQWidgetのMoveイベントだけでは
        # 取りこぼす場合があるため、候補表示中だけ親ダイアログの位置を監視する。
        self._popup_host_watch_timer = QTimer(self)
        self._popup_host_watch_timer.setInterval(40)
        self._popup_host_watch_timer.timeout.connect(
            self._hide_if_host_window_moved_or_resized
        )

        # ToolTip型の独立小窓として出す。親は寿命管理のためのダイアログで、
        # 位置決めは show_for_line_edit() 側の画面座標で行う。
        self._popup = TagCompletionPopup(
            tag_color_dictionary=tag_color_dictionary,
            tag_color_enabled=tag_color_enabled,
            parent=self._host_window,
        )
        self._popup.candidate_chosen.connect(self._apply_candidate)

        # QLineEditだけでなく、ダイアログ内のクリックやダイアログ移動も監視する。
        # 候補ポップアップはToolTip型の独立小窓なので、親と一緒には移動しない。
        self._line_edit.installEventFilter(self)
        self._host_window.installEventFilter(self)
        application = QApplication.instance()
        if application is not None:
            application.installEventFilter(self)

        self._line_edit.textChanged.connect(self._schedule_refresh)

    def is_available(self) -> bool:
        return self._dictionary is not None and self._dictionary.is_available()

    @staticmethod
    def _is_in_object_tree(watched: QObject, root: QObject) -> bool:
        """watched が root 自身またはその子孫なら True を返す。"""
        current: QObject | None = watched
        while current is not None:
            if current is root:
                return True
            current = current.parent()
        return False

    def _is_click_inside_input_or_popup(self) -> bool:
        """
        現在のマウス座標から、実際にクリックされたウィジェットを判定する。

        QApplicationのイベントフィルタでは watched が入力欄自身ではなく
        内部viewport等になる場合があるため、watchedだけでは判定しない。
        """
        clicked_widget = QApplication.widgetAt(QCursor.pos())
        if clicked_widget is None:
            return False

        return (
            self._is_in_object_tree(clicked_widget, self._line_edit)
            or self._is_in_object_tree(clicked_widget, self._popup)
        )

    def hide_popup(self) -> None:
        """保留中の候補更新・親位置監視を止めて、候補を閉じる。"""
        self._refresh_timer.stop()
        self._popup_host_watch_timer.stop()
        self._popup_host_frame_geometry = None
        self._popup.hide()

    def _start_popup_host_watch(self) -> None:
        """候補表示時の親ダイアログ位置を記録して監視を開始する。"""
        self._popup_host_frame_geometry = self._host_window.frameGeometry()
        self._popup_host_watch_timer.start()

    def _hide_if_host_window_moved_or_resized(self) -> None:
        """親ダイアログが移動・リサイズされたら、候補を閉じる。"""
        if not self._popup.isVisible():
            self._popup_host_watch_timer.stop()
            return

        if self._popup_host_frame_geometry is None:
            self._popup_host_frame_geometry = self._host_window.frameGeometry()
            return

        if self._host_window.frameGeometry() != self._popup_host_frame_geometry:
            self.hide_popup()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        event_type = event.type()

        # ToolTip型の候補ポップアップは親ウインドウに追従しないため、
        # 通常の移動・リサイズ通知を受け取れた場合も即時に閉じる。
        if watched is self._host_window and event_type in (
            QEvent.Type.Move,
            QEvent.Type.Resize,
            QEvent.Type.Hide,
            QEvent.Type.Close,
        ):
            self.hide_popup()

        non_client_press = getattr(
            QEvent.Type,
            "NonClientAreaMouseButtonPress",
            None,
        )
        is_mouse_press = (
            event_type == QEvent.Type.MouseButtonPress
            or (
                non_client_press is not None
                and event_type == non_client_press
            )
        )

        # 候補を表示中は、実際のクリック先が入力欄でも候補でもない場合に閉じる。
        # タイトルバーのクリックも、Qtが非クライアント領域イベントを渡す環境ではここで閉じる。
        if is_mouse_press and self._popup.isVisible():
            if not self._is_click_inside_input_or_popup():
                self.hide_popup()

        if watched is not self._line_edit:
            return super().eventFilter(watched, event)

        if event_type == QEvent.Type.KeyPress:
            key = event.key()

            if key == Qt.Key.Key_Escape and self._popup.isVisible():
                self.hide_popup()
                return True

            # 候補の移動は上下キーに割り当てる。
            # 左右キーはQLineEdit本来のカーソル移動として通し、カンマ区切り入力の
            # 前後移動を妨げない。
            has_navigation_modifier = bool(
                event.modifiers()
                & (
                    Qt.KeyboardModifier.ControlModifier
                    | Qt.KeyboardModifier.ShiftModifier
                    | Qt.KeyboardModifier.AltModifier
                    | Qt.KeyboardModifier.MetaModifier
                )
            )
            if (
                key == Qt.Key.Key_Down
                and self._popup.isVisible()
                and not has_navigation_modifier
            ):
                self._popup.move_current_row(1)
                return True

            if (
                key == Qt.Key.Key_Up
                and self._popup.isVisible()
                and not has_navigation_modifier
            ):
                self._popup.move_current_row(-1)
                return True

            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._popup.isVisible():
                candidate = self._popup.current_candidate()
                if candidate is not None:
                    self._apply_candidate(candidate)
                    return True

        if event_type == QEvent.Type.FocusOut:
            QTimer.singleShot(80, self._hide_if_focus_left)

        return super().eventFilter(watched, event)

    def _schedule_refresh(self, _text: str) -> None:
        if self._applying_candidate:
            return
        self._refresh_timer.start()

    def _refresh(self) -> None:
        # 入力欄から離れた操作中に、古いtextChangedタイマーで候補が
        # 再表示されないようにする。
        if not self._line_edit.hasFocus():
            self.hide_popup()
            return

        if self._dictionary is None or not self._dictionary.is_available():
            self.hide_popup()
            return

        query = self._active_segment_text()
        if not query:
            self.hide_popup()
            return

        candidates = self._dictionary.search(query, limit=self._limit)
        if not candidates:
            self.hide_popup()
            return

        self._popup.set_candidates(candidates)
        self._popup.show_for_line_edit(self._line_edit)
        if self._popup.isVisible():
            self._start_popup_host_watch()

    def _active_segment_bounds(self) -> tuple[int, int]:
        text = self._line_edit.text()
        cursor = self._line_edit.cursorPosition()
        start = text.rfind(",", 0, cursor) + 1
        end = text.find(",", cursor)
        if end < 0:
            end = len(text)
        return start, end

    def _active_segment_text(self) -> str:
        start, end = self._active_segment_bounds()
        return self._line_edit.text()[start:end].strip()

    def _apply_candidate(self, candidate: TagCompletionCandidate) -> None:
        text = self._line_edit.text()
        start, end = self._active_segment_bounds()
        active_segment = text[start:end]

        # カンマ直後に入っていた先頭空白だけは残し、候補決定時の見た目を保つ。
        leading_space = " " if start > 0 and active_segment.startswith(" ") else ""
        replacement = leading_space + candidate.tag.name

        if end >= len(text):
            # 最後の入力区間を確定した場合は、次のタグを続けて打てるよう
            # カンマと半角スペースを自動で補う。
            updated = text[:start] + replacement + ", "
            cursor_position = start + len(replacement) + 2
        else:
            # すでに後続カンマがある区間では重複させない。
            updated = text[:start] + replacement + text[end:]
            cursor_position = start + len(replacement)

        self._applying_candidate = True
        try:
            self._line_edit.setText(updated)
            self._line_edit.setCursorPosition(cursor_position)
            self._line_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        finally:
            self._applying_candidate = False

        self.hide_popup()

    def _hide_if_focus_left(self) -> None:
        focus_widget = QApplication.focusWidget()
        if (
            focus_widget is self._line_edit
            or focus_widget is self._popup.list_widget
            or self._popup.underMouse()
            or self._popup.list_widget.underMouse()
        ):
            return
        self.hide_popup()
