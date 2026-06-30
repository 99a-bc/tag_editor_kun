from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv
import re


_TAG_SEPARATOR_RE = re.compile(r"[\s_]+")


def default_danbooru_dictionary_path() -> Path:
    """TagComplete互換のDanbooru補完CSVの標準配置先を返す。"""
    return Path(__file__).resolve().parent / "resources" / "dictionaries" / "danbooru.csv"


def normalize_completion_key(value: str) -> str:
    """補完検索用に空白・underscore・大文字小文字を正規化する。"""
    return _TAG_SEPARATOR_RE.sub(" ", value.strip()).casefold()


def display_tag(value: str) -> str:
    """アプリ内のタグ表記へ寄せる。"""
    return _TAG_SEPARATOR_RE.sub(" ", value.strip())


def _parse_int(value: str) -> int:
    try:
        return max(0, int(value.strip()))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class DanbooruTag:
    name: str
    category: int
    post_count: int
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class TagCompletionCandidate:
    tag: DanbooruTag
    matched_alias: str | None
    match_rank: int


@dataclass(frozen=True)
class _SearchEntry:
    key: str
    tag_index: int
    alias: str | None


class DanbooruTagDictionary:
    """
    a1111-sd-webui-tagcomplete互換CSVを読む、ローカル固定の補完辞書。

    CSVが無い・壊れている場合は空辞書として扱う。
    タグ編集を止めず、利用側で補完だけ無効にできるようにする。
    """

    def __init__(
        self,
        tags: list[DanbooruTag] | None = None,
        *,
        source_path: Path | None = None,
        load_error: str | None = None,
    ) -> None:
        self._tags = list(tags or [])
        self.source_path = source_path
        self.load_error = load_error

        # 部分一致検索でも入力ごとに全タグ・全aliasを走査しないよう、
        # 1文字/2文字の索引を作る。
        #
        # 2文字以上の検索は、検索語の先頭2文字を含む候補だけへ絞ってから
        # 実際の部分一致を確認する。例: "cow" -> "co" を含む候補だけを調べる。
        self._entries_by_character: dict[str, list[_SearchEntry]] = {}
        self._entries_by_bigram: dict[str, list[_SearchEntry]] = {}

        for index, tag in enumerate(self._tags):
            self._add_search_entry(
                normalize_completion_key(tag.name),
                tag_index=index,
                alias=None,
            )

            for alias in tag.aliases:
                self._add_search_entry(
                    normalize_completion_key(alias),
                    tag_index=index,
                    alias=alias,
                )

    def _add_search_entry(
        self,
        key: str,
        *,
        tag_index: int,
        alias: str | None,
    ) -> None:
        """正規タグまたはaliasの検索用索引を追加する。"""
        if not key:
            return

        entry = _SearchEntry(key, tag_index, alias)

        # 同じ文字/2文字列を複数回登録しない。
        for character in set(key):
            self._entries_by_character.setdefault(character, []).append(entry)

        if len(key) >= 2:
            bigrams = {key[position : position + 2] for position in range(len(key) - 1)}
            for bigram in bigrams:
                self._entries_by_bigram.setdefault(bigram, []).append(entry)

    @classmethod
    def load(cls, path: Path | None = None) -> "DanbooruTagDictionary":
        dictionary_path = path or default_danbooru_dictionary_path()

        try:
            with dictionary_path.open("r", encoding="utf-8-sig", newline="") as file:
                reader = csv.reader(file)
                rows = list(reader)
        except FileNotFoundError:
            return cls(source_path=dictionary_path, load_error="辞書ファイルが見つかりません。")
        except (OSError, UnicodeDecodeError, csv.Error) as error:
            return cls(source_path=dictionary_path, load_error=f"辞書を読み込めませんでした: {error}")

        tags: list[DanbooruTag] = []

        for row in rows:
            if not row or not row[0].strip():
                continue

            first = row[0].strip().casefold()
            # TagComplete形式のヘッダー有無を許容する。
            if first in {"tag", "tag_name", "name"} and len(row) >= 2:
                continue

            if len(row) < 3:
                continue

            name = display_tag(row[0])
            if not name:
                continue

            category = _parse_int(row[1])
            post_count = _parse_int(row[2])

            aliases: list[str] = []
            if len(row) >= 4 and row[3].strip():
                seen_aliases: set[str] = set()
                for raw_alias in row[3].split(","):
                    alias = display_tag(raw_alias)
                    alias_key = normalize_completion_key(alias)
                    if not alias_key or alias_key == normalize_completion_key(name):
                        continue
                    if alias_key in seen_aliases:
                        continue
                    aliases.append(alias)
                    seen_aliases.add(alias_key)

            tags.append(
                DanbooruTag(
                    name=name,
                    category=category,
                    post_count=post_count,
                    aliases=tuple(aliases),
                )
            )

        if not tags:
            return cls(
                source_path=dictionary_path,
                load_error="有効なタグ行が見つかりませんでした。",
            )

        return cls(tags, source_path=dictionary_path)

    def is_available(self) -> bool:
        return bool(self._tags)

    def tag_count(self) -> int:
        return len(self._tags)

    def status_message(self) -> str:
        if self.is_available():
            return f"Danbooru補完辞書: {self.tag_count():,}タグ"
        path_text = str(self.source_path) if self.source_path is not None else "resources/dictionaries/danbooru.csv"
        detail = self.load_error or "辞書を利用できません。"
        return f"Danbooru補完辞書を利用できません。\\n{detail}\\n配置先: {path_text}"

    def search(self, query: str, *, limit: int = 12) -> list[TagCompletionCandidate]:
        """
        正規タグ名・alias名を対象に、部分一致で候補を返す。

        候補の並びは一致位置ではなく投稿数の多い順。
        例: "cow" なら "cowboy shot" だけでなく
        "reverse cowgirl position" も候補に入り、投稿数順に並ぶ。
        """
        key = normalize_completion_key(query)
        if not key or not self._tags:
            return []

        if len(key) == 1:
            search_entries = self._entries_by_character.get(key, [])
        else:
            search_entries = self._entries_by_bigram.get(key[:2], [])

        candidates_by_tag_index: dict[int, TagCompletionCandidate] = {}
        for entry in search_entries:
            if key not in entry.key:
                continue

            # 同じ正規タグがタグ名・複数aliasのどれからもヒットした場合は、
            # 正規タグ一致を優先して表示する。aliasだけのヒットなら、そのaliasを
            # 候補行に "alias → 正規タグ" と表示する。
            match_rank = 0 if entry.alias is None else 1
            candidate = TagCompletionCandidate(
                tag=self._tags[entry.tag_index],
                matched_alias=entry.alias,
                match_rank=match_rank,
            )
            previous = candidates_by_tag_index.get(entry.tag_index)
            if previous is None or candidate.match_rank < previous.match_rank:
                candidates_by_tag_index[entry.tag_index] = candidate

        ordered = sorted(
            candidates_by_tag_index.values(),
            key=lambda candidate: (
                -candidate.tag.post_count,
                candidate.tag.name.casefold(),
            ),
        )
        return ordered[:max(1, limit)]
