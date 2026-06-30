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

    @property
    def all_candidates(self) -> list[TagCandidate]:
        """候補表示・反映で共有する正式なカテゴリ順を返す。"""
        return [
            *self.character_candidates,
            *self.copyright_candidates,
            *self.general_candidates,
        ]

    def output_tags(
        self,
        *,
        include_character: bool = True,
        include_copyright: bool = True,
    ) -> list[str]:
        """候補欄と同じカテゴリ順で、反映用タグを返す。"""
        candidates: list[TagCandidate] = []
        if include_character:
            candidates.extend(self.character_candidates)
        if include_copyright:
            candidates.extend(self.copyright_candidates)
        candidates.extend(self.general_candidates)
        return [candidate.tag for candidate in candidates]
