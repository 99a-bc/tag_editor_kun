from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import time

from dev_config import dev_log_enabled
from logger import get_logger
from .result import TagCandidate, TaggerResult


log = get_logger(__name__)

PIXAI_MODEL_REPO = "pixai-labs/pixai-tagger-v0.9"
PIXAI_WEIGHTS_FILENAME = "model_v0.9.pth"
PIXAI_TAGS_FILENAME = "tags_v0.9_13k.json"
PIXAI_CHAR_IP_MAP_FILENAME = "char_ip_map.json"
PIXAI_MODEL_FILENAMES = (
    PIXAI_WEIGHTS_FILENAME,
    PIXAI_TAGS_FILENAME,
    PIXAI_CHAR_IP_MAP_FILENAME,
)
PIXAI_BASE_ENCODER_REPO = "hf_hub:SmilingWolf/wd-eva02-large-tagger-v3"


def pixai_model_is_cached() -> bool:
    """PixAIの必須ファイルがHugging Faceキャッシュに揃っているかを返す。

    ネットワーク接続やダウンロードは行わない。未導入時にもTagger画面を開けるよう、
    状態確認に失敗した場合は未導入として扱う。
    """
    try:
        from huggingface_hub import try_to_load_from_cache
    except Exception:
        return False

    for filename in PIXAI_MODEL_FILENAMES:
        try:
            cached_path = try_to_load_from_cache(
                repo_id=PIXAI_MODEL_REPO,
                filename=filename,
            )
            if not isinstance(cached_path, str) or not Path(cached_path).is_file():
                return False
        except Exception:
            return False

    return True


@dataclass(frozen=True)
class _TagRow:
    name: str
    category: str


class PixAITaggerBackend:
    # general / character に加え、character候補をchar_ip_map.jsonで
    # 逆引きした作品タグ（copyright / IP）の補助候補も返す。

    def __init__(self, model_repo: str = PIXAI_MODEL_REPO, device: str = "auto") -> None:
        self.model_repo = str(model_repo or "").strip() or PIXAI_MODEL_REPO
        self.requested_device = (device or "auto").strip().lower()
        self.device_name = ""
        self.device = None
        self.model = None
        self.transform = None
        self.tag_rows: list[_TagRow] = []
        self._character_ip_map: dict[str, list[str]] = {}

    @staticmethod
    def _normalize_output_tag(tag: str) -> str:
        return " ".join(str(tag).replace("_", " ").split())

    @staticmethod
    def _ensure_rgb_with_white_background(image):
        from PIL import Image

        if image.mode == "RGBA":
            image.load()
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[3])
            return background

        if image.mode == "P":
            image = image.convert("RGBA")
            image.load()
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[3])
            return background

        return image.convert("RGB")

    def _load_runtime(self):
        try:
            import torch
            from huggingface_hub import hf_hub_download
            from PIL import Image  # noqa: F401
            from timm import create_model
            import torchvision.transforms as transforms
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "PixAI Taggerの依存ライブラリが見つかりません。install.bat を再実行してください。"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                "PixAI Taggerの依存ライブラリを読み込めませんでした。"
                f"install.bat を再実行してください。\n{exc}"
            ) from exc

        return torch, hf_hub_download, create_model, transforms

    def _resolve_device(self, torch) -> str:
        if self.requested_device == "gpu":
            if not torch.cuda.is_available():
                raise RuntimeError(
                    "GPU が利用できません。CPU を選ぶか、CUDA対応PyTorchを導入してください。"
                )
            return "cuda"
        if self.requested_device == "cpu":
            return "cpu"
        return "cuda" if torch.cuda.is_available() else "cpu"

    @staticmethod
    def _load_tag_rows(tags_path: Path) -> list[_TagRow]:
        try:
            with tags_path.open("r", encoding="utf-8") as handle:
                tag_info = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"PixAIのタグ定義を読み込めませんでした。\n{tags_path}\n{exc}"
            ) from exc

        tag_map = tag_info.get("tag_map")
        tag_split = tag_info.get("tag_split")
        if not isinstance(tag_map, dict) or not isinstance(tag_split, dict):
            raise RuntimeError("PixAIのtags_v0.9_13k.jsonの形式が不正です。")

        try:
            general_count = int(tag_split["gen_tag_count"])
            character_count = int(tag_split["character_tag_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("PixAIのタグ分類数を読み取れませんでした。") from exc

        total_count = general_count + character_count
        if general_count <= 0 or character_count <= 0:
            raise RuntimeError(
                "PixAIのタグ分類数が不正です: "
                f"general={general_count}, character={character_count}"
            )

        index_to_tag: dict[int, str] = {}
        for raw_tag, raw_index in tag_map.items():
            try:
                index = int(raw_index)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"PixAIのタグ番号が不正です: {raw_tag!r} -> {raw_index!r}"
                ) from exc
            index_to_tag[index] = str(raw_tag)

        if len(index_to_tag) != total_count or set(index_to_tag) != set(range(total_count)):
            raise RuntimeError(
                "PixAIのタグ定義数が分類情報と一致しません: "
                f"tags={len(index_to_tag)}, expected={total_count}"
            )

        return [
            _TagRow(
                name=index_to_tag[index],
                category="general" if index < general_count else "character",
            )
            for index in range(total_count)
        ]

    @staticmethod
    def _load_character_ip_map(mapping_path: Path) -> dict[str, list[str]]:
        try:
            with mapping_path.open("r", encoding="utf-8") as handle:
                raw_mapping = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"PixAIのcharacter-IP対応表を読み込めませんでした。\n{mapping_path}\n{exc}"
            ) from exc

        if not isinstance(raw_mapping, dict):
            raise RuntimeError("PixAIのchar_ip_map.jsonの形式が不正です。")

        return {
            str(character): [
                str(ip_tag)
                for ip_tag in ips
                if isinstance(ip_tag, str) and ip_tag.strip()
            ]
            for character, ips in raw_mapping.items()
            if isinstance(character, str) and isinstance(ips, list)
        }

    def _download_model_files(self, hf_hub_download) -> tuple[Path, Path, Path]:
        try:
            weights_path = Path(
                hf_hub_download(
                    repo_id=self.model_repo,
                    filename=PIXAI_WEIGHTS_FILENAME,
                )
            )
            tags_path = Path(
                hf_hub_download(
                    repo_id=self.model_repo,
                    filename=PIXAI_TAGS_FILENAME,
                )
            )
            ip_map_path = Path(
                hf_hub_download(
                    repo_id=self.model_repo,
                    filename=PIXAI_CHAR_IP_MAP_FILENAME,
                )
            )
        except Exception as exc:
            raise RuntimeError(
                "PixAI Tagger v0.9のモデルファイルをHugging Faceキャッシュから取得できませんでした。\n"
                "モデルページでアクセス許可を済ませたアカウントで、"
                "先に hf_login.bat と download_pixai_tagger.bat を実行してください。\n"
                f"{exc}"
            ) from exc

        return weights_path, tags_path, ip_map_path

    @staticmethod
    def _build_model(torch, create_model, *, num_tags: int):
        # 公式handler.pyと同じencoder + 分類head構成。
        class TaggingHead(torch.nn.Module):
            def __init__(self, input_dim: int, num_classes: int) -> None:
                super().__init__()
                self.head = torch.nn.Sequential(
                    torch.nn.Linear(input_dim, num_classes)
                )

            def forward(self, value):
                return torch.nn.functional.sigmoid(self.head(value))

        encoder = create_model(PIXAI_BASE_ENCODER_REPO, pretrained=False)
        encoder.reset_classifier(0)

        input_dim = int(getattr(encoder, "num_features", 0))
        if input_dim != 1024:
            raise RuntimeError(
                "PixAI v0.9が想定するencoder出力次元と一致しません: "
                f"{input_dim}（期待値: 1024）"
            )

        return torch.nn.Sequential(encoder, TaggingHead(input_dim, num_tags))

    def load(self) -> None:
        perf_started = time.perf_counter() if dev_log_enabled("TAGGER_PERF") else 0.0
        torch, hf_hub_download, create_model, transforms = self._load_runtime()

        self.device_name = self._resolve_device(torch)
        self.device = torch.device(self.device_name)

        files_started = time.perf_counter() if perf_started else 0.0
        weights_path, tags_path, ip_map_path = self._download_model_files(hf_hub_download)
        files_ms = (time.perf_counter() - files_started) * 1000 if files_started else 0.0

        metadata_started = time.perf_counter() if perf_started else 0.0
        self.tag_rows = self._load_tag_rows(tags_path)
        self._character_ip_map = self._load_character_ip_map(ip_map_path)
        metadata_ms = (time.perf_counter() - metadata_started) * 1000 if metadata_started else 0.0

        model_started = time.perf_counter() if perf_started else 0.0
        self.model = self._build_model(
            torch,
            create_model,
            num_tags=len(self.tag_rows),
        )

        try:
            state_dict = torch.load(
                weights_path,
                map_location="cpu",
                weights_only=True,
            )
        except TypeError:
            state_dict = torch.load(weights_path, map_location="cpu")

        if isinstance(state_dict, dict) and isinstance(state_dict.get("state_dict"), dict):
            state_dict = state_dict["state_dict"]

        if not isinstance(state_dict, dict):
            raise RuntimeError(f"PixAIの重みファイル形式が不正です: {weights_path}")

        incompatible = self.model.load_state_dict(state_dict, strict=True)
        if incompatible.missing_keys or incompatible.unexpected_keys:
            raise RuntimeError(
                "PixAIの重みキーがモデル構成と一致しません。\n"
                f"missing={incompatible.missing_keys}\n"
                f"unexpected={incompatible.unexpected_keys}"
            )

        self.model.eval()
        self.model.to(self.device)
        model_ms = (time.perf_counter() - model_started) * 1000 if model_started else 0.0

        # 公式handler.py準拠。余白追加は行わず、448x448へ直接Resize。
        self.transform = transforms.Compose(
            [
                transforms.Resize((448, 448)),
                transforms.ToTensor(),
                transforms.Normalize(
                    mean=[0.5, 0.5, 0.5],
                    std=[0.5, 0.5, 0.5],
                ),
            ]
        )

        if perf_started:
            log.info(
                "[TAGGER_PERF] pixai_backend_load repo=%s device=%s files=%.2fms "
                "metadata=%.2fms model=%.2fms tags=%d character_ip=%d total=%.2fms",
                self.model_repo,
                self.device_name,
                files_ms,
                metadata_ms,
                model_ms,
                len(self.tag_rows),
                len(self._character_ip_map),
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

        torch, _, _, _ = self._load_runtime()
        from PIL import Image

        preprocess_started = time.perf_counter() if perf_started else 0.0
        with Image.open(image_path) as image:
            image = self._ensure_rgb_with_white_background(image)
            tensor = self.transform(image).unsqueeze(0)

        if self.device_name == "cuda":
            tensor = tensor.pin_memory().to(self.device, non_blocking=True)
        else:
            tensor = tensor.to(self.device)
        preprocess_ms = (
            (time.perf_counter() - preprocess_started) * 1000
            if preprocess_started
            else 0.0
        )

        inference_started = time.perf_counter() if perf_started else 0.0
        with torch.inference_mode():
            # 分類headはsigmoid済みの確率を返すため、WDのような再sigmoidは不要。
            outputs = self.model(tensor)
            if isinstance(outputs, (tuple, list)):
                outputs = outputs[0]
            scores = outputs.squeeze(0).detach().float().cpu().tolist()
        inference_ms = (
            (time.perf_counter() - inference_started) * 1000
            if inference_started
            else 0.0
        )

        if len(scores) != len(self.tag_rows):
            raise RuntimeError(
                f"PixAIのモデル出力数 ({len(scores)}) とタグ定義数 ({len(self.tag_rows)}) が一致しません。"
            )

        candidate_started = time.perf_counter() if perf_started else 0.0
        general_candidates: list[TagCandidate] = []
        character_candidates: list[TagCandidate] = []
        # raw IP名 -> {表示済みキャラ名: 最大スコア}
        copyright_sources: dict[str, dict[str, float]] = {}

        for score, tag_row in zip(scores, self.tag_rows):
            score_value = float(score)
            if (
                include_general
                and tag_row.category == "general"
                and score_value >= float(general_threshold)
            ):
                general_candidates.append(
                    TagCandidate(
                        self._normalize_output_tag(tag_row.name),
                        score_value,
                        "general",
                    )
                )
                continue

            if (
                tag_row.category != "character"
                or score_value < float(character_threshold)
            ):
                continue

            character_tag = self._normalize_output_tag(tag_row.name)
            if include_character:
                character_candidates.append(
                    TagCandidate(
                        character_tag,
                        score_value,
                        "character",
                    )
                )

            # 作品タグはモデルの直接出力ではなく、しきい値を通過した
            # character候補からだけ導出する。character表示自体がOFFでも、
            # 作品タグだけ欲しい用途に備えて逆引きは可能にしておく。
            if include_copyright:
                for raw_ip_tag in self._character_ip_map.get(tag_row.name, []):
                    source_scores = copyright_sources.setdefault(raw_ip_tag, {})
                    previous_score = source_scores.get(character_tag, -1.0)
                    if score_value > previous_score:
                        source_scores[character_tag] = score_value

        general_candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        character_candidates.sort(key=lambda candidate: candidate.score, reverse=True)

        copyright_candidates: list[TagCandidate] = []
        for raw_ip_tag, source_scores in copyright_sources.items():
            sorted_sources = sorted(
                source_scores.items(),
                key=lambda pair: (-pair[1], pair[0]),
            )
            if not sorted_sources:
                continue

            copyright_candidates.append(
                TagCandidate(
                    self._normalize_output_tag(raw_ip_tag),
                    sorted_sources[0][1],
                    "copyright",
                    tuple(source_tag for source_tag, _score in sorted_sources),
                )
            )

        copyright_candidates.sort(
            key=lambda candidate: (-candidate.score, candidate.tag)
        )
        candidate_ms = (
            (time.perf_counter() - candidate_started) * 1000
            if candidate_started
            else 0.0
        )

        result = TaggerResult(
            image_path=Path(image_path),
            general_candidates=general_candidates,
            character_candidates=character_candidates,
            copyright_candidates=copyright_candidates,
        )

        if perf_started:
            log.info(
                "[TAGGER_PERF] pixai_backend_predict path=%s preprocess=%.2fms "
                "inference=%.2fms candidates=%.2fms general=%d character=%d copyright=%d total=%d elapsed=%.2fms",
                image_path.name,
                preprocess_ms,
                inference_ms,
                candidate_ms,
                len(general_candidates),
                len(character_candidates),
                len(copyright_candidates),
                len(result.all_candidates),
                (time.perf_counter() - perf_started) * 1000,
            )

        return result
