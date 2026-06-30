from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv
import time

from dev_config import dev_log_enabled
from logger import get_logger
from .result import TagCandidate, TaggerResult


log = get_logger(__name__)


@dataclass(frozen=True)
class _TagRow:
    name: str
    category: str


class WDTaggerBackend:
    """timm版 WD EVA02 Tagger v3 の単一モデル推論器。"""

    def __init__(self, model_repo: str, device: str = "auto") -> None:
        self.model_repo = model_repo.strip() or "SmilingWolf/wd-eva02-large-tagger-v3"
        self.requested_device = (device or "auto").strip().lower()
        self.device_name = ""
        self.device = None
        self.model = None
        self.transform = None
        self.tag_rows: list[_TagRow] = []

    @staticmethod
    def _ensure_rgb_with_white_background(image):
        from PIL import Image

        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA") if "transparency" in image.info else image.convert("RGB")

        if image.mode == "RGBA":
            canvas = Image.new("RGBA", image.size, (255, 255, 255, 255))
            canvas.alpha_composite(image)
            image = canvas.convert("RGB")

        return image

    @staticmethod
    def _pad_square_white(image):
        from PIL import Image

        width, height = image.size
        size = max(width, height)
        canvas = Image.new("RGB", (size, size), (255, 255, 255))
        canvas.paste(image, ((size - width) // 2, (size - height) // 2))
        return canvas

    @staticmethod
    def _normalize_output_tag(tag: str) -> str:
        """WDのunderscore区切りタグを、アプリ内で使う空白表記へそろえる。"""
        return " ".join(str(tag).replace("_", " ").split())

    def _load_runtime(self):
        try:
            import torch
            from huggingface_hub import hf_hub_download
            from PIL import Image  # noqa: F401
            from timm import create_model
            from timm.data import create_transform, resolve_data_config
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Tagger依存ライブラリが見つかりません。install.bat を再実行してください。"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"Tagger依存ライブラリを読み込めませんでした。install.bat を再実行してください。\\n{exc}"
            ) from exc

        return torch, hf_hub_download, create_model, create_transform, resolve_data_config

    def _resolve_device(self, torch):
        if self.requested_device == "gpu":
            if not torch.cuda.is_available():
                raise RuntimeError(
                    "GPU が利用できません。CPU を選ぶか、CUDA対応PyTorchを導入してください。"
                )
            return "cuda"
        if self.requested_device == "cpu":
            return "cpu"
        return "cuda" if torch.cuda.is_available() else "cpu"

    def load(self) -> None:
        perf_started = time.perf_counter() if dev_log_enabled("TAGGER_PERF") else 0.0
        torch, hf_hub_download, create_model, create_transform, resolve_data_config = self._load_runtime()

        self.device_name = self._resolve_device(torch)
        self.device = torch.device(self.device_name)

        model_started = time.perf_counter() if perf_started else 0.0
        self.model = create_model(
            f"hf_hub:{self.model_repo}",
            pretrained=True,
        )
        self.model.eval()
        self.model.to(self.device)
        model_ms = (time.perf_counter() - model_started) * 1000 if model_started else 0.0

        transform_started = time.perf_counter() if perf_started else 0.0
        data_config = resolve_data_config(self.model.pretrained_cfg, model=self.model)
        self.transform = create_transform(**data_config)
        transform_ms = (time.perf_counter() - transform_started) * 1000 if transform_started else 0.0

        csv_started = time.perf_counter() if perf_started else 0.0
        csv_path = hf_hub_download(
            repo_id=self.model_repo,
            filename="selected_tags.csv",
        )
        with open(csv_path, "r", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        csv_ms = (time.perf_counter() - csv_started) * 1000 if csv_started else 0.0

        if not rows:
            raise RuntimeError("selected_tags.csv が空です。")

        self.tag_rows = [
            _TagRow(
                name=str(row.get("name", "")).strip(),
                category=str(row.get("category", "")).strip(),
            )
            for row in rows
        ]

        if perf_started:
            log.info(
                "[TAGGER_PERF] backend_load model=%s device=%s model=%.2fms transform=%.2fms tags_csv=%.2fms rows=%d total=%.2fms",
                self.model_repo,
                self.device_name,
                model_ms,
                transform_ms,
                csv_ms,
                len(self.tag_rows),
                (time.perf_counter() - perf_started) * 1000,
            )

    def predict_image(
        self,
        image_path: Path,
        *,
        include_general: bool,
        general_threshold: float,
        include_character: bool,
        character_threshold: float,
        include_copyright: bool = False,
    ) -> TaggerResult:
        perf_started = time.perf_counter() if dev_log_enabled("TAGGER_PERF") else 0.0

        if self.model is None or self.transform is None or self.device is None or not self.tag_rows:
            self.load()

        torch, _, _, _, _ = self._load_runtime()
        from PIL import Image

        preprocess_started = time.perf_counter() if perf_started else 0.0
        with Image.open(image_path) as image:
            image = self._ensure_rgb_with_white_background(image)
            image = self._pad_square_white(image)
            tensor = self.transform(image).unsqueeze(0)

        # WD Tagger v3 の timm推論例に合わせ、NCHW の RGB を BGR へ並び替える。
        tensor = tensor[:, [2, 1, 0]].to(self.device)
        preprocess_ms = (time.perf_counter() - preprocess_started) * 1000 if preprocess_started else 0.0

        inference_started = time.perf_counter() if perf_started else 0.0
        with torch.inference_mode():
            outputs = self.model.forward(tensor)
            if isinstance(outputs, (tuple, list)):
                outputs = outputs[0]
            # CPUへ戻す時点でCUDA処理の完了を待つため、推論時間をここまで含めて測れる。
            scores = torch.sigmoid(outputs).squeeze(0).detach().cpu().tolist()
        inference_ms = (time.perf_counter() - inference_started) * 1000 if inference_started else 0.0

        if len(scores) != len(self.tag_rows):
            raise RuntimeError(
                f"モデル出力数 ({len(scores)}) と selected_tags.csv の行数 ({len(self.tag_rows)}) が一致しません。"
            )

        candidate_started = time.perf_counter() if perf_started else 0.0
        general_candidates: list[TagCandidate] = []
        character_candidates: list[TagCandidate] = []

        for score, tag_row in zip(scores, self.tag_rows):
            score_value = float(score)
            if (
                include_general
                and tag_row.category == "0"
                and score_value >= float(general_threshold)
            ):
                general_candidates.append(
                    TagCandidate(
                        self._normalize_output_tag(tag_row.name),
                        score_value,
                        "general",
                    )
                )
            elif (
                include_character
                and tag_row.category == "4"
                and score_value >= float(character_threshold)
            ):
                character_candidates.append(
                    TagCandidate(
                        self._normalize_output_tag(tag_row.name),
                        score_value,
                        "character",
                    )
                )

        general_candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        character_candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        candidate_ms = (time.perf_counter() - candidate_started) * 1000 if candidate_started else 0.0

        result = TaggerResult(
            image_path=Path(image_path),
            general_candidates=general_candidates,
            character_candidates=character_candidates,
        )

        if perf_started:
            log.info(
                "[TAGGER_PERF] backend_predict path=%s preprocess=%.2fms inference=%.2fms candidates=%.2fms general=%d character=%d total=%d elapsed=%.2fms",
                image_path.name,
                preprocess_ms,
                inference_ms,
                candidate_ms,
                len(general_candidates),
                len(character_candidates),
                len(result.all_candidates),
                (time.perf_counter() - perf_started) * 1000,
            )

        return result
