from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TagCandidate:
    tag: str
    score: float
    category: str = "general"
    # 作品タグでは、導出元になったキャラクター候補を表示用に保持する。
    source_tags: tuple[str, ...] = ()


@dataclass
class TaggerResult:
    image_path: Path
    general_candidates: list[TagCandidate] = field(default_factory=list)
    character_candidates: list[TagCandidate] = field(default_factory=list)
    # PixAIではcharacter候補からchar_ip_map.jsonを逆引きした補助候補。
    copyright_candidates: list[TagCandidate] = field(default_factory=list)

    style_candidates: list[TagCandidate] = field(default_factory=list)
    meta_candidates: list[TagCandidate] = field(default_factory=list)
    rating_candidates: list[TagCandidate] = field(default_factory=list)

    @property
    def all_candidates(self) -> list[TagCandidate]:
        """結果データの従来のカテゴリ順を返す。候補画面の表示順とは独立。"""
        return [
            *self.character_candidates,
            *self.copyright_candidates,
            *self.general_candidates,
            *self.style_candidates,
            *self.meta_candidates,
            *self.rating_candidates,
        ]

    def output_tags(
        self,
        *,
        include_character: bool = True,
        include_copyright: bool = True,
    ) -> list[str]:
        """既存の反映順序を維持してタグを返す。"""
        candidates: list[TagCandidate] = []
        if include_character:
            candidates.extend(self.character_candidates)
        if include_copyright:
            candidates.extend(self.copyright_candidates)
        candidates.extend(self.general_candidates)
        candidates.extend(self.style_candidates)
        candidates.extend(self.meta_candidates)
        candidates.extend(self.rating_candidates)
        return [candidate.tag for candidate in candidates]
