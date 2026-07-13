from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from tag_history import TagHistoryEntry
from theme import ADD_TAG_DIALOG_QSS, CHIP_SCROLL_QSS


class HistoryDetailDialog(QDialog):
    """タグ操作履歴1件の詳細を表示する小ダイアログ。"""

    def __init__(self, entry: TagHistoryEntry, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("タグ操作履歴の詳細")
        self.setModal(True)
        self.resize(640, 520)
        self.setStyleSheet(ADD_TAG_DIALOG_QSS)

        card = QFrame()
        card.setObjectName("DialogCard")

        title = QLabel(f"{entry.operation_type}")
        title.setObjectName("DialogTitle")

        body = QTextEdit()
        body.setReadOnly(True)
        body.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        body.setPlainText(self._build_detail_text(entry))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(12)
        card_layout.addWidget(title)
        card_layout.addWidget(body, 1)
        card_layout.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(card)

    def _status_text(self, entry: TagHistoryEntry) -> str:
        if entry.invalidated:
            return entry.invalidated_reason or "外部再読み込みにより無効"
        if entry.undone:
            return "取り消し済み"
        return "通常"

    def _invalidated_explanation(self, entry: TagHistoryEntry) -> str:
        if not entry.invalidated:
            return ""
        return (
            "この操作に含まれる一部または全部のtxtが外部から再読み込みされたため、"
            "この履歴は取り消せません。"
        )

    def _build_detail_text(self, entry: TagHistoryEntry) -> str:
        detail_text = entry.detail or "詳細なし"
        lines = [
            f"種別: {entry.operation_type}",
            f"概要: {entry.summary}",
            f"状態: {self._status_text(entry)}",
            f"時刻: {entry.created_at.strftime('%Y-%m-%d %H:%M:%S')}",
        ]

        invalidated_explanation = self._invalidated_explanation(entry)
        if invalidated_explanation:
            lines.extend(["", "無効理由:", invalidated_explanation])

        lines.extend([
            "",
            "操作内容:",
            detail_text,
        ])

        # タグ別内訳は履歴詳細側で一元表示する。
        # 旧セッション中のエントリなど、detail側にすでに内訳が含まれている場合は重複表示しない。
        if entry.tag_counts_by_tag and "タグ別内訳:" not in detail_text:
            lines.extend(["", "タグ別内訳:"])
            for tag, count in entry.tag_counts_by_tag.items():
                lines.append(f"- {tag}: {count}枚")

        lines.extend(["", f"対象画像: {len(entry.target_image_paths)}件"])

        for image_path in entry.target_image_paths:
            lines.append(f"- {image_path.name}")

        if entry.target_tag_paths:
            lines.extend(["", "対象txt:"])
            for tag_path in entry.target_tag_paths:
                lines.append(f"- {tag_path.name}")

        return "\n".join(lines)


class OperationHistoryDialog(QDialog):
    """タグ編集操作の履歴一覧ダイアログ。"""

    undo_latest_requested = Signal()

    def __init__(self, entries: list[TagHistoryEntry], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("タグ操作履歴")
        self.setModal(True)
        self.resize(820, 520)
        self.setStyleSheet(ADD_TAG_DIALOG_QSS)
        # 呼び出し元の履歴リストを参照し続ける。
        # 取り消し後に同じダイアログ内で表示を更新するため、コピーにはしない。
        self._entries = entries

        card = QFrame()
        card.setObjectName("DialogCard")

        title = QLabel("タグ操作履歴")
        title.setObjectName("DialogTitle")

        hint = QLabel(
            "取り消せるのは、まだ取り消されていない最新のタグ編集操作だけです。"
            "続けて取り消すと、1件ずつ過去へ遡れます。"
        )
        hint.setObjectName("DialogHint")
        hint.setWordWrap(True)

        self.rows_container = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_container)
        self.rows_layout.setContentsMargins(8, 8, 8, 8)
        self.rows_layout.setSpacing(8)

        self.history_scroll = QScrollArea()
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setWidget(self.rows_container)
        self.history_scroll.setStyleSheet(CHIP_SCROLL_QSS)

        self._populate_rows()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(12)
        card_layout.addWidget(title)
        card_layout.addWidget(hint)
        card_layout.addWidget(self.history_scroll, 1)
        card_layout.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(card)

    def _clear_rows(self) -> None:
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

    def _populate_rows(self) -> None:
        """履歴一覧を現在の履歴状態で再描画する。"""
        scroll_value = 0
        if hasattr(self, "history_scroll"):
            scroll_value = self.history_scroll.verticalScrollBar().value()

        self._clear_rows()

        if not self._entries:
            empty = QLabel("まだ操作履歴はありません。")
            empty.setObjectName("DialogHint")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.rows_layout.addWidget(empty)
        else:
            undoable_index = self._latest_undoable_index()
            for index, entry in enumerate(self._entries):
                self.rows_layout.addWidget(self._make_row(index, entry, undoable_index))
            self.rows_layout.addStretch(1)

        if hasattr(self, "history_scroll"):
            def restore_scroll() -> None:
                bar = self.history_scroll.verticalScrollBar()
                bar.setValue(min(scroll_value, bar.maximum()))

            QTimer.singleShot(0, restore_scroll)

    def _latest_undoable_index(self) -> int | None:
        for index, entry in enumerate(self._entries):
            if not entry.is_undo_entry and not entry.undone and not entry.invalidated:
                return index
        return None

    def _make_row(self, index: int, entry: TagHistoryEntry, undoable_index: int | None) -> QWidget:
        row = QFrame()
        row.setStyleSheet(
            """
            QFrame {
                background: #202020;
                border: 1px solid #4a4a4a;
                border-radius: 10px;
            }
            QLabel {
                border: none;
                background: transparent;
            }
            """
        )

        label_text = f"[{entry.operation_type}] {entry.summary}"
        if index == 0:
            label_text = "最新  " + label_text

        label = QLabel(label_text)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setWordWrap(True)

        detail_button = QPushButton("詳細")
        detail_button.clicked.connect(lambda _checked=False, e=entry: self._show_detail(e))

        undo_button = QPushButton("取り消す")
        can_undo = index == undoable_index
        undo_button.setEnabled(can_undo)
        undo_button.setVisible(can_undo)
        undo_button.clicked.connect(self._request_undo_latest)

        status_label = QLabel("")
        status_label.setObjectName("HistoryStatusLabel")
        status_label.setStyleSheet("color: #ff7a7a; font-weight: bold;")
        if entry.invalidated:
            status_label.setText(entry.invalidated_reason or "外部再読み込みにより無効")
        elif entry.undone:
            status_label.setText("取り消し済み")

        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(10, 8, 10, 8)
        row_layout.setSpacing(8)
        row_layout.addWidget(label, 1)
        if undo_button.isVisible():
            row_layout.addWidget(undo_button)
        if status_label.text():
            row_layout.addWidget(status_label)
        row_layout.addWidget(detail_button)

        return row

    def _show_detail(self, entry: TagHistoryEntry) -> None:
        dialog = HistoryDetailDialog(entry, self)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def _request_undo_latest(self) -> None:
        self.undo_latest_requested.emit()
        self._populate_rows()
