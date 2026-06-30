from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re


DEFAULT_UNKNOWN_CATEGORY = "unknown"

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_TAG_KEY_WHITESPACE_RE = re.compile(r"\s+")


def default_tag_color_dictionary_path() -> Path:
    """タグ色分け辞書の標準配置先を返す。"""
    return Path(__file__).resolve().parent / "resources" / "dictionaries" / "tag_color_dictionary.json"


def normalize_tag_lookup_key(tag: str) -> str:
    """
    色分け辞書参照用のタグキーへ正規化する。

    画面表示やtxt内のタグは変更せず、辞書検索時だけ
    Danbooru/WD Tagger系の underscore 区切りに寄せる。

    例:
        "yellow eyes" -> "yellow_eyes"
        "very long hair" -> "very_long_hair"
        "hakurei_reimu" -> "hakurei_reimu"
    """
    normalized = _TAG_KEY_WHITESPACE_RE.sub("_", tag.strip())
    return normalized.casefold()


def _normalize_category_id(value: object) -> str:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    return DEFAULT_UNKNOWN_CATEGORY


def _normalize_color(value: object, *, default: str = "#2b2b2b") -> str:
    if isinstance(value, str) and _HEX_COLOR_RE.match(value.strip()):
        return value.strip()
    return default


def _mix_channel(value: int, target: int, ratio: float) -> int:
    return max(0, min(255, round(value + (target - value) * ratio)))


def _mix_color(hex_color: str, target: str, ratio: float) -> str:
    color = _normalize_color(hex_color)
    target = _normalize_color(target, default="#ffffff")
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    tr = int(target[1:3], 16)
    tg = int(target[3:5], 16)
    tb = int(target[5:7], 16)
    return f"#{_mix_channel(r, tr, ratio):02x}{_mix_channel(g, tg, ratio):02x}{_mix_channel(b, tb, ratio):02x}"


def _dark_neon_surface(accent_color: str, *, ratio: float) -> str:
    """ネオン系のアクセント色から、黒背景に馴染む暗い面色を作る。

    color をそのまま背景にするとベタ塗り感が強く汚く見えやすいので、
    辞書の color は「発光アクセント」として扱い、チップ面は暗い背景へ
    少しだけ色を乗せる。
    """
    return _mix_color("#0b1018", accent_color, ratio)


def _selected_dash_color(accent_color: str) -> str:
    """選択状態の破線用に、カテゴリのアクセント色を少しだけ明るくする。"""
    return _mix_color(accent_color, "#ffffff", 0.34)


def _readable_text_color(background: str) -> str:
    color = _normalize_color(background)
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    # WCAGの相対輝度に近い簡易判定。ダークテーマでは基本明るい文字になる。
    brightness = (r * 299 + g * 587 + b * 114) / 1000
    return "#171717" if brightness >= 160 else "#e8e8e8"


@dataclass(frozen=True)
class TagCategoryStyle:
    category_id: str
    label: str
    color: str
    text_color: str
    border_color: str
    hover_color: str
    pressed_color: str

    @classmethod
    def from_category(cls, category_id: str, category_data: dict[str, object]) -> "TagCategoryStyle":
        accent_color = _normalize_color(category_data.get("color"))
        surface_color = _dark_neon_surface(accent_color, ratio=0.30)
        return cls(
            category_id=category_id,
            label=str(category_data.get("label") or category_id),
            # color はQSS上の背景色として使う。辞書の color はアクセント色として扱い、
            # ここで黒背景になじむネオン調の暗い面色へ変換する。
            color=surface_color,
            text_color="#f4fbff",
            border_color=accent_color,
            hover_color=_dark_neon_surface(accent_color, ratio=0.44),
            pressed_color=_dark_neon_surface(accent_color, ratio=0.24),
        )


class TagColorDictionary:
    """タグ名から表示カテゴリとカテゴリ色を引くための軽量辞書。"""

    def __init__(self, categories: dict[str, TagCategoryStyle], tags: dict[str, str]) -> None:
        self._categories = dict(categories)
        self._tags = dict(tags)
        if DEFAULT_UNKNOWN_CATEGORY not in self._categories:
            self._categories[DEFAULT_UNKNOWN_CATEGORY] = TagCategoryStyle.from_category(
                DEFAULT_UNKNOWN_CATEGORY,
                {"label": "未分類", "color": "#2b2b2b"},
            )

        # Python の dict は挿入順を保持する。
        # tag_color_dictionary.json の categories 記述順を、
        # 「色分け順」の並べ替え順として使う。
        #
        # unknown は並び順から一度除外してから連番を振る。
        # enumerate(self._categories.keys()) しながら unknown だけ飛ばすと、
        # unknown より後ろに書かれた最後のカテゴリが unknown と同じ
        # ソート位置になることがあるため。
        ordered_category_ids = [
            category_id
            for category_id in self._categories.keys()
            if category_id != DEFAULT_UNKNOWN_CATEGORY
        ]
        self._category_sort_order: dict[str, int] = {
            category_id: index
            for index, category_id in enumerate(ordered_category_ids)
        }
        # unknown は常に最後へ送る。
        self._unknown_sort_index = len(ordered_category_ids)

    @classmethod
    def empty(cls) -> "TagColorDictionary":
        return cls(
            {
                DEFAULT_UNKNOWN_CATEGORY: TagCategoryStyle.from_category(
                    DEFAULT_UNKNOWN_CATEGORY,
                    {"label": "未分類", "color": "#2b2b2b"},
                )
            },
            {},
        )

    @classmethod
    def load(cls, path: Path | None = None) -> "TagColorDictionary":
        dictionary_path = path or default_tag_color_dictionary_path()
        try:
            raw = json.loads(dictionary_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return cls.empty()

        if not isinstance(raw, dict):
            return cls.empty()

        raw_categories = raw.get("categories")
        raw_tags = raw.get("tags")
        if not isinstance(raw_categories, dict) or not isinstance(raw_tags, dict):
            return cls.empty()

        categories: dict[str, TagCategoryStyle] = {}
        for category_id, category_data in raw_categories.items():
            normalized_id = _normalize_category_id(category_id)
            if not isinstance(category_data, dict):
                continue
            categories[normalized_id] = TagCategoryStyle.from_category(normalized_id, category_data)

        tags: dict[str, str] = {}
        for tag, category_id in raw_tags.items():
            if not isinstance(tag, str) or not tag.strip():
                continue
            normalized_tag = normalize_tag_lookup_key(tag)
            if not normalized_tag:
                continue
            tags[normalized_tag] = _normalize_category_id(category_id)

        return cls(categories, tags)

    def category_id_for_tag(self, tag: str) -> str:
        normalized_tag = normalize_tag_lookup_key(tag)
        category_id = self._tags.get(normalized_tag, DEFAULT_UNKNOWN_CATEGORY)
        if category_id not in self._categories:
            return DEFAULT_UNKNOWN_CATEGORY
        return category_id

    def style_for_tag(self, tag: str) -> TagCategoryStyle:
        return self._categories[self.category_id_for_tag(tag)]

    def categories(self) -> dict[str, TagCategoryStyle]:
        return dict(self._categories)

    def category_sort_index_for_tag(self, tag: str) -> int:
        """タグの色分けカテゴリ順インデックスを返す。

        JSON の categories に書かれている順を表示順として使う。
        辞書にないタグ、または存在しないカテゴリを指しているタグは最後に並べる。
        """
        category_id = self.category_id_for_tag(tag)
        if category_id == DEFAULT_UNKNOWN_CATEGORY:
            return self._unknown_sort_index
        return self._category_sort_order.get(category_id, self._unknown_sort_index)

    def sort_key_for_tag(self, tag: str) -> tuple[int, str]:
        """色分け順ソート用キー。

        同じカテゴリ内では、画面上のタグ名を大文字小文字無視で名前順にする。
        """
        return (self.category_sort_index_for_tag(tag), tag.casefold())

    def tag_count(self) -> int:
        return len(self._tags)

    def is_empty(self) -> bool:
        return not self._tags


def tag_chip_qss(style: TagCategoryStyle) -> str | None:
    if style.category_id == DEFAULT_UNKNOWN_CATEGORY:
        return None

    selected_border_color = _selected_dash_color(style.border_color)

    return f"""
QPushButton {{
    padding: 6px 13px;
    border: 1px solid {style.border_color};
    border-radius: 9px;
    background: {style.color};
    color: {style.text_color};
    text-align: center;
}}

QPushButton:hover {{
    background: {style.hover_color};
    border-color: {style.border_color};
}}

QPushButton:checked {{
    padding: 5px 12px;
    background: {style.color};
    border: 2px dashed {selected_border_color};
    border-radius: 5px;
    color: {style.text_color};
}}

QPushButton:checked:hover {{
    padding: 5px 12px;
    background: {style.hover_color};
    border: 2px dashed {selected_border_color};
    border-radius: 5px;
    color: #ffffff;
}}
"""


def all_tag_chip_qss(style: TagCategoryStyle) -> str | None:
    # 右側タグ一覧は、緑/赤のフィルタボタン・区切り線・件数バッジを含むため、
    # チップ全体にカテゴリ色を載せると色の情報量が多くなりすぎる。
    # 右側では外枠は既存テーマのままにし、中央のタグ名ボタンだけをカテゴリ色にする。
    return None


def all_tag_button_qss(style: TagCategoryStyle) -> str | None:
    if style.category_id == DEFAULT_UNKNOWN_CATEGORY:
        return None

    selected_border_color = _selected_dash_color(style.border_color)

    return f"""
QPushButton {{
    padding: 6px 9px;
    border: 1px solid {style.border_color};
    border-radius: 6px;
    background: {style.color};
    color: {style.text_color};
}}

QPushButton:hover {{
    background: {style.hover_color};
    border-color: {style.border_color};
    color: white;
}}

QPushButton:checked {{
    padding: 5px 8px;
    background: {style.color};
    border: 2px dashed {selected_border_color};
    color: {style.text_color};
    border-radius: 4px;
}}

QPushButton:checked:hover {{
    padding: 5px 8px;
    background: {style.hover_color};
    border: 2px dashed {selected_border_color};
    color: #ffffff;
    border-radius: 4px;
}}
"""
