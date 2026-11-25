"""モデル・実ウィンドウなしで既存の反映、履歴記録、Undoを直接検証する。

画面描画・確認ダイアログのみmockにし、本体の反映処理は置き換えない。
"""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app_window import AppWindow
from models import ImageItem
from tag_panel import TagPanel
from tagger.exclusions import ExclusionRuleSet
from tagger.result import TagCandidate, TaggerResult
from tagger.session import TaggerSession


class HistoryHarness:
    record_tagger_apply_history = TagPanel.record_tagger_apply_history
    _tag_history_changes_from_snapshot = TagPanel._tag_history_changes_from_snapshot
    _tag_history_changes_from_snapshots = TagPanel._tag_history_changes_from_snapshots
    _snapshot_tags_for_paths = TagPanel._snapshot_tags_for_paths
    _item_for_image_path = TagPanel._item_for_image_path
    _push_tag_history_entry = TagPanel._push_tag_history_entry
    _latest_undoable_history_entry = TagPanel._latest_undoable_history_entry
    undo_latest_tag_history_entry = TagPanel.undo_latest_tag_history_entry

    def __init__(self, items):
        self.items = items
        self._tag_history = []
        self.refresh_current_tags = Mock()
        self.refresh_all_tags = Mock()
        self.refresh_target_label = Mock()
        self._refresh_after_change = Mock()


class ApplyHarness:
    _apply_tagger_stage1 = AppWindow._apply_tagger_stage1
    _unique_tagger_output_tags = staticmethod(AppWindow._unique_tagger_output_tags)
    _reset_tagger_stage1 = AppWindow._reset_tagger_stage1
    _start_tagger_phase2c_batch = AppWindow._start_tagger_phase2c_batch
    select_tagger_image = AppWindow.select_tagger_image
    _refresh_tagger_current_result = AppWindow._refresh_tagger_current_result

    def __init__(self, items):
        self.items = items
        self.tagger_session = TaggerSession()
        self.tagger_session.start([item.image_path for item in items])
        self.tagger_exclusion_rules = ExclusionRuleSet()
        self.tag_panel = HistoryHarness(items)
        self.tagger_panel = Mock()
        self.thumbnail_panel = Mock()
        self.preview_panel = Mock()
        self._confirm_tagger_apply = Mock(return_value=True)
        self._tagger_execution_is_active = Mock(return_value=False)
        self._on_tags_changed = Mock()
        self._update_preview_placeholder_state = Mock()
        self._clear_pending_tagger_thumbnail_status_updates = Mock()
        self._refresh_tagger_thumbnail_statuses = Mock()
        self._preview_position_and_total_label = lambda index: (index + 1, len(items))
        self._embedded_preview_active = False
        self._tagger_current_index = 0
        self._tagger_force_cancel_in_progress = False
        self._tagger_process_client = Mock()
        self._tagger_process_client.send_predict_batch.return_value = 1

    def set_candidates(self, index, tags):
        result = TaggerResult(self.items[index].image_path,
                              [TagCandidate(tag, .9) for tag in tags])
        self.tagger_session.set_result(result.image_path, result)
        return result


class ApplyExclusionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.information = self.enterContext(patch("app_window.QMessageBox.information"))
        # 両モジュールのQMessageBoxは同一クラスなので、Undoにもこのmockが効く。
        self.save_tags = self.enterContext(patch("app_window.save_tags"))

    def make_app(self, existing=((), ())):
        items = [ImageItem(self.root / f"{index}.png", list(tags))
                 for index, tags in enumerate(existing)]
        app = ApplyHarness(items)
        app.tagger_exclusion_rules.add("shirt", "word")
        return app

    def snapshot(self, app):
        return deepcopy(([item.__dict__ for item in app.items], app.tag_panel._tag_history,
                         app.tagger_session.result_by_path))

    def test_three_modes_filter_candidates_and_preserve_expected_existing_tags(self):
        expected = {
            "empty_only": (["socks"], ["white shirt", "old"]),
            "overwrite": (["socks"], ["socks"]),
            "append": (["socks"], ["white shirt", "old", "socks"]),
        }
        for mode, output in expected.items():
            with self.subTest(mode=mode):
                app = self.make_app(((), ("white shirt", "old")))
                results = [app.set_candidates(index, ["white_shirt", "socks"])
                           for index in range(2)]
                original = deepcopy(results)
                app._apply_tagger_stage1(mode)
                self.assertEqual([item.tags for item in app.items], list(output))
                self.assertEqual(results, original)
                self.assertTrue(app.items[0].dirty)
                self.assertEqual(app.items[1].dirty, mode != "empty_only")
                entry = app.tag_panel._tag_history[0]
                self.assertEqual(entry.target_count, 1 if mode == "empty_only" else 2)
                self.assertNotIn("white shirt", entry.after_tags_by_path[app.items[0].image_path])
        self.save_tags.assert_not_called()


    def test_permanent_exclude_is_applied_to_all_images(self):
        app = self.make_app()
        for index in range(2):
            app.set_candidates(index, ["socks", "smile"])
        app.tagger_exclusion_rules.add("SOCKS")
        app._apply_tagger_stage1("overwrite")
        self.assertEqual([item.tags for item in app.items], [["smile"], ["smile"]])

    def test_excluded_zero_and_original_zero_are_separate_and_unchanged(self):
        for mode in ("empty_only", "overwrite", "append"):
            with self.subTest(mode=mode):
                app = self.make_app((("old",), ("old",)))
                app.set_candidates(0, ["shirt"])
                app.tagger_session.set_no_result(app.items[1].image_path)
                before = self.snapshot(app)
                app._apply_tagger_stage1(mode)
                self.assertEqual(self.snapshot(app), before)
                app._confirm_tagger_apply.assert_not_called()
                text = self.information.call_args.args[2]
                self.assertIn("除外により候補0件のためスキップ: 1件", text)
                self.assertIn("最初から候補なし: 1件", text)

    def test_mixed_batch_reports_skips_and_only_records_changed_images(self):
        app = self.make_app((("old",), ("old",), ("old",), ("old",)))
        app.set_candidates(0, ["shirt"])
        app.tagger_session.set_no_result(app.items[1].image_path)
        app.set_candidates(2, ["socks"])
        app.set_candidates(3, [])
        app._apply_tagger_stage1("overwrite")
        args = app._confirm_tagger_apply.call_args.kwargs
        self.assertEqual(args["skipped_excluded_count"], 1)
        self.assertEqual(args["no_result_count"], 2)
        self.assertEqual(args["candidate_count"], 1)
        self.assertEqual(app.tag_panel._tag_history[0].target_image_paths, [app.items[2].image_path])
        self.assertEqual([item.dirty for item in app.items], [False, False, True, False])
        text = app.tagger_panel.set_status_text.call_args.args[0]
        self.assertIn("除外により候補0件のためスキップ: 1件", text)
        self.assertIn("最初から候補なし: 2件", text)

    def test_cancel_preserves_tags_history_dirty_candidates(self):
        for mode in ("empty_only", "overwrite", "append"):
            with self.subTest(mode=mode):
                app = self.make_app()
                app.set_candidates(0, ["shirt", "socks"])
                app._apply_tagger_stage1(mode)  # 既存の実際の履歴を用意する。
                app.set_candidates(1, ["smile"])
                app._confirm_tagger_apply.return_value = False
                before = self.snapshot(app)
                app._on_tags_changed.reset_mock()
                app._apply_tagger_stage1(mode)
                self.assertEqual(self.snapshot(app), before)
                app._on_tags_changed.assert_not_called()
        self.save_tags.assert_not_called()

    def test_reapply_is_noop_and_does_not_duplicate_history(self):
        for mode in ("empty_only", "overwrite", "append"):
            with self.subTest(mode=mode):
                app = self.make_app()
                app.set_candidates(0, ["shirt", "socks", "socks"])
                app._apply_tagger_stage1(mode)
                before = self.snapshot(app)
                app._confirm_tagger_apply.reset_mock()
                app._apply_tagger_stage1(mode)
                self.assertEqual(self.snapshot(app), before)
                app._confirm_tagger_apply.assert_not_called()

    def test_real_undo_restores_saved_tags_and_dirty_for_each_mode(self):
        for mode in ("empty_only", "overwrite", "append"):
            with self.subTest(mode=mode):
                app = self.make_app((() if mode == "empty_only" else ("old",),))
                app.set_candidates(0, ["shirt", "socks"])
                before_tags = list(app.items[0].tags)
                app._apply_tagger_stage1(mode)
                self.assertTrue(app.items[0].dirty)
                entry = app.tag_panel._tag_history[0]
                self.assertEqual(entry.before_tags_by_path, {app.items[0].image_path: before_tags})
                app.tag_panel.undo_latest_tag_history_entry()
                self.assertEqual(app.items[0].tags, before_tags)
                self.assertFalse(app.items[0].dirty)
                self.assertTrue(entry.undone)

    def test_undo_preserves_previous_unsaved_changes(self):
        app = self.make_app((("saved",),))
        item = app.items[0]
        item.tags = ["unsaved"]
        item.refresh_dirty()
        app.set_candidates(0, ["socks"])
        app._apply_tagger_stage1("overwrite")
        app.tag_panel.undo_latest_tag_history_entry()
        self.assertEqual(item.tags, ["unsaved"])
        self.assertEqual(item.saved_tags, ["saved"])
        self.assertTrue(item.dirty)

    def test_apply_and_undo_do_not_write_txt_or_saved_metadata(self):
        app = self.make_app((("saved",),))
        item = app.items[0]
        item.tag_path.write_bytes(b"saved\r\n")
        item.tag_mtime_ns = item.tag_path.stat().st_mtime_ns
        metadata = (list(item.saved_tags), item.tag_mtime_ns)
        app.set_candidates(0, ["socks"])
        app._apply_tagger_stage1("overwrite")
        app.tag_panel.undo_latest_tag_history_entry()
        self.save_tags.assert_not_called()
        self.assertEqual(item.tag_path.read_bytes(), b"saved\r\n")
        self.assertEqual((item.saved_tags, item.tag_mtime_ns), metadata)


    def test_existing_reset_and_batch_start_preserve_permanent_rules(self):
        for action in ("reset", "start"):
            with self.subTest(action=action):
                app = self.make_app()
                app.set_candidates(0, ["shirt"])
                if action == "reset":
                    app._reset_tagger_stage1()
                else:
                    app._start_tagger_phase2c_batch([app.items[1].image_path], {})
                self.assertEqual(app.tagger_session.result_by_path, {})
                self.assertTrue(bool(app.tagger_exclusion_rules.matching_rules("shirt")))
                self.assertEqual(len(app.tagger_exclusion_rules.rules), 1)

    def test_exclusion_skip_preserves_preexisting_dirty_and_history(self):
        app = self.make_app((("saved",), ()))
        app.items[0].tags = ["unsaved"]
        app.items[0].refresh_dirty()
        app.set_candidates(1, ["socks"])
        app._apply_tagger_stage1("append")
        app.set_candidates(0, ["shirt"])
        before = self.snapshot(app)
        app._apply_tagger_stage1("overwrite")
        self.assertEqual(self.snapshot(app), before)

    def test_old_category_order_and_deduplication_are_preserved(self):
        app = self.make_app(((),))
        result = app.set_candidates(0, ["smile", "smile"])
        result.character_candidates = [TagCandidate("a_character", .9, "character")]
        result.copyright_candidates = [TagCandidate("a_series", .9, "copyright", ("a character",))]
        app._apply_tagger_stage1("overwrite")
        self.assertEqual(app.items[0].tags, ["a character", "a series", "smile"])

    def test_all_old_candidate_categories_use_same_exclusion_decision(self):
        app = self.make_app(((),))
        result = app.set_candidates(0, ["shirt", "smile"])
        result.character_candidates = [TagCandidate("a_character", .9, "character")]
        result.copyright_candidates = [TagCandidate("a_series", .9, "copyright", ("a character",))]
        app.tagger_exclusion_rules.add("a character")
        app.tagger_exclusion_rules.add("a_series")
        original = deepcopy(result)
        app._apply_tagger_stage1("overwrite")
        self.assertEqual(app.items[0].tags, ["smile"])
        self.assertEqual(result, original)

    def test_no_change_message_still_reports_exclusion_skip(self):
        app = self.make_app((("old",), ("socks",)))
        app.set_candidates(0, ["shirt"])
        app.set_candidates(1, ["socks"])
        before = self.snapshot(app)
        app._apply_tagger_stage1("overwrite")
        self.assertEqual(self.snapshot(app), before)
        text = self.information.call_args.args[2]
        self.assertIn("除外により候補0件のためスキップ: 1件", text)
        self.assertIn("候補が現在タグと同一: 1件", text)

    def test_confirmation_includes_both_skip_counts_and_defaults_to_cancel(self):
        app = self.make_app()
        with patch("app_window.QMessageBox") as message_class:
            message = message_class.return_value
            apply_button, cancel_button = object(), object()
            message.addButton.side_effect = [apply_button, cancel_button]
            message.clickedButton.return_value = cancel_button
            confirmed = AppWindow._confirm_tagger_apply(
                app, mode_text="上書き", candidate_count=1,
                changed_entries=[(app.items[0], ["socks"])],
                skipped_existing_count=0, unchanged_count=0,
                skipped_excluded_count=2, no_result_count=3,
            )
            self.assertFalse(confirmed)
            message.setDefaultButton.assert_called_once_with(cancel_button)
            text = message.setInformativeText.call_args.args[0]
            self.assertIn("除外により候補0件のためスキップ: 2件", text)
            self.assertIn("最初から候補なし: 3件", text)


if __name__ == "__main__":
    unittest.main()
