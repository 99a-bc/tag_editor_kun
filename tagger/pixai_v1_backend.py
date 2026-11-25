"""公式PixAI Tagger v1.0のTransformers Pipelineを使うバックエンド。"""

from contextlib import redirect_stdout
from dataclasses import dataclass, field
import gc
import hashlib
import math
from pathlib import Path
import sys
from types import MappingProxyType

from .result import TagCandidate

MODEL_REPO = "pixai-labs/pixai-tagger-v1.0"
MODEL_REVISION = "9fe10addf9326e292da8a85a98ea74cd91b41771"
# trust_remote_codeで使用する設定とPythonコードを、確認済みの内容に限定する。
REVIEWED_FILES = {
    "config.json": "f8a19b38661c37dc5fd519f2137be70f6f897972021b4ebc17b7d35a0e27b9dd",
    "preprocessor_config.json": "20451575e627950993de7b00641d5b263eceb3e7436e4ad518fea2411b60e49d",
    "tagger_pipeline.py": "3431279fdfff234f62664999fc1fe6bb20f1e8756f7435d0e165ebd4456f7fcd",
}
WEIGHTS_FILE = "model.safetensors"
TAG_COUNT = 30_877
CATEGORY_ORDER = ("character", "copyright", "general", "style", "meta", "rating")
DEFAULT_THRESHOLDS = MappingProxyType(dict(zip(CATEGORY_ORDER, (.27, .24, .17, .15, .17, .41))))


@dataclass(frozen=True)
class CategorySettings:
    enabled: bool
    threshold: float

    def __post_init__(self):
        if (type(self.enabled) is not bool or type(self.threshold) not in (int, float)
                or not math.isfinite(self.threshold) or not 0 <= self.threshold <= 1):
            raise ValueError("カテゴリー設定にはboolと0〜1の有限しきい値が必要です。")


def default_categories():
    return {name: CategorySettings(name == "general", DEFAULT_THRESHOLDS[name]) for name in CATEGORY_ORDER}


@dataclass
class PixAIV1Result:
    image_path: Path
    candidates_by_category: dict[str, list[TagCandidate]] = field(
        default_factory=lambda: {name: [] for name in CATEGORY_ORDER}
    )

    @property
    def all_candidates(self):
        return [candidate for name in CATEGORY_ORDER for candidate in self.candidates_by_category[name]]


class PixAIV1TaggerBackend:
    def __init__(self, device="auto", *, cache_dir=None, allow_download=False):
        if device not in ("auto", "cpu", "gpu"):
            raise ValueError("deviceが不正です")
        self.requested_device = device
        self.model_repo = MODEL_REPO
        self.cache_dir = Path(cache_dir) if cache_dir is not None else Path(__file__).resolve().parents[1] / "models" / "pixai_v1_cache"
        self.allow_download = allow_download
        self.pipeline = None
        self.device_name = ""

    def _resolve_snapshot(self):
        from huggingface_hub import hf_hub_download
        from huggingface_hub.errors import LocalEntryNotFoundError

        try:
            files = {}
            for name in (*REVIEWED_FILES, WEIGHTS_FILE):
                kwargs = dict(revision=MODEL_REVISION, cache_dir=self.cache_dir,
                              token=False, endpoint="https://huggingface.co")
                try:
                    path = hf_hub_download(MODEL_REPO, name, local_files_only=True, **kwargs)
                except LocalEntryNotFoundError:
                    if not self.allow_download:
                        raise
                    path = hf_hub_download(MODEL_REPO, name, **kwargs)
                files[name] = Path(path)
                if name in REVIEWED_FILES:
                    if hashlib.sha256(files[name].read_bytes()).hexdigest() != REVIEWED_FILES[name]:
                        raise ValueError(f"{name} が確認済みの公式ファイルと一致しません")
            snapshot = files[WEIGHTS_FILE].parent
            if any(path.parent != snapshot for path in files.values()):
                raise ValueError("公式ファイルの取得リビジョンが一致しません")
            return snapshot
        except Exception as exc:
            raise RuntimeError(f"PixAI v1.0公式モデルの取得またはキャッシュ検証に失敗しました: {exc}") from exc

    def load(self):
        if self.pipeline is not None:
            return
        import torch

        cuda_available = self.requested_device != "cpu" and torch.cuda.is_available()
        if self.requested_device == "gpu" and not cuda_available:
            raise RuntimeError("GPU が利用できません。CPU を選ぶか、CUDA対応PyTorchを導入してください。")
        device = "cuda" if cuda_available else "cpu"
        try:
            # ライブラリの診断出力を子プロセスのJSONLに混ぜない。
            with redirect_stdout(sys.stderr):
                from transformers.pipelines import pipeline

                snapshot = str(self._resolve_snapshot())
                loaded = pipeline(
                    model=snapshot, image_processor=snapshot, trust_remote_code=True,
                    device=device, dtype=torch.float32,
                    local_files_only=True, model_kwargs={"use_safetensors": True},
                )
                if loaded.model.device.type != device:
                    raise RuntimeError(f"指定デバイスと実行デバイスが異なります: {loaded.model.device}")
        except Exception as exc:
            # GPUロードの例外は常駐ワーカーにモデル参照を残さない。
            loaded = None
            self.device_name = device
            self.close()
            raise RuntimeError(f"PixAI v1.0公式モデルを読み込めません ({device}): {exc}") from exc
        self.pipeline = loaded
        self.device_name = device

    def predict_image(self, image_path, *, categories=None):
        import numpy as np
        from PIL import Image

        settings = default_categories()
        if categories is not None:
            if set(categories) - set(CATEGORY_ORDER) or any(not isinstance(v, CategorySettings) for v in categories.values()):
                raise ValueError("不正なカテゴリー設定")
            settings.update(categories)
        self.load()
        # 公式Pipelineの比較は >。直前のFP32値を渡して既存の >= を維持する。
        # OFFは確率の上限1を渡して、公式Pipeline内で候補を抑止する。
        thresholds = {
            name: np.nextafter(np.float32(setting.threshold), np.float32(-np.inf)).item()
            if setting.enabled else 1.0
            for name, setting in settings.items()
        }
        try:
            with Image.open(image_path) as image, redirect_stdout(sys.stderr):
                output = self.pipeline(image, threshold=thresholds)
            return self._parse_result(image_path, output, settings)
        except Exception as exc:
            raise RuntimeError(f"PixAI v1.0推論失敗: {image_path}: {exc}") from exc

    @staticmethod
    def _parse_result(image_path, output, settings):
        grouped = output.get("results") if isinstance(output, dict) else None
        if not isinstance(grouped, dict) or set(grouped) != set(CATEGORY_ORDER):
            raise ValueError("公式Pipelineの6カテゴリー出力が不正です")
        result = PixAIV1Result(Path(image_path))
        count = 0
        for category, tags in grouped.items():
            if not isinstance(tags, dict):
                raise ValueError(f"{category}のタグ出力が不正です")
            count += len(tags)
            candidates = result.candidates_by_category[category]
            for raw_tag, score in tags.items():
                tag = " ".join(raw_tag.replace("_", " ").split()) if isinstance(raw_tag, str) else ""
                if not tag or type(score) not in (int, float) or not 0 <= score <= 1:
                    raise ValueError(f"{category}のタグ名または確率が不正です")
                if settings[category].enabled:
                    candidates.append(TagCandidate(tag, float(score), category))
            candidates.sort(key=lambda candidate: -candidate.score)
        if count > TAG_COUNT:
            raise ValueError(f"公式Pipelineの出力タグ件数が上限を超えています: {count}")
        return result

    def close(self):
        device, self.device_name = self.device_name, ""
        self.pipeline = None
        if device == "cuda":
            import torch

            gc.collect()
            torch.cuda.empty_cache()
