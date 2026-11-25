import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from PIL import Image
from PySide6.QtCore import QEvent, QTimer, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionSpinBox
from tagger.pixai_v1_backend import MODEL_REPO, CATEGORY_ORDER, PixAIV1Result, PixAIV1TaggerBackend
from tagger.profiles import normalize_profiles, WD_REPO, OLD_PIXAI_REPO
from tagger.result import TagCandidate, TaggerResult
from tagger.worker_process import serialize_result, ensure_backend, release_backend
from tagger_panel import TaggerPanel
from settings import AppSettings, save_app_settings, load_app_settings
from app_window import AppWindow
from models import ImageItem
from tagger.settings_migration import LEGACY_V1_REPO


class PixAIV1IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.widgets=[]

    def tearDown(self):
        QTest.qWait(30)
        for w in self.widgets:
            w.close(); w.deleteLater()
        self.app.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def panel(self):
        panel = TaggerPanel(); self.widgets.append(panel)
        panel.resize(1000,700); panel.show(); self.app.processEvents()
        return panel

    def test_model_choices_and_v1_grid_defaults(self):
        p=self.panel()
        self.assertEqual([p.model_combo.itemData(i) for i in range(p.model_combo.count())], [WD_REPO,OLD_PIXAI_REPO,MODEL_REPO])
        p.model_combo.setCurrentIndex(2)
        self.assertTrue(p.v1_category_card.isVisible())
        self.assertTrue(all(not row.isVisible() for row in p._legacy_category_rows))
        self.assertEqual([k for k,(check,spin) in p.v1_categories.items() if check.isChecked()],["general"])
        self.assertEqual([spin.value() for check,spin in p.v1_categories.values()],[17,27,24,15,17,41])
        grid=p.v1_category_card.layout()
        self.assertEqual(grid.rowCount(),2)
        self.assertEqual(sum(grid.itemAt(i).widget() is not None for i in range(grid.count())),6)
        self.assertTrue(all(spin.isEnabled() for check,spin in p.v1_categories.values()))

    def test_model_switch_keeps_independent_settings(self):
        p=self.panel(); p.general_threshold_spin.setValue(42)
        p.model_combo.setCurrentIndex(1); p.general_threshold_spin.setValue(53)
        p.model_combo.setCurrentIndex(2)
        p.v1_categories["style"][0].setChecked(True); p.v1_categories["style"][1].setValue(65)
        p.model_combo.setCurrentIndex(0); self.assertEqual(p.general_threshold_spin.value(),42)
        p.model_combo.setCurrentIndex(1); self.assertEqual(p.general_threshold_spin.value(),53)
        p.model_combo.setCurrentIndex(2); self.assertEqual(p.v1_categories["style"][1].value(),65)
        self.assertNotIn("variant", p.settings_dict())

    def test_legacy_migration_and_profile_json_roundtrip(self):
        settings=AppSettings.from_dict({"tagger_general_threshold":.62,"tagger_include_character":True})
        for repo in (WD_REPO,OLD_PIXAI_REPO):
            self.assertEqual(settings.tagger_model_profiles[repo]["general_threshold"],.62)
        self.assertEqual(settings.tagger_model_profiles[MODEL_REPO]["categories"]["general"]["threshold"],.17)
        settings.tagger_model_profiles[MODEL_REPO]["categories"]["style"]={"enabled":False,"threshold":.71}
        path=self.root/"settings.json";save_app_settings(settings,path)
        self.assertEqual(load_app_settings(path).tagger_model_profiles,settings.tagger_model_profiles)

    def test_invalid_profiles_normalized(self):
        result=normalize_profiles({MODEL_REPO:{"device":"bad","variant":"bad","categories":{"general":{"enabled":1,"threshold":float("nan")}}}})
        self.assertEqual(result[MODEL_REPO]["device"],"auto")
        self.assertTrue(result[MODEL_REPO]["categories"]["general"]["enabled"])
        self.assertEqual(result[MODEL_REPO]["categories"]["general"]["threshold"],.17)

    def test_settings_snapshot_and_no_reinference_on_threshold_change(self):
        p=self.panel();p.model_combo.setCurrentIndex(2)
        captured=[];p.run_requested.connect(captured.append);p.run_button.click()
        result=TaggerResult(Path("test.png"),[TagCandidate("shirt",.2)])
        p.set_current_result("test.png",result,status="generated")
        before=list(p._candidate_rows)
        p.v1_categories["general"][1].setValue(90)
        self.assertEqual(captured[0]["categories"]["general"]["threshold"],.17)
        self.assertEqual(p._candidate_rows,before)
        p.set_running(True)
        self.assertFalse(hasattr(p,"variant_combo"))
        self.assertTrue(all(not spin.isEnabled() for check,spin in p.v1_categories.values()))

    def test_all_v1_categories_off_disables_run(self):
        p=self.panel();p.model_combo.setCurrentIndex(2)
        p.v1_categories["general"][0].setChecked(False)
        self.assertFalse(p.run_button.isEnabled())
        p.v1_categories["rating"][0].setChecked(True)
        self.assertTrue(p.run_button.isEnabled())

    def test_old_v1_settings_are_migrated_and_backed_up_only_on_save(self):
        path = self.root / "settings.json"
        backup = path.with_name("settings.json.pre-pixai-v1.bak")
        meta = dict(enabled=False, threshold=.63)
        path.write_text(json.dumps(dict(tagger_model_repo=LEGACY_V1_REPO, tagger_model_profiles={
            LEGACY_V1_REPO: dict(device="gpu", variant="fp16", categories={"meta": meta}),
            WD_REPO: dict(device="cpu", general_threshold=.52),
        })), encoding="utf-8")
        original = path.read_bytes()
        settings = load_app_settings(path)
        self.assertEqual(settings.tagger_model_repo, MODEL_REPO)
        self.assertEqual(settings.tagger_model_profiles[MODEL_REPO]["device"], "gpu")
        self.assertEqual(settings.tagger_model_profiles[MODEL_REPO]["categories"]["meta"], meta)
        self.assertEqual(settings.tagger_model_profiles[WD_REPO]["general_threshold"], .52)
        self.assertNotIn("variant", settings.tagger_model_profiles[MODEL_REPO])
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(backup.exists())
        save_app_settings(settings, path)
        self.assertEqual(backup.read_bytes(), original)
        self.assertNotIn(LEGACY_V1_REPO, path.read_text(encoding="utf-8"))
        self.assertEqual(load_app_settings(path), settings)
        save_app_settings(settings, path)
        self.assertEqual(backup.read_bytes(), original)

    def test_migration_backup_conflict_preserves_old_file(self):
        path = self.root / "settings.json"
        original = json.dumps({"tagger_model_repo": LEGACY_V1_REPO}).encode()
        path.write_bytes(original)
        backup = path.with_name("settings.json.pre-pixai-v1.bak")
        backup.write_bytes(b"another backup")
        with self.assertRaises(OSError):
            save_app_settings(load_app_settings(path), path)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(backup.read_bytes(), b"another backup")

    def test_legacy_model_without_profile_preserves_flat_device(self):
        settings = AppSettings.from_dict({"tagger_model_repo": LEGACY_V1_REPO, "tagger_device": "cpu"})
        self.assertEqual(settings.tagger_model_repo, MODEL_REPO)
        self.assertEqual(settings.tagger_model_profiles[MODEL_REPO]["device"], "cpu")

    def test_migration_replace_failure_keeps_original_and_backup(self):
        path = self.root / "settings.json"
        original = json.dumps({"tagger_model_repo": LEGACY_V1_REPO}).encode()
        path.write_bytes(original)
        settings = load_app_settings(path)
        with patch.object(Path, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                save_app_settings(settings, path)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.with_name("settings.json.pre-pixai-v1.bak").read_bytes(), original)
        save_app_settings(settings, path)
        self.assertEqual(load_app_settings(path).tagger_model_repo, MODEL_REPO)

    def test_normal_ui_and_worker_use_pytorch_device(self):
        p=self.panel();p.model_combo.setCurrentIndex(2)
        self.assertFalse(hasattr(p,"variant_combo"))
        settings=p.settings_dict();settings["device"]="cpu"
        with patch.object(PixAIV1TaggerBackend, "load"):
            backend, key, _ = ensure_backend(None, None, settings)
            self.assertEqual(backend.requested_device, "cpu")
            self.assertEqual(key, ("pixai_v1", MODEL_REPO, "cpu"))

    def test_grid_cells_fit_narrow_panel(self):
        p=self.panel();p.model_combo.setCurrentIndex(2);p.resize(650,700)
        self.app.processEvents()
        grid=p.v1_category_card.layout()
        for i,(check,spin) in enumerate(p.v1_categories.values()):
            cell=grid.itemAtPosition(i//3,i%3).widget()
            self.assertIs(check.parentWidget(),cell)
            self.assertTrue(cell.rect().contains(check.geometry()))
            self.assertTrue(cell.rect().contains(spin.geometry()))
            self.assertFalse(check.geometry().intersects(spin.geometry()))

    def test_v1_settings_height_matches_legacy_and_restores_legacy_layout(self):
        p=self.panel()
        for width in (800,900,1000):
            p.model_combo.setCurrentIndex(0)
            p.resize(width,700);self.app.processEvents()
            cards=[p._settings_layout.itemAtPosition(0,i).widget() for i in (0,1)]
            before=[card.height() for card in cards]
            p.model_combo.setCurrentIndex(2);self.app.processEvents()
            self.assertEqual([card.height() for card in cards],before)
            for name,(check,spin) in p.v1_categories.items():
                self.assertEqual(check.geometry().center().y(),spin.geometry().center().y())
                self.assertLess(check.geometry().right(),spin.geometry().left())
            p.model_combo.setCurrentIndex(1);self.app.processEvents()
            self.assertEqual([card.height() for card in cards],before)
            self.assertEqual(p._settings_layout.columnStretch(1),1)
            self.assertEqual(p._device_setting_label.text(),"推論実行デバイス")

    def test_v1_percent_editor_has_room_for_maximum_and_suffix(self):
        p=self.panel();p.model_combo.setCurrentIndex(2)
        for check,spin in p.v1_categories.values():
            for value in (41,100):
                spin.setValue(value);self.app.processEvents()
                editor=spin.lineEdit()
                margins=editor.textMargins()
                available=editor.contentsRect().width()-margins.left()-margins.right()-4
                self.assertGreaterEqual(available,editor.fontMetrics().horizontalAdvance(spin.text()))
                self.assertTrue(spin.text().endswith("%"))

    def test_card_geometry_identical_through_model_cycle(self):
        p=self.panel()
        for width in (1000,1100,1200):
            p.resize(width,700);QTest.qWait(30)
            p.setFixedSize(p.size())
            before=[c.geometry().getRect() for c in p._settings_cards]
            for index in (0,2,1,0,2):
                p.model_combo.setCurrentIndex(index);QTest.qWait(30)
                self.assertEqual([c.geometry().getRect() for c in p._settings_cards],before)
                self.assertLessEqual(abs(p._settings_cards[0].width()-p._settings_cards[1].width()),1)
            p.setMinimumSize(0,0);p.setMaximumSize(16777215,16777215)

    def test_v1_arrow_and_wheel_step_five_percentage_points_for_all_categories(self):
        p=self.panel();p.model_combo.setCurrentIndex(2);QTest.qWait(30)
        geometry=[c.geometry().getRect() for c in p._settings_cards]
        for name,(check,spin) in p.v1_categories.items():
            with self.subTest(category=name):
                self.assertEqual(spin.singleStep(),5.0)
                initial=spin.value()
                option=QStyleOptionSpinBox();spin.initStyleOption(option)
                for control,expected in ((QStyle.SubControl.SC_SpinBoxUp,initial+5),
                                         (QStyle.SubControl.SC_SpinBoxDown,initial)):
                    rect=spin.style().subControlRect(QStyle.ComplexControl.CC_SpinBox,option,control,spin)
                    QTest.mouseClick(spin,Qt.MouseButton.LeftButton,pos=rect.center())
                    self.assertEqual(spin.value(),expected)
                for delta,expected in ((120,initial+5),(-120,initial)):
                    point=spin.rect().center()
                    event=QWheelEvent(QPointF(point),QPointF(spin.mapToGlobal(point)),QPoint(),QPoint(0,delta),
                                      Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier,
                                      Qt.ScrollPhase.NoScrollPhase,False)
                    self.app.sendEvent(spin,event)
                    self.assertEqual(spin.value(),expected)
                spin.setValue(98);spin.stepUp();self.assertEqual(spin.value(),100)
                spin.setValue(2);spin.stepDown();self.assertEqual(spin.value(),0)
                spin.setValue(initial)
        self.assertEqual([c.geometry().getRect() for c in p._settings_cards],geometry)

    def test_v1_direct_input_still_accepts_single_percent_and_transfers_exact_value(self):
        p=self.panel();p.model_combo.setCurrentIndex(2)
        for name,(check,spin) in p.v1_categories.items():
            with self.subTest(category=name):
                spin.lineEdit().selectAll()
                QTest.keyClicks(spin.lineEdit(),"23")
                QTest.keyClick(spin.lineEdit(),Qt.Key.Key_Return)
                self.assertEqual(spin.value(),23)
                self.assertEqual(p.settings_dict()["categories"][name]["threshold"],.23)
        p.model_combo.setCurrentIndex(0);p.model_combo.setCurrentIndex(2)
        self.assertTrue(all(spin.value()==23 for check,spin in p.v1_categories.values()))

    def test_v1_category_columns_use_natural_width_and_trailing_space(self):
        p=self.panel();p.model_combo.setCurrentIndex(2);p.resize(1200,700);QTest.qWait(30)
        grid=p.v1_category_card.layout()
        self.assertEqual(grid.columnStretch(3),1)
        for column in range(3):
            self.assertEqual(grid.columnStretch(column),0)
            cells=[grid.itemAtPosition(row,column).widget() for row in range(2)]
            self.assertLessEqual(cells[0].width(),max(c.sizeHint().width() for c in cells))
        for check,spin in p.v1_categories.values():
            self.assertEqual(check.parentWidget().layout().count(),2)

    def test_candidate_colors_use_dictionary_not_inference_category(self):
        from tag_color_dictionary import TagColorDictionary, tag_chip_qss
        p=self.panel();dictionary=TagColorDictionary.load()
        p._tag_color_dictionary=dictionary
        p.set_tag_color_enabled(True)
        for tag in ("1girl","highres","rating:g","bkub"):
            self.assertEqual(p._candidate_tag_text_color(TagCandidate(tag,.9,"general")),dictionary.style_for_tag(tag).border_color)
        self.assertIsNone(p._candidate_tag_text_color(TagCandidate("watercolor",.9,"style")))
        p.exclusion_controller.path=self.root/"exclusions.json"
        p.exclusion_panel.dictionary=dictionary
        p.exclusion_controller.add_rule("highres","exact")
        p.set_current_result("image.png",TaggerResult(Path("image.png"),meta_candidates=[TagCandidate("highres",.9,"meta")]),status="generated")
        self.assertTrue(p._candidate_rows[0].excluded)
        row=next(iter(p.exclusion_panel.rows.values()))
        self.assertEqual(row.chip.styleSheet(),tag_chip_qss(dictionary.style_for_tag("highres")))
        p.set_tag_color_enabled(False)
        self.assertIsNone(p._candidate_tag_text_color(TagCandidate("highres",.9,"meta")))
        self.assertTrue(p._candidate_rows[0].excluded)

    def test_display_reorder_does_not_change_apply_order_or_scores(self):
        p=self.panel()
        result=TaggerResult(Path("image.png"),general_candidates=[TagCandidate("g",.8,"general")],
                            style_candidates=[TagCandidate("s1",.9,"style"),TagCandidate("s2",.7,"style")])
        before=result.output_tags()
        p.set_current_result("image.png",result,status="generated")
        self.assertEqual([r.candidate.tag for r in p._candidate_rows],["s1","s2","g"])
        self.assertEqual(result.output_tags(),before)
        self.assertEqual([r.candidate.score for r in p._candidate_rows],[.9,.7,.8])

    def test_six_category_wire_decode_display_and_exclusion(self):
        p=self.panel()
        result=PixAIV1Result(Path("image.png"))
        for name in CATEGORY_ORDER: result.candidates_by_category[name]=[TagCandidate(name,.8,name)]
        # 同じ親アプリのJSONL受信側デコーダを使用。
        decoded=AppWindow._tagger_result_from_process_message({"path":str(result.image_path),"result":serialize_result(result)})
        p.set_current_result("image.png",decoded,status="generated")
        self.assertEqual([r.candidate.category for r in p._candidate_rows],["character","copyright","style","meta","rating","general"])
        p.exclusion_controller.path=self.root/"exclusions.json"
        p.exclusion_controller.add_rule("style","exact")
        self.assertTrue(next(r for r in p._candidate_rows if r.candidate.tag=="style").excluded)
        self.assertNotIn("推定",p._candidate_tooltip(decoded.copyright_candidates[0]))

    def test_new_categories_apply_undo_preserves_existing(self):
        from test_tagger_apply_exclusions import ApplyHarness
        app=ApplyHarness([ImageItem(self.root/"one.png",["old"])])
        result=TaggerResult(app.items[0].image_path,style_candidates=[TagCandidate("oil paint",.8,"style")],rating_candidates=[TagCandidate("rating:g",.9,"rating")])
        app.tagger_session.set_result(result.image_path,result)
        app.tagger_exclusion_rules.add("rating:g")
        app._apply_tagger_stage1("append")
        self.assertEqual(app.items[0].tags,["old","oil paint"])
        with patch("tag_panel.QMessageBox.information"):
            app.tag_panel.undo_latest_tag_history_entry()
        self.assertEqual(app.items[0].tags,["old"])
        self.assertFalse(app.items[0].dirty)

    def test_worker_device_reuse_and_release(self):
        settings = {"model_repo": MODEL_REPO, "device": "cpu"}
        with patch.object(PixAIV1TaggerBackend, "load"), patch("torch.cuda.is_available") as cuda:
            cuda.return_value = False
            backend, key, reused = ensure_backend(None, None, settings)
            self.assertFalse(reused)
            backend.pipeline = Mock()
            same, same_key, reused = ensure_backend(backend, key, {**settings, "device": "auto"})
            self.assertIs(same, backend)
            self.assertTrue(reused)
            self.assertEqual(same_key, key)
            cuda.return_value = True
            replacement, new_key, reused = ensure_backend(backend, key, {**settings, "device": "gpu"})
            self.assertFalse(reused)
            self.assertIsNone(backend.pipeline)
            self.assertEqual(new_key[-1], "cuda")
            release_backend(replacement)

    def test_worker_real_jsonl_mock_pipeline_reuse_and_error(self):
        image = self.root / "image.png"
        Image.new("RGB", (4, 4), "white").save(image)
        images = [image]
        for index in range(1, 100):
            copy = self.root / f"image_{index}.png"
            copy.write_bytes(image.read_bytes())
            images.append(copy)
        code = """
from tagger.pixai_v1_backend import CATEGORY_ORDER, PixAIV1TaggerBackend
from tagger.worker_process import main

def load(self):
    if self.pipeline is None:
        self.pipeline = lambda image, threshold: {"results": {name: {name: .8} for name in CATEGORY_ORDER}}
        self.device_name = "cpu"

PixAIV1TaggerBackend.load = load
main()
"""
        settings={"model_repo":MODEL_REPO,"device":"cpu","categories":{n:{"enabled":True,"threshold":.1} for n in CATEGORY_ORDER}}
        requests = [
            dict(type="predict_batch", request_id=1, paths=[str(path.resolve()) for path in images], settings=settings),
            dict(type="predict_batch", request_id=2, paths=[str(image.resolve())], settings=settings),
        ]
        requests += [dict(type="predict_batch",request_id=3,paths=[str((self.root/"missing.png").resolve())],settings=settings),dict(type="shutdown",request_id=4)]
        process=subprocess.run([sys.executable,"-X","utf8","-B","-u","-c",code],input="".join(json.dumps(r)+"\n" for r in requests),text=True,encoding="utf-8",capture_output=True,timeout=40)
        self.assertEqual(process.returncode,0,process.stderr)
        messages=[json.loads(line) for line in process.stdout.splitlines()]
        starts=[m for m in messages if m["type"]=="batch_started"]
        self.assertEqual([m["backend_reused"] for m in starts],[False,True,True])
        results=[m for m in messages if m["type"]=="batch_item_result"]
        self.assertEqual(len(results), 101)
        self.assertEqual(set(results[0]["result"]),{f"{n}_candidates" for n in CATEGORY_ORDER})
        self.assertTrue(any(m["type"]=="batch_item_error" for m in messages))
        self.assertEqual(messages[-1]["type"],"shutdown_complete")

    def test_saved_profile_restores_ui_without_signals(self):
        p=self.panel();p.model_combo.setCurrentIndex(2)
        p.v1_categories["meta"][1].setValue(63)
        settings=p.settings_dict()
        app=Mock();app.app_settings=AppSettings()
        path=self.root/"settings.json"
        with patch("app_window.save_app_settings",side_effect=lambda value:save_app_settings(value,path)):
            AppWindow._on_tagger_settings_changed(app,settings)
        loaded=load_app_settings(path)
        q=self.panel();changed=[];q.settings_changed.connect(changed.append)
        q.restore_persisted_settings(model_repo=loaded.tagger_model_repo,device=loaded.tagger_device,
            include_general=loaded.tagger_include_general,general_threshold=loaded.tagger_general_threshold,
            include_character=loaded.tagger_include_character,character_threshold=loaded.tagger_character_threshold,
            include_copyright=loaded.tagger_include_copyright,copyright_expanded=False,apply_mode=loaded.tagger_apply_mode,
            model_profiles=loaded.tagger_model_profiles)
        self.assertEqual(changed,[])
        self.assertTrue(q._is_v1_selected())
        self.assertEqual(q.v1_categories["meta"][1].value(),63)
        self.assertFalse(q.v1_categories["meta"][0].isChecked())
        self.assertNotIn("variant", q.settings_dict())

    def test_v1_process_client_abort_and_restart(self):
        from tagger.process_client import TaggerProcessClient
        client=TaggerProcessClient(Path(__file__).resolve().parents[1])
        try:
            self.assertTrue(client.ensure_started())
            for _ in range(100):
                if client.is_ready: break
                QTest.qWait(30)
            self.assertTrue(client.is_ready)
            # GPU指定はこのVMではエラーになる。取得を始めずに中断経路を検証。
            self.assertIsNotNone(client.send_predict_batch([self.root/"image.png"],dict(model_repo=MODEL_REPO,device="gpu")))
            self.assertTrue(client.abort_current_run())
            for _ in range(100):
                if not client.is_active: break
                QTest.qWait(30)
            self.assertFalse(client.is_active)
            self.assertTrue(client.ensure_started())
            for _ in range(100):
                if client.is_ready: break
                QTest.qWait(30)
            self.assertTrue(client.is_ready)
        finally:
            client.shutdown_and_wait()
            client.deleteLater()

    def test_worker_v1_gpu_failure_is_explicit(self):
        with patch("torch.cuda.is_available", return_value=False):
            with self.assertRaisesRegex(RuntimeError,"GPU が利用できません"):
                ensure_backend(None,None,dict(model_repo=MODEL_REPO,device="gpu"))

    def test_worker_load_failure_does_not_change_tags_or_settings(self):
        from io import StringIO
        from tagger.worker_process import main

        image = self.root / "image.png"
        Image.new("RGB", (4, 4)).save(image)
        tag_file = image.with_suffix(".txt")
        tag_file.write_bytes(b"existing tag")
        settings_file = self.root / "settings.json"
        save_app_settings(AppSettings(), settings_file)
        original_settings = settings_file.read_bytes()
        request = dict(type="predict_batch", request_id=1, paths=[str(image)],
                       settings=dict(model_repo=MODEL_REPO, device="cpu"))
        output = StringIO()
        with patch.object(sys, "stdin", StringIO(json.dumps(request) + "\n")), \
                patch.object(sys, "stdout", output), patch.object(sys, "stderr", StringIO()), \
                patch.object(PixAIV1TaggerBackend, "load", side_effect=RuntimeError("model load failed")):
            self.assertEqual(main(), 0)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([message["type"] for message in messages], ["ready", "batch_failed"])
        self.assertIn("model load failed", messages[-1]["message"])
        self.assertEqual(tag_file.read_bytes(), b"existing tag")
        self.assertEqual(settings_file.read_bytes(), original_settings)
