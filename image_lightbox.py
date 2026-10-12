from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from filename_status import SelectableFilenameStatus

from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


def _apply_button_glow(button: QPushButton, color: str, *, blur_radius: float = 20.0) -> None:
    """右上操作ボタンへ常時の薄い発光を付ける。"""
    effect = QGraphicsDropShadowEffect(button)
    effect.setBlurRadius(blur_radius)
    effect.setOffset(0, 0)
    glow_color = QColor(color)
    glow_color.setAlpha(190)
    effect.setColor(glow_color)
    button.setGraphicsEffect(effect)



_STATUS_PREFERRED_WIDTH = 920
_STATUS_MIN_WIDTH = 460
_STATUS_SIDE_RESERVE = 40


def _update_status_label_width(label: SelectableFilenameStatus, container_width: int) -> int:
    """ステータス欄を長めの一定幅に寄せつつ、狭い画面では収まる幅まで縮める。"""
    available = max(280, int(container_width) - _STATUS_SIDE_RESERVE)
    width = min(_STATUS_PREFERRED_WIDTH, available)
    if available >= _STATUS_MIN_WIDTH:
        width = max(_STATUS_MIN_WIDTH, width)
    label.setFixedWidth(max(280, width))
    return label.width()


def _set_expanded_status(
    status: SelectableFilenameStatus,
    path: Path,
    *,
    position: int,
    total: int,
    checked: bool,
    selection_context: str,
) -> None:
    """ファイル名のみ選択可能とし、ステータス表示は2行固定で維持。"""
    if checked:
        check_color = "#57d8c4" if selection_context == "tagger" else "#ffe58a"
        check_text = f'<span style="color:{check_color}; font-weight:700;">● チェック中</span>'
    else:
        check_text = '<span style="color:#eeeeee;">○ 未チェック</span>'

    controls = (
        "←/→・◁/▷: 前後移動　Space: チェック切替　"
        "ホイール: ズーム　左ドラッグ: 移動　"
        "ダブルクリック: フィット　Esc / × / 背景クリック: 閉じる"
    )
    status.set_filename_line(
        path.name,
        suffix=f"　{position + 1}/{max(1, total)}　{check_text}",
        controls=controls,
    )


class ZoomableImageCanvas(QWidget):
    """
    ライトボックス内の画像表示領域。

    - 初期表示は全体フィット。ただし小さい画像を原寸以上に自動拡大しない。
    - ホイールで1段ごとに1.2倍ズーム。
    - 左ドラッグでパン。
    - ダブルクリックで初期フィットへ戻る。
    """

    background_clicked = Signal()

    ZOOM_FACTOR = 1.2
    MAX_SCALE = 8.0
    # サムネイル一覧のアイテム枠（180x140）に近い縦横比。
    # ライトボックスでは、この比率の「画像下敷きカード」内に画像をfit表示する。
    CARD_ASPECT_RATIO = 180 / 140
    CARD_MARGIN = 10

    def __init__(self) -> None:
        super().__init__()
        self._pixmap: QPixmap | None = None
        self._base_scale: float = 1.0
        self._zoom_step: int = 0
        self._offset = QPointF(0.0, 0.0)
        self._dragging = False
        self._last_mouse_pos = QPointF()
        self._checked = False
        # "edit" は通常の黄色チェック、"tagger" はTagger用の青緑チェック。
        self._selection_context = "edit"
        self.setMouseTracking(True)
        self.setStyleSheet("background: transparent;")

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self.reset_fit()

    def clear_image(self) -> None:
        self._pixmap = None
        self._zoom_step = 0
        self._offset = QPointF(0.0, 0.0)
        self._dragging = False
        self._checked = False
        self._selection_context = "edit"
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def set_checked(
        self,
        checked: bool,
        *,
        selection_context: str = "edit",
    ) -> None:
        """
        ライトボックス上のチェック表示を更新する。

        編集タブでは黄色、Taggerタブではサムネイル欄のTagger用チェックと
        同じ青緑系で描画する。
        """
        normalized_context = "tagger" if selection_context == "tagger" else "edit"
        if (
            self._checked == checked
            and self._selection_context == normalized_context
        ):
            return

        self._checked = checked
        self._selection_context = normalized_context
        self.update()

    def reset_fit(self) -> None:
        self._zoom_step = 0
        self._offset = QPointF(0.0, 0.0)
        self._recalculate_base_scale()
        self._update_cursor()
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._recalculate_base_scale()
        if self._zoom_step == 0:
            self._offset = QPointF(0.0, 0.0)
        else:
            self._constrain_offset()
        self._update_cursor()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._pixmap is None or self._pixmap.isNull():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        card_rect = self._card_rect()

        # 画像の下敷きになるカード領域。サムネイルと同じ比率に固定し、
        # チェック枠もこのカード外周へ出すことで、サムネイル欄と意味を揃える。
        # 暗幕の外側と区別できるよう、内側はさらに暗く、枠は明るく表示する。
        # 画像自体は暗くしない（画像描画前のカード下敷きだけ変更）。
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 190))
        painter.drawRoundedRect(card_rect, 10, 10)

        painter.save()
        painter.setClipRect(card_rect.adjusted(1, 1, -1, -1))
        painter.drawPixmap(self._target_rect(), self._pixmap, QRectF(self._pixmap.rect()))
        painter.restore()

        # 元画像を描き終えてからカード枠を重ね、画像の端にも見えるようにする。
        painter.setPen(QPen(QColor(235, 245, 255, 150), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(card_rect.adjusted(1, 1, -1, -1), 10, 10)

        if self._checked:
            # サムネイル欄の選択文脈と同じ意味・色をライトボックスでも示す。
            # 編集用チェックは黄色、Tagger用対象チェックは青緑系にする。
            # 画像サイズ計算を変えないよう、QSSのborderではなく描画だけで重ねる。
            if self._selection_context == "tagger":
                check_color = QColor("#57d8c4")
            else:
                check_color = QColor("#ffe58a")

            glow_color = QColor(check_color)
            glow_color.setAlpha(85)
            glow_pen = QPen(glow_color, 8)
            painter.setPen(glow_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(card_rect.adjusted(4, 4, -4, -4), 12, 12)

            border_color = QColor(check_color)
            border_color.setAlpha(245)
            pen = QPen(border_color, 4)
            painter.setPen(pen)
            painter.drawRoundedRect(card_rect.adjusted(3, 3, -3, -3), 10, 10)

        painter.end()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._pixmap is None or self._pixmap.isNull():
            event.ignore()
            return

        delta = event.angleDelta().y()
        if delta == 0:
            event.ignore()
            return

        old_scale = self._current_scale()
        old_rect = self._target_rect()
        cursor = event.position()

        if delta > 0:
            candidate_step = self._zoom_step + 1
            if self._scale_for_step(candidate_step) > self.MAX_SCALE + 1e-9:
                event.accept()
                return
            self._zoom_step = candidate_step
        else:
            if self._zoom_step <= 0:
                event.accept()
                return
            self._zoom_step -= 1

        if self._zoom_step == 0:
            # 整数段数0を初期表示として扱い、倍率誤差やパンずれを残さない。
            self._offset = QPointF(0.0, 0.0)
        else:
            source_x = (cursor.x() - old_rect.left()) / old_scale
            source_y = (cursor.y() - old_rect.top()) / old_scale
            new_scale = self._current_scale()
            new_width = self._pixmap.width() * new_scale
            new_height = self._pixmap.height() * new_scale
            center = self._card_rect().center()
            desired_top_left = QPointF(
                cursor.x() - source_x * new_scale,
                cursor.y() - source_y * new_scale,
            )
            self._offset = QPointF(
                desired_top_left.x() + new_width / 2.0 - center.x(),
                desired_top_left.y() + new_height / 2.0 - center.y(),
            )
            self._constrain_offset()

        self._update_cursor()
        self.update()
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._card_rect().contains(event.position()):
                self.background_clicked.emit()
                event.accept()
                return

            if self._can_pan():
                self._dragging = True
                self._last_mouse_pos = event.position()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            delta = event.position() - self._last_mouse_pos
            self._last_mouse_pos = event.position()
            self._offset += delta
            self._constrain_offset()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self._update_cursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._card_rect().contains(event.position()):
            self.reset_fit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _recalculate_base_scale(self) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            self._base_scale = 1.0
            return

        card_rect = self._card_rect()
        fit_scale = min(
            max(1.0, card_rect.width()) / self._pixmap.width(),
            max(1.0, card_rect.height()) / self._pixmap.height(),
        )
        self._base_scale = min(1.0, fit_scale)

    def _scale_for_step(self, step: int) -> float:
        return self._base_scale * (self.ZOOM_FACTOR ** step)

    def _current_scale(self) -> float:
        return self._scale_for_step(self._zoom_step)

    def _card_rect(self) -> QRectF:
        """サムネイルカード比率の、画像下敷き領域を返す。"""
        available_width = max(1.0, self.width() - self.CARD_MARGIN * 2)
        available_height = max(1.0, self.height() - self.CARD_MARGIN * 2)
        available_ratio = available_width / available_height

        if available_ratio > self.CARD_ASPECT_RATIO:
            card_height = available_height
            card_width = card_height * self.CARD_ASPECT_RATIO
        else:
            card_width = available_width
            card_height = card_width / self.CARD_ASPECT_RATIO

        left = (self.width() - card_width) / 2.0
        top = (self.height() - card_height) / 2.0
        return QRectF(left, top, card_width, card_height)

    def _target_rect(self) -> QRectF:
        if self._pixmap is None or self._pixmap.isNull():
            return QRectF()

        scale = self._current_scale()
        width = self._pixmap.width() * scale
        height = self._pixmap.height() * scale
        center = self._card_rect().center() + self._offset
        return QRectF(center.x() - width / 2.0, center.y() - height / 2.0, width, height)

    def _can_pan(self) -> bool:
        target = self._target_rect()
        card = self._card_rect()
        return target.width() > card.width() + 0.5 or target.height() > card.height() + 0.5

    def _constrain_offset(self) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            self._offset = QPointF(0.0, 0.0)
            return

        scale = self._current_scale()
        width = self._pixmap.width() * scale
        height = self._pixmap.height() * scale
        card = self._card_rect()
        max_x = max(0.0, (width - card.width()) / 2.0)
        max_y = max(0.0, (height - card.height()) / 2.0)
        self._offset = QPointF(
            max(-max_x, min(max_x, self._offset.x())),
            max(-max_y, min(max_y, self._offset.y())),
        )

    def _update_cursor(self) -> None:
        if self._dragging:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self._can_pan():
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)


class ImageLightbox(QFrame):
    """アプリ画面内で元画像を拡大確認するライトボックス型オーバーレイ。"""

    closed = Signal()
    previous_requested = Signal()
    next_requested = Signal()
    check_toggle_requested = Signal()
    embedded_preview_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ImageLightbox")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._current_path: Path | None = None

        self.setStyleSheet(
            """
            QFrame#ImageLightbox {
                background: rgba(0, 0, 0, 160);
            }
            QPushButton#LightboxNavButton {
                color: rgba(255, 255, 255, 210);
                background: rgba(0, 0, 0, 60);
                border: 1px solid rgba(255, 255, 255, 65);
                border-radius: 22px;
                font-size: 34px;
                font-weight: bold;
            }
            QPushButton#LightboxNavButton:hover {
                background: rgba(255, 255, 255, 35);
                color: rgba(255, 255, 255, 245);
            }
            QPushButton#LightboxCloseButton {
                color: #ffffff;
                background: rgba(92, 24, 34, 205);
                border: 2px solid rgba(255, 118, 135, 235);
                border-radius: 22px;
                font-size: 28px;
                font-weight: bold;
            }
            QPushButton#LightboxCloseButton:hover {
                background: rgba(150, 35, 52, 235);
                border-color: #ff9aaa;
                color: #ffffff;
            }
            QPushButton#LightboxEmbeddedPreviewButton {
                color: #f1f9ff;
                background: rgba(31, 73, 102, 225);
                border: 2px solid rgba(113, 202, 255, 245);
                border-radius: 11px;
                font-size: 13px;
                font-weight: bold;
                padding: 6px 10px;
            }
            QPushButton#LightboxEmbeddedPreviewButton:hover {
                color: #ffffff;
                background: rgba(49, 108, 147, 240);
                border-color: #a8e2ff;
            }
            QPushButton#LightboxEmbeddedPreviewButton:pressed {
                background: rgba(32, 78, 108, 245);
                border-color: #d0efff;
            }
            QFrame#LightboxStatus {
                color: #eeeeee;
                background: rgba(0, 0, 0, 95);
                border-radius: 7px;
                padding: 6px 12px;
            }
            """
        )

        self.close_button = QPushButton("×")
        self.close_button.setObjectName("LightboxCloseButton")
        self.close_button.setFixedSize(44, 44)
        self.close_button.setToolTip("閉じる")
        self.close_button.clicked.connect(self.close_overlay)

        self.embedded_preview_button = QPushButton("拡大プレビュー", self)
        self.embedded_preview_button.setObjectName("LightboxEmbeddedPreviewButton")
        self.embedded_preview_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.embedded_preview_button.setToolTip(
            "タグ操作を残したまま使える拡大プレビューへ切り替えます"
        )
        self.embedded_preview_button.setFixedSize(132, 42)
        self.embedded_preview_button.clicked.connect(
            self.embedded_preview_requested.emit
        )
        _apply_button_glow(self.close_button, "#ff667d", blur_radius=22.0)
        _apply_button_glow(self.embedded_preview_button, "#63c9ff", blur_radius=24.0)

        self.previous_button = QPushButton("◁")
        self.previous_button.setObjectName("LightboxNavButton")
        self.previous_button.setFixedSize(56, 92)
        self.previous_button.setToolTip("前の画像")
        self.previous_button.clicked.connect(self.previous_requested.emit)

        self.next_button = QPushButton("▷")
        self.next_button.setObjectName("LightboxNavButton")
        self.next_button.setFixedSize(56, 92)
        self.next_button.setToolTip("次の画像")
        self.next_button.clicked.connect(self.next_requested.emit)

        self.canvas = ZoomableImageCanvas()
        self.canvas.setMinimumSize(240, 160)
        self.canvas.background_clicked.connect(self.close_overlay)

        self.status_label = SelectableFilenameStatus(expanded=True)
        self.status_label.setObjectName("LightboxStatus")
        self.status_label.background_clicked.connect(self.close_overlay)

        top_layout = QHBoxLayout()
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addStretch(1)
        top_layout.addWidget(self.close_button)

        center_layout = QHBoxLayout()
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(14)
        center_layout.addWidget(self.previous_button, 0, Qt.AlignmentFlag.AlignVCenter)
        center_layout.addWidget(self.canvas, 1)
        center_layout.addWidget(self.next_button, 0, Qt.AlignmentFlag.AlignVCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 16, 48, 18)
        layout.setSpacing(8)
        layout.addLayout(top_layout)
        layout.addLayout(center_layout, 1)
        layout.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignCenter)

        self.hide()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 切替ボタンは画像レイアウトを押し下げず、右上の×の真下へ浮かせる。
        margin_right = 48
        top = 16 + self.close_button.height() + 10
        x = max(8, self.width() - margin_right - self.embedded_preview_button.width())
        self.embedded_preview_button.move(x, top)
        self.embedded_preview_button.raise_()
        _update_status_label_width(self.status_label, self.width())

    def show_image(
        self,
        path: Path,
        *,
        position: int = 0,
        total: int = 1,
        checked: bool = False,
        selection_context: str = "edit",
    ) -> bool:
        # Spaceでチェック状態だけ切り替えた場合、AppWindow側から同じ画像を
        # show_image() し直される。ここで毎回 set_pixmap() するとズーム/パンが
        # 初期化されてしまうため、同じ画像なら表示状態を維持してチェック表示だけ更新する。
        if self._current_path != path:
            pixmap = QPixmap(str(path))
            if pixmap.isNull():
                return False

            self.canvas.set_pixmap(pixmap)
            self._current_path = path

        is_tagger_context = selection_context == "tagger"
        self.canvas.set_checked(
            checked,
            selection_context="tagger" if is_tagger_context else "edit",
        )

        _update_status_label_width(self.status_label, self.width())
        _set_expanded_status(
            self.status_label,
            path,
            position=position,
            total=total,
            checked=checked,
            selection_context=selection_context,
        )
        self.show()
        self.raise_()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        return True

    def close_overlay(self) -> None:
        if not self.isVisible():
            return
        self.hide()
        self._current_path = None
        self.canvas.clear_image()
        self.closed.emit()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()

        if key == Qt.Key.Key_Escape:
            self.close_overlay()
            event.accept()
            return

        if key == Qt.Key.Key_Left:
            self.previous_requested.emit()
            event.accept()
            return

        if key == Qt.Key.Key_Right:
            self.next_requested.emit()
            event.accept()
            return

        if key == Qt.Key.Key_Space:
            self.check_toggle_requested.emit()
            event.accept()
            return

        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not self.canvas.geometry().contains(event.position().toPoint())
        ):
            self.close_overlay()
            event.accept()
            return
        super().mousePressEvent(event)

class EmbeddedImageViewer(QFrame):
    """
    左側の通常プレビュー＋サムネイル領域へ重ねて使う埋め込み拡大プレビュー。

    画像表示ロジックはライトボックスと同じ ZoomableImageCanvas を共有し、
    元画像のfit表示・ホイールズーム・左ドラッグパン・ダブルクリックfitを揃える。
    半透明背景で通常プレビュー／サムネイルを薄く残し、右側タグパネルは覆わない。
    """

    previous_requested = Signal()
    next_requested = Signal()
    check_toggle_requested = Signal()
    exit_requested = Signal()
    lightbox_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("EmbeddedImageViewer")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._current_path: Path | None = None

        self.setStyleSheet(
            """
            QFrame#EmbeddedImageViewer {
                background: rgba(0, 0, 0, 205);
                border: 1px solid rgba(255, 255, 255, 32);
                border-radius: 10px;
            }
            QPushButton#EmbeddedPreviewNavButton {
                color: rgba(255, 255, 255, 220);
                background: rgba(0, 0, 0, 70);
                border: 1px solid rgba(255, 255, 255, 75);
                border-radius: 20px;
                font-size: 30px;
                font-weight: bold;
            }
            QPushButton#EmbeddedPreviewNavButton:hover {
                background: rgba(255, 255, 255, 42);
                color: #ffffff;
                border-color: rgba(255, 255, 255, 125);
            }
            QPushButton#EmbeddedPreviewCloseButton {
                color: #ffffff;
                background: rgba(92, 24, 34, 205);
                border: 2px solid rgba(255, 118, 135, 235);
                border-radius: 22px;
                font-size: 28px;
                font-weight: bold;
            }
            QPushButton#EmbeddedPreviewCloseButton:hover {
                background: rgba(150, 35, 52, 235);
                border-color: #ff9aaa;
                color: #ffffff;
            }
            QPushButton#EmbeddedPreviewModeButton {
                color: #f1f9ff;
                background: rgba(31, 73, 102, 225);
                border: 2px solid rgba(113, 202, 255, 245);
                border-radius: 11px;
                font-size: 13px;
                font-weight: bold;
                padding: 6px 10px;
            }
            QPushButton#EmbeddedPreviewModeButton:hover {
                color: #ffffff;
                background: rgba(49, 108, 147, 240);
                border-color: #a8e2ff;
            }
            QPushButton#EmbeddedPreviewModeButton:pressed {
                background: rgba(32, 78, 108, 245);
                border-color: #d0efff;
            }
            QFrame#EmbeddedPreviewStatus {
                color: #eeeeee;
                background: rgba(0, 0, 0, 105);
                border: 1px solid rgba(255, 255, 255, 32);
                border-radius: 7px;
                padding: 5px 10px;
            }
            """
        )

        self.close_button = QPushButton("×", self)
        self.close_button.setObjectName("EmbeddedPreviewCloseButton")
        self.close_button.setFixedSize(44, 44)
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.setToolTip("拡大プレビューを閉じる")
        self.close_button.clicked.connect(self.exit_requested.emit)

        self.mode_button = QPushButton("拡大確認表示", self)
        self.mode_button.setObjectName("EmbeddedPreviewModeButton")
        self.mode_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mode_button.setToolTip("全画面の拡大確認表示へ切り替えます")
        self.mode_button.setFixedSize(132, 42)
        self.mode_button.clicked.connect(self.lightbox_requested.emit)
        _apply_button_glow(self.close_button, "#ff667d", blur_radius=22.0)
        _apply_button_glow(self.mode_button, "#63c9ff", blur_radius=24.0)

        self.previous_button = QPushButton("◁")
        self.previous_button.setObjectName("EmbeddedPreviewNavButton")
        self.previous_button.setFixedSize(52, 88)
        self.previous_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.previous_button.setToolTip("前の画像")
        self.previous_button.clicked.connect(self.previous_requested.emit)

        self.next_button = QPushButton("▷")
        self.next_button.setObjectName("EmbeddedPreviewNavButton")
        self.next_button.setFixedSize(52, 88)
        self.next_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.next_button.setToolTip("次の画像")
        self.next_button.clicked.connect(self.next_requested.emit)

        self.canvas = ZoomableImageCanvas()
        self.canvas.setMinimumSize(240, 160)
        self.canvas.installEventFilter(self)
        self.canvas.background_clicked.connect(self.exit_requested.emit)

        self.status_label = SelectableFilenameStatus(expanded=True)
        self.status_label.setObjectName("EmbeddedPreviewStatus")
        self.status_label.background_clicked.connect(self.exit_requested.emit)

        center_layout = QHBoxLayout()
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(10)
        center_layout.addWidget(self.previous_button, 0, Qt.AlignmentFlag.AlignVCenter)
        center_layout.addWidget(self.canvas, 1)
        center_layout.addWidget(self.next_button, 0, Qt.AlignmentFlag.AlignVCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        layout.addLayout(center_layout, 1)
        layout.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignCenter)

        self.hide()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        margin_right = 38
        top = 12
        close_x = max(8, self.width() - margin_right - self.close_button.width())
        self.close_button.move(close_x, top)
        self.close_button.raise_()

        mode_top = top + self.close_button.height() + 10
        mode_x = max(8, self.width() - margin_right - self.mode_button.width())
        self.mode_button.move(mode_x, mode_top)
        self.mode_button.raise_()
        _update_status_label_width(self.status_label, self.width())

    def show_image(
        self,
        path: Path,
        *,
        position: int = 0,
        total: int = 1,
        checked: bool = False,
        selection_context: str = "edit",
        take_focus: bool = True,
    ) -> bool:
        """
        指定画像を表示する。同じ画像のチェック状態だけが変わった場合は、
        ライトボックスと同様にズーム・パン状態を維持する。
        """
        if self._current_path != path:
            pixmap = QPixmap(str(path))
            if pixmap.isNull():
                return False

            self.canvas.set_pixmap(pixmap)
            self._current_path = path

        is_tagger_context = selection_context == "tagger"
        self.canvas.set_checked(
            checked,
            selection_context="tagger" if is_tagger_context else "edit",
        )

        _update_status_label_width(self.status_label, self.width())
        _set_expanded_status(
            self.status_label,
            path,
            position=position,
            total=total,
            checked=checked,
            selection_context=selection_context,
        )
        self.show()
        self.raise_()
        if take_focus:
            self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        return True

    def clear_view(self) -> None:
        self._current_path = None
        self.status_label.clear()
        self.canvas.clear_image()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()

        if key == Qt.Key.Key_Escape:
            self.exit_requested.emit()
            event.accept()
            return

        if key == Qt.Key.Key_Left:
            self.previous_requested.emit()
            event.accept()
            return

        if key == Qt.Key.Key_Right:
            self.next_requested.emit()
            event.accept()
            return

        if key == Qt.Key.Key_Space:
            self.check_toggle_requested.emit()
            event.accept()
            return

        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not self.canvas.geometry().contains(event.position().toPoint())
        ):
            self.exit_requested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.canvas and event.type() == QEvent.Type.MouseButtonPress:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
        return super().eventFilter(watched, event)
