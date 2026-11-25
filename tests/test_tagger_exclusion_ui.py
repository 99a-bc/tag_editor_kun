import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent, QDropEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from app_window import AppWindow
from danbooru_tag_dictionary import DanbooruTag, DanbooruTagDictionary
from models import ImageItem
from settings import AppSettings, load_app_settings, save_app_settings
from tag_color_dictionary import TagColorDictionary, tag_chip_qss
from tag_panel import AddTagDialog, TAG_CHIP_QSS
from tagger.exclusions import ExclusionRuleSet, load_exclusions, save_exclusions
from tagger.result import TagCandidate, TaggerResult
from tagger_exclusion_panel import (
    ExclusionController,
    ExclusionDeleteDialog,
    ExclusionEditDialog,
    ExclusionPanel,
    RULE_MIME,
)
from tagger_panel import TaggerPanel
from theme import APP_QSS


class ExclusionUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication(["exclusion-tests"])
        cls.qt.setStyleSheet(APP_QSS)
        cls.dictionary = TagColorDictionary.load()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.temp.name)
        self.widgets = []
        self.callback_errors = []
        self.enterContext(patch("sys.excepthook", side_effect=lambda _kind, error, _trace: self.callback_errors.append(repr(error))))
        self.controller = ExclusionController()
        self.controller.path = self.root / "exclusions.json"
        self.warning = self.enterContext(patch("tagger_exclusion_panel.QMessageBox.warning"))
        self.question = self.enterContext(patch("tagger_exclusion_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes))

    def tearDown(self):
        # 既存の折返しチップが予約したレイアウト処理を、破棄前に完了させる。
        QTest.qWait(45)
        for widget in reversed(self.widgets):
            if isinstance(widget, ExclusionEditDialog):
                widget.completion.dispose()
            widget.close()
            widget.deleteLater()
        self.qt.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.qt.processEvents()
        self.temp.cleanup()
        self.assertEqual(self.callback_errors, [], "Qtコールバック内の例外")

    def own(self, widget):
        self.widgets.append(widget)
        return widget

    def dialog(self, rules=(), **kwargs):
        return self.own(ExclusionEditDialog(rules=rules, dictionary=self.dictionary, **kwargs))

    def interact_with_delete_dialog(self, action):
        def interact():
            dialog = QApplication.activeModalWidget()
            try:
                self.assertIsInstance(dialog, ExclusionDeleteDialog)
                action(dialog)
            finally:
                if isinstance(dialog, QDialog) and dialog.isVisible():
                    dialog.reject()
        QTimer.singleShot(0, interact)

    def approve_delete_dialog(self):
        self.interact_with_delete_dialog(lambda dialog: dialog.delete_button.click())

    def panel(self):
        return self.own(ExclusionPanel(self.controller, self.dictionary))

    def tagger(self):
        with patch("tagger_panel.pixai_model_is_cached", return_value=False):
            panel = self.own(TaggerPanel())
        panel.set_exclusion_context(self.controller.rules, path=self.controller.path)
        self.controller = panel.exclusion_controller
        panel.resize(850, 620)
        panel.show()
        self.qt.processEvents()
        return panel

    def result(self, panel):
        result = TaggerResult(Path("one.png"), [TagCandidate("white shirt", .9), TagCandidate("smile", .8)])
        panel.set_current_result("one.png", result, status="generated")
        self.qt.processEvents()
        return result

    def test_dialog_comma_preview_individual_modes_and_preservation(self):
        dialog = self.dialog()
        dialog.input_line.setText("shirt, socks")
        self.assertEqual(len(dialog.rows), 2)
        first = dialog.drafts[0]
        dialog.rows[first.key].mode.setCurrentIndex(1)
        dialog.input_line.setText("SHIRT, socks, smile")
        self.assertEqual([(d.tag, d.mode) for d in dialog.drafts], [("SHIRT", "word"), ("socks", "exact"), ("smile", "exact")])
        self.assertEqual(dialog.drafts[0].key, first.key)
        QTest.mouseClick(dialog.all_word, Qt.MouseButton.LeftButton)
        self.assertTrue(all(d.mode == "word" for d in dialog.drafts))
        dialog.rows[dialog.drafts[1].key].mode.setCurrentIndex(0)
        self.assertEqual(dialog.drafts[1].mode, "exact")

    def test_dialog_deduplicates_normalized_input_and_removes_rows(self):
        dialog = self.dialog()
        dialog.input_line.setText("white_shirt, WHITE shirt, socks")
        self.assertEqual(len(dialog.drafts), 2)
        key = dialog.drafts[0].key
        QTest.mouseClick(dialog.rows[key].remove, Qt.MouseButton.LeftButton)
        self.assertEqual([d.tag for d in dialog.drafts], ["socks"])
        self.assertEqual(dialog.input_line.text(), "socks")

    def test_edit_preserves_same_name_different_modes_and_stable_ids(self):
        rules = ExclusionRuleSet()
        a, b = rules.add("shirt"), rules.add("shirt", "word")
        self.controller.rules = rules
        dialog = self.dialog(rules.rules, validator=lambda drafts: self.controller.edited_rules({a.rule_id, b.rule_id}, drafts))
        self.assertEqual(len(dialog.rows), 2)
        dialog.input_line.setText("SHIRT, shirt")
        self.assertEqual([(d.key, d.mode) for d in dialog.drafts], [(a.rule_id, "exact"), (b.rule_id, "word")])
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_edit_rename_keeps_rule_id_and_mode(self):
        rule = self.controller.rules.add("shirt", "word")
        dialog = self.dialog([rule])
        dialog.input_line.setText("socks")
        new = self.controller.edited_rules({rule.rule_id}, dialog.drafts)[0]
        self.assertEqual((new.rule_id, new.tag, new.match_mode), (rule.rule_id, "socks", "word"))

    def test_duplicate_against_existing_rule_blocks_dialog_accept(self):
        self.controller.rules.add("shirt")
        dialog = self.dialog(validator=lambda drafts: self.controller.edited_rules(set(), drafts))
        dialog.input_line.setText("SHIRT")
        dialog.accept()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertTrue(dialog.error_label.text())

    def test_panel_add_edit_and_cancel_use_same_dialog_and_preserve_identity(self):
        panel = self.panel()

        def add(dialog):
            dialog.input_line.setText("shirt")
            dialog.set_all_modes("word")
            dialog.accept()
            return dialog.result()

        QTimer.singleShot(0, lambda: add(QApplication.activeModalWidget()))
        panel.add_button.click()
        original = self.controller.rules.rules[0]
        panel.select_rule(original.rule_id)

        def edit(dialog):
            self.assertEqual(dialog.drafts[0].mode, "word")
            dialog.input_line.setText("white shirt")
            dialog.accept()
            return dialog.result()

        QTimer.singleShot(0, lambda: edit(QApplication.activeModalWidget()))
        panel.edit_button.click()
        edited = self.controller.rules.rules[0]
        self.assertEqual((edited.rule_id, edited.registered_order, edited.manual_order),
                         (original.rule_id, original.registered_order, original.manual_order))
        self.assertEqual((edited.tag, edited.match_mode), ("white shirt", "word"))
        before = self.controller.path.read_bytes()
        QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
        panel.add_button.click()
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_completion_uses_existing_controller_and_disposes(self):
        dictionary = DanbooruTagDictionary([DanbooruTag("white shirt", 0, 100, ())])
        dialog = self.dialog(tag_dictionary=dictionary)
        self.assertTrue(dialog.completion.is_available())
        dialog.show()
        dialog.input_line.setFocus()
        self.qt.processEvents()
        dialog.input_line.setText("white")
        dialog.completion._refresh()
        candidate = dialog.completion._popup.current_candidate()
        self.assertIsNotNone(candidate)
        dialog.completion._apply_candidate(candidate)
        self.assertEqual(dialog.drafts[0].tag, "white shirt")
        dialog.reject()
        self.assertTrue(dialog.completion._disposed)

    def test_existing_add_dialog_trigger_feature_is_unchanged(self):
        dialog = self.own(AddTagDialog("既存タグ追加", tag_color_dictionary=self.dictionary))
        dialog.input_line.setText("shirt, socks")
        dialog.front_tag_checkbox.setChecked(True)
        dialog._select_front_tag("socks")
        self.assertEqual(dialog.front_tag(), "socks")
        dialog.reject()

    def test_one_rule_per_row_modes_outside_chips_and_dictionary_colors(self):
        rules = self.controller.rules
        rules.add("hakurei reimu")
        rules.add("shirt", "word")
        panel = self.panel()
        panel.resize(350, 450)
        panel.show()
        self.qt.processEvents()
        self.assertEqual(panel.list.count(), 2)
        rows = list(panel.rows.values())
        self.assertGreater(rows[1].geometry().top(), rows[0].geometry().top())
        self.assertGreater(rows[0].mode.x(), rows[0].chip.geometry().right())
        self.assertFalse(hasattr(rows[0], "remove"))
        self.assertEqual(rows[0].chip.styleSheet(), tag_chip_qss(self.dictionary.style_for_tag("hakurei reimu")) or TAG_CHIP_QSS)
        self.assertNotIn("テキストで編集", [b.text() for b in panel.findChildren(type(panel.add_button))])

    def test_search_jumps_between_same_name_rules_by_id(self):
        a = self.controller.rules.add("shirt")
        b = self.controller.rules.add("shirt", "word")
        self.controller.rules.add("white shirt")
        panel = self.panel()
        panel.search.line.setText("SHIRT")
        panel.search.exact_check.setChecked(True)
        self.assertEqual(panel.matches, [a.rule_id, b.rule_id])
        self.assertEqual(panel.current_match, a.rule_id)
        panel.search.next_button.click()
        self.assertEqual(panel.current_match, b.rule_id)
        self.assertTrue(panel.rows[b.rule_id].chip._search_current)
        self.assertFalse(panel.rows[a.rule_id].chip._search_current)
        panel.search.prev_button.click()
        self.assertEqual(panel.current_match, a.rule_id)
        panel.search.clear_button.click()
        self.assertEqual(panel.matches, [])

    def test_sorting_and_manual_order_roundtrip_keep_ids_and_registration(self):
        b = self.controller.rules.add("socks")
        a = self.controller.rules.add("shirt")
        panel = self.panel()
        panel.set_sort("name")
        self.assertEqual([r.rule_id for r in panel.ordered_rules()], [a.rule_id, b.rule_id])
        panel.set_sort("registered")
        panel.move_rule(a.rule_id, 0)
        self.assertEqual(panel.sort_mode, "manual")
        self.assertEqual([r.rule_id for r in panel.ordered_rules()], [a.rule_id, b.rule_id])
        loaded = load_exclusions(self.controller.path).rule_set.rules
        self.assertEqual({r.rule_id: r.registered_order for r in loaded}, {a.rule_id: 1, b.rule_id: 0})
        panel.set_sort("registered")
        self.assertEqual([r.rule_id for r in panel.ordered_rules()], [b.rule_id, a.rule_id])
        panel.set_sort("category")
        self.assertEqual(panel.ordered_rules(), sorted(self.controller.rules.rules, key=lambda r: (*self.dictionary.sort_key_for_tag(r.tag), r.registered_order)))

    def test_colorless_group_order_matches_both_tag_editor_lists(self):
        from tag_panel import TagPanel

        tags = ["white shirt", "see-through clothes", "1girl", "Purple hat",
                "full body", "armor", "rating:q"]
        expected = ["rating:q", "1girl", "full body", "armor", "Purple hat",
                    "see-through clothes", "white shirt"]
        editor = self.own(TagPanel())
        item = ImageItem(self.root / "image.png", tags=list(tags), checked=True)
        editor.set_items([item])
        editor.resize(1100, 700)
        editor.show()

        rules = {tag: self.controller.rules.add(tag) for tag in tags}
        second_armor = self.controller.rules.add("ARMOR", "word")
        tagger = self.tagger()
        exclusions = tagger.exclusion_panel

        editor._apply_current_tag_sort_mode("color_group")
        editor._apply_all_tag_sort_mode("color_group")
        exclusions.set_sort("category")
        self.qt.processEvents()

        self.assertEqual(list(editor._current_tag_buttons), expected)
        self.assertEqual(list(editor._all_tag_rows), expected)
        expected_ids = [rules[tag].rule_id for tag in expected]
        expected_ids.insert(expected.index("armor") + 1, second_armor.rule_id)
        self.assertEqual(
            [exclusions.list.item(i).data(Qt.ItemDataRole.UserRole)
             for i in range(exclusions.list.count())],
            expected_ids,
        )

        editor._apply_current_tag_sort_mode("name_asc")
        editor._apply_all_tag_sort_mode("name_asc")
        self.assertEqual(list(editor._current_tag_buttons), sorted(tags, key=str.casefold))
        self.assertEqual(list(editor._all_tag_rows), sorted(tags, key=str.casefold))
        self.assertEqual(item.tags, tags)
        self.assertFalse(item.dirty)

    def test_drag_mime_rejects_editor_foreign_list_and_external_sources(self):
        rule = self.controller.rules.add("shirt")
        panel = self.panel()
        mime = panel.list.rule_mime(rule.rule_id)
        self.assertTrue(panel.list.accepts_mime(mime, panel.list))
        self.assertFalse(panel.list.accepts_mime(mime, None))
        editor = QMimeData()
        editor.setData("application/x-tag-editor-current-tag-reorder", b"shirt")
        self.assertFalse(panel.list.accepts_mime(editor, panel.list))
        foreign = QMimeData()
        foreign.setData(RULE_MIME, f"foreign:{rule.rule_id}".encode())
        self.assertFalse(panel.list.accepts_mime(foreign, panel.list))
        event = QDropEvent(QPointF(3, 3), Qt.DropAction.MoveAction, editor,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        panel.list.dropEvent(event)
        self.assertFalse(event.isAccepted())

    def test_valid_drop_reorders_and_persists_via_drop_event(self):
        from unittest.mock import Mock
        first = self.controller.rules.add("shirt")
        last = self.controller.rules.add("socks")
        panel = self.panel()
        panel.resize(350, 450)
        panel.show()
        self.qt.processEvents()
        event = Mock()
        event.mimeData.return_value = panel.list.rule_mime(last.rule_id)
        event.source.return_value = panel.list
        event.position.return_value = QPointF(2, 2)
        panel.list.dropEvent(event)
        event.acceptProposedAction.assert_called_once()
        self.assertEqual(panel.sort_mode, "manual")
        self.assertEqual([r.rule_id for r in panel.ordered_rules()], [last.rule_id, first.rule_id])
        self.assertEqual([r.rule_id for r in sorted(load_exclusions(self.controller.path).rule_set.rules,
                                                  key=lambda r: r.manual_order)], [last.rule_id, first.rule_id])

    def test_cancel_delete_preserves_json_and_other_rule_delete_keeps_exclusion(self):
        a, b = self.controller.rules.add("shirt"), self.controller.rules.add("shirt", "word")
        save_exclusions(self.controller.rules, self.controller.path)
        before = self.controller.path.read_bytes()
        for dismiss in (
            lambda dialog: dialog.cancel_button.click(),
            lambda dialog: QTest.keyClick(dialog, Qt.Key.Key_Escape),
            lambda dialog: dialog.close(),
            lambda dialog: QTest.keyClick(dialog.cancel_button, Qt.Key.Key_Return),
            lambda dialog: QTest.keyClick(dialog.delete_button, Qt.Key.Key_Return),
        ):
            self.interact_with_delete_dialog(dismiss)
            self.assertFalse(self.controller.remove_rules({a.rule_id}))
            self.assertEqual(self.controller.rules.rules, (a, b))
            self.assertEqual(self.controller.path.read_bytes(), before)

        self.approve_delete_dialog()
        self.assertTrue(self.controller.remove_rules({a.rule_id}))
        self.assertEqual(self.controller.rules.rules, (b,))
        self.assertEqual(load_exclusions(self.controller.path).rule_set.rules, (b,))
        self.assertTrue(self.controller.decision("shirt").excluded)

    def test_delete_dialog_size_scroll_and_button_visibility(self):
        rules = ExclusionRuleSet()
        for index in range(200):
            tag = f"tag {index}" if index else "long tag " * 150 + "end"
            rules.add(tag, "word" if index % 2 else "exact")
        panel = self.tagger()
        screen = panel.screen()
        available = screen.availableGeometry()
        sizes = []

        for count in (1, 10, 89, 200):
            with self.subTest(count=count):
                selected = rules.rules[:count]
                dialog = self.own(ExclusionDeleteDialog(selected, panel))
                dialog.show()
                QTest.qWait(30)
                self.assertTrue(available.contains(dialog.frameGeometry()))
                sizes.append(dialog.size())
                self.assertEqual(dialog.message.text(), f"選択した{count}件の除外ルールを削除しますか？")
                self.assertEqual(dialog.delete_button.text(), f"{count}件を削除")
                self.assertIs(dialog.focusWidget(), dialog.cancel_button)
                self.assertTrue(dialog.cancel_button.isDefault())
                self.assertFalse(dialog.delete_button.autoDefault())
                self.assertEqual(dialog.listing.topLevelItemCount(), count)
                first = dialog.listing.topLevelItem(0)
                self.assertEqual(first.text(0), selected[0].tag)
                self.assertEqual(first.text(1), "完全一致")
                self.assertEqual(first.data(0, Qt.ItemDataRole.UserRole), selected[0].rule_id)
                self.assertIn(selected[0].rule_id, first.toolTip(0))
                self.assertEqual(dialog.listing.horizontalScrollBar().maximum(), 0)

                buttons = (dialog.cancel_button, dialog.delete_button)
                positions = [button.mapToGlobal(QPoint()) for button in buttons]
                for button, position in zip(buttons, positions):
                    self.assertTrue(button.isVisible())
                    self.assertTrue(available.contains(QRect(position, button.size())))
                if count >= 89:
                    scroll = dialog.listing.verticalScrollBar()
                    self.assertGreater(scroll.maximum(), 0)
                    scroll.setValue(scroll.maximum())
                    self.qt.processEvents()
                    last = dialog.listing.topLevelItem(count - 1)
                    self.assertTrue(dialog.listing.viewport().rect().intersects(dialog.listing.visualItemRect(last)))
                    self.assertEqual([button.mapToGlobal(QPoint()) for button in buttons], positions)
                dialog.close()
        self.assertTrue(all(size == sizes[0] for size in sizes))

        small_screen = QRect(available.topLeft(), QSize(420, 300))
        with patch.object(type(screen), "availableGeometry", return_value=small_screen):
            dialog = self.own(ExclusionDeleteDialog(rules.rules, panel))
            dialog.show()
            QTest.qWait(30)
            self.assertTrue(small_screen.contains(dialog.frameGeometry()))
            self.assertGreater(dialog.listing.verticalScrollBar().maximum(), 0)
            for button in (dialog.cancel_button, dialog.delete_button):
                self.assertTrue(button.isVisible())
                self.assertTrue(small_screen.contains(QRect(button.mapToGlobal(QPoint()), button.size())))
            dialog.close()

    def test_selected_list_delete_save_failure_preserves_rules_and_json(self):
        panel, ids = self.selection_panel()
        panel.select_rule(ids[0])
        panel.select_rule(ids[2], Qt.KeyboardModifier.ControlModifier)
        save_exclusions(self.controller.rules, self.controller.path)
        before = self.controller.rules
        original = self.controller.path.read_bytes()
        spy = QSignalSpy(self.controller.rules_changed)

        self.approve_delete_dialog()
        with patch("tagger_exclusion_panel.save_exclusions", side_effect=PermissionError("locked")):
            panel.delete_button.click()
        self.assertIs(self.controller.rules, before)
        self.assertEqual(self.controller.path.read_bytes(), original)
        self.assertEqual(panel.selected_ids(), {ids[0], ids[2]})
        self.assertEqual(spy.count(), 0)
        self.warning.assert_called_once()

    def test_failed_save_does_not_publish_rules_or_change_file(self):
        original = self.controller.rules.add("shirt")
        save_exclusions(self.controller.rules, self.controller.path)
        before = self.controller.path.read_bytes()
        spy = QSignalSpy(self.controller.rules_changed)
        with patch("tagger_exclusion_panel.save_exclusions", side_effect=PermissionError("locked")):
            self.assertFalse(self.controller.add_rule("socks", "exact"))
        self.assertEqual(self.controller.rules.rules, (original,))
        self.assertEqual(self.controller.path.read_bytes(), before)
        self.assertEqual(spy.count(), 0)
        self.warning.assert_called_once()

    def test_corrupt_settings_notify_and_disable_edits_without_overwrite(self):
        panel = self.tagger()
        self.controller.path.write_bytes(b"broken")
        loaded = load_exclusions(self.controller.path)
        panel.set_exclusion_context(loaded.rule_set, loaded.issues, self.controller.path)
        self.qt.processEvents()
        self.warning.assert_called_once()
        self.assertFalse(panel.exclusion_panel.add_button.isEnabled())
        self.assertTrue(panel.exclusion_panel.error_label.text())
        self.assertFalse(self.controller.add_rule("shirt", "exact"))
        self.assertEqual(self.controller.path.read_bytes(), b"broken")

    def test_candidate_menu_dimming_and_refresh_without_reinference_or_mutation(self):
        panel = self.tagger()
        result = self.result(panel)
        original = deepcopy(result)
        first = panel._candidate_rows[0]
        self.controller.add_rule("white shirt", "exact")
        self.assertIs(panel._candidate_rows[0], first)
        self.assertTrue(first.excluded)
        self.assertEqual(first.opacity.opacity(), .33)
        self.approve_delete_dialog()
        self.controller.remove_rules({r.rule_id for r in self.controller.rules.rules})
        self.assertFalse(first.excluded)
        self.assertEqual(first.opacity.opacity(), 1)
        self.assertEqual(result, original)

    def test_right_click_does_not_open_candidate_menu(self):
        panel = self.tagger()
        self.result(panel)
        row = panel._candidate_rows[0]
        for name in ("tag_label", "bar", "score_label"):
            widget = getattr(row, name)
            with self.subTest(widget=name):
                with patch.object(row, "show_menu") as execute:
                    QApplication.sendEvent(widget, QContextMenuEvent(
                        QContextMenuEvent.Reason.Mouse, QPoint(2, 2), widget.mapToGlobal(QPoint(2, 2))))
                    execute.assert_not_called()
    def test_permanent_menu_add_delete_identifies_multiple_matches(self):
        panel = self.tagger()
        self.result(panel)
        self.controller.add_rule("shirt", "word")
        def add():
            dialog = QApplication.activeModalWidget()
            self.assertEqual(dialog.drafts[0].tag, "white shirt")
            dialog.accept()
        QTimer.singleShot(0, add)
        menu = self.controller.candidate_menu(["white shirt"], panel)
        menu.actions()[0].trigger()
        self.assertEqual(len(self.controller.matching_rules_for_tags(["white shirt", "WHITE_SHIRT"])), 2)
        def remove():
            from PySide6.QtWidgets import QListWidget
            dialog = QApplication.activeModalWidget()
            listing = dialog.findChild(QListWidget)
            self.assertEqual(listing.count(), 2)
            listing.item(0).setCheckState(Qt.CheckState.Checked)
            dialog.accept()
        QTimer.singleShot(0, remove)
        menu.actions()[1].trigger()
        self.assertTrue(panel._candidate_rows[0].excluded)
        self.assertEqual(len(self.controller.rules.rules), 1)
        menu.deleteLater()

    def test_color_toggle_affects_rule_chips_without_dimming_them(self):
        self.controller.rules.add("hakurei reimu")
        panel = self.tagger()
        panel.set_tag_color_enabled(False)
        row = next(iter(panel.exclusion_panel.rows.values()))
        self.assertEqual(row.chip.styleSheet(), TAG_CHIP_QSS)
        self.assertIsNone(row.graphicsEffect())
        panel.set_tag_color_enabled(True)
        row = next(iter(panel.exclusion_panel.rows.values()))
        self.assertEqual(row.chip.styleSheet(), tag_chip_qss(self.dictionary.style_for_tag("hakurei reimu")) or TAG_CHIP_QSS)

    def test_close_reopen_and_setting_roundtrip(self):
        panel = self.tagger()
        panel.candidate_splitter.setSizes([400, 390])
        panel._remember_exclusion_width(0, 1)
        saved_width = panel._exclusion_width
        opened = panel.candidate_scroll.width()
        panel.exclusion_toggle.click()
        self.qt.processEvents()
        self.assertGreater(panel.candidate_scroll.width(), opened)
        self.assertFalse(panel.exclusion_panel.isVisible())
        self.assertEqual(panel._exclusion_width, saved_width)
        data = panel.settings_dict()
        settings = AppSettings.from_dict({f"tagger_{key}": value for key, value in data.items()})
        path = self.root / "settings.json"
        save_app_settings(settings, path)
        settings = load_app_settings(path)
        self.assertFalse(settings.tagger_exclusion_panel_open)
        self.assertEqual(settings.tagger_exclusion_panel_width, saved_width)
        panel.restore_persisted_settings(
            model_repo=settings.tagger_model_repo, device=settings.tagger_device,
            include_general=True, general_threshold=.35, include_character=False,
            character_threshold=.85, include_copyright=False, copyright_expanded=False,
            apply_mode="append", exclusion_panel_open=False,
            exclusion_panel_width=saved_width, exclusion_sort_mode="manual")
        self.qt.processEvents()
        self.assertEqual(panel.settings_dict()["exclusion_sort_mode"], "manual")
        panel.exclusion_toggle.click()
        self.qt.processEvents()
        self.assertAlmostEqual(panel.exclusion_panel.width(), saved_width, delta=3)

    def test_narrow_candidate_geometry_keeps_scores_and_bars_inside(self):
        panel = self.tagger()
        self.result(panel)
        # 共通設定カードの最小幅とは独立に、候補表示の狭幅回帰を検証する。
        panel.setFixedSize(800, 600)
        panel._restore_exclusion_width()
        self.qt.processEvents()
        self.assertEqual(panel.width(), 800)
        for row in panel._candidate_rows:
            self.assertLessEqual(row.score_label.geometry().right(), row.width())
            self.assertGreater(row.bar.width(), 0)
            self.assertGreater(row.tag_label.width(), 0)
        self.assertEqual(panel.candidate_scroll.horizontalScrollBar().maximum(), 0)


    def test_fresh_panel_restores_closed_width_without_overwriting_it(self):
        panel = self.tagger()
        panel.restore_persisted_settings(
            model_repo="SmilingWolf/wd-eva02-large-tagger-v3", device="cpu",
            include_general=True, general_threshold=.35, include_character=False,
            character_threshold=.85, include_copyright=False, copyright_expanded=False,
            apply_mode="overwrite", exclusion_panel_open=False,
            exclusion_panel_width=420, exclusion_sort_mode="name")
        self.qt.processEvents()
        self.assertEqual(panel._exclusion_width, 420)
        self.assertFalse(panel.exclusion_toggle.isChecked())
        panel.exclusion_toggle.click()
        self.qt.processEvents()
        self.assertAlmostEqual(panel.exclusion_panel.width(), 420, delta=3)

    def test_app_settings_handler_persists_new_keys_and_validates_old_defaults(self):
        from types import SimpleNamespace
        state = SimpleNamespace(app_settings=AppSettings())
        path = self.root / "settings.json"
        with patch("app_window.save_app_settings", side_effect=lambda settings: save_app_settings(settings, path)):
            AppWindow._on_tagger_settings_changed(state, {
                "model_repo": "pixai-labs/pixai-tagger-v0.9", "device": "cpu",
                "include_general": True, "general_threshold": .55,
                "include_character": False, "character_threshold": .9,
                "apply_mode": "append", "exclusion_panel_open": False,
                "exclusion_panel_width": 410, "exclusion_sort_mode": "manual"})
        loaded = load_app_settings(path)
        self.assertEqual((loaded.tagger_exclusion_panel_open, loaded.tagger_exclusion_panel_width,
                          loaded.tagger_exclusion_sort_mode), (False, 410, "manual"))
        self.assertEqual(loaded.tagger_general_threshold, .55)
        invalid = AppSettings.from_dict({"tagger_exclusion_panel_open": "false",
                                        "tagger_exclusion_panel_width": True,
                                        "tagger_exclusion_sort_mode": {}})
        self.assertEqual((invalid.tagger_exclusion_panel_open, invalid.tagger_exclusion_panel_width,
                          invalid.tagger_exclusion_sort_mode), (True, 350, "registered"))

    def test_ui_rule_save_updates_existing_apply_pipeline(self):
        # 既存B-2の本体・履歴Harnessを使用し、UIから保存したルールを接続する。
        from test_tagger_apply_exclusions import ApplyHarness
        app = ApplyHarness([ImageItem(self.root / "image.png")])
        app.set_candidates(0, ["shirt", "socks"])
        panel = self.tagger()
        panel.set_exclusion_context(app.tagger_exclusion_rules, path=self.controller.path)
        panel.exclusion_rules_changed.connect(lambda rules: AppWindow._on_tagger_exclusion_rules_changed(app, rules))
        self.controller.add_rule("shirt", "word")
        app._apply_tagger_stage1("overwrite")
        self.assertEqual(app.items[0].tags, ["socks"])
        self.approve_delete_dialog()
        self.controller.remove_rules({r.rule_id for r in self.controller.rules.rules})
        app._apply_tagger_stage1("overwrite")
        self.assertEqual(app.items[0].tags, ["shirt", "socks"])

    def selection_panel(self):
        for tag, mode in (("shirt", "exact"), ("shirt", "word"), ("socks", "exact"), ("hakurei reimu", "exact")):
            self.controller.rules.add(tag, mode)
        panel = self.panel()
        panel.resize(380, 460)
        panel.show()
        self.qt.processEvents()
        return panel, list(panel.rows)

    def test_plain_chip_and_row_click_toggle_rules(self):
        panel, ids = self.selection_panel()
        QTest.mouseClick(panel.rows[ids[0]].chip, Qt.MouseButton.LeftButton)
        self.assertEqual(panel.selected_ids(), {ids[0]})
        QTest.mouseClick(panel.rows[ids[1]], Qt.MouseButton.LeftButton, pos=QPoint(2, 20))
        self.assertEqual(panel.selected_ids(), {ids[0], ids[1]})
        self.assertTrue(panel.rows[ids[1]].is_selected)
        self.assertTrue(panel.rows[ids[0]].is_selected)
        QTest.mouseClick(panel.rows[ids[0]].chip, Qt.MouseButton.LeftButton)
        self.assertEqual(panel.selected_ids(), {ids[1]})
        self.assertTrue(panel.edit_button.isEnabled())
        self.assertTrue(panel.delete_button.isEnabled())

    def test_ctrl_click_adds_and_removes_same_name_rules_independently(self):
        panel, ids = self.selection_panel()
        QTest.mouseClick(panel.rows[ids[0]].chip, Qt.MouseButton.LeftButton)
        QTest.mouseClick(panel.rows[ids[1]].chip, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(panel.selected_ids(), {ids[0], ids[1]})
        QTest.mouseClick(panel.rows[ids[0]], Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, QPoint(2, 20))
        self.assertEqual(panel.selected_ids(), {ids[1]})

    def test_shift_range_uses_anchor_and_current_display_order(self):
        panel, ids = self.selection_panel()
        QTest.mouseClick(panel.rows[ids[0]].chip, Qt.MouseButton.LeftButton)
        QTest.mouseClick(panel.rows[ids[2]].chip, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(panel.selected_ids(), set(ids[:3]))
        QTest.mouseClick(panel.rows[ids[1]], Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, QPoint(2, 20))
        self.assertEqual(panel.selected_ids(), set(ids[:2]))
        panel.set_sort("name")
        ordered = [r.rule_id for r in panel.ordered_rules()]
        anchor = ordered.index(ids[0])
        QTest.mouseClick(panel.rows[ordered[0]].chip, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(panel.selected_ids(), set(ordered[:anchor + 1]))

    def test_viewport_clicks_share_ctrl_shift_selection_rules(self):
        panel, ids = self.selection_panel()
        viewport = panel.list.viewport()
        def click(index, modifiers=Qt.KeyboardModifier.NoModifier):
            point = panel.list.visualItemRect(panel.items_by_id[ids[index]]).center()
            QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, modifiers, point)
        click(0)
        click(1, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(panel.selected_ids(), set(ids[:2]))
        click(3, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(panel.selected_ids(), set(ids[1:]))

    def test_selected_dashed_frame_preserves_category_chip_style(self):
        panel, ids = self.selection_panel()
        row = panel.rows[ids[3]]
        style = row.chip.styleSheet()
        unselected = row.grab().toImage()
        QTest.mouseClick(row.chip, Qt.MouseButton.LeftButton)
        self.qt.processEvents()
        self.assertTrue(row.is_selected)
        self.assertEqual(row.chip.styleSheet(), style)
        self.assertNotEqual(row.grab().toImage(), unselected)
        QTest.mouseClick(panel.rows[ids[1]].chip, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(row.is_selected)
        self.assertTrue(panel.rows[ids[1]].is_selected)

    def test_selection_survives_search_sort_and_controller_refresh(self):
        panel, ids = self.selection_panel()
        panel.select_rule(ids[0])
        panel.select_rule(ids[1], Qt.KeyboardModifier.ControlModifier)
        panel.search.line.setText("shirt")
        panel.search.exact_check.setChecked(True)
        panel.set_sort("category")
        self.controller.changed.emit()
        self.assertEqual(panel.selected_ids(), set(ids[:2]))
        self.assertTrue(all(panel.rows[key].is_selected for key in ids[:2]))
        self.assertEqual(panel.list.anchor_id, ids[1])

    def test_multiple_selected_rules_edit_and_delete_as_independent_ids(self):
        panel, ids = self.selection_panel()
        panel.select_rule(ids[0])
        panel.select_rule(ids[1], Qt.KeyboardModifier.ControlModifier)
        def edit(dialog):
            self.assertEqual({d.key for d in dialog.drafts}, set(ids[:2]))
            dialog.reject()
        QTimer.singleShot(0, lambda: edit(QApplication.activeModalWidget()))
        panel.edit_button.click()

        def confirm(dialog):
            rows = [dialog.listing.topLevelItem(i) for i in range(dialog.listing.topLevelItemCount())]
            self.assertEqual([row.data(0, Qt.ItemDataRole.UserRole) for row in rows], ids[:2])
            self.assertEqual([row.text(1) for row in rows], ["完全一致", "単語一致"])
            dialog.delete_button.click()
        self.interact_with_delete_dialog(confirm)
        panel.delete_button.click()
        self.assertEqual({r.rule_id for r in self.controller.rules.rules}, set(ids[2:]))

    def test_header_order_matches_panel_order(self):
        panel = self.tagger()
        self.assertLess(panel.candidate_title.geometry().right(), panel.exclusion_toggle.x())

    def test_only_tag_click_selects_and_right_click_opens_even_when_excluded(self):
        panel = self.tagger()
        self.result(panel)
        row = panel._candidate_rows[0]
        for excluded in (False, True):
            self.controller.commit([])
            if excluded:
                self.controller.add_rule("white shirt", "exact")
            with patch.object(row, "show_menu") as show:
                QTest.mouseClick(row.tag_label, Qt.MouseButton.LeftButton)
                show.assert_not_called()
                QTest.mouseClick(row.tag_label, Qt.MouseButton.RightButton)
                show.assert_called_once()
                show.reset_mock()
                for widget in (row.bar, row.score_label, row):
                    QTest.mouseClick(widget, Qt.MouseButton.RightButton)
                    QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
                show.assert_not_called()
        self.assertIn("左クリック", row.tag_label.toolTip())

    def test_candidate_hover_cursor_background_border_and_dimming_coexist(self):
        panel = self.tagger()
        self.result(panel)
        row = panel._candidate_rows[0]
        tag = row.tag_label
        self.assertEqual(tag.cursor().shape(), Qt.CursorShape.PointingHandCursor)
        QTest.mouseMove(panel.exclusion_toggle)
        self.qt.processEvents()
        before = tag.grab().toImage()
        QTest.mouseMove(tag, tag.rect().center())
        self.qt.processEvents()
        self.assertTrue(tag.underMouse())
        self.assertNotEqual(tag.grab().toImage(), before)
        self.assertIn("background: #2b2f33", tag.styleSheet())
        self.assertIn("border-color: #505961", tag.styleSheet())
        self.controller.add_rule("white shirt", "exact")
        self.assertIn("background: #2b2f33", tag.styleSheet())
        self.assertIsNone(tag.graphicsEffect())
        self.assertEqual(row.bar.graphicsEffect().opacity(), .33)
        self.assertEqual(row.score_label.graphicsEffect().opacity(), .33)

    def test_menu_position_stays_on_screen_at_bottom_right(self):
        from unittest.mock import Mock
        panel = self.tagger()
        self.result(panel)
        row = panel._candidate_rows[0]
        screen = row.tag_label.screen().availableGeometry()
        menu = Mock()
        menu.sizeHint.return_value = QSize(240, 180)
        position = row.menu_position(menu, screen.bottomRight())
        self.assertTrue(screen.contains(QRect(position, QSize(240, 180))))

    def test_inline_mode_change_persists_and_updates_candidate_without_inference(self):
        rule = self.controller.rules.add("shirt", "exact")
        panel = self.tagger()
        result = self.result(panel)
        original = deepcopy(result)
        self.assertFalse(panel._candidate_rows[0].excluded)
        panel.exclusion_panel.select_rule(rule.rule_id)
        panel.exclusion_panel.rows[rule.rule_id].mode.setCurrentIndex(1)
        updated = self.controller.rules.rules[0]
        self.assertEqual((updated.rule_id, updated.match_mode), (rule.rule_id, "word"))
        self.assertTrue(panel._candidate_rows[0].excluded)
        self.assertEqual(panel.exclusion_panel.rows[rule.rule_id].mode.currentData(), "word")
        self.assertEqual(panel.exclusion_panel.selected_ids(), {rule.rule_id})
        self.assertEqual(load_exclusions(self.controller.path).rule_set.rules, (updated,))
        self.assertEqual(result, original)

    def test_inline_duplicate_is_rejected_without_merging_or_deleting(self):
        first = self.controller.rules.add("shirt", "exact")
        self.controller.rules.add("SHIRT", "word")
        save_exclusions(self.controller.rules, self.controller.path)
        before_rules = self.controller.rules.rules
        before_json = self.controller.path.read_bytes()
        panel = self.panel()
        spy = QSignalSpy(self.controller.rules_changed)
        panel.rows[first.rule_id].mode.setCurrentIndex(1)
        self.assertEqual(panel.rows[first.rule_id].mode.currentData(), "exact")
        self.assertEqual(self.controller.rules.rules, before_rules)
        self.assertEqual(self.controller.path.read_bytes(), before_json)
        self.assertEqual(spy.count(), 0)
        self.warning.assert_called_once()

    def test_inline_save_failure_restores_ui_internal_state_and_json(self):
        rule = self.controller.rules.add("shirt", "exact")
        save_exclusions(self.controller.rules, self.controller.path)
        before = self.controller.path.read_bytes()
        panel = self.tagger()
        self.result(panel)
        rows = panel.exclusion_panel
        rows.select_rule(rule.rule_id)
        with patch("tagger_exclusion_panel.save_exclusions", side_effect=PermissionError("locked")):
            rows.rows[rule.rule_id].mode.setCurrentIndex(1)
        self.assertEqual(rows.rows[rule.rule_id].mode.currentData(), "exact")
        self.assertEqual(self.controller.rules.rules, (rule,))
        self.assertEqual(self.controller.path.read_bytes(), before)
        self.assertEqual(rows.selected_ids(), {rule.rule_id})
        self.assertFalse(panel._candidate_rows[0].excluded)
        self.warning.assert_called_once()

    def test_combo_popup_does_not_change_selection_or_start_drag(self):
        panel, ids = self.selection_panel()
        panel.select_rule(ids[1])
        combo = panel.rows[ids[0]].mode
        with patch.object(panel.list, "start_rule_drag") as drag:
            QTest.mouseClick(combo, Qt.MouseButton.LeftButton)
            self.assertEqual(panel.selected_ids(), {ids[1]})
            QTest.keyClick(combo, Qt.Key.Key_Escape)
            self.assertEqual(panel.selected_ids(), {ids[1]})
            drag.assert_not_called()
        self.assertIsNone(panel.rows[ids[0]].chip.start)

    def test_combo_popup_selection_commits_without_affecting_selected_rows(self):
        panel, ids = self.selection_panel()
        panel.select_rule(ids[1])
        combo = panel.rows[ids[2]].mode
        with patch.object(panel.list, "start_rule_drag") as drag:
            QTest.mouseClick(combo, Qt.MouseButton.LeftButton)
            view = combo.view()
            self.assertTrue(view.isVisible())
            QTest.keyClick(view, Qt.Key.Key_Down)
            QTest.keyClick(view, Qt.Key.Key_Return)
            self.qt.processEvents()
            drag.assert_not_called()
        self.assertEqual(panel.rows[ids[2]].mode.currentData(), "word")
        self.assertEqual(panel.selected_ids(), {ids[1]})
        saved = next(r for r in load_exclusions(self.controller.path).rule_set.rules if r.rule_id == ids[2])
        self.assertEqual(saved.match_mode, "word")


    def test_combo_popup_mouse_choice_commits(self):
        panel, ids = self.selection_panel()
        combo = panel.rows[ids[2]].mode
        QTest.mouseClick(combo, Qt.MouseButton.LeftButton)
        # Qtは開いた直後のマウスリリースを無視するため、実際の選択操作を分ける。
        QTest.qWait(200)
        view = combo.view()
        point = view.visualRect(view.model().index(1, 0)).center()
        QTest.mouseMove(view.viewport(), point)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.qt.processEvents()
        self.assertEqual(panel.rows[ids[2]].mode.currentData(), "word")

    def test_modifier_chip_motion_does_not_start_drag(self):
        panel, ids = self.selection_panel()
        for modifier in (Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.ShiftModifier):
            chip = panel.rows[ids[0]].chip
            with patch.object(panel.list, "start_rule_drag") as start:
                QTest.mousePress(chip, Qt.MouseButton.LeftButton, modifier)
                QTest.mouseMove(chip, QPoint(2, 3))
                QTest.mouseRelease(chip, Qt.MouseButton.LeftButton, modifier)
                start.assert_not_called()

    def test_inline_combo_and_chip_fit_narrow_panel(self):
        panel, _ids = self.selection_panel()
        panel.resize(320, 460)
        self.qt.processEvents()
        for row in panel.rows.values():
            self.assertGreater(row.mode.x(), row.chip.geometry().right())
            self.assertFalse(hasattr(row, "remove"))
            self.assertLess(row.mode.geometry().right(), row.width())
            self.assertEqual(row.mode.count(), 2)

    def test_candidate_toggle_ctrl_shift_and_row_identity(self):
        panel = self.tagger()
        result = TaggerResult(Path("one.png"), [TagCandidate("shirt", .9), TagCandidate("SHIRT", .8), TagCandidate("smile", .7)])
        panel.set_current_result("one.png", result, status="generated")
        a, b, c = panel._candidate_rows
        QTest.mouseClick(a.tag_label, Qt.MouseButton.LeftButton)
        QTest.mouseClick(b.tag_label, Qt.MouseButton.LeftButton)
        self.assertEqual(panel._selected_candidates, {a, b})
        QTest.mouseClick(b.tag_label, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(panel._selected_candidates, {a})
        QTest.mouseClick(c.tag_label, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(panel._selected_candidates, {a, b, c})
        self.assertIn("3", panel.selection_count.text())
        self.assertFalse(self.controller.rules.rules)

    def test_candidate_context_target_selection_and_single_unselected(self):
        panel = self.tagger()
        self.result(panel)
        a, b = panel._candidate_rows
        panel.select_candidate(a, Qt.KeyboardModifier.NoModifier)
        self.assertEqual(panel.candidate_menu_targets(a), ["white shirt"])
        self.assertEqual(panel.candidate_menu_targets(b), ["smile"])
        panel.select_candidate(b, Qt.KeyboardModifier.NoModifier)
        self.assertEqual(panel.candidate_menu_targets(a), ["white shirt", "smile"])


    def test_candidate_selection_and_dimming_independent(self):
        panel = self.tagger()
        self.result(panel)
        row = panel._candidate_rows[0]
        panel.select_candidate(row, Qt.KeyboardModifier.NoModifier)
        self.controller.add_rule(row.candidate.tag, "exact")
        self.assertTrue(row.tag_label.is_selected)
        self.assertEqual(row.opacity.opacity(), .33)
        panel.clear_candidate_selection()
        self.assertFalse(row.tag_label.is_selected)
        self.assertEqual(row.opacity.opacity(), .33)

    def test_candidate_selection_clears_on_escape_blank_image_run_reset(self):
        panel = self.tagger()
        self.result(panel)
        row = panel._candidate_rows[0]
        for action in (lambda: QTest.keyClick(row.tag_label, Qt.Key.Key_Escape),
                       lambda: QTest.mouseClick(panel.candidate_container, Qt.MouseButton.LeftButton, pos=QPoint(2, 2)),
                       lambda: panel.set_current_index(5),
                       lambda: panel.set_running(True),
                       lambda: panel.reset_button.click()):
            panel.set_running(False)
            QTest.mouseClick(row.tag_label, Qt.MouseButton.LeftButton)
            self.assertTrue(panel._selected_candidates)
            action()
            self.assertFalse(panel._selected_candidates)

    def test_menu_cancel_and_dialog_cancel_clear_all_selection(self):
        panel = self.tagger()
        self.result(panel)
        a, b = panel._candidate_rows
        for action_index in (None, 0, 1):
            self.controller.add_rule("shirt", "word") if not self.controller.rules.rules else None
            panel.select_candidate(a, Qt.KeyboardModifier.NoModifier)
            panel.select_candidate(b, Qt.KeyboardModifier.NoModifier)
            def close_menu():
                menu = QApplication.activePopupWidget()
                if action_index is not None:
                    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
                    menu.actions()[action_index].trigger()
                menu.close()
            QTimer.singleShot(0, close_menu)
            QTest.mouseClick(a.tag_label, Qt.MouseButton.RightButton)
            self.assertFalse(panel._selected_candidates)
            self.assertEqual(len(self.controller.rules.rules), 1)

    def test_bulk_add_dialog_modes_and_cancel_preserve_rules(self):
        panel = self.tagger()
        def accept():
            dialog = QApplication.activeModalWidget()
            self.assertEqual(len(dialog.drafts), 2)
            dialog.rows[dialog.drafts[1].key].mode.setCurrentIndex(1)
            dialog.accept()
        QTimer.singleShot(0, accept)
        panel.exclusion_panel.open_dialog(tags=["shirt", "SHIRT", "smile"])
        self.assertEqual([r.match_mode for r in self.controller.rules.rules], ["exact", "word"])
        self.assertEqual(len(load_exclusions(self.controller.path).rule_set.rules), 2)

    def test_rule_collection_deduplicates_ids_not_modes(self):
        one = self.controller.rules.add("shirt", "word")
        two = self.controller.rules.add("white shirt", "exact")
        self.controller.rules.add("unrelated")
        rules = self.controller.matching_rules_for_tags(["white shirt", "blue shirt", "WHITE_SHIRT"])
        self.assertEqual({r.rule_id for r in rules}, {one.rule_id, two.rule_id})

    def test_left_and_right_panel_selection_independent(self):
        self.controller.rules.add("shirt")
        panel = self.tagger()
        self.result(panel)
        key = self.controller.rules.rules[0].rule_id
        panel.exclusion_panel.select_rule(key)
        panel.select_candidate(panel._candidate_rows[0], Qt.KeyboardModifier.NoModifier)
        panel.clear_candidate_selection()
        self.assertEqual(panel.exclusion_panel.selected_ids(), {key})


    def test_bulk_delete_save_failure_preserves_all_rules_and_json(self):
        panel = self.tagger()
        self.controller.add_rule("shirt", "word")
        self.controller.add_rule("unrelated", "exact")
        original = self.controller.path.read_bytes()
        before = self.controller.rules
        def select():
            from PySide6.QtWidgets import QListWidget
            dialog = QApplication.activeModalWidget()
            listing = dialog.findChild(QListWidget)
            self.assertEqual(listing.count(), 1)
            listing.item(0).setCheckState(Qt.CheckState.Checked)
            dialog.accept()
        QTimer.singleShot(0, select)
        with patch("tagger_exclusion_panel.save_exclusions", side_effect=PermissionError("locked")):
            self.controller.choose_rules_to_remove(["white shirt", "blue shirt"], panel)
        self.assertIs(self.controller.rules, before)
        self.assertEqual(self.controller.path.read_bytes(), original)
        self.question.assert_called_once()
        self.warning.assert_called_once()

    def test_candidate_gap_preserves_selection_bottom_blank_clears(self):
        panel = self.tagger()
        self.result(panel)
        a, b = panel._candidate_rows
        panel.select_candidate(a, Qt.KeyboardModifier.NoModifier)
        panel.select_candidate(b, Qt.KeyboardModifier.NoModifier)
        point = QPoint(a.tag_label.width() // 2, a.geometry().bottom() + 1)
        self.assertLess(point.y(), b.geometry().top())
        QTest.mouseClick(panel.candidate_container, Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(panel._selected_candidates, {a, b})
        QTest.mouseClick(panel.candidate_container, Qt.MouseButton.LeftButton,
                         pos=QPoint(20, panel.candidate_container.height() - 10))
        self.assertFalse(panel._selected_candidates)

    def test_rule_viewport_pressed_mouse_move_never_range_selects(self):
        from PySide6.QtGui import QMouseEvent
        panel, ids = self.selection_panel()
        chip = panel.rows[ids[0]].chip
        QTest.mousePress(chip, Qt.MouseButton.LeftButton)
        before = panel.selected_ids()
        point = panel.list.visualItemRect(panel.items_by_id[ids[-1]]).center()
        move = QMouseEvent(QEvent.Type.MouseMove, QPointF(point), QPointF(panel.list.viewport().mapToGlobal(point)),
                           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(panel.list.viewport(), move)
        self.assertEqual(panel.selected_ids(), before)
        QTest.mouseRelease(chip, Qt.MouseButton.LeftButton)
        self.assertEqual(panel.selected_ids(), {ids[0]})

    def test_rule_drag_cancel_blocks_selection_changes(self):
        panel, ids = self.selection_panel()
        panel.select_rule(ids[0])
        before = panel.selected_ids()
        def cancel(*args):
            self.assertTrue(panel.list.drag_active)
            panel.select_rule(ids[2], Qt.KeyboardModifier.ShiftModifier)
            return Qt.DropAction.IgnoreAction
        with patch("tagger_exclusion_panel.QDrag.exec", side_effect=cancel):
            panel.list.start_rule_drag(ids[1])
        self.assertFalse(panel.list.drag_active)
        self.assertEqual(panel.selected_ids(), before)

    def test_chip_motion_starts_reorder_without_selection_change(self):
        panel, ids = self.selection_panel()
        chip = panel.rows[ids[0]].chip
        with patch.object(panel.list, "start_rule_drag") as start:
            QTest.mousePress(chip, Qt.MouseButton.LeftButton)
            QTest.mouseMove(chip, QPoint(chip.width() - 2, 4))
            QTest.mouseRelease(chip, Qt.MouseButton.LeftButton)
            start.assert_called_once()
        self.assertFalse(panel.selected_ids())

    def test_common_search_excerpt_and_clear_keep_geometry(self):
        from tag_panel import SearchHighlightButton
        text = "very long sleeved white shirt"
        button = self.own(SearchHighlightButton("very long slee...", full_text=text))
        button.resize(140, 30)
        button.show()
        before = button.sizeHint()
        button.set_search_marker("shirt", current=True)
        display = button.search_display_text(110)
        self.assertIn("shirt", display)
        self.assertTrue(display.startswith("…"))
        self.assertLessEqual(button.fontMetrics().horizontalAdvance(display), 110)
        self.assertEqual(button.sizeHint(), before)
        button.set_search_marker("")
        self.assertEqual(button.search_display_text(110), "very long slee...")
        self.assertEqual(button.text(), "very long slee...")

    def test_exclusion_search_hidden_suffix_and_jump(self):
        self.controller.rules.add("very long sleeved white shirt")
        self.controller.rules.add("another very long white shirt")
        panel = self.panel()
        panel.resize(330, 400)
        panel.show()
        self.qt.processEvents()
        widths = {key: row.chip.width() for key, row in panel.rows.items()}
        panel.search.line.setText("shirt")
        self.assertEqual(len(panel.matches), 2)
        first = panel.current_match
        for row in panel.rows.values():
            self.assertIn("shirt", row.chip.search_display_text(90))
        panel.jump(1)
        self.assertNotEqual(panel.current_match, first)
        self.assertTrue(panel.rows[panel.current_match].chip._search_current)
        self.assertFalse(panel.rows[first].chip._search_current)
        self.qt.processEvents()
        self.assertEqual(widths, {key: row.chip.width() for key, row in panel.rows.items()})
        panel.search.line.clear()
        self.assertFalse(panel.matches)

    def test_tag_chip_full_source_preserved_for_shared_search(self):
        from tag_panel import TagChip, AllTagChip
        text = "very long " * 10 + "shirt"
        chip = self.own(TagChip(text))
        chip.tag_button.set_search_marker("shirt")
        self.assertIn("shirt", chip.tag_button.search_display_text(100))
        self.assertEqual(chip.tag, text)
        all_chip = self.own(AllTagChip(text, 3))
        all_chip.tag_button.set_search_marker("shirt")
        self.assertIn("shirt", all_chip.tag_button.search_display_text(100))
        all_chip.tag_button.set_search_marker("")
        self.assertEqual(all_chip.tag_button.search_display_text(100), all_chip.tag_button.text())

    def test_preset_search_controller_uses_full_tag_for_excerpt(self):
        from tag_panel import SearchHighlightButton, PresetTagSearchBar, PresetTagSearchController
        from PySide6.QtWidgets import QScrollArea
        text = "very long " * 10 + "shirt"
        button = self.own(SearchHighlightButton("very long...", full_text=text))
        bar = self.own(PresetTagSearchBar())
        scroll = self.own(QScrollArea())
        controller = PresetTagSearchController(bar, lambda: [(text, button)], lambda: scroll)
        bar.line.setText("shirt")
        self.assertEqual(controller._matches, [text])
        self.assertIn("shirt", button.search_display_text(100))
        bar.line.clear()
        self.assertEqual(button.search_display_text(100), "very long...")

    def test_candidate_menu_keeps_permanent_operations_first(self):
        from PySide6.QtWidgets import QPushButton
        panel = self.tagger()
        self.result(panel)
        menu = self.controller.candidate_menu(["white shirt", "smile"], panel)
        self.assertEqual([a.text() for a in menu.actions()[:2]], ["恒久除外リストに追加...", "恒久除外ルールを削除..."])
        self.assertTrue(menu.actions()[2].isSeparator())
        self.assertEqual(len(menu.actions()), 9)
        self.assertEqual([b.text() for b in panel.exclusion_panel.findChildren(QPushButton)
                          if "今回のみ" in b.text()], [])
        menu.deleteLater()

    def test_permanent_bulk_menu_success_clears_selection_and_dims(self):
        panel = self.tagger()
        self.result(panel)
        for row in panel._candidate_rows:
            panel.select_candidate(row, Qt.KeyboardModifier.NoModifier)
        def choose():
            menu = QApplication.activePopupWidget()
            QTimer.singleShot(0, lambda: QApplication.activeModalWidget().accept())
            menu.actions()[0].trigger()
            menu.close()
        QTimer.singleShot(0, choose)
        QTest.mouseClick(panel._candidate_rows[0].tag_label, Qt.MouseButton.RightButton)
        self.assertFalse(panel._selected_candidates)
        self.assertTrue(all(row.excluded for row in panel._candidate_rows))
        self.assertEqual(len(load_exclusions(self.controller.path).rule_set.rules), 2)

    def test_rule_ghost_uses_chip_press_offset_preserves_selection(self):
        from PySide6.QtGui import QDrag
        panel, ids = self.selection_panel()
        panel.select_rule(ids[0])
        row = panel.rows[ids[0]]
        origin = row.chip.mapToGlobal(QPoint(3, 4))
        before = panel.selected_ids()
        for action in (Qt.DropAction.IgnoreAction, Qt.DropAction.MoveAction):
            def execute(drag, _actions):
                self.assertEqual(drag.hotSpot(), row.chip.mapFromGlobal(origin))
                self.assertEqual(drag.pixmap().deviceIndependentSize().toSize(), row.chip.size())
                self.assertTrue(panel.list.accepts_mime(drag.mimeData(), panel.list))
                self.assertEqual(panel.selected_ids(), before)
                return action
            with patch.object(QDrag, "exec", new=execute):
                panel.list.start_rule_drag(ids[0], origin)
            self.assertFalse(panel.list.drag_active)
            self.assertEqual(panel.selected_ids(), before)

    def test_rule_marker_matches_drop_boundary_and_clears(self):
        from unittest.mock import Mock
        panel, ids = self.selection_panel()
        event = Mock()
        event.mimeData.return_value = panel.list.rule_mime(ids[0])
        event.source.return_value = panel.list
        rect = panel.list.visualItemRect(panel.items_by_id[ids[1]])
        for y, index in ((rect.top() + 2, 1), (rect.bottom() - 2, 2)):
            event.position.return_value = QPointF(10, y)
            panel.list.dragMoveEvent(event)
            self.assertTrue(panel.list.insertion_marker.isVisible())
            self.assertEqual(panel.list.insertion_index(QPoint(10, y)), index)
            boundary = panel.list.visualItemRect(panel.list.item(index)).top()
            self.assertEqual(panel.list.insertion_marker.y(), boundary - 2)
        panel.list.dropEvent(event)
        self.assertFalse(panel.list.insertion_marker.isVisible())
        self.assertEqual([r.rule_id for r in panel.ordered_rules()][:2], [ids[1], ids[0]])
        panel.list.dragMoveEvent(event)
        panel.list.dragLeaveEvent(Mock())
        self.assertFalse(panel.list.insertion_marker.isVisible())

    def test_rule_marker_cancel_and_click_only(self):
        from PySide6.QtGui import QDrag
        panel, ids = self.selection_panel()
        chip = panel.rows[ids[0]].chip
        with patch.object(panel.list, "start_rule_drag") as start:
            QTest.mouseClick(chip, Qt.MouseButton.LeftButton)
            start.assert_not_called()
        before = panel.selected_ids()
        panel.list._drop_point = QPoint(2, 2)
        panel.list.refresh_insertion_marker()
        with patch.object(QDrag, "exec", return_value=Qt.DropAction.IgnoreAction):
            panel.list.start_rule_drag(ids[0])
        self.assertFalse(panel.list.insertion_marker.isVisible())
        self.assertEqual(panel.selected_ids(), before)

    def test_rule_chip_uses_remaining_width_and_short_names_fit(self):
        for tag in ("timestamp", "border", "very long sleeved white shirt with many more words"):
            self.controller.rules.add(tag)
        panel = self.panel()
        panel.resize(350, 450)
        panel.show()
        self.qt.processEvents()
        from PySide6.QtWidgets import QPushButton
        for row in panel.rows.values():
            self.assertEqual(row.chip.width(), min(row.chip.sizeHint().width(), row.width() - 8 - 5 - row.mode.width()))
            self.assertFalse(hasattr(row, "handle"))
            self.assertFalse(any(button.text() == "×" for button in row.findChildren(QPushButton)))
            if row.tag in ("timestamp", "border"):
                self.assertEqual(row.chip.text(), row.tag)
            else:
                self.assertNotEqual(row.chip.text(), row.tag)
                row.chip.set_search_marker("words", current=True)
                self.assertIn("words", row.chip.search_display_text(row.chip.width() - 20))

    def test_natural_chip_width_resize_and_search_restore(self):
        for tag in ("shirt", "socks", "border", "timestamp", "very long white shirt with many words at the end"):
            self.controller.rules.add(tag)
        panel = self.panel()
        panel.resize(650, 450)
        panel.show()
        self.qt.processEvents()
        long_row = list(panel.rows.values())[-1]
        wide = long_row.chip.width()
        for row in list(panel.rows.values())[:-1]:
            self.assertEqual(row.chip.width(), row.chip.sizeHint().width())
            self.assertEqual(row.chip.text(), row.tag)
            self.assertLess(row.chip.geometry().right(), row.mode.x() - 20)
        panel.resize(320, 450)
        QTest.qWait(60)
        self.qt.processEvents()
        self.assertLess(long_row.chip.width(), wide)
        self.assertNotEqual(long_row.chip.text(), long_row.tag)
        panel.search.line.setText("end")
        self.assertIn("end", long_row.chip.search_display_text(long_row.chip.width() - 20))
        panel.search.line.clear()
        panel.resize(850, 450)
        QTest.qWait(60)
        self.qt.processEvents()
        self.assertEqual(long_row.chip.text(), long_row.tag)

    def test_preset_modes_geometry_toggle_reopen_and_preserve_entries(self):
        from tag_panel import TagPresetPanel
        from tag_presets import TagPresetState, TagPresetEntry
        state = TagPresetState(presets=[], selected_preset_id="")
        state.ensure_minimum_slots()
        state.selected_preset().tags = [TagPresetEntry("shirt", True), TagPresetEntry("socks", False), TagPresetEntry("border", True)]
        before = deepcopy(state.selected_preset().tags)
        panel = self.own(TagPresetPanel(state))
        panel.resize(700, 600)
        panel.show()
        for mode in (False, True, False):
            panel.set_compact_tags(mode)
            self.qt.processEvents()
            QTest.qWait(30)
            self.assertEqual(panel.tag_compact_scroll.isVisible(), mode)
            self.assertEqual(panel.tag_list_scroll.isVisible(), not mode)
            widgets = [button.parentWidget() for _, button in panel._search_chips]
            if mode:
                self.assertEqual(widgets[0].y(), widgets[1].y())
            else:
                self.assertLess(widgets[0].geometry().bottom(), widgets[1].y())
                self.assertEqual(widgets[0].x(), widgets[1].x())
                self.assertLess(widgets[0].width(), panel.tag_list_scroll.width() // 2)
            panel.hide()
            panel.show()
            self.qt.processEvents()
            self.assertEqual(panel.tag_compact_scroll.isVisible(), mode)
            self.assertEqual(state.selected_preset().tags, before)

    def test_app_preset_setting_has_single_source_and_roundtrips(self):
        path = self.root / "settings.json"
        settings = AppSettings()
        settings.tag_preset_compact_enabled = False
        save_app_settings(settings, path)
        self.enterContext(patch("app_window.load_app_settings", side_effect=lambda: load_app_settings(path)))
        self.enterContext(patch("app_window.save_app_settings", side_effect=lambda value: save_app_settings(value, path)))
        # 旧実装では補完設定適用時に別ファイル由来の値を読んでしまう。
        other = AppSettings()
        other.tag_preset_compact_enabled = True
        reread = self.enterContext(patch("tag_panel.load_app_settings", return_value=other, create=True))
        window = self.own(AppWindow())
        self.assertFalse(window.tag_panel.preset_panel._compact_tags)
        for mode in (True, False):
            window.app_settings.tag_preset_compact_enabled = mode
            save_app_settings(window.app_settings, path)
            window._apply_app_settings()
            self.assertEqual(window.tag_panel.preset_panel._compact_tags, mode)
            window.app_settings = load_app_settings(path)
            window._apply_app_settings()
            self.assertEqual(window.tag_panel.preset_panel._compact_tags, mode)
        reread.assert_not_called()

    def test_shared_lookup_menu_labels_copy_full_tag_and_exact_urls(self):
        from PySide6.QtWidgets import QMenu
        from tag_context_menu import add_tag_lookup_actions
        menu = self.own(QMenu())
        tag = "  white shirt_(a&b)  "
        previous = self.qt.clipboard().text()
        try:
            add_tag_lookup_actions(menu, tag)
            self.assertEqual([a.text() for a in menu.actions()], ["コピー", "Googleで検索", "Danbooruで検索", "Danbooru Wikiを開く", "Gelbooruで検索", "Gelbooru Wikiで検索"])
            menu.actions()[0].trigger()
            self.assertEqual(self.qt.clipboard().text(), tag)
            expected = [
                "https://www.google.com/search?q=++white+shirt_%28a%26b%29++",
                "https://danbooru.donmai.us/posts?tags=white_shirt_%28a%26b%29",
                "https://danbooru.donmai.us/wiki_pages/white_shirt_%28a%26b%29",
                "https://gelbooru.com/index.php?page=post&s=list&tags=white_shirt_%28a%26b%29",
                "https://gelbooru.com/index.php?page=wiki&s=list&search=white_shirt_%28a%26b%29",
            ]
            with patch("tag_context_menu.QDesktopServices.openUrl") as opened:
                for action, url in zip(menu.actions()[1:], expected):
                    action.trigger()
                    from PySide6.QtCore import QUrl
                    self.assertEqual(opened.call_args.args[0], QUrl(url))
                self.assertEqual(opened.call_count, 5)
        finally:
            self.qt.clipboard().setText(previous)

    def test_candidate_copy_uses_clicked_tag_and_clears_selection(self):
        panel = self.tagger()
        self.result(panel)
        a, b = panel._candidate_rows
        self.controller.add_rule(a.candidate.tag, "exact")
        original = self.controller.rules.rules
        for row in (a, b):
            panel.select_candidate(row, Qt.KeyboardModifier.NoModifier)
        previous = self.qt.clipboard().text()
        def copy():
            menu = QApplication.activePopupWidget()
            self.assertEqual(menu.actions()[3].text(), "コピー")
            menu.actions()[3].trigger()
            menu.close()
        try:
            QTimer.singleShot(0, copy)
            QTest.mouseClick(a.tag_label, Qt.MouseButton.RightButton)
            self.assertEqual(self.qt.clipboard().text(), a.candidate.tag)
            self.assertFalse(panel._selected_candidates)
            self.assertEqual(self.controller.rules.rules, original)
            self.assertTrue(a.excluded)
        finally:
            self.qt.clipboard().setText(previous)

    def test_candidate_lookup_does_not_use_bulk_tags(self):
        panel = self.tagger()
        menu = self.controller.candidate_menu(["shirt", "socks"], panel, clicked_tag="socks")
        with patch("tag_context_menu.QDesktopServices.openUrl") as opened:
            menu.actions()[4].trigger()
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(opened.call_args.args[0].toString(), "https://www.google.com/search?q=socks")
        with patch.object(panel.exclusion_panel, "open_dialog") as add:
            menu.actions()[0].trigger()
            add.assert_called_once_with(tags=["shirt", "socks"])
        menu.deleteLater()

    def test_rule_lookup_only_chip_preserves_identity_selection_and_order(self):
        from PySide6.QtWidgets import QMenu
        rule = self.controller.rules.add("very long white shirt with many words at the end", "word")
        panel = self.panel()
        panel.resize(320, 450)
        panel.show()
        self.qt.processEvents()
        panel.select_rule(rule.rule_id)
        row = panel.rows[rule.rule_id]
        before = self.controller.rules.rules
        with patch("tagger_exclusion_panel.show_tag_lookup_menu") as show:
            for widget in (row.chip, row.mode, row):
                QApplication.sendEvent(widget, QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                                      QPoint(2, 2), widget.mapToGlobal(QPoint(2, 2))))
            show.assert_called_once_with(row.chip, rule.tag, row.chip.mapToGlobal(QPoint(2, 2)))
        self.assertEqual(panel.selected_ids(), {rule.rule_id})
        self.assertEqual(self.controller.rules.rules, before)
        self.assertIsNone(row.chip.start)
        observed = []
        def cancel():
            menu = QApplication.activePopupWidget()
            observed.extend(a.text() for a in menu.actions())
            menu.close()
        QTimer.singleShot(0, cancel)
        row.chip.show_lookup_menu(QPoint(2, 2))
        self.assertEqual(observed, ["コピー", "Googleで検索", "Danbooruで検索", "Danbooru Wikiを開く", "Gelbooruで検索", "Gelbooru Wikiで検索"])
        self.assertEqual(panel.selected_ids(), {rule.rule_id})

    def test_rule_chip_remeasures_font_and_qss_after_construction(self):
        from math import ceil
        from PySide6.QtGui import QFont, QFontMetricsF
        from PySide6.QtWidgets import QStyle, QStyleOptionButton
        from tagger_exclusion_panel import RuleRow, RuleList
        listing=self.own(RuleList())
        for tag in ("sakai kyuuta", "anime screenshot", "highres"):
            row=self.own(RuleRow("key",tag,"exact",self.dictionary,True,rule_list=listing))
            row.resize(800,40);row.show();QTest.qWait(30)
            for points in (14,18,9):
                font=QFont(row.chip.font());font.setPointSize(points);row.chip.setFont(font)
                QTest.qWait(30)
                self.assertEqual(row.chip.text(),tag)
                self.assertEqual(row.chip.width(),row.chip.sizeHint().width())
                option=QStyleOptionButton();row.chip.initStyleOption(option)
                rect=row.chip.style().subElementRect(QStyle.SubElement.SE_PushButtonContents,option,row.chip)
                self.assertGreaterEqual(rect.width(),row.chip.fontMetrics().horizontalAdvance(tag))
            row.chip.setStyleSheet("QPushButton { padding: 6px 25px; border: 2px solid #4DB6AC; }")
            QTest.qWait(30)
            self.assertEqual(row.chip.text(),tag)
            self.assertEqual(row.chip.width(), ceil(QFontMetricsF(row.chip.fontMetrics()).horizontalAdvance(tag)) + 54)

    def test_fractional_text_width_in_visible_tagger_rule_list(self):
        from PySide6.QtGui import QFont, QFontMetricsF
        from PySide6.QtWidgets import QStyle, QStyleOptionButton

        tags = ("scan", "sakai kyuuta", "anime screenshot", "highres")
        for tag in tags:
            self.controller.rules.add(tag)
        panel = self.tagger()
        panel.resize(1350, 700)
        splitter = panel.candidate_splitter

        def check_rows(narrow=False):
            QTest.qWait(50)
            elided = []
            for row in panel.exclusion_panel.rows.values():
                chip = row.chip
                self.assertTrue(chip.isVisible())
                option = QStyleOptionButton()
                chip.initStyleOption(option)
                contents = chip.style().subElementRect(
                    QStyle.SubElement.SE_PushButtonContents, option, chip)
                advance = QFontMetricsF(chip.fontMetrics()).horizontalAdvance(row.tag)
                self.assertEqual(row.mode.width(), 108)
                self.assertLess(chip.geometry().right(), row.mode.x())
                if narrow and contents.width() < advance:
                    self.assertTrue(chip.text().endswith("…"), row.tag)
                    elided.append(row.tag)
                else:
                    self.assertGreaterEqual(contents.width(), advance, row.tag)
                    self.assertEqual(chip.text(), row.tag)
                    self.assertEqual(chip.width(), chip.sizeHint().width())
            if narrow:
                self.assertIn("anime screenshot", elided)

        splitter.setSizes([650, 650])
        check_rows()
        # Exercise fractional advances even on a CI display at integer scaling.
        for row in panel.exclusion_panel.rows.values():
            font = QFont(row.chip.font())
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.0625)
            row.chip.setFont(font)
        check_rows()
        splitter.setSizes([1100, 200])
        check_rows(narrow=True)
        # Moving the handle also remembers the restored width for the next show.
        splitter.moveSplitter(splitter.width() // 2, 1)
        check_rows()
        panel.hide()
        panel.show()
        check_rows()
        panel.exclusion_toggle.click()
        panel.exclusion_toggle.click()
        check_rows()
        self.assertTrue(self.controller.add_rule("new tag", "exact"))
        check_rows()
        added = next(r for r in self.controller.rules.rules if r.tag == "new tag")
        self.approve_delete_dialog()
        self.assertTrue(self.controller.remove_rules({added.rule_id}))
        check_rows()

    def test_splitter_reexpansion_restores_full_rule_text_without_stretching(self):
        for tag in ("shirt","sakai kyuuta","anime screenshot","highres","very long tag with many words at the end"):
            self.controller.rules.add(tag)
        panel=self.tagger();panel.resize(1350,700);QTest.qWait(30)
        rules_before=deepcopy(self.controller.rules.rules)
        mode_positions=[]
        for sizes in ([650,650],[1000,300],[650,650]):
            panel.candidate_splitter.setSizes(sizes);QTest.qWait(60)
            rows=list(panel.exclusion_panel.rows.values())
            mode_positions.append([r.mode.x() for r in rows])
            for row in rows:
                self.assertEqual(row.mode.width(),108)
                self.assertLess(row.chip.geometry().right(),row.mode.x())
                if sizes==[650,650]:
                    self.assertEqual(row.chip.text(),row.tag)
                    self.assertEqual(row.chip.width(),row.chip.sizeHint().width())
            if sizes==[1000,300]:
                self.assertNotEqual(rows[-1].chip.text(),rows[-1].tag)
                rows[-1].chip.set_search_marker("end",current=True)
                self.assertIn("end",rows[-1].chip.search_display_text(rows[-1].chip.width()-28))
                rows[-1].chip.set_search_marker("")
        self.assertEqual(mode_positions[0],mode_positions[-1])
        self.assertEqual(self.controller.rules.rules,rules_before)

    def test_general_and_unknown_use_default_styles_across_chip_uis(self):
        from tag_color_dictionary import NO_COLOR_CATEGORIES, all_tag_button_qss
        from tag_panel import TagPanel, TagChip, AllTagChip, TagPresetPanel
        from tag_presets import TagPresetState
        from theme import ALL_TAG_BUTTON_QSS
        editor=self.own(TagPanel())
        editor._tag_color_dictionary=self.dictionary
        editor._tag_color_enabled=True
        preset=self.own(TagPresetPanel(TagPresetState(presets=[],selected_preset_id=""),tag_color_dictionary=self.dictionary))
        for tag in ("newsboy cap","purple hat","white shirt","unregistered test tag","highres","rating:g","upper body"):
            style=self.dictionary.style_for_tag(tag)
            expected=tag_chip_qss(style)
            colorless=style.category_id in NO_COLOR_CATEGORIES
            self.assertEqual(expected is None,colorless)
            self.assertEqual(all_tag_button_qss(style) is None,colorless)
            chip=self.own(TagChip(tag));all_chip=self.own(AllTagChip(tag,2))
            editor._apply_tag_color_style(chip);editor._apply_all_tag_color_style(all_chip)
            self.assertEqual(chip.tag_button.styleSheet(),expected or TAG_CHIP_QSS)
            self.assertEqual(all_chip.tag_button.styleSheet(),all_tag_button_qss(style) or ALL_TAG_BUTTON_QSS)
            self.assertEqual(preset._tag_chip_qss(tag),expected or TAG_CHIP_QSS)
            if tag in ("newsboy cap","purple hat"):
                self.assertEqual(style.category_id,"general")
            if not colorless:
                self.assertIn(style.border_color,expected)

    def test_general_candidates_are_readable_and_exclusion_dimming_remains(self):
        from tagger.result import TagCandidate, TaggerResult
        panel=self.tagger()
        tags=("newsboy cap","purple hat","white shirt","highres","rating:g","upper body")
        panel.set_tag_color_enabled(True)
        result=TaggerResult(self.root/"test.png",general_candidates=[TagCandidate(t,.9) for t in tags])
        panel.set_current_result("test.png",result,status="generated")
        for tag in tags:
            style=self.dictionary.style_for_tag(tag)
            from tag_color_dictionary import NO_COLOR_CATEGORIES
            self.assertEqual(panel._candidate_tag_text_color(TagCandidate(tag,.9)),
                             None if style.category_id in NO_COLOR_CATEGORIES else style.border_color)
        self.controller.add_rule("purple hat","exact")
        self.assertTrue(next(r for r in panel._candidate_rows if r.candidate.tag=="purple hat").excluded)
        self.assertEqual(result.output_tags(),list(tags))
        panel.set_tag_color_enabled(False)
        self.assertTrue(all(panel._candidate_tag_text_color(TagCandidate(t,.9)) is None for t in tags))
        self.assertTrue(next(r for r in panel._candidate_rows if r.candidate.tag=="purple hat").excluded)

    def test_completion_uses_shared_colorless_categories(self):
        from tag_completion import _CompletionRow
        from danbooru_tag_dictionary import DanbooruTag, TagCompletionCandidate
        for tag in ("newsboy cap","purple hat","unregistered test tag","highres"):
            candidate=TagCompletionCandidate(DanbooruTag(tag,0,10,()),None,0)
            row=self.own(_CompletionRow(candidate,tag_color_dictionary=self.dictionary,tag_color_enabled=True))
            expected="#4DB6AC" if tag=="highres" else "#edf6ff"
            self.assertIn(expected,row._tag_label.styleSheet())

    def test_editor_menu_delegates_same_full_tag_and_position(self):
        from tag_panel import TagPanel
        from unittest.mock import Mock
        parent = Mock()
        with patch("tag_panel.show_tag_lookup_menu") as show:
            TagPanel._show_tag_context_menu(parent, "white shirt", QPoint(4, 7))
            show.assert_called_once_with(parent, "white shirt", QPoint(4, 7))


if __name__ == "__main__":
    unittest.main()
