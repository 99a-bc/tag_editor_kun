"""タグ並べ替えの描画だけを共用する。MIME・開始判定・ドロップ処理は呼出元が管理。"""
from contextlib import contextmanager

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid


def ghost_pixmap(widget):
    source = widget.grab()
    ghost = QPixmap(source.size())
    ghost.setDevicePixelRatio(source.devicePixelRatio())
    ghost.fill(Qt.GlobalColor.transparent)
    painter = QPainter(ghost)
    painter.setOpacity(.75)
    painter.drawPixmap(QPoint(0, 0), source)
    painter.end()
    return ghost


@contextmanager
def drag_visual(drag, widget, hotspot):
    # grabは開始時の1回だけ。HotSpotはQtの論理座標のまま渡す。
    drag.setPixmap(ghost_pixmap(widget))
    drag.setHotSpot(hotspot)
    shade = QWidget(widget)
    shade.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    shade.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    shade.setStyleSheet("background-color: rgba(0, 0, 0, 55); border: none; border-radius: 5px;")
    shade.setGeometry(widget.rect())
    shade.show()
    try:
        yield
    finally:
        # ドロップ先が一覧を再構築し、元Widgetを破棄する場合もある。
        if isValid(shade):
            shade.hide()
            shade.deleteLater()
