from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json


TAG_PRESET_SCHEMA_VERSION = 2
TAG_PRESET_SLOTS_PER_TAB = 8
TAG_PRESET_TAB_COUNT = 4
DEFAULT_TAG_PRESET_SLOTS = TAG_PRESET_SLOTS_PER_TAB * TAG_PRESET_TAB_COUNT


def default_tag_preset_tab_names() -> list[str]:
    """各タブが受け持つプリセット番号の範囲を初期名として返す。"""
    return [
        f"{index * TAG_PRESET_SLOTS_PER_TAB + 1}～{(index + 1) * TAG_PRESET_SLOTS_PER_TAB}"
        for index in range(TAG_PRESET_TAB_COUNT)
    ]


def default_tag_presets_path() -> Path:
    """タグプリセット設定の保存先を返す。"""
    return Path(__file__).resolve().parent / "config" / "tag_presets.json"


@dataclass(slots=True)
class TagPresetEntry:
    text: str
    checked: bool = True
    metadata: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> "TagPresetEntry | None":
        if isinstance(data, str):
            text = data.strip()
            return cls(text=text) if text else None
        if not isinstance(data, dict):
            return None

        text = str(data.get("text") or "").strip()
        if not text:
            return None
        checked = data.get("checked")
        metadata = data.get("metadata")
        return cls(
            text=text,
            checked=checked if isinstance(checked, bool) else True,
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "text": self.text,
            "checked": bool(self.checked),
        }
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data


@dataclass(slots=True)
class TagPreset:
    preset_id: str
    name: str
    tags: list[TagPresetEntry] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object, *, fallback_id: str, fallback_name: str) -> "TagPreset":
        if not isinstance(data, dict):
            return cls(preset_id=fallback_id, name=fallback_name)

        preset_id = str(data.get("id") or fallback_id).strip() or fallback_id
        name = str(data.get("name") or fallback_name).strip() or fallback_name
        raw_tags = data.get("tags")
        entries: list[TagPresetEntry] = []
        seen: set[str] = set()
        if isinstance(raw_tags, list):
            for raw_entry in raw_tags:
                entry = TagPresetEntry.from_dict(raw_entry)
                if entry is None or entry.text in seen:
                    continue
                entries.append(entry)
                seen.add(entry.text)

        metadata = data.get("metadata")
        return cls(
            preset_id=preset_id,
            name=name,
            tags=entries,
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "id": self.preset_id,
            "name": self.name,
            "tags": [entry.to_dict() for entry in self.tags],
        }
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data


@dataclass(slots=True)
class TagPresetState:
    presets: list[TagPreset]
    selected_preset_id: str
    tab_names: list[str] = field(default_factory=default_tag_preset_tab_names)
    selected_tab_index: int = 0

    def selected_index(self) -> int:
        for index, preset in enumerate(self.presets):
            if preset.preset_id == self.selected_preset_id:
                return index
        return 0

    def selected_preset(self) -> TagPreset:
        self.ensure_minimum_slots()
        return self.presets[self.selected_index()]

    def ensure_minimum_slots(self, count: int = DEFAULT_TAG_PRESET_SLOTS) -> None:
        """旧データの1～8を維持しつつ不足分を追加、タブ選択情報を補完する。"""
        count = max(DEFAULT_TAG_PRESET_SLOTS, count)
        used_ids = {preset.preset_id for preset in self.presets}
        slot = 1
        while len(self.presets) < count:
            while f"preset-{slot}" in used_ids:
                slot += 1
            preset_id = f"preset-{slot}"
            self.presets.append(TagPreset(preset_id=preset_id, name=f"プリセット{slot}"))
            used_ids.add(preset_id)
            slot += 1

        default_names = default_tag_preset_tab_names()
        names = self.tab_names if isinstance(self.tab_names, list) else []
        normalized_names: list[str] = []
        for tab_index, default_name in enumerate(default_names):
            value = names[tab_index] if tab_index < len(names) else default_name
            normalized_names.append(
                value.strip() if isinstance(value, str) and value.strip() else default_name
            )
        self.tab_names = normalized_names

        # 選択中IDは従来のJSONにも存在するので、旧データ移行時もそのまま優先する。
        visible_count = TAG_PRESET_TAB_COUNT * TAG_PRESET_SLOTS_PER_TAB
        selectable = self.presets[:visible_count]
        selectable_ids = {preset.preset_id for preset in selectable}
        if self.selected_preset_id not in selectable_ids:
            self.selected_preset_id = selectable[0].preset_id

        # 表示ページと適用中のプリセットは独立して保持する。
        # タブを切り替えても現在のプリセットIDを変更しない。
        if type(self.selected_tab_index) is not int or not (
            0 <= self.selected_tab_index < TAG_PRESET_TAB_COUNT
        ):
            self.selected_tab_index = 0

    def select_index(self, index: int) -> bool:
        """プリセットボタンのクリック時だけ適用中のプリセットを変更する。"""
        self.ensure_minimum_slots()
        if not (0 <= index < DEFAULT_TAG_PRESET_SLOTS):
            return False
        next_id = self.presets[index].preset_id
        if self.selected_preset_id == next_id:
            return False
        self.selected_preset_id = next_id
        return True

    def select_tab_index(self, index: int) -> bool:
        """ボタン表示ページのみを変更し、適用中プリセットは維持する。"""
        if not (0 <= index < TAG_PRESET_TAB_COUNT):
            return False
        self.ensure_minimum_slots()
        if self.selected_tab_index == index:
            return False
        self.selected_tab_index = index
        return True

    def rename_tab(self, index: int, name: str) -> bool:
        if not (0 <= index < TAG_PRESET_TAB_COUNT):
            return False
        normalized = name.strip()
        if not normalized or self.tab_names[index] == normalized:
            return False
        self.tab_names[index] = normalized
        return True

    def to_dict(self) -> dict[str, object]:
        self.ensure_minimum_slots()
        return {
            "version": TAG_PRESET_SCHEMA_VERSION,
            "selected_preset_id": self.selected_preset_id,
            "selected_tab_index": self.selected_tab_index,
            "tab_names": list(self.tab_names),
            "presets": [preset.to_dict() for preset in self.presets],
        }


def _default_presets(count: int = DEFAULT_TAG_PRESET_SLOTS) -> list[TagPreset]:
    return [
        TagPreset(preset_id=f"preset-{index}", name=f"プリセット{index}")
        for index in range(1, count + 1)
    ]


def default_tag_preset_state() -> TagPresetState:
    presets = _default_presets()
    state = TagPresetState(presets=presets, selected_preset_id=presets[0].preset_id)
    state.ensure_minimum_slots()
    return state


def load_tag_preset_state(path: Path | None = None) -> TagPresetState:
    """タグプリセットを読み込む。存在しない/壊れている場合は既定値を返す。"""
    preset_path = path or default_tag_presets_path()
    try:
        raw = json.loads(preset_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default_tag_preset_state()

    if not isinstance(raw, dict):
        return default_tag_preset_state()

    raw_presets = raw.get("presets")
    presets: list[TagPreset] = []
    used_ids: set[str] = set()
    if isinstance(raw_presets, list):
        for index, raw_preset in enumerate(raw_presets, start=1):
            fallback_id = f"preset-{index}"
            preset = TagPreset.from_dict(
                raw_preset,
                fallback_id=fallback_id,
                fallback_name=f"プリセット{index}",
            )
            if preset.preset_id in used_ids:
                suffix = index
                candidate = f"preset-{suffix}"
                while candidate in used_ids:
                    suffix += 1
                    candidate = f"preset-{suffix}"
                preset.preset_id = candidate
            used_ids.add(preset.preset_id)
            presets.append(preset)

    if not presets:
        return default_tag_preset_state()

    selected_id = str(raw.get("selected_preset_id") or presets[0].preset_id)
    raw_tab_names = raw.get("tab_names")
    raw_tab_index = raw.get("selected_tab_index")
    # v1にはページ情報がないため、従来の選択済みプリセットの所属ページを初期表示する。
    if type(raw_tab_index) is not int:
        raw_tab_index = next(
            (i // TAG_PRESET_SLOTS_PER_TAB for i, preset in enumerate(presets)
             if preset.preset_id == selected_id), 0
        )
    state = TagPresetState(
        presets=presets,
        selected_preset_id=selected_id,
        tab_names=raw_tab_names if isinstance(raw_tab_names, list) else default_tag_preset_tab_names(),
        selected_tab_index=raw_tab_index,
    )
    state.ensure_minimum_slots()
    return state


def save_tag_preset_state(
    state: TagPresetState,
    path: Path | None = None,
) -> Path:
    """タグプリセットをJSONへ保存する。途中書き込みを避けるため一時ファイル経由で置換する。"""
    preset_path = path or default_tag_presets_path()
    preset_path.parent.mkdir(parents=True, exist_ok=True)

    normalized = state.to_dict()
    text = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
    temp_path = preset_path.with_suffix(preset_path.suffix + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(preset_path)
    return preset_path
