"""TAGエディタ君全画面共通の右ドラッグ・画像送りジェスチャー。

gazou_kiritori の水平判定・橙色の軌跡表示を参考にした共通実装。
表示範囲を跨いでも軌跡を継続し、短い右クリックの既存メニューを維持する。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QContextMenuEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget

# gazou_kiritori の判定値に合わせる。
MIN_HORIZONTAL_PX = 120
MIN_HORIZONTAL_TO_VERTICAL = 1.5
MIN_BBOX_RATIO = 1.3
MAX_ANGLE_DEG = 28.0
MAX_CURVATURE = 1.35
MIN_DIRECTION_CONSISTENCY = 0.80
FADE_DURATION_MS = 600

# 少しの右ドラッグを通常クリックと誤認しないための制限。
CONTEXT_CLICK_TOLERANCE_PX = 12


def classify_horizontal_gesture(points: list[tuple[float, float]]) -> int:
    """+1=右、-1=左、0=不成立。平行性と軌跡の直進性も評価する。"""
    if len(points) < 2:
        return 0

    start_x, start_y = points[0]
    end_x, end_y = points[-1]
    dx_total = end_x - start_x
    dy_total = end_y - start_y
    if abs(dx_total) < MIN_HORIZONTAL_PX:
        return 0
    straight = math.hypot(dx_total, dy_total)
    if straight <= 1e-6:
        return 0

    path_length = 0.0
    horizontal = 0.0
    vertical = 0.0
    min_x = max_x = start_x
    min_y = max_y = start_y
    segments: list[tuple[float, float, float]] = []
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        path_length += length
        horizontal += abs(dx)
        vertical += abs(dy)
        if length > 1e-6:
            segments.append((dx, dy, length))
        min_x = min(min_x, x1)
        max_x = max(max_x, x1)
        min_y = min(min_y, y1)
        max_y = max(max_y, y1)

    unit_x, unit_y = dx_total / straight, dy_total / straight
    consistency = (
        sum(max(0.0, (dx / length) * unit_x + (dy / length) * unit_y)
            for dx, dy, length in segments) / len(segments)
        if segments else 0.0
    )
    angle = math.degrees(math.atan2(abs(dy_total), abs(dx_total)))
    if path_length / straight > MAX_CURVATURE:
        return 0
    if consistency < MIN_DIRECTION_CONSISTENCY:
        return 0
    if horizontal < vertical * MIN_HORIZONTAL_TO_VERTICAL:
        return 0
    if max_x - min_x < (max_y - min_y) * MIN_BBOX_RATIO:
        return 0
    if angle > MAX_ANGLE_DEG:
        return 0
    return 1 if dx_total > 0 else -1


class GestureTrailOverlay(QWidget):
    """全画面に重ねるマウス透過の描画専用レイヤー。"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("MouseGestureTrailOverlay")
        self.setStyleSheet("QWidget#MouseGestureTrailOverlay { background: transparent; border: none; }")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._points: list[QPointF] = []
        self._opacity = 0.0
        self._fade_started = 0.0
        self._fading = False
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(16)
        self._fade_timer.timeout.connect(self._tick_fade)
        self.hide()

    def show_trail(self, points: list[tuple[float, float]]) -> None:
        self._fade_timer.stop()
        self._fading = False
        # マウスイベントのglobal座標から、透明レイヤーの座標へ変換。
        self._points = [QPointF(self.mapFromGlobal(QPoint(round(x), round(y))))
                        for x, y in points]
        self._opacity = 1.0
        self.show()
        self.raise_()
        self.update()

    def fade(self) -> None:
        if len(self._points) < 2:
            self.clear()
            return
        self._fading = True
        self._fade_started = time.monotonic()
        self._fade_timer.start()

    def clear(self) -> None:
        self._fade_timer.stop()
        self._fading = False
        self._opacity = 0.0
        self._points.clear()
        self.hide()

    def _tick_fade(self) -> None:
        elapsed = (time.monotonic() - self._fade_started) * 1000
        t = min(1.0, elapsed / FADE_DURATION_MS)
        # OutQuad: gazou_kiritori の軌跡がすっと薄くなる感触に合わせる。
        self._opacity = (1.0 - t) ** 2
        if t >= 1.0:
            self.clear()
        else:
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        if len(self._points) < 2 or self._opacity <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath(self._points[0])
        for index in range(1, len(self._points)):
            previous = self._points[index - 1]
            current = self._points[index]
            middle = QPointF((previous.x() + current.x()) / 2.0,
                             (previous.y() + current.y()) / 2.0)
            if index == 1:
                path.lineTo(middle)
            else:
                path.quadTo(previous, middle)
        path.lineTo(self._points[-1])
        for color, width in ((QColor(0, 0, 0, int(130 * self._opacity)), 10),
                             (QColor(255, 170, 0, int(220 * self._opacity)), 4)):
            pen = QPen(color, width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPath(path)
        painter.end()


class MouseGestureController:
    """QApplicationイベントフィルタから呼ぶ右ドラッグ処理。

    既存の右クリックは横取りしない。右ボタン押下時にContextMenuイベントが
    発生する環境では解放時まで保留し、通常クリックなら一度だけ復元する。
    """

    def __init__(self, window: QWidget, overlay_parent: QWidget,
                 navigate: Callable[[int], None]):
        self.window = window
        self.navigate = navigate
        self.overlay = GestureTrailOverlay(overlay_parent)
        self.overlay.setGeometry(overlay_parent.rect())
        self._active = False
        self._points: list[tuple[float, float]] = []
        self._pending_context: tuple[QWidget, QPoint, Qt.KeyboardModifiers] | None = None
        self._replaying_context = False
        self._suppress_post_release_context = False

    def resize_overlay(self) -> None:
        self.overlay.setGeometry(self.overlay.parentWidget().rect())
        if self.overlay.isVisible():
            self.overlay.raise_()

    def bring_overlay_forward(self) -> None:
        if self.overlay.isVisible():
            self.overlay.raise_()

    def _eligible(self, obj) -> bool:
        if not isinstance(obj, QWidget):
            return False
        # ダイアログやポップアップなど、メインとは別のトップレベルには侵入しない。
        if obj.window() is not self.window:
            return False
        if QApplication.activeModalWidget() or QApplication.activePopupWidget():
            return False
        return QApplication.activeWindow() is self.window

    def _cancel(self) -> None:
        self._active = False
        self._points.clear()
        self._pending_context = None
        self.overlay.clear()

    def process(self, obj, event) -> bool:
        """True ならイベントを消費した。AppWindow.eventFilter の先頭から呼ぶ。"""
        kind = event.type()
        if kind == QEvent.Type.WindowDeactivate and obj is self.window:
            self._cancel()
            return False

        if kind == QEvent.Type.ContextMenu:
            if self._replaying_context:
                return False
            if not isinstance(obj, QWidget) or obj.window() is not self.window:
                return False
            if self._active and event.reason() == QContextMenuEvent.Reason.Mouse:
                self._pending_context = (obj, event.globalPos(), event.modifiers())
                return True
            if self._suppress_post_release_context and event.reason() == QContextMenuEvent.Reason.Mouse:
                return True
            return False

        if kind not in (QEvent.Type.MouseButtonPress,
                        QEvent.Type.MouseButtonDblClick,
                        QEvent.Type.MouseMove,
                        QEvent.Type.MouseButtonRelease):
            return False
        if not self._active and not self._eligible(obj):
            return False
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            if event.button() != Qt.MouseButton.RightButton:
                return False
            self._suppress_post_release_context = False
            self._active = True
            pos = event.globalPosition()
            self._points = [(pos.x(), pos.y())]
            self._pending_context = None
            self.overlay.clear()
            return False

        if not self._active:
            return False
        if kind == QEvent.Type.MouseMove:
            if not (event.buttons() & Qt.MouseButton.RightButton):
                self._cancel()
                return False
            pos = event.globalPosition()
            point = (pos.x(), pos.y())
            if not self._points or point != self._points[-1]:
                self._points.append(point)
                self.overlay.show_trail(self._points)
            return False

        if event.button() != Qt.MouseButton.RightButton:
            return False
        pos = event.globalPosition()
        point = (pos.x(), pos.y())
        if self._points and point != self._points[-1]:
            self._points.append(point)
        direction = classify_horizontal_gesture(self._points)
        max_distance = (max((math.hypot(x-self._points[0][0], y-self._points[0][1])
                             for x, y in self._points), default=0.0)
                        if self._points else 0.0)
        pending = self._pending_context
        self._active = False
        self._points = []
        self._pending_context = None
        self.overlay.fade()
        if direction:
            self._suppress_post_release_context = True
            self.navigate(direction)
            event.accept()
            return True
        if max_distance > CONTEXT_CLICK_TOLERANCE_PX:
            # 失敗ジェスチャーでコンテキストメニューが出るのは煩わしい。
            self._suppress_post_release_context = True
        elif pending is not None:
            # Qt/Linux等で右ボタン押下時にメニューイベントが届く場合の復元。
            widget, global_pos, modifiers = pending
            self._suppress_post_release_context = True
            QTimer.singleShot(0, lambda w=widget, p=global_pos, m=modifiers:
                              self._replay_context_menu(w, p, m))
        return False

    def _replay_context_menu(self, widget: QWidget, global_pos: QPoint,
                             modifiers: Qt.KeyboardModifiers) -> None:
        try:
            if not widget.isVisible() or widget.window() is not self.window:
                return
            event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                                      widget.mapFromGlobal(global_pos), global_pos, modifiers)
            self._replaying_context = True
            QApplication.sendEvent(widget, event)
        except RuntimeError:
            pass  # クリック直後にウィジェットが破棄された場合
        finally:
            self._replaying_context = False
