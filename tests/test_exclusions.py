from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tagger.exclusions import (
    DuplicateExclusionRuleError,
    ExclusionPersistenceError,
    ExclusionRule,
    ExclusionRuleSet,
    ExclusionValidationError,
    default_exclusions_path,
    load_exclusions,
    normalize_tag,
    save_exclusions,
)


def make_rule(tag="shirt", mode="word", index=0):
    return ExclusionRule(f"rule-{index}", tag, mode, index, index)


class MatchingTests(unittest.TestCase):
    def test_normalization_preserves_hyphen_and_punctuation(self):
        self.assertEqual(normalize_tag("  WHITE__\tShirt\n"), "white shirt")
        self.assertEqual(normalize_tag("T-Shirt_(X)"), "t-shirt (x)")
        self.assertEqual(normalize_tag("Straße"), "strasse")

    def test_display_text_is_preserved_and_rule_is_immutable(self):
        text = "  WHITE__Shirt  "
        rule = make_rule(text)
        self.assertEqual(rule.tag, text)
        self.assertEqual(rule.to_dict()["tag"], text)
        self.assertTrue(rule.matches("white shirt"))
        with self.assertRaises(FrozenInstanceError):
            rule.tag = "changed"

    def test_exact_matches_only_whole_normalized_tag(self):
        rule = make_rule("White_Shirt", "exact")
        for text in ("white shirt", " WHITE__SHIRT "):
            with self.subTest(text=text):
                self.assertTrue(rule.matches(text))
        for text in ("shirt", "long white shirt", "white shirts", ""):
            with self.subTest(text=text):
                self.assertFalse(rule.matches(text))

    def test_word_boundaries(self):
        rule = make_rule()
        for text in ("shirt", "white shirt", "collared_shirt", "shirt lift", "SHIRT"):
            with self.subTest(text=text):
                self.assertTrue(rule.matches(text))
        for text in ("sweatshirt", "AshirtB", "shirtless", "t-shirt", "shirt-lift", "(shirt)", ""):
            with self.subTest(text=text):
                self.assertFalse(rule.matches(text))

    def test_hyphenated_rule_can_match_whole_token(self):
        self.assertTrue(make_rule("t-shirt").matches("white t-shirt"))
        self.assertFalse(make_rule("t-shirt").matches("white t shirt"))

    def test_phrase_requires_contiguous_ordered_tokens(self):
        rule = make_rule("white__shirt")
        for text in ("white shirt", "long WHITE_shirt lift", "white white shirt"):
            with self.subTest(text=text):
                self.assertTrue(rule.matches(text))
        for text in ("shirt white", "white collared shirt", "white sweatshirt", "off-white shirt", "white", ""):
            with self.subTest(text=text):
                self.assertFalse(rule.matches(text))

    def test_all_matching_rules_returned_independently_of_order(self):
        rules = [make_rule("shirt", index=2), make_rule("white shirt", "exact", 0),
                 make_rule("white", index=1)]
        forward = ExclusionRuleSet(rules)
        reverse = ExclusionRuleSet(reversed(rules))
        self.assertEqual(forward.matching_rules("white_shirt"), reverse.matching_rules("white_shirt"))
        self.assertEqual([r.rule_id for r in forward.matching_rules("white shirt")],
                         ["rule-0", "rule-1", "rule-2"])
        self.assertTrue(ExclusionRuleSet(rules[1:]).is_excluded("white shirt"))
        self.assertTrue(ExclusionRuleSet([rules[0]]).is_excluded("white shirt"))
        self.assertFalse(forward.is_excluded("socks"))
        self.assertEqual(forward.matching_rules(""), ())

    def test_empty_set(self):
        self.assertFalse(ExclusionRuleSet().is_excluded("shirt"))


class ValidationTests(unittest.TestCase):
    def test_invalid_rule_fields(self):
        invalid = {
            "rule_id": (None, 1, "", "  "),
            "tag": (None, 42, "", "_\t __"),
            "match_mode": (None, "substring", "EXACT", [], True),
            "registered_order": (-1, True, 1.5, "0", None),
            "manual_order": (-1, False, 1.5, "0", None),
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ExclusionValidationError):
                        replace(make_rule(), **{field: value})

    def test_invalid_record_shape(self):
        raw = make_rule().to_dict()
        for value in (None, [], "shirt", {}, {**raw, "future": 1},
                      {k: v for k, v in raw.items() if k != "rule_id"}):
            with self.subTest(value=value):
                with self.assertRaises(ExclusionValidationError):
                    ExclusionRule.from_dict(value)

    def test_normalized_duplicate_is_rejected_without_mutation(self):
        rules = ExclusionRuleSet()
        original = rules.add(" WHITE__Shirt ", "word")
        with self.assertRaises(DuplicateExclusionRuleError):
            rules.add("white shirt", "word")
        self.assertEqual(rules.rules, (original,))

    def test_casefold_duplicate(self):
        rules = ExclusionRuleSet()
        rules.add("Straße")
        with self.assertRaises(DuplicateExclusionRuleError):
            rules.add("STRASSE")

    def test_same_tag_different_modes_allowed(self):
        rules = ExclusionRuleSet()
        first = rules.add("shirt")
        second = rules.add("SHIRT", "word")
        self.assertNotEqual(first.rule_id, second.rule_id)
        self.assertEqual(len(rules.matching_rules("shirt")), 2)

    def test_duplicate_ids_or_orders_are_rejected(self):
        first = make_rule()
        for field in ("rule_id", "registered_order", "manual_order"):
            with self.subTest(field=field):
                second = replace(make_rule("socks", index=1), **{field: getattr(first, field)})
                with self.assertRaises(ExclusionValidationError):
                    ExclusionRuleSet([first, second])

    def test_wrong_rule_object_is_rejected(self):
        with self.assertRaises(ExclusionValidationError):
            ExclusionRuleSet([{}])

    def test_add_uses_next_orders_without_changing_existing_ids(self):
        original = replace(make_rule(), registered_order=5, manual_order=9)
        rules = ExclusionRuleSet([original])
        new = rules.add("socks")
        self.assertEqual((new.registered_order, new.manual_order), (6, 10))
        self.assertEqual(rules.rules[0], original)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        # テスト用ファイルも開発リポジトリ内だけに作成する。
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config" / "tagger_exclusions.json"

    def write_bytes(self, content):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(content)

    def write_payload(self, rules, **extra):
        payload = {"schema_version": 1, "rules": rules, **extra}
        self.write_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def assert_protected(self):
        before = self.path.read_bytes()
        result = load_exclusions(self.path)
        self.assertTrue(result.issues)
        self.assertFalse(result.can_save)
        for rules in (result.rule_set, ExclusionRuleSet()):
            with self.assertRaises(ExclusionPersistenceError):
                save_exclusions(rules, self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        return result

    def test_default_path_is_in_project_config(self):
        self.assertEqual(default_exclusions_path(),
                         Path(__file__).resolve().parent.parent / "config" / "tagger_exclusions.json")

    def test_missing_file_does_not_create_directories(self):
        result = load_exclusions(self.path)
        self.assertEqual(result.rule_set.rules, ())
        self.assertTrue(result.can_save)
        self.assertFalse(self.path.parent.exists())

    def test_roundtrip_preserves_ids_display_and_independent_orders(self):
        rules = ExclusionRuleSet([
            replace(make_rule(" WHITE__Shirt ", index=3), manual_order=7),
            replace(make_rule("日本語_タグ", "exact", 8), manual_order=0),
        ])
        self.assertEqual(save_exclusions(rules, self.path), self.path)
        result = load_exclusions(self.path)
        self.assertEqual(result.rule_set.rules, rules.rules)
        self.assertEqual(result.issues, ())
        self.assertTrue(result.rule_set.is_excluded("white shirt"))
        self.assertIn("日本語", self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_valid_file_can_be_replaced_with_empty_rules(self):
        save_exclusions(ExclusionRuleSet([make_rule()]), self.path)
        save_exclusions(ExclusionRuleSet(), self.path)
        self.assertEqual(load_exclusions(self.path).rule_set.rules, ())

    def test_invalid_json_encoding_duplicate_keys_and_constants_protected(self):
        for raw in (b"{", b"", b"\xff", b'{"schema_version":1,"schema_version":1,"rules":[]}',
                    b'{"schema_version":NaN,"rules":[]}', b'{"schema_version":Infinity,"rules":[]}'):
            with self.subTest(raw=raw):
                self.write_bytes(raw)
                self.assert_protected()

    def test_invalid_schema_is_protected(self):
        values = ([], None, {}, {"schema_version": 2, "rules": []},
                  {"schema_version": True, "rules": []},
                  {"schema_version": 1.0, "rules": []},
                  {"schema_version": 1, "rules": {}},
                  {"schema_version": 1, "rules": [], "future": 1})
        for value in values:
            with self.subTest(value=value):
                self.write_bytes(json.dumps(value).encode("utf-8"))
                self.assert_protected()

    def test_partial_load_keeps_valid_rules_but_blocks_overwrite(self):
        first = make_rule().to_dict()
        last = make_rule("socks", index=2).to_dict()
        self.write_payload([first, {**make_rule(index=1).to_dict(), "tag": 9}, last])
        result = self.assert_protected()
        self.assertEqual([r.tag for r in result.rule_set.rules], ["shirt", "socks"])
        self.assertIn("ルール2:", result.issues[0])

    def test_duplicate_records_are_reported_without_replacing_first(self):
        original = make_rule().to_dict()
        for duplicate in (
            make_rule(" SHIRT__", index=1).to_dict(),
            {**make_rule("socks", index=1).to_dict(), "rule_id": "rule-0"},
            {**make_rule("socks", index=1).to_dict(), "registered_order": 0},
            {**make_rule("socks", index=1).to_dict(), "manual_order": 0},
        ):
            with self.subTest(duplicate=duplicate):
                self.write_payload([original, duplicate])
                result = self.assert_protected()
                self.assertEqual(result.rule_set.rules, (make_rule(),))

    def test_destination_is_rechecked_before_save(self):
        save_exclusions(ExclusionRuleSet([make_rule()]), self.path)
        loaded = load_exclusions(self.path)
        self.write_bytes(b"broken after load")
        with self.assertRaises(ExclusionPersistenceError):
            save_exclusions(loaded.rule_set, self.path)
        self.assertEqual(self.path.read_bytes(), b"broken after load")

    def test_read_error_is_reported_and_save_is_blocked(self):
        self.write_payload([])
        before = self.path.read_bytes()
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            result = load_exclusions(self.path)
            self.assertTrue(result.issues)
            with self.assertRaises(ExclusionPersistenceError):
                save_exclusions(ExclusionRuleSet(), self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_replace_failure_preserves_file_and_removes_temp(self):
        save_exclusions(ExclusionRuleSet([make_rule()]), self.path)
        before = self.path.read_bytes()
        with patch("tagger.exclusions.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(ExclusionPersistenceError):
                save_exclusions(ExclusionRuleSet(), self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_flush_failure_preserves_file_and_removes_temp(self):
        save_exclusions(ExclusionRuleSet([make_rule()]), self.path)
        before = self.path.read_bytes()
        with patch("tagger.exclusions.os.fsync", side_effect=OSError("disk error")):
            with self.assertRaises(ExclusionPersistenceError):
                save_exclusions(ExclusionRuleSet(), self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_temp_creation_failure_preserves_file(self):
        save_exclusions(ExclusionRuleSet([make_rule()]), self.path)
        before = self.path.read_bytes()
        with patch("tagger.exclusions.tempfile.NamedTemporaryFile", side_effect=OSError("disk full")):
            with self.assertRaises(ExclusionPersistenceError):
                save_exclusions(ExclusionRuleSet(), self.path)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
