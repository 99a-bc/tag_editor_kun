import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, Qt, QEvent
from PySide6.QtGui import QDrag, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QWidget
from tag_drag_visual import ghost_pixmap, drag_visual
from tag_panel import TagChip, PresetEditTagChip, CURRENT_TAG_REORDER_MIME
from theme import APP_QSS


class TagDragVisualTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(APP_QSS)

    def setUp(self):
        self.widgets = []

    def own(self, widget):
        self.widgets.append(widget)
        widget.show()
        self.app.processEvents()
        return widget

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_pixmap_preserves_size_color_and_75_percent_alpha(self):
        widget = self.own(QLabel("tag"))
        widget.setStyleSheet("background: #a040c0; color: white;")
        widget.resize(100, 30)
        source = widget.grab()
        ghost = ghost_pixmap(widget)
        self.assertEqual(ghost.size(), source.size())
        self.assertEqual(ghost.devicePixelRatio(), source.devicePixelRatio())
        color = ghost.toImage().pixelColor(2, 2)
        original = source.toImage().pixelColor(2, 2)
        self.assertAlmostEqual(color.alpha(), original.alpha() * .75, delta=2)
        for channel in ("red", "green", "blue"):
            self.assertAlmostEqual(getattr(color, channel)(), getattr(original, channel)(), delta=2)

    def test_high_dpi_keeps_logical_size_and_hotspot(self):
        widget = self.own(QLabel("tag"))
        source = QPixmap(200, 60)
        source.setDevicePixelRatio(2)
        source.fill(Qt.GlobalColor.red)
        drag = QDrag(widget)
        with patch.object(widget, "grab", return_value=source) as grab:
            with drag_visual(drag, widget, QPoint(7, 9)):
                self.assertEqual(drag.pixmap().deviceIndependentSize().width(), 100)
                self.assertEqual(drag.hotSpot(), QPoint(7, 9))
            grab.assert_called_once()

    def test_visual_restores_on_exception_without_restyling(self):
        widget = self.own(QLabel("tag"))
        widget.setStyleSheet("background: #123456;")
        before = widget.styleSheet()
        with self.assertRaises(RuntimeError):
            with drag_visual(QDrag(widget), widget, QPoint(1, 2)):
                self.assertTrue(any(c.isVisible() for c in widget.findChildren(QWidget)))
                raise RuntimeError("cancel")
        self.assertEqual(widget.styleSheet(), before)
        self.assertFalse(any(c.isVisible() for c in widget.findChildren(QWidget)))

    def check_chip(self, kind, action):
        chip = self.own(kind("hakurei reimu"))
        chip._drag_start_pos = QPoint(9, 5)
        before = chip.tag_button.styleSheet()
        checked = chip.tag_button.isChecked()
        calls = []
        def execute(drag, _actions):
            calls.append(drag.pixmap())
            self.assertEqual(drag.hotSpot(), QPoint(9, 5))
            self.assertEqual(drag.pixmap().deviceIndependentSize().toSize(), chip.tag_button.size())
            self.assertEqual(bytes(drag.mimeData().data(CURRENT_TAG_REORDER_MIME)), b"hakurei reimu")
            self.assertEqual(chip.tag_button.isChecked(), checked)
            self.assertTrue(any(c.isVisible() for c in chip.tag_button.findChildren(QWidget)))
            return action
        with patch.object(QDrag, "exec", new=execute):
            chip._start_drag()
        self.assertEqual(len(calls), 1)
        self.assertEqual(chip.tag_button.styleSheet(), before)
        self.assertEqual(chip.tag_button.isChecked(), checked)
        self.assertFalse(any(c.isVisible() for c in chip.tag_button.findChildren(QWidget)))

    def test_editor_cancel_restores_source(self):
        self.check_chip(TagChip, Qt.DropAction.IgnoreAction)

    def test_editor_success_restores_source(self):
        self.check_chip(TagChip, Qt.DropAction.MoveAction)

    def test_preset_cancel_ghost_excludes_delete_button(self):
        self.check_chip(PresetEditTagChip, Qt.DropAction.IgnoreAction)

    def test_preset_success_restores_source(self):
        self.check_chip(PresetEditTagChip, Qt.DropAction.MoveAction)
