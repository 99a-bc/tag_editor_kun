import hashlib
import json
from pathlib import Path
import unittest

from tag_color_dictionary import (
    TagCategoryStyle,
    TagColorDictionary,
    normalize_tag_lookup_key,
)

DICTIONARY_PATH = Path(__file__).resolve().parents[1] / "resources" / "dictionaries" / "tag_color_dictionary.json"
REVIEWED_TAGS_SHA256 = "7f8dbaa8ebac220d39c55c584991554c55079a05654f7bf97b18d0f637d6e9c9"


class PixAIColorDictionaryTests(unittest.TestCase):
    def test_color_group_order_puts_general_and_unknown_last_together(self):
        dictionary = TagColorDictionary.load()
        colored_tags = {
            "character": "okamisty",
            "copyright": "bokura wa ima no naka de",
            "rating": "rating:q",
            "subject_presence": "1girl",
            "orientation_gaze": "looking at viewer",
            "subject_framing": "full body",
            "camera_view": "from behind",
            "focus_target": "male focus",
            "style_period": "retro artstyle",
            "visual_context": "simple background",
            "style": "bkub",
            "meta": "highres",
        }
        for category, tag in colored_tags.items():
            self.assertEqual(dictionary.category_id_for_tag(tag), category)

        colorless_tags = {
            "armor": "unknown",
            "Purple hat": "general",
            "see-through clothes": "general",
            "white shirt": "unknown",
        }
        last_rank = dictionary.category_sort_index_for_tag("armor")
        for tag, category in colorless_tags.items():
            self.assertEqual(dictionary.category_id_for_tag(tag), category)
            self.assertEqual(dictionary.category_sort_index_for_tag(tag), last_rank)
        for tag in colored_tags.values():
            self.assertLess(dictionary.category_sort_index_for_tag(tag), last_rank)

        expected = [*colored_tags.values(), *colorless_tags]
        self.assertEqual(sorted(reversed(expected), key=dictionary.sort_key_for_tag), expected)
        self.assertEqual(dictionary.sort_key_for_tag("ARMOR"), dictionary.sort_key_for_tag("armor"))
        self.assertEqual(sorted(["ARMOR", "armor"], key=dictionary.sort_key_for_tag), ["ARMOR", "armor"])


    def test_missing_tag_and_invalid_category_share_colorless_rank(self):
        dictionary = TagColorDictionary(
            {"general": TagCategoryStyle.from_category("general", {})},
            {"hat": "general", "armor": "unknown", "invalid": "undefined"},
        )
        for tag in ("missing", "invalid"):
            self.assertEqual(dictionary.category_id_for_tag(tag), "unknown")
            self.assertEqual(
                dictionary.category_sort_index_for_tag(tag),
                dictionary.category_sort_index_for_tag("hat"),
            )


    def test_named_examples_use_exact_official_names(self):
        dictionary = TagColorDictionary.load()
        self.assertEqual(dictionary.category_id_for_tag("watercolor"), "unknown")
        self.assertEqual(dictionary.category_id_for_tag("watercolor (medium)"), "meta")
        self.assertEqual(dictionary.style_for_tag("HIGHRES").border_color, "#4DB6AC")
        self.assertEqual(dictionary.style_for_tag("rating:g").border_color, "#FF3D6E")
        self.assertEqual(dictionary.style_for_tag("bkub").border_color, "#B39DDB")


    def test_bundled_dictionary_has_unique_tags_and_valid_categories(self):
        data = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
        tags = data["tags"]
        self.assertEqual(data["version"], 3)
        self.assertEqual(len(tags), 93_496)
        self.assertEqual(len({normalize_tag_lookup_key(tag) for tag in tags}), len(tags))
        self.assertEqual(set(tags.values()) - data["categories"].keys(), set())

    def test_bundled_tag_assignments_match_the_reviewed_snapshot(self):
        data = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
        # Use a reviewed digest rather than shipping the development audit reports.
        payload = json.dumps(data["tags"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertEqual(hashlib.sha256(payload).hexdigest(), REVIEWED_TAGS_SHA256)

    def test_approved_reclassifications_and_display_categories(self):
        data = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
        approved = {
            "traditional_media": "meta",
            "painting_(medium)": "meta",
            "marker_(medium)": "meta",
            "watercolor_(medium)": "meta",
            "graphite_(medium)": "meta",
            "colored_pencil_(medium)": "meta",
            "millipen_(medium)": "meta",
            "mixed_media": "meta",
            "acrylic_paint_(medium)": "meta",
            "nib_pen_(medium)": "meta",
            "ballpoint_pen_(medium)": "meta",
            "ink_(medium)": "meta",
            "watercolor_pencil_(medium)": "meta",
            "calligraphy_brush_(medium)": "meta",
            "pastel_(medium)": "meta",
            "bokura_wa_ima_no_naka_de": "copyright",
        }
        self.assertEqual({tag: data["tags"].get(tag) for tag in approved}, approved)
        self.assertEqual(data["categories"]["style"]["label"], "絵師・制作グループ")
        self.assertEqual(data["categories"]["style"]["color"], "#B39DDB")
        self.assertEqual(data["categories"]["meta"]["color"], "#4DB6AC")
