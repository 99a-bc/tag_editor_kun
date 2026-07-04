from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


# GIF は静止・アニメーションを問わず、表示とTaggerでは先頭フレームを静止画として扱う。
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


@dataclass
class ImageItem:
    image_path: Path
    tags: list[str] = field(default_factory=list)
    checked: bool = False
    dirty: bool = False
    # フォルダ読込・保存成功時点のタグ状態。
    # Undoなどで見た目上元に戻った場合に、未保存扱いを正しく解除するために使う。
    saved_tags: list[str] = field(default_factory=list)
    # フォルダ読込・保存時点の対応txt更新時刻。
    # save時に現在のmtimeと比較し、外部エディタでの変更を検知する。
    tag_mtime_ns: int | None = None

    def __post_init__(self) -> None:
        # 既存コードから saved_tags 未指定で作られた場合は、読み込み時点のタグを保存済み基準にする。
        if not self.saved_tags:
            self.saved_tags = list(self.tags)
        self.refresh_dirty()

    @property
    def tag_path(self) -> Path:
        return self.image_path.with_suffix(".txt")

    @property
    def filename(self) -> str:
        return self.image_path.name

    def refresh_dirty(self) -> bool:
        """現在タグと保存済み基準を比較し、未保存状態を再計算する。"""
        self.dirty = list(self.tags) != list(self.saved_tags)
        return self.dirty

    def mark_saved(self, tag_mtime_ns: int | None) -> None:
        """現在タグを保存済み基準として確定し、未保存状態を解除する。"""
        self.saved_tags = list(self.tags)
        self.tag_mtime_ns = tag_mtime_ns
        self.dirty = False
