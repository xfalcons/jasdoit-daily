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
    def equal(self, left, right):
        self.assertEqual(annotate_post.normalize(left), annotate_post.normalize(right))

    def differs(self, left, right):
        self.assertNotEqual(annotate_post.normalize(left), annotate_post.normalize(right))

    def test_ten_based_minute_counters_accept_their_standard_variants(self):
        for readings in (("さんじゅっぷん", "サンジュウフン"), ("にじゅっぷん", "にじゅうふん"),
                         ("にじっぷん", "ニジュウフン"), ("じゅっぷん", "ジュップン"),
                         ("じゅっぷん", "じゅうふん")):
            with self.subTest(readings=readings):
                self.equal(*readings)

    def test_wrong_readings_keep_their_difference(self):
        for wrong, contextual in (("いうふん", "イップン"), ("ろうふん", "ロップン"),
                                  ("ううふん", "ウップン"), ("じゅうふん", "ジュウブン"),
                                  ("きょうしち", "キョウシツ")):
            with self.subTest(wrong=wrong):
                self.differs(wrong, contextual)


class ParseLineTest(unittest.TestCase):
    def test_base_with_punctuation_is_malformed(self):
        plain, spans, finding = annotate_post.parse_line(line("これからも、日本語", "にほんご"), 99)
        self.assertIsNone(plain)
        self.assertEqual(spans, [])
        self.assertEqual(finding.reason, "malformed_ruby")
        self.assertEqual(finding.line, 99)

    def test_word_internal_kana_digits_and_rare_kanji_are_valid_bases(self):
        for base in ("当たり前", "３日", "思い出", "𠮷田", "神社"):
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
