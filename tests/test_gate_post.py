"""Tests for the stage-4 gate's canonical counter and section detection.

Run from the repository root: python3 -m unittest tests.test_gate_post
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("gate_post", ROOT / "tools" / "gate-post.py")
gate = importlib.util.module_from_spec(_spec)
sys.modules["gate_post"] = gate
_spec.loader.exec_module(gate)


def japanese_lines(post_name):
    post = gate.load_post(ROOT / "source" / "_posts" / post_name)
    sections, _, _ = gate.detect_sections(post.lines, offset=post.body_start)
    japanese = [s for s in sections if s.kind == "japanese"]
    assert len(japanese) == 1, post_name
    return japanese[0].lines


class CanonicalCountTest(unittest.TestCase):
    def test_corpus_fixtures_days_14_to_17(self):
        expected = {
            "day-014-te-de-oboeru.md": 284,
            "day-015-wasureru-no-wa-atarimae.md": 294,
            "day-016-junbi-ga-totonowanakutemo.md": 266,
            "day-017-onaji-shippai.md": 279,
        }
        for name, count in expected.items():
            with self.subTest(post=name):
                self.assertEqual(gate.canonical_count(japanese_lines(name)), count)

    def test_headings_translations_punctuation_contribute_zero(self):
        lines = [
            (1, "### <ruby>見出<rp>（</rp><rt>みだ</rt><rp>）</rp></ruby>し漢字"),
            (2, ""),
            (3, "> <span lang=\"zh-Hant\" data-translation>漢字翻譯，全形標點。</span>"),
            (4, ""),
            (5, "こんにちは。「Hello, world」、ｱｲｳｴｵ 123"),
        ]
        self.assertEqual(gate.canonical_count(lines), 5)

    def test_ruby_contributes_reading_not_base(self):
        lines = [(1, "<ruby>一日<rp>（</rp><rt>いちにち</rt><rp>）</rp></ruby>")]
        self.assertEqual(gate.canonical_count(lines), 4)


class SectionDetectionTest(unittest.TestCase):
    def detect(self, lines):
        return gate.detect_sections(lines)

    def test_fenced_heading_is_not_a_section(self):
        lines = [
            "```",
            "## 日文文章：fenced",
            "```",
            "## 3 個重點單字",
        ]
        sections, _, _ = self.detect(lines)
        kinds = [s.kind for s in sections]
        self.assertEqual(kinds, ["vocabulary"])

    def test_quoted_heading_is_not_a_section(self):
        lines = [
            "> ## 日文文章：quoted",
            "## 1 個重點文法",
        ]
        sections, _, _ = self.detect(lines)
        kinds = [s.kind for s in sections]
        self.assertEqual(kinds, ["grammar"])

    def test_duplicate_sections_are_all_kept(self):
        lines = [
            "## 日文文章：first",
            "## 日文文章：second",
        ]
        sections, _, _ = self.detect(lines)
        japanese = [s for s in sections if s.kind == "japanese"]
        self.assertEqual(len(japanese), 2)

    def test_recognized_sections_keep_absolute_line_numbers(self):
        lines = ["intro", "", "<!-- more -->", "", "## 日文文章：x", "body", "## 3 個重點單字"]
        sections, intro, more = self.detect(lines)
        self.assertEqual([s.kind for s in sections], ["japanese", "vocabulary"])
        self.assertEqual([s.heading_no for s in sections], [5, 7])
        self.assertEqual(more, [3])


if __name__ == "__main__":
    unittest.main()
