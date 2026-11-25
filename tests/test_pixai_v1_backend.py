import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from huggingface_hub.errors import LocalEntryNotFoundError
from PIL import Image

from tagger.pixai_v1_backend import (
    CATEGORY_ORDER, DEFAULT_THRESHOLDS, MODEL_REPO, MODEL_REVISION, REVIEWED_FILES,
    TAG_COUNT, WEIGHTS_FILE, CategorySettings, PixAIV1TaggerBackend, default_categories,
)


class PixAIV1Tests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.image = self.root / "transparent.png"
        Image.new("RGBA", (8, 2), (120, 40, 30, 0)).save(self.image)
        self.backend = PixAIV1TaggerBackend("cpu", cache_dir=self.root)
        self.model = Mock()
        self.model.model.device = SimpleNamespace(type="cpu")

    def test_fixed_official_snapshot_anonymous_and_cached(self):
        self.assertEqual(MODEL_REPO, "pixai-labs/pixai-tagger-v1.0")
        self.assertEqual(MODEL_REVISION, "9fe10addf9326e292da8a85a98ea74cd91b41771")
        reviewed = {}
        self.backend.allow_download = True
        for name in (*REVIEWED_FILES, WEIGHTS_FILE):
            data = name.encode()
            (self.root / name).write_bytes(data)
            if name in REVIEWED_FILES:
                reviewed[name] = hashlib.sha256(data).hexdigest()

        def hub(repo, name, **kwargs):
            self.assertEqual(repo, MODEL_REPO)
            self.assertEqual(kwargs["revision"], MODEL_REVISION)
            self.assertEqual(kwargs["endpoint"], "https://huggingface.co")
            self.assertIs(kwargs["token"], False)
            if kwargs.get("local_files_only") and name == WEIGHTS_FILE:
                raise LocalEntryNotFoundError("not cached")
            return str(self.root / name)

        with patch("huggingface_hub.hf_hub_download", side_effect=hub) as download:
            with patch("tagger.pixai_v1_backend.REVIEWED_FILES", reviewed):
                self.assertEqual(self.backend._resolve_snapshot(), self.root)
                self.assertEqual([call.args[1] for call in download.call_args_list],
                                 [*reviewed, WEIGHTS_FILE, WEIGHTS_FILE])
                (self.root / "tagger_pipeline.py").write_bytes(b"changed")
                with self.assertRaisesRegex(RuntimeError, "確認済み"):
                    self.backend._resolve_snapshot()

    def test_missing_cache_does_not_download_without_permission(self):
        with patch("huggingface_hub.hf_hub_download", side_effect=LocalEntryNotFoundError("not cached")) as hub:
            with self.assertRaisesRegex(RuntimeError, "取得またはキャッシュ検証"):
                self.backend._resolve_snapshot()
        hub.assert_called_once()
        self.assertTrue(hub.call_args.kwargs["local_files_only"])

    def test_load_uses_verified_local_snapshot_and_reuses_pipeline(self):
        with patch.object(self.backend, "_resolve_snapshot", return_value=self.root), \
                patch("transformers.pipelines.pipeline", return_value=self.model) as factory:
            self.backend.load()
            self.backend.load()
            factory.assert_called_once()
            args = factory.call_args.kwargs
            self.assertEqual(args["model"], str(self.root))
            self.assertEqual(args["image_processor"], str(self.root))
            self.assertTrue(args["trust_remote_code"])
            self.assertTrue(args["local_files_only"])
            self.assertTrue(args["model_kwargs"]["use_safetensors"])
            self.assertEqual(args["device"], "cpu")
            self.assertEqual(str(args["dtype"]), "torch.float32")
            self.backend.close()
            self.assertIsNone(self.backend.pipeline)
            self.assertEqual(self.backend.device_name, "")
            self.backend.load()
            self.assertEqual(factory.call_count, 2)

    def test_gpu_and_auto_device_selection_and_release(self):
        for requested, cuda, expected in (("auto", False, "cpu"), ("auto", True, "cuda"),
                                          ("gpu", True, "cuda"), ("cpu", True, "cpu")):
            with self.subTest(requested=requested, cuda=cuda):
                backend = PixAIV1TaggerBackend(requested)
                self.model.model.device.type = expected
                with patch("torch.cuda.is_available", return_value=cuda), \
                        patch("torch.cuda.empty_cache") as clear, \
                        patch.object(backend, "_resolve_snapshot", return_value=self.root), \
                        patch("transformers.pipelines.pipeline", return_value=self.model) as factory:
                    backend.load()
                    self.assertEqual(backend.device_name, expected)
                    self.assertEqual(factory.call_args.kwargs["device"], expected)
                    backend.close()
                    self.assertEqual(clear.call_count, int(expected == "cuda"))
        with patch("torch.cuda.is_available", return_value=False), \
                patch.object(PixAIV1TaggerBackend, "_resolve_snapshot") as cache:
            with self.assertRaisesRegex(RuntimeError, "GPU が利用できません"):
                PixAIV1TaggerBackend("gpu").load()
        cache.assert_not_called()

    def test_load_failure_does_not_publish_model(self):
        with patch.object(self.backend, "_resolve_snapshot", return_value=self.root), \
                patch("transformers.pipelines.pipeline", side_effect=OSError("damaged")):
            with self.assertRaisesRegex(RuntimeError, "damaged"):
                self.backend.load()
        self.assertIsNone(self.backend.pipeline)
        self.assertEqual(self.backend.device_name, "")

    def test_pipeline_receives_original_image_and_all_thresholds(self):
        import numpy as np

        def predict(image, *, threshold):
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getpixel((0, 0)), (120, 40, 30, 0))
            self.assertEqual(set(threshold), set(CATEGORY_ORDER))
            self.assertEqual(threshold["style"], 1.0)
            scores = {"general": {"white_shirt": .5, "large_hat": .8, "below": .49},
                      "copyright": {"Some__Series": .25}}
            return {"results": {name: {tag: score for tag, score in scores.get(name, {}).items()
                                       if np.float32(score) > threshold[name]} for name in CATEGORY_ORDER}}

        self.backend.pipeline = predict
        settings = dict(general=CategorySettings(True, .5), copyright=CategorySettings(True, .25))
        result = self.backend.predict_image(self.image, categories=settings)
        self.assertEqual([item.tag for item in result.candidates_by_category["general"]], ["large hat", "white shirt"])
        self.assertEqual([item.score for item in result.candidates_by_category["general"]], [.8, .5])
        self.assertEqual(result.candidates_by_category["copyright"][0].tag, "Some Series")
        self.assertFalse(result.candidates_by_category["style"])

    def test_defaults_and_settings_validation(self):
        self.assertEqual({name for name, s in default_categories().items() if s.enabled}, {"general"})
        self.assertEqual(list(DEFAULT_THRESHOLDS.values()), [.27, .24, .17, .15, .17, .41])
        for threshold in (True, -0.1, 1.1, float("nan"), "0.5"):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                CategorySettings(True, threshold)

    def test_correctly_parses_all_30877_tags_in_six_categories(self):
        sizes = dict(general=15043, character=8308, copyright=2460, style=4917, meta=145, rating=4)
        self.backend.pipeline = lambda *_args, **_kw: {
            "results": {name: {f"{name}_{i}": .75 for i in range(count)} for name, count in sizes.items()}
        }
        settings = {name: CategorySettings(True, 0.0) for name in sizes}
        result = self.backend.predict_image(self.image, categories=settings)
        self.assertEqual(len(result.all_candidates), TAG_COUNT)
        self.assertEqual({name: len(tags) for name, tags in result.candidates_by_category.items()}, sizes)
        self.assertEqual(result.all_candidates[0].category, "character")

    def test_invalid_pipeline_output_and_inference_failure(self):
        for wrong in ([], {"results": {}}, None):
            self.backend.pipeline = Mock(return_value=wrong)
            with self.assertRaisesRegex(RuntimeError, "出力が不正"):
                self.backend.predict_image(self.image)
        for payload in ([], {"tag": float("nan")}, {"tag": -0.1}, {"tag": 1.1}, {"__": .5}, {"tag": True}):
            grouped = {name: {} for name in CATEGORY_ORDER}
            grouped["general"] = payload
            self.backend.pipeline = Mock(return_value={"results": grouped})
            with self.assertRaisesRegex(RuntimeError, "不正"):
                self.backend.predict_image(self.image)
        self.backend.pipeline = Mock(side_effect=RuntimeError("test inference failure"))
        with self.assertRaisesRegex(RuntimeError, "test inference failure"):
            self.backend.predict_image(self.image)

    def test_existing_backend_dispatch(self):
        from tagger.settings_migration import LEGACY_V1_REPO
        from tagger.worker_process import resolve_backend_kind

        self.assertEqual(resolve_backend_kind("pixai-labs/pixai-tagger-v0.9"), "pixai")
        self.assertEqual(resolve_backend_kind("SmilingWolf/wd-eva02-large-tagger-v3"), "wd")
        self.assertEqual(resolve_backend_kind(MODEL_REPO), "pixai_v1")
        with self.assertRaisesRegex(ValueError, "未対応"):
            resolve_backend_kind(LEGACY_V1_REPO)
