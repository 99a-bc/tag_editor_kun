from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


MAX_TAG_HISTORY_ENTRIES = 100


@dataclass(slots=True)
class TagHistoryEntry:
    """1回のタグ編集操作を、Undo用スナップショット付きで保持する。"""

    operation_type: str
    summary: str
    detail: str
    target_image_paths: list[Path]
    target_tag_paths: list[Path]
    before_tags_by_path: dict[Path, list[str]]
    after_tags_by_path: dict[Path, list[str]]
    tag_counts_by_tag: dict[str, int] = field(default_factory=dict)
    is_undo_entry: bool = False
    undone: bool = False
    invalidated: bool = False
    invalidated_reason: str = ""
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def target_count(self) -> int:
        return len(self.target_image_paths)
