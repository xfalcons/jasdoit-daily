"""Tests for the shared engine helpers: .env precedence, R2 key building,
theme-row parsing, vendor preflight, and the gate-aligned spoken-text
extractor (entity decoding included)."""
import importlib.util
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("loop_support", ROOT / "tools" / "loop_support.py")
loop_support = importlib.util.module_from_spec(_spec)
sys.modules["loop_support"] = loop_support
_spec.loader.exec_module(loop_support)


def fixture(name):
    return (ROOT / "tests" / "fixtures" / name).read_text(encoding="utf-8")


class DotEnvTest(unittest.TestCase):
    def test_env_vars_win_over_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("LOOP_TEST_A=file\nLOOP_TEST_B=file\n", encoding="utf-8")
            self.environ = {"LOOP_TEST_B": "shell"}
            with unittest.mock.patch.dict("os.environ", self.environ, clear=False):
                loop_support.load_dotenv(root)
                self.assertEqual(os.environ["LOOP_TEST_A"], "file")
                self.assertEqual(os.environ["LOOP_TEST_B"], "shell")

    def test_parses_quotes_comments_and_blanks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "# comment\n\nKEY_ONE=bare\nKEY_TWO='quoted'\nKEY_THREE=\"dquoted\"\n",
                encoding="utf-8",
            )
            with unittest.mock.patch.dict("os.environ", {}, clear=False):
                for key in ("KEY_ONE", "KEY_TWO", "KEY_THREE"):
                    os.environ.pop(key, None)
                loop_support.load_dotenv(root)
                self.assertEqual(os.environ["KEY_ONE"], "bare")
                self.assertEqual(os.environ["KEY_TWO"], "quoted")
                self.assertEqual(os.environ["KEY_THREE"], "dquoted")

    def test_missing_file_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop_support.load_dotenv(Path(tmp))

    def test_malformed_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("not-a-pair\n=noname\nGOOD=1\n", encoding="utf-8")
            with unittest.mock.patch.dict("os.environ", {}, clear=False):
                os.environ.pop("GOOD", None)
                loop_support.load_dotenv(root)
                self.assertEqual(os.environ["GOOD"], "1")

    def test_empty_values_stay_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("GEN_MODEL=\nR2_PUBLIC_BASE=\n", encoding="utf-8")
            with unittest.mock.patch.dict("os.environ", {}, clear=False):
                for key in ("GEN_MODEL", "R2_PUBLIC_BASE"):
                    os.environ.pop(key, None)
                loop_support.load_dotenv(root)
                self.assertNotIn("GEN_MODEL", os.environ)
                self.assertNotIn("R2_PUBLIC_BASE", os.environ)


class CompletionContentTest(unittest.TestCase):
    def test_valid_payload_returns_content(self):
        payload = {"choices": [{"message": {"content": "hello"}}], "usage": {"cost": 1}}
        self.assertEqual(loop_support.completion_content(payload), "hello")

    def test_shape_violations_raise_value_error(self):
        for bad in (
            [],
            "text",
            {},
            {"choices": []},
            {"choices": "x"},
            {"choices": [{}]},
            {"choices": [{"message": {}}]},
            {"choices": [{"message": {"content": 7}}]},
            {"choices": [{"message": {"content": ""}}]},
            {"choices": [{"message": {"content": "   \n"}}]},
        ):
            with self.subTest(payload=bad):
                with self.assertRaises(ValueError):
                    loop_support.completion_content(bad)


class AudioKeyTest(unittest.TestCase):
    def test_key_shape(self):
        self.assertEqual(loop_support.audio_key(18), "audio/day-018.mp3")
        self.assertEqual(loop_support.audio_key(1), "audio/day-001.mp3")
        self.assertEqual(loop_support.audio_key(100), "audio/day-100.mp3")

    def test_public_url_join(self):
        url = loop_support.public_audio_url("https://pub.example/r2", 18)
        self.assertEqual(url, "https://pub.example/r2/audio/day-018.mp3")

    def test_public_url_strips_trailing_slash(self):
        url = loop_support.public_audio_url("https://pub.example/r2/", 18)
        self.assertEqual(url, "https://pub.example/r2/audio/day-018.mp3")

    def test_public_url_none_without_base(self):
        self.assertIsNone(loop_support.public_audio_url("", 18))
        self.assertIsNone(loop_support.public_audio_url(None, 18))


class ThemeRowTest(unittest.TestCase):
    def test_parses_real_row(self):
        theme = (ROOT / "aidd-docs" / "references" / "japanese-theme-rotation.md").read_text(encoding="utf-8")
        row = loop_support.parse_theme_row(theme, 19)
        self.assertEqual(row.title_ja, "小さな勝利を祝う")
        self.assertEqual(row.core_sentence, "小さな勝利を祝う人は、学びを長く続けられる。")
        self.assertIsNone(loop_support.parse_theme_row(theme, 101))

    def test_anchor_and_phase(self):
        theme = (ROOT / "aidd-docs" / "references" / "japanese-theme-rotation.md").read_text(encoding="utf-8")
        self.assertEqual(loop_support.theme_anchor(theme), (2026, 11, 4))
        self.assertEqual(loop_support.phase_of_day(19), 2)
        self.assertEqual(loop_support.phase_of_day(30), 3)
        self.assertEqual(loop_support.phase_of_day(100), 10)


class VendorPreflightTest(unittest.TestCase):
    def test_distinct_vendors(self):
        self.assertTrue(loop_support.vendors_distinct("z-ai/glm-5.3", "openai/gpt-4o"))
        self.assertTrue(loop_support.vendors_distinct("a/b", "c/d"))

    def test_same_vendor_forbidden(self):
        self.assertFalse(loop_support.vendors_distinct("z-ai/glm-5.3", "z-ai/glm-4.6"))

    def test_unknown_vendor_forbidden(self):
        self.assertFalse(loop_support.vendors_distinct("z-ai/glm-5.3", "gpt-4o"))
        self.assertFalse(loop_support.vendors_distinct("glm-5.3", "openai/gpt-4o"))


def spoken_post(body_lines):
    return "---\nday: 18\nkind: daily\n---\n\n## 日文文章\n\n" + body_lines + "\n"


class SpokenTextTest(unittest.TestCase):
    def test_ruby_becomes_reading_and_entities_decode(self):
        post = spoken_post("<ruby>日本語<rp>（</rp><rt>にほんご</rt><rp>）</rp></ruby>&#x3042;です。")
        result = loop_support.extract_spoken(post)
        self.assertEqual(result.day, 18)
        self.assertEqual(result.spoken, "にほんごあです。")
        self.assertEqual(result.unannotated_kanji, [])

    def test_unannotated_entity_kanji_is_reported(self):
        post = spoken_post("です&#x6f22;ね")
        result = loop_support.extract_spoken(post)
        self.assertEqual(result.spoken, "です漢ね")
        self.assertEqual(result.unannotated_kanji, ["漢"])

    def test_translations_headings_and_lazy_continuation_excluded(self):
        body = (
            "### <ruby>見出<rp>（</rp><rt>みだ</rt><rp>）</rp></ruby>し\n\n"
            "```<ruby>围<rp>（</rp><rt>い</rt><rp>）</rp></ruby>\n```\n\n"
            "<ruby>今日<rp>（</rp><rt>きょう</rt><rp>）</rp></ruby>も。\n\n"
            "> <span lang=\"zh-Hant\" data-translation>中文</span>\n"
            "引用の続きは入らない\n\n"
            "<!-- <ruby>围<rp>（</rp><rt>い</rt><rp>）</rp></ruby> -->"
        )
        result = loop_support.extract_spoken(spoken_post(body))
        self.assertEqual(result.spoken, "きょうも。")

    def test_empty_spoken_raises(self):
        post = spoken_post("> <span lang=\"zh-Hant\" data-translation>只有翻譯</span>")
        with self.assertRaises(loop_support.EmptySpokenText):
            loop_support.extract_spoken(post)

    def test_real_post_extracts_clean(self):
        post = (ROOT / "source" / "_posts" / "day-018-nakama-no-sonzai.md").read_text(encoding="utf-8")
        result = loop_support.extract_spoken(post)
        self.assertEqual(result.day, 18)
        self.assertNotIn("<", result.spoken)
        self.assertNotIn("&", result.spoken)
        self.assertEqual(result.unannotated_kanji, [])


if __name__ == "__main__":
    unittest.main()
