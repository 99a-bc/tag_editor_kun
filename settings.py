from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json


DEFAULT_THUMBNAIL_WHEEL_SCROLL_PX = 120
MIN_THUMBNAIL_WHEEL_SCROLL_PX = 20
MAX_THUMBNAIL_WHEEL_SCROLL_PX = 600
THUMBNAIL_WHEEL_SCROLL_STEP = 10
DEFAULT_TAG_COLOR_ENABLED = True
DEFAULT_TAG_COMPLETION_ENABLED = True
DEFAULT_TAG_COMPLETION_LIMIT = 12
MIN_TAG_COMPLETION_LIMIT = 1
MAX_TAG_COMPLETION_LIMIT = 30
DEFAULT_RECURSIVE_LOAD_DEPTH = 0
MIN_RECURSIVE_LOAD_DEPTH = 0
MAX_RECURSIVE_LOAD_DEPTH = 99
DEFAULT_TAGGER_MODEL_REPO = "SmilingWolf/wd-eva02-large-tagger-v3"
DEFAULT_TAGGER_DEVICE = "auto"
DEFAULT_TAGGER_INCLUDE_GENERAL = True
DEFAULT_TAGGER_GENERAL_THRESHOLD = 0.35
DEFAULT_TAGGER_INCLUDE_CHARACTER = False
DEFAULT_TAGGER_CHARACTER_THRESHOLD = 0.85
DEFAULT_TAGGER_INCLUDE_COPYRIGHT = False
DEFAULT_TAGGER_COPYRIGHT_EXPANDED = False
DEFAULT_TAGGER_APPLY_MODE = "empty_only"

_TAGGER_DEVICE_VALUES = {"auto", "cpu", "gpu"}
_TAGGER_APPLY_MODE_VALUES = {"empty_only", "overwrite", "append"}


def default_settings_path() -> Path:
    """
    アプリ設定ファイルの保存先を返す。

    ユーザーが場所を把握しやすいように、リポジトリ/アプリ直下の
    config/settings.json へ保存する。

    このファイルは実行時の個人設定なので、Git管理対象にはしない。
    .gitignore に以下を追加する想定:
        config/settings.json
    """
    return Path(__file__).resolve().parent / "config" / "settings.json"


def normalize_thumbnail_wheel_scroll_px(value: object) -> int:
    """サムネイル欄のホイールスクロール量を安全な範囲へ丸める。"""
    try:
        int_value = int(value)
    except (TypeError, ValueError):
        int_value = DEFAULT_THUMBNAIL_WHEEL_SCROLL_PX

    return max(
        MIN_THUMBNAIL_WHEEL_SCROLL_PX,
        min(MAX_THUMBNAIL_WHEEL_SCROLL_PX, int_value),
    )


def normalize_recursive_load_depth(value: object) -> int:
    """サブフォルダ読み込み深さを安全な範囲へ丸める。

    0 は「制限なし」を表す。
    1 以上は、選択フォルダ直下から何階層下まで読むかを表す。
    """
    try:
        int_value = int(value)
    except (TypeError, ValueError):
        int_value = DEFAULT_RECURSIVE_LOAD_DEPTH

    return max(
        MIN_RECURSIVE_LOAD_DEPTH,
        min(MAX_RECURSIVE_LOAD_DEPTH, int_value),
    )


def normalize_tag_completion_limit(value: object) -> int:
    """Danbooru候補の表示件数を安全な範囲へ丸める。"""
    try:
        int_value = int(value)
    except (TypeError, ValueError):
        int_value = DEFAULT_TAG_COMPLETION_LIMIT

    return max(
        MIN_TAG_COMPLETION_LIMIT,
        min(MAX_TAG_COMPLETION_LIMIT, int_value),
    )


def normalize_bool(value: object, *, default: bool = False) -> bool:
    """JSON由来のbool設定を安全に取り出す。"""
    if isinstance(value, bool):
        return value
    return default


def normalize_tagger_model_repo(value: object) -> str:
    """TaggerモデルのリポジトリIDを安全な既定値へ寄せる。"""
    model_repo = str(value or "").strip()
    return model_repo or DEFAULT_TAGGER_MODEL_REPO


def normalize_tagger_device(value: object) -> str:
    """Taggerの実行デバイス選択を正規化する。"""
    device = str(value or "").strip().lower()
    return device if device in _TAGGER_DEVICE_VALUES else DEFAULT_TAGGER_DEVICE


def normalize_tagger_threshold(value: object, *, default: float) -> float:
    """Taggerしきい値を0.0〜1.0へ丸める。"""
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        threshold = default
    return max(0.0, min(1.0, threshold))


def normalize_tagger_apply_mode(value: object) -> str:
    """Tagger反映方法を既知の値へ正規化する。"""
    mode = str(value or "").strip()
    return mode if mode in _TAGGER_APPLY_MODE_VALUES else DEFAULT_TAGGER_APPLY_MODE


@dataclass(slots=True)
class AppSettings:
    thumbnail_wheel_scroll_px: int = DEFAULT_THUMBNAIL_WHEEL_SCROLL_PX
    remember_recursive_load: bool = False
    recursive_load_enabled: bool = False
    recursive_load_depth: int = DEFAULT_RECURSIVE_LOAD_DEPTH
    tag_color_enabled: bool = DEFAULT_TAG_COLOR_ENABLED
    tag_completion_enabled: bool = DEFAULT_TAG_COMPLETION_ENABLED
    tag_completion_limit: int = DEFAULT_TAG_COMPLETION_LIMIT
    tagger_model_repo: str = DEFAULT_TAGGER_MODEL_REPO
    tagger_device: str = DEFAULT_TAGGER_DEVICE
    tagger_include_general: bool = DEFAULT_TAGGER_INCLUDE_GENERAL
    tagger_general_threshold: float = DEFAULT_TAGGER_GENERAL_THRESHOLD
    tagger_include_character: bool = DEFAULT_TAGGER_INCLUDE_CHARACTER
    tagger_character_threshold: float = DEFAULT_TAGGER_CHARACTER_THRESHOLD
    tagger_include_copyright: bool = DEFAULT_TAGGER_INCLUDE_COPYRIGHT
    tagger_copyright_expanded: bool = DEFAULT_TAGGER_COPYRIGHT_EXPANDED
    tagger_apply_mode: str = DEFAULT_TAGGER_APPLY_MODE

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "AppSettings":
        return cls(
            thumbnail_wheel_scroll_px=normalize_thumbnail_wheel_scroll_px(
                data.get("thumbnail_wheel_scroll_px")
            ),
            remember_recursive_load=normalize_bool(
                data.get("remember_recursive_load"),
                default=False,
            ),
            recursive_load_enabled=normalize_bool(
                data.get("recursive_load_enabled"),
                default=False,
            ),
            recursive_load_depth=normalize_recursive_load_depth(
                data.get("recursive_load_depth")
            ),
            tag_color_enabled=normalize_bool(
                data.get("tag_color_enabled"),
                default=DEFAULT_TAG_COLOR_ENABLED,
            ),
            tag_completion_enabled=normalize_bool(
                data.get("tag_completion_enabled"),
                default=DEFAULT_TAG_COMPLETION_ENABLED,
            ),
            tag_completion_limit=normalize_tag_completion_limit(
                data.get("tag_completion_limit")
            ),
            tagger_model_repo=normalize_tagger_model_repo(
                data.get("tagger_model_repo")
            ),
            tagger_device=normalize_tagger_device(data.get("tagger_device")),
            tagger_include_general=normalize_bool(
                data.get("tagger_include_general"),
                default=DEFAULT_TAGGER_INCLUDE_GENERAL,
            ),
            tagger_general_threshold=normalize_tagger_threshold(
                data.get("tagger_general_threshold"),
                default=DEFAULT_TAGGER_GENERAL_THRESHOLD,
            ),
            tagger_include_character=normalize_bool(
                data.get("tagger_include_character"),
                default=DEFAULT_TAGGER_INCLUDE_CHARACTER,
            ),
            tagger_character_threshold=normalize_tagger_threshold(
                data.get("tagger_character_threshold"),
                default=DEFAULT_TAGGER_CHARACTER_THRESHOLD,
            ),
            tagger_include_copyright=normalize_bool(
                data.get("tagger_include_copyright"),
                default=DEFAULT_TAGGER_INCLUDE_COPYRIGHT,
            ),
            tagger_copyright_expanded=normalize_bool(
                data.get("tagger_copyright_expanded"),
                default=DEFAULT_TAGGER_COPYRIGHT_EXPANDED,
            ),
            tagger_apply_mode=normalize_tagger_apply_mode(
                data.get("tagger_apply_mode")
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "thumbnail_wheel_scroll_px": normalize_thumbnail_wheel_scroll_px(
                self.thumbnail_wheel_scroll_px
            ),
            "remember_recursive_load": bool(self.remember_recursive_load),
            "recursive_load_enabled": bool(self.recursive_load_enabled),
            "recursive_load_depth": normalize_recursive_load_depth(
                self.recursive_load_depth
            ),
            "tag_color_enabled": bool(self.tag_color_enabled),
            "tag_completion_enabled": bool(self.tag_completion_enabled),
            "tag_completion_limit": normalize_tag_completion_limit(
                self.tag_completion_limit
            ),
            "tagger_model_repo": normalize_tagger_model_repo(self.tagger_model_repo),
            "tagger_device": normalize_tagger_device(self.tagger_device),
            "tagger_include_general": bool(self.tagger_include_general),
            "tagger_general_threshold": normalize_tagger_threshold(
                self.tagger_general_threshold,
                default=DEFAULT_TAGGER_GENERAL_THRESHOLD,
            ),
            "tagger_include_character": bool(self.tagger_include_character),
            "tagger_character_threshold": normalize_tagger_threshold(
                self.tagger_character_threshold,
                default=DEFAULT_TAGGER_CHARACTER_THRESHOLD,
            ),
            "tagger_include_copyright": bool(self.tagger_include_copyright),
            "tagger_copyright_expanded": bool(self.tagger_copyright_expanded),
            "tagger_apply_mode": normalize_tagger_apply_mode(self.tagger_apply_mode),
        }


def load_app_settings(path: Path | None = None) -> AppSettings:
    """アプリ設定を読み込む。存在しない/壊れている場合は既定値を返す。"""
    settings_path = path or default_settings_path()

    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return AppSettings()
    except (OSError, json.JSONDecodeError):
        return AppSettings()

    if not isinstance(data, dict):
        return AppSettings()

    return AppSettings.from_dict(data)


def save_app_settings(settings: AppSettings, path: Path | None = None) -> Path:
    """アプリ設定をJSONへ保存し、保存先パスを返す。"""
    settings_path = path or default_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    normalized = AppSettings.from_dict(settings.to_dict())
    settings_path.write_text(
        json.dumps(normalized.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return settings_path
