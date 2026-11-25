import unittest

from tagger.exclusions import (
    ExclusionRuleSet, evaluate_exclusion,
)


class PermanentDecisionTests(unittest.TestCase):
    def setUp(self):
        self.rules = ExclusionRuleSet()
        self.rules.add("shirt", "word")
        self.rules.add("white shirt", "exact")

    def decide(self, tag="white shirt"):
        return evaluate_exclusion(tag, self.rules)

    def test_permanent_rules_determine_decision(self):
        result = self.decide()
        self.assertTrue(result.excluded)
        self.assertEqual(len(result.matching_rules), 2)
        self.assertFalse(self.decide("socks").excluded)


    def test_remaining_permanent_rule_keeps_tag_excluded(self):
        for remaining in self.rules.rules:
            with self.subTest(rule=remaining.rule_id):
                result = evaluate_exclusion("white shirt", ExclusionRuleSet([remaining]))
                self.assertTrue(result.excluded)
                self.assertEqual(result.matching_rules, (remaining,))


if __name__ == "__main__":
    unittest.main()
