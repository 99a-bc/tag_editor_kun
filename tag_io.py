from __future__ import annotations

from pathlib import Path


def parse_tags(text: str) -> list[str]:
    """カンマ区切りのタグ文字列を list[str] にする。空要素は捨てる。"""
    return [part.strip() for part in text.replace("\n", ",").split(",") if part.strip()]


def tags_to_text(tags: list[str]) -> str:
    """タグを LoRA 用の一般的なカンマ区切り形式にする。"""
    return ", ".join(tags)


def tag_mtime_ns(tag_path: Path) -> int | None:
    """対応txtの更新時刻をns単位で返す。存在しない場合はNone。"""
    try:
        return tag_path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def load_tags(tag_path: Path) -> list[str]:
    if not tag_path.exists():
        return []

    try:
        return parse_tags(tag_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        # 既存ファイルがShift-JIS等だった場合の保険
        return parse_tags(tag_path.read_text(encoding="cp932", errors="replace"))


def save_tags(tag_path: Path, tags: list[str]) -> int | None:
    tag_path.write_text(tags_to_text(tags), encoding="utf-8")
    return tag_mtime_ns(tag_path)
