from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from theme import PREVIEW_IMAGE_LABEL_QSS, PREVIEW_STATUS_LABEL_QSS


class ClickablePreviewLabel(QLabel):
    """通常画像／操作対象1枚プレビュー用のクリック可能な表示ラベル。"""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class ClickableGalleryCard(QFrame):
    """操作対象プレビューの複数枚一覧で、拡大対象を選べるカード。"""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class PreviewPanel(QFrame):
    previous_requested = Signal()
    next_requested = Signal()
    hover_changed = Signal(bool)
    enlarge_requested = Signal(object, str)

    def __init__(self) -> None:
        super().__init__()
        self._path: Path | None = None
        self._pixmap: QPixmap | None = None
        self._normal_message: str | None = None
        self._normal_status_text = "ファイル名　0/0"

        self._display_mode = "normal"
        self._placeholder_text: str | None = None
        self._temporary_path: Path | None = None
        self._temporary_pixmap: QPixmap | None = None

        self._gallery_paths: list[Path] = []
        self._gallery_pixmaps: dict[Path, QPixmap] = {}
        self._gallery_current_path: Path | None = None
        self._gallery_columns: int = -1
        self._gallery_signature: tuple[tuple[Path, ...], Path | None] | None = None

        self.setObjectName("PreviewPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)

        self.prev_button = QPushButton("◁")
        self.prev_button.setObjectName("NavButton")
        # 前後移動ボタンがSpaceキーのフォーカスを奪うと、ボタン再押下になって
        # 現在画像のチェック切替ができなくなるため、キーボードフォーカス対象から外す。
        self.prev_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.prev_button.clicked.connect(self.previous_requested.emit)

        self.next_button = QPushButton("▷")
        self.next_button.setObjectName("NavButton")
        self.next_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.next_button.clicked.connect(self.next_requested.emit)

        self.image_label = ClickablePreviewLabel("")
        self.image_label.clicked.connect(self._request_current_image_enlarge)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.image_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.image_label.setStyleSheet(PREVIEW_IMAGE_LABEL_QSS)

        self.gallery_container = QWidget()
        self.gallery_container.setObjectName("CheckedGalleryContainer")
        self.gallery_grid = QGridLayout(self.gallery_container)
        self.gallery_grid.setContentsMargins(12, 12, 12, 12)
        self.gallery_grid.setHorizontalSpacing(10)
        self.gallery_grid.setVerticalSpacing(10)
        self.gallery_grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self.gallery_scroll = QScrollArea()
        self.gallery_scroll.setWidgetResizable(True)
        self.gallery_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.gallery_scroll.setWidget(self.gallery_container)
        self.gallery_scroll.setStyleSheet(
            """
            QScrollArea {
                background: #101010;
                border: 1px solid #404040;
                border-radius: 8px;
            }
            QWidget#CheckedGalleryContainer {
                background: #101010;
            }
            """
        )

        self.display_stack = QStackedWidget()
        self.display_stack.setMinimumSize(520, 320)
        self.display_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.display_stack.addWidget(self.image_label)
        self.display_stack.addWidget(self.gallery_scroll)
        self.display_stack.setCurrentWidget(self.image_label)

        center_layout = QHBoxLayout()
        center_layout.addWidget(self.prev_button)
        center_layout.addWidget(self.display_stack, 1)
        center_layout.addWidget(self.next_button)

        self.status_label = QLabel(self._normal_status_text)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet(PREVIEW_STATUS_LABEL_QSS)

        layout = QVBoxLayout(self)
        layout.addLayout(center_layout, 1)
        layout.addWidget(self.status_label)

    def set_image(self, path: Path | None, index: int = 0, total: int = 0) -> None:
        """通常の青枠選択画像を保持し、通常プレビューとして表示する。"""
        self._path = path
        self._pixmap = None
        self._normal_message = None
        self._normal_status_text = "ファイル名　0/0" if path is None else f"{path.name}　{index}/{total}"

        self._display_mode = "normal"
        self._placeholder_text = None
        self._temporary_path = None
        self._temporary_pixmap = None
        self.display_stack.setCurrentWidget(self.image_label)
        self.image_label.clear()

        if path is None:
            # 拡大表示機能は実装済みなので、通常プレビュー中央の案内文は出さない。
            # 画像未選択時は下部ステータスだけ表示し、中央は空欄にする。
            self.status_label.setText(self._normal_status_text)
            return

        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self._normal_message = f"画像を読み込めませんでした\n{path.name}"
            self.image_label.setText(self._normal_message)
            self.status_label.setText(self._normal_status_text)
            return

        self._pixmap = pixmap
        self._update_scaled_pixmap()
        self.status_label.setText(self._normal_status_text)

    def set_placeholder(self, text: str, status_text: str | None = None) -> None:
        """通常画像を保持したまま、一時的に文字表示へ切り替える。"""
        self._display_mode = "placeholder"
        self._placeholder_text = text
        self._temporary_path = None
        self._temporary_pixmap = None
        self.display_stack.setCurrentWidget(self.image_label)
        self.image_label.clear()
        self.image_label.setText(text)

        if status_text is not None:
            self.status_label.setText(status_text)

    def set_temporary_image(self, path: Path, status_text: str) -> None:
        """
        通常の青枠画像は保持したまま、チェック中の1枚を一時プレビュー表示する。
        ホバー終了時は restore_current_image() で通常画像へ戻る。
        """
        self._display_mode = "temporary_image"
        self._placeholder_text = None
        self._temporary_path = path
        self._temporary_pixmap = QPixmap(str(path))
        self.display_stack.setCurrentWidget(self.image_label)
        self.image_label.clear()
        self.status_label.setText(status_text)

        if self._temporary_pixmap.isNull():
            self.image_label.setText(f"画像を読み込めませんでした\n{path.name}")
            return

        self._update_scaled_pixmap()

    def set_checked_gallery(
        self,
        entries: list[tuple[Path, QPixmap]],
        *,
        current_path: Path | None = None,
        status_text: str | None = None,
    ) -> None:
        """
        通常の青枠画像は保持したまま、複数のチェック中画像を表示専用の
        サムネイル一覧として一時表示する。

        entries の QPixmap は ThumbnailPanel の既存サムネイルを利用する。
        ここで元画像を枚数分読み込まないため、大量チェック時の表示を軽くする。
        """
        paths = [path for path, _pixmap in entries]
        signature = (tuple(paths), current_path)

        self._display_mode = "gallery"
        self._placeholder_text = None
        self._temporary_path = None
        self._temporary_pixmap = None

        if signature != self._gallery_signature:
            self._gallery_paths = paths
            self._gallery_current_path = current_path
            self._gallery_pixmaps = {path: pixmap for path, pixmap in entries}
            self._gallery_signature = signature
            self._gallery_columns = -1
            self._rebuild_gallery()

        self.display_stack.setCurrentWidget(self.gallery_scroll)

        if status_text is not None:
            self.status_label.setText(status_text)

    def restore_current_image(self) -> None:
        """一時表示を終了し、現在の青枠画像の通常プレビューへ戻す。"""
        self._display_mode = "normal"
        self._placeholder_text = None
        self._temporary_path = None
        self._temporary_pixmap = None
        self.display_stack.setCurrentWidget(self.image_label)
        self.image_label.clear()
        self.status_label.setText(self._normal_status_text)

        if self._pixmap is not None:
            self._update_scaled_pixmap()
        elif self._normal_message is not None:
            self.image_label.setText(self._normal_message)

    def _request_current_image_enlarge(self) -> None:
        """現在、単体プレビューとして表示されている画像の拡大表示を要求する。"""
        if self._display_mode == "normal" and self._path is not None:
            self.enlarge_requested.emit(self._path, "normal")
        elif self._display_mode == "temporary_image" and self._temporary_path is not None:
            self.enlarge_requested.emit(self._temporary_path, "checked")

    def enterEvent(self, event) -> None:  # noqa: N802
        super().enterEvent(event)
        self.hover_changed.emit(True)

    def leaveEvent(self, event) -> None:  # noqa: N802
        super().leaveEvent(event)
        self.hover_changed.emit(False)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_scaled_pixmap()

        if self._display_mode == "gallery":
            columns = self._desired_gallery_columns()
            if columns != self._gallery_columns:
                self._rebuild_gallery()

    def _update_scaled_pixmap(self) -> None:
        if self._display_mode == "normal":
            pixmap = self._pixmap
        elif self._display_mode == "temporary_image":
            pixmap = self._temporary_pixmap
        else:
            return

        if pixmap is None or pixmap.isNull():
            return

        scaled = pixmap.scaled(
            self.image_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setPixmap(scaled)

    def _desired_gallery_columns(self) -> int:
        width = max(160, self.gallery_scroll.viewport().width())
        return max(1, (width - 16) // 160)

    def _clear_gallery(self) -> None:
        while self.gallery_grid.count():
            item = self.gallery_grid.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

    def _rebuild_gallery(self) -> None:
        self._clear_gallery()
        columns = self._desired_gallery_columns()
        self._gallery_columns = columns

        for position, path in enumerate(self._gallery_paths):
            is_current = path == self._gallery_current_path

            card = ClickableGalleryCard()
            card.setObjectName("CheckedPreviewCard")
            card.setFixedSize(150, 132)
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            card.setToolTip(f"{path.name}\nクリックで拡大表示")
            card.clicked.connect(lambda selected_path=path: self.enlarge_requested.emit(selected_path, "checked"))
            if is_current:
                card.setStyleSheet(
                    """
                    QFrame#CheckedPreviewCard {
                        background: #30291b;
                        border: 2px solid #3f8cff;
                        border-radius: 8px;
                    }
                    """
                )
            else:
                card.setStyleSheet(
                    """
                    QFrame#CheckedPreviewCard {
                        background: #30291b;
                        border: 1px solid #e0b84a;
                        border-radius: 8px;
                    }
                    """
                )

            thumb = QLabel()
            thumb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            thumb.setFixedSize(138, 96)
            thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pixmap = self._gallery_pixmaps.get(path)
            if pixmap is not None and not pixmap.isNull():
                thumb.setPixmap(
                    pixmap.scaled(
                        thumb.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            else:
                thumb.setText("読込失敗")
                thumb.setStyleSheet("color: #b0b0b0;")

            name_text = path.name if len(path.name) <= 20 else path.name[:17] + "..."
            name_label = QLabel(name_text)
            name_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            name_label.setToolTip(path.name)
            name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name_label.setStyleSheet("color: #e8e8e8; font-size: 10px;")

            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(5, 5, 5, 5)
            card_layout.setSpacing(3)
            card_layout.addWidget(thumb)
            card_layout.addWidget(name_label)

            row = position // columns
            column = position % columns
            self.gallery_grid.addWidget(card, row, column)
