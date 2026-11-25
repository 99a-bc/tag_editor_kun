from __future__ import annotations

"""モデルやQtに依存しない恒久除外ルールの判定・永続化。

読込エラーは LoadResult.issues で通知する。正常な行だけ利用できるが、
不正な保存先への書込みは拒否するため、部分読込によるデータ消失を防げる。
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Iterable, Literal
from uuid import uuid4


SCHEMA_VERSION = 1
MatchMode = Literal["exact", "word"]
_RULE_FIELDS = {"rule_id", "tag", "match_mode", "registered_order", "manual_order"}


class ExclusionValidationError(ValueError):
    """ルールまたはルール集合の形式が不正。"""


class DuplicateExclusionRuleError(ExclusionValidationError):
    """正規化タグと一致方式の組合せが登録済み。"""


class ExclusionPersistenceError(OSError):
    """安全に保存できない。既存ファイルの修復は呼出側で明示的に行う。"""


def default_exclusions_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "tagger_exclusions.json"


def normalize_tag(tag: str) -> str:
    """判定用キー。表示文字列は変更せず、ハイフンや句読点も保持する。"""
    if not isinstance(tag, str):
        raise ExclusionValidationError("タグ名は文字列である必要があります。")
    return " ".join(tag.casefold().replace("_", " ").split())


@dataclass(frozen=True)
class ExclusionRule:
    rule_id: str
    tag: str
    match_mode: MatchMode
    registered_order: int
    manual_order: int

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, str) or not self.rule_id.strip():
            raise ExclusionValidationError("rule_id は空でない文字列が必要です。")
        if not normalize_tag(self.tag):
            raise ExclusionValidationError("タグ名は空にできません。")
        if self.match_mode not in ("exact", "word"):
            raise ExclusionValidationError("match_mode は exact または word が必要です。")
        for name in ("registered_order", "manual_order"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ExclusionValidationError(f"{name} は0以上の整数が必要です。")

    @property
    def normalized_tag(self) -> str:
        return normalize_tag(self.tag)

    def matches(self, tag: str) -> bool:
        return self._matches_normalized(normalize_tag(tag))

    def _matches_normalized(self, tag: str) -> bool:
        if self.match_mode == "exact":
            return self.normalized_tag == tag
        words = tag.split()
        phrase = self.normalized_tag.split()
        return any(
            words[index:index + len(phrase)] == phrase
            for index in range(len(words) - len(phrase) + 1)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "tag": self.tag,
            "match_mode": self.match_mode,
            "registered_order": self.registered_order,
            "manual_order": self.manual_order,
        }

    @classmethod
    def from_dict(cls, data: object) -> ExclusionRule:
        if not isinstance(data, dict) or set(data) != _RULE_FIELDS:
            raise ExclusionValidationError("ルールのフィールド構成が不正です。")
        return cls(**data)


class ExclusionRuleSet:
    """検証済みルール集合。同名でも一致方式が異なれば別ルールとして保持する。"""

    def __init__(self, rules: Iterable[ExclusionRule] = ()) -> None:
        self._rules: tuple[ExclusionRule, ...] = ()
        for rule in rules:
            self._append(rule)

    @property
    def rules(self) -> tuple[ExclusionRule, ...]:
        return self._rules

    def _append(self, rule: ExclusionRule) -> None:
        if not isinstance(rule, ExclusionRule):
            raise ExclusionValidationError("ExclusionRule が必要です。")
        for existing in self._rules:
            if (existing.normalized_tag, existing.match_mode) == (
                rule.normalized_tag, rule.match_mode
            ):
                raise DuplicateExclusionRuleError("同じタグと一致方式が登録済みです。")
            if existing.rule_id == rule.rule_id:
                raise ExclusionValidationError("rule_id が重複しています。")
            if existing.registered_order == rule.registered_order:
                raise ExclusionValidationError("registered_order が重複しています。")
            if existing.manual_order == rule.manual_order:
                raise ExclusionValidationError("manual_order が重複しています。")
        self._rules = (*self._rules, rule)

    def add(self, tag: str, match_mode: MatchMode = "exact") -> ExclusionRule:
        rule = ExclusionRule(
            rule_id=uuid4().hex,
            tag=tag,
            match_mode=match_mode,
            registered_order=max((r.registered_order for r in self._rules), default=-1) + 1,
            manual_order=max((r.manual_order for r in self._rules), default=-1) + 1,
        )
        self._append(rule)
        return rule

    def matching_rules(self, tag: str) -> tuple[ExclusionRule, ...]:
        """先頭一致で打ち切らず、全一致ルールを登録順で返す。"""
        normalized = normalize_tag(tag)
        return tuple(sorted(
            (rule for rule in self._rules if rule._matches_normalized(normalized)),
            key=lambda rule: rule.registered_order,
        ))

    def is_excluded(self, tag: str) -> bool:
        return bool(self.matching_rules(tag))


@dataclass(frozen=True)
class ExclusionDecision:
    """表示と反映で共用する恒久ルールの判定。"""

    normalized_tag: str
    excluded: bool
    matching_rules: tuple[ExclusionRule, ...]


def evaluate_exclusion(
    tag: str,
    permanent_rules: ExclusionRuleSet,
) -> ExclusionDecision:
    """一致する恒久ルールが1件でもあれば除外する。"""
    normalized = normalize_tag(tag)
    matched = permanent_rules.matching_rules(tag)
    return ExclusionDecision(
        normalized_tag=normalized,
        excluded=bool(matched),
        matching_rules=matched,
    )


@dataclass(frozen=True)
class LoadResult:
    rule_set: ExclusionRuleSet
    issues: tuple[str, ...] = ()

    @property
    def can_save(self) -> bool:
        """読込時点の状態。save_exclusions は保存先を再検証する。"""
        return not self.issues


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    data: dict[str, object] = {}
    for key, value in pairs:
        if key in data:
            raise ExclusionValidationError(f"JSONキーが重複しています: {key}")
        data[key] = value
    return data


def _reject_constant(value: str) -> None:
    raise ExclusionValidationError(f"JSONに不正な数値があります: {value}")


def load_exclusions(path: Path | None = None) -> LoadResult:
    """読み取るだけでファイルを作成・修復しない。不正行は行番号付きで通知。"""
    source = Path(path) if path is not None else default_exclusions_path()
    rules = ExclusionRuleSet()
    try:
        data = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_constant,
        )
    except FileNotFoundError:
        return LoadResult(rules)
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        return LoadResult(rules, (f"除外設定を読み込めません: {error}",))

    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "rules"}
        or type(data["schema_version"]) is not int
        or data["schema_version"] != SCHEMA_VERSION
        or not isinstance(data["rules"], list)
    ):
        return LoadResult(rules, ("除外設定の形式またはスキーマバージョンが不正です。",))

    issues: list[str] = []
    for index, raw_rule in enumerate(data["rules"], start=1):
        try:
            rules._append(ExclusionRule.from_dict(raw_rule))
        except ExclusionValidationError as error:
            issues.append(f"ルール{index}: {error}")
    return LoadResult(rules, tuple(issues))


def save_exclusions(rule_set: ExclusionRuleSet, path: Path | None = None) -> Path:
    """同じディレクトリの一時ファイルをflush/fsync後、原子的に置換する。

    壊れたJSON、不正行、未知のスキーマを含む既存保存先は上書きしない。
    書込・置換失敗は例外で通知し、一時ファイルは削除する。
    """
    target = Path(path) if path is not None else default_exclusions_path()
    checked = ExclusionRuleSet(rule_set.rules)
    existing = load_exclusions(target)
    if existing.issues:
        raise ExclusionPersistenceError(
            "不正な既存除外設定の上書きを拒否しました: " + "; ".join(existing.issues)
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "rules": [rule.to_dict() for rule in checked.rules],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temp_path: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n",
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except OSError as error:
        raise ExclusionPersistenceError(f"除外設定を保存できません: {error}") from error
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return target
