"""通常・拡大プレビューで共用する、ファイル名だけ選択できるステータス欄。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QVBoxLayout, QWidget


class _FilenameLabel(QLabel):
    """ファイル名だけをマウス選択でき、右クリックから全文をコピーできる。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.full_filename = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._open_copy_menu)
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; padding: 0; color: #eeeeee; "
            "selection-background-color: #3f8cff; selection-color: #ffffff; }"
        )

    def _open_copy_menu(self, pos) -> None:
        if not self.full_filename:
            return
        menu = QMenu(self)
        selected_action = None
        if self.hasSelectedText():
            selected_action = menu.addAction("選択部分をコピー")
        full_action = menu.addAction("ファイル名をコピー")
        action = menu.exec(self.mapToGlobal(pos))
        if action is selected_action and selected_action is not None:
            QApplication.clipboard().setText(self.selectedText())
        elif action is full_action:
            QApplication.clipboard().setText(self.full_filename)


class SelectableFilenameStatus(QFrame):
    """画像名だけ選択可能なステータス。拡大時は2行構成を固定する。

    画像名以外のマウスイベントは透過に近い操作感を保つため、
    拡大側では背景クリックとして親へ通知できるようにする。
    """

    background_clicked = Signal()

    def __init__(self, *, expanded: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._expanded = expanded
        self._filename = ""

        self.filename_label = _FilenameLabel(self)
        self.prefix_label = QLabel(self)
        self.suffix_label = QLabel(self)
        self.controls_label = QLabel(self)
        self.generic_label = QLabel(self)

        for label in (self.prefix_label, self.suffix_label, self.controls_label, self.generic_label):
            label.setTextFormat(Qt.TextFormat.RichText if label is self.suffix_label else Qt.TextFormat.PlainText)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            label.setStyleSheet("QLabel { color: #eeeeee; border: none; background: transparent; padding: 0; }")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.filename_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.generic_label.setWordWrap(False)
        self.controls_label.setWordWrap(False)

        self.line_one = QWidget(self)
        self.line_one.setStyleSheet("background: transparent; border: none;")
        row = QHBoxLayout(self.line_one)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addStretch(1)
        row.addWidget(self.prefix_label, 0)
        row.addWidget(self.filename_label, 0)
        # QtのQLabelは文字列先頭の空白を表示上詰める場合があるため、
        # 枚数・チェック表示との間隔はテキストではなく明示的な幅で確保する。
        self._filename_suffix_gap = QWidget(self.line_one)
        self._filename_suffix_gap.setFixedWidth(12)
        self._filename_suffix_gap.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row.addWidget(self._filename_suffix_gap, 0)
        row.addWidget(self.suffix_label, 0)
        row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10 if expanded else 4, 4, 10 if expanded else 4, 4)
        layout.setSpacing(0)
        layout.addWidget(self.line_one, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.controls_label, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.generic_label, 0, Qt.AlignmentFlag.AlignCenter)
        self.controls_label.setVisible(False)
        self.generic_label.setVisible(False)
        if expanded:
            self.setMinimumHeight(42)

    def set_filename_line(
        self,
        filename: str,
        *,
        prefix: str = "",
        suffix: str = "",
        controls: str = "",
    ) -> None:
        self._filename = filename
        self.filename_label.full_filename = filename
        self.filename_label.setToolTip(filename + "\n右クリック：ファイル名をコピー")
        self.prefix_label.setText(prefix)
        self.suffix_label.setText(suffix)
        self._filename_suffix_gap.setVisible(bool(suffix))
        self.controls_label.setText(controls)
        self.controls_label.setVisible(self._expanded and bool(controls))
        self.generic_label.hide()
        self.line_one.show()
        self._fit_filename()

    def setText(self, text: str) -> None:  # noqa: N802
        """ファイル名がない状態（ギャラリーなど）は従来どおり一般文を表示。"""
        self._filename = ""
        self.filename_label.full_filename = ""
        self.line_one.hide()
        self.controls_label.hide()
        self.generic_label.setText(text)
        self.generic_label.show()

    def clear(self) -> None:
        self.setText("")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit_filename()

    def _fit_filename(self) -> None:
        if not self._filename:
            return
        # 幅は外枠側が決める。名前の長さで枠やレイアウトが拡大しないようにする。
        available = max(44, self.width() - self.prefix_label.sizeHint().width()
                        - self.suffix_label.sizeHint().width()
                        - (12 if self.suffix_label.text() else 0)
                        - (28 if self._expanded else 16))
        metrics = self.filename_label.fontMetrics()
        text = metrics.elidedText(self._filename, Qt.TextElideMode.ElideMiddle, available)
        self.filename_label.setText(text)
        self.filename_label.setFixedWidth(min(available, metrics.horizontalAdvance(text) + 3))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._expanded and event.button() == Qt.MouseButton.LeftButton:
            self.background_clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)
