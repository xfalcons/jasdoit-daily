"""Tests for the annotator's pure parsing and reading-normalization rules."""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("annotate_post", ROOT / "tools" / "annotate-post.py")
annotate_post = importlib.util.module_from_spec(_spec)
sys.modules["annotate_post"] = annotate_post
_spec.loader.exec_module(annotate_post)

RUBY = "<ruby>{base}<rp>（</rp><rt>{rt}</rt><rp>）</rp></ruby>"


def line(base, rt):
    return RUBY.format(base=base, rt=rt) + "を<ruby>見<rp>（</rp><rt>み</rt><rp>）</rp></ruby>ました。"


class NormalizeTest(unittest.TestCase):
    def test_minute_counter_variants_compare_equal(self):
        for pair in (("さんじゅっぷん", "さんじゅうふん"), ("にじゅっぷん", "にじゅうふん")):
            with self.subTest(pair=pair):
                self.assertEqual(annotate_post.normalize(pair[0]), annotate_post.normalize(pair[1]))

    def test_ten_minutes_stays_distinct_from_enough(self):
        self.assertNotEqual(annotate_post.normalize("じゅっぷん"), annotate_post.normalize("じゅうぶん"))

    def test_a_wrong_reading_still_differs(self):
        self.assertNotEqual(annotate_post.normalize("きょうしち"), annotate_post.normalize("キョウシツ"))


class ParseLineTest(unittest.TestCase):
    def test_base_with_punctuation_is_malformed(self):
        plain, spans, finding = annotate_post.parse_line(line("これからも、日本語", "にほんご"), 99)
        self.assertIsNone(plain)
        self.assertEqual(spans, [])
        self.assertEqual(finding.reason, "malformed_ruby")
        self.assertEqual(finding.line, 99)

    def test_word_internal_kana_and_digits_are_valid_bases(self):
        for base in ("当たり前", "３日", "思い出"):
            with self.subTest(base=base):
                plain, spans, finding = annotate_post.parse_line(line(base, "あ"), 7)
                self.assertIsNone(finding)
                self.assertEqual([s.surface for s in spans], [base, "見"])

    def test_reading_with_punctuation_is_malformed(self):
        _, _, finding = annotate_post.parse_line(line("日本語", "にほんご、"), 3)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.reason, "malformed_ruby")


if __name__ == "__main__":
    unittest.main()
