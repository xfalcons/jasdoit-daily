"""Tests for the stage-4 gate's canonical counter, block context, entities,
front-matter validation, and annotation contract.

Run from the repository root: python3 -m unittest tests.test_gate_post
"""
import importlib.util
import json
import sys
import tempfile
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


def src(lines):
    return gate.scan_source_lines(lines)


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
        lines = src([
            "### <ruby>見出<rp>（</rp><rt>みだ</rt><rp>）</rp></ruby>し漢字",
            "",
            "> <span lang=\"zh-Hant\" data-translation>漢字翻譯，全形標點。</span>",
            "",
            "こんにちは。「Hello, world」、ｱｲｳｴｵ 123",
        ])
        self.assertEqual(gate.canonical_count(lines), 5)

    def test_ruby_contributes_reading_not_base(self):
        lines = src(["<ruby>一日<rp>（</rp><rt>いちにち</rt><rp>）</rp></ruby>"])
        self.assertEqual(gate.canonical_count(lines), 4)

    def test_hash_without_space_is_prose_and_counts(self):
        self.assertEqual(gate.canonical_count(src(["#漢字"])), 2)
        self.assertEqual(len(gate.bare_kanji(src(["#漢字"]))), 1)

    def test_lazy_continuation_of_quote_is_excluded(self):
        lines = src(["> 訳文", "漢字の続きは引用の一部"])
        self.assertEqual(gate.canonical_count(lines), 0)
        self.assertEqual(gate.bare_kanji(lines), [])

    def test_numeric_entities_count_and_flag_bare_kanji(self):
        lines = src(["&#x6f22;&#x304b;&#x3093;だ"])
        self.assertEqual(gate.canonical_count(lines), 4)
        bare = gate.bare_kanji(lines)
        self.assertEqual(len(bare), 1)
        self.assertEqual(bare[0]["character"], "漢")

    def test_decoded_entity_in_bare_kanji_is_flagged(self):
        bare = gate.bare_kanji(src(["です&#x6f22;ね"]))
        self.assertEqual(len(bare), 1)
        self.assertEqual(bare[0]["character"], "漢")


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

    def test_four_backtick_fence_not_closed_by_three(self):
        lines = [
            "````text",
            "```",
            "## 日文文章：still fenced",
            "````",
            "## 3 個重點單字",
        ]
        sections, _, _ = self.detect(lines)
        self.assertEqual([s.kind for s in sections], ["vocabulary"])

    def test_tilde_fence_not_closed_by_backticks(self):
        lines = [
            "~~~",
            "## 日文文章：fenced",
            "```",
            "~~~",
            "## 1 個重點文法",
        ]
        sections, _, _ = self.detect(lines)
        self.assertEqual([s.kind for s in sections], ["grammar"])

    def test_comment_block_hides_headings(self):
        lines = [
            "<!--",
            "## 日文文章：hidden",
            "-->",
            "## 3 個重點單字",
        ]
        sections, _, _ = self.detect(lines)
        self.assertEqual([s.kind for s in sections], ["vocabulary"])

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

    def test_fenced_h3_is_not_a_vocabulary_card(self):
        lines = src(["### 1. 本物", "```", "### 9. fake", "```", "- **例句：**x"])
        cards = gate.vocabulary_cards(lines)
        self.assertEqual([c[1] for c in cards], ["1. 本物"])

    def test_more_marker_is_not_intro_prose(self):
        _, intro, more = self.detect(["<!-- more -->", "", "## 日文文章：x"])
        self.assertEqual(gate.intro_prose_count(intro, more[0]), 0)
        _, intro2, more2 = self.detect(["導言段落。", "", "<!-- more -->"])
        self.assertEqual(gate.intro_prose_count(intro2, more2[0]), 1)

    def test_comment_only_intro_is_not_prose(self):
        _, intro, more = self.detect(["<!-- hidden commentary -->", "<!-- more -->"])
        self.assertEqual(gate.intro_prose_count(intro, more[0]), 0)


class RubyGuardTest(unittest.TestCase):
    def test_unclosed_ruby_swarm_is_flagged_without_regex(self):
        line = "<ruby>未閉" * 60
        issues = gate.ruby_structure_issues([line])
        self.assertEqual(len(issues), 1)
        self.assertIn("too dense", issues[0]["detail"])


class LineSplitTest(unittest.TestCase):
    FM = (
        "---\n"
        "layout: post\nlearning: true\nkind: daily\nday: 18\nphase: 2\n"
        "lang: zh-TW\nreading_title: t\ncore_sentence: c\n"
        "---\n"
    )

    def load(self, body: str, stem: str = "day-018-x.md"):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / stem
            path.write_text(self.FM + body, encoding="utf-8")
            return gate.load_post(path)

    def test_u2028_does_not_split_lines(self):
        post = self.load("第一行" + chr(0x2028) + "同一行\n\nfooter")
        self.assertEqual([l for l in post.lines if l.strip()], ["第一行" + chr(0x2028) + "同一行", "footer"])

    def test_crlf_is_normalized(self):
        post = self.load("行一\r\n行二\r\n")
        self.assertEqual(post.lines[0], "行一")
        self.assertEqual(post.lines[1], "行二")


class FrontMatterTest(unittest.TestCase):
    def parse(self, fm_body: str):
        return gate.parse_front_matter("---\n" + fm_body + "---\n")

    def test_unbalanced_quote_on_consumed_key_is_surfaced(self):
        scalars, _, issues = self.parse("layout: \"post\n")
        self.assertEqual(scalars.get("layout"), "post")
        self.assertTrue(any("unbalanced quotes" in i for i in issues))

    def test_unclosed_flow_on_consumed_key_is_surfaced(self):
        _, _, issues = self.parse("core_sentence: [unclosed\n")
        self.assertTrue(any("unclosed flow" in i for i in issues))

    def test_colon_line_without_key_syntax_is_surfaced(self):
        _, _, issues = self.parse("  - bad item: x\n")
        self.assertTrue(any("not a key: value pair" in i for i in issues))

    def test_valid_front_matter_has_no_issues(self):
        scalars, _, issues = self.parse("layout: post\nday: 18\nphase: 2\ncategories:\n  - 日語學習\n")
        self.assertEqual(issues, [])
        self.assertEqual(scalars.get("layout"), "post")

    def test_phase_out_of_range_is_a_contract_issue(self):
        post = gate.Post(stem="day-018-x", day=18, raw=b"", text="", lines=[], body_start=10,
                         scalars={"phase": 999})
        issues, _ = gate.contract_issues(post, gate.date(2026, 11, 4))
        self.assertTrue(any("integer 1-10" in e["detail"] for e in issues))


class AnnotationContractTest(unittest.TestCase):
    def make_post(self, lines=None):
        return gate.Post(stem="day-017-x", day=17, raw=b"x", text="", lines=lines or [], body_start=0,
                         scalars={})

    def make_report(self, counts=None, findings=None):
        counts = {"ruby": 1, "regions": 1, "ok": 1, "ok_contextual": 1, "ok_attested": 0,
                  "low_confidence": 0, "divergent": 0, **(counts or {})}
        return {
            "tool": "annotate-post", "schema": 1, "post": "day-017-x", "day": 17,
            "post_sha256": "a" * 64, "counts": counts,
            "findings": findings if findings is not None else [],
        }

    def decode(self, report, post=None):
        return gate.decode_annotation(json.dumps(report).encode("utf-8"), post or self.make_post())

    def test_valid_report_passes(self):
        data, issues = self.decode(self.make_report())
        self.assertEqual(issues, [])
        self.assertEqual(data["counts"]["ruby"], 1)

    def test_divergent_finding_with_zero_count_fails(self):
        finding = {"line": 5, "col": None, "surface": "x", "rt": None,
                   "verdict": "divergent", "reason": "no_support", "evidence": {}}
        data, issues = self.decode(self.make_report(findings=[finding]))
        self.assertIsNone(data)
        self.assertTrue(any("counts.divergent is 0" in e["detail"] for e in issues))

    def test_divergent_count_without_finding_fails(self):
        data, issues = self.decode(self.make_report(counts={"divergent": 1}))
        self.assertIsNone(data)
        self.assertTrue(any("no divergent finding" in e["detail"] for e in issues))

    def test_zero_ruby_count_on_ruby_post_fails(self):
        post = self.make_post(lines=["<ruby>漢<rt>かん</rt></ruby>"])
        data, issues = self.decode(self.make_report(counts={"ruby": 0}), post)
        self.assertIsNone(data)
        self.assertTrue(any("body contains ruby markup" in e["detail"] for e in issues))

    def test_finding_entry_types_are_validated(self):
        bad = [
            {"line": {}, "surface": "x", "verdict": "divergent", "reason": "no_support"},
            {"line": 3, "col": "7", "surface": "x", "verdict": "divergent", "reason": "no_support"},
            {"line": 3, "surface": [], "verdict": "divergent", "reason": "no_support"},
            {"line": 3, "surface": "x", "rt": 9, "verdict": "divergent", "reason": "no_support"},
            {"line": 3, "surface": "x", "verdict": "wrong", "reason": "no_support"},
            {"line": 3, "surface": "x", "verdict": "divergent", "reason": ""},
        ]
        for finding in bad:
            with self.subTest(finding=finding):
                data, issues = self.decode(self.make_report(
                    counts={"divergent": 1}, findings=[finding]))
                self.assertIsNone(data)

    def test_lone_surrogate_reason_is_rejected(self):
        finding = {"line": 3, "col": None, "surface": "x", "rt": None,
                   "verdict": "divergent", "reason": "bad \ud800", "evidence": {}}
        raw = json.dumps(self.make_report(counts={"divergent": 1}, findings=[finding])).encode("utf-8")
        data, issues = gate.decode_annotation(raw, self.make_post())
        self.assertIsNone(data)
        self.assertTrue(any("surrogate" in e["detail"] for e in issues))


if __name__ == "__main__":
    unittest.main()
