"""Tests for stage tools' pure prompt/verdict logic and their no-network
fake modes (GEN_FAKE_PATH / VERIFY_FAKE_PATH / REVISE_FAKE_PATH)."""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


verify_post = load("verify_post_tool", "verify-post.py")
generate_post = load("generate_post_tool", "generate-post.py")
loop_support = sys.modules["loop_support"] if "loop_support" in sys.modules else load("loop_support", "loop_support.py")


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class VerdictParsingTest(unittest.TestCase):
    def test_fixture_verdicts(self):
        self.assertEqual(verify_post.parse_verdict(fixture("verify-pass.txt")), "PASS")
        self.assertEqual(verify_post.parse_verdict(fixture("verify-flag.txt")), "FLAG")
        self.assertEqual(verify_post.parse_verdict(fixture("verify-fail.txt")), "FAIL")

    def test_first_nonempty_line_only(self):
        self.assertEqual(verify_post.parse_verdict("\n\nVERDICT: PASS\nbody"), "PASS")
        self.assertEqual(verify_post.parse_verdict("前言\nVERDICT: PASS"), None)

    def test_foreign_or_lowercase_verdicts_never_guessed(self):
        self.assertIsNone(verify_post.parse_verdict("VERDICT: REVISE\n"))
        self.assertIsNone(verify_post.parse_verdict("verdict: pass\n"))
        self.assertIsNone(verify_post.parse_verdict("VERDICT: PASS後續"))

    def test_trailing_commentary_is_not_a_verdict(self):
        self.assertIsNone(verify_post.parse_verdict("VERDICT: PASS (actually FLAG)\nfindings…"))
        self.assertIsNone(verify_post.parse_verdict("VERDICT: FAIL，但請再看\n"))
        self.assertEqual(verify_post.parse_verdict("VERDICT: PASS  \nbody"), "PASS")


class BuildPromptTest(unittest.TestCase):
    def test_prompt_carries_row_digest_and_sunday_toggle(self):
        row = loop_support.ThemeRow(19, "小さな勝利を祝う", "慶祝小勝利", "小さな勝利を祝う人は、長く続く。")
        sunday = generate_post.build_prompt("PERSONA", row, 19, True, "GOLD", ["- Day 18《仲間の存在》core：一人で学び、みんなで続ける。"])
        weekday = generate_post.build_prompt("PERSONA", row, 19, False, "GOLD", [])
        self.assertIn("小さな勝利を祝う人は、長く続く。", sunday)
        self.assertIn("Day 18《仲間の存在》", sunday)
        self.assertIn("週日寫作提示", sunday)
        self.assertNotIn("週日寫作提示", weekday)
        self.assertIn("250–300 字", sunday)
        self.assertIn("JMdict", sunday)

    def test_strip_outer_fence(self):
        fenced = "```markdown\n---\nday: 19\n---\nbody\n```"
        stripped = generate_post.strip_outer_fence(fenced)
        self.assertTrue(stripped.startswith("---"))
        self.assertTrue(stripped.rstrip().endswith("body"))
        self.assertEqual(generate_post.strip_outer_fence("---\nplain\n"), "---\nplain\n")


class VerifyPromptTest(unittest.TestCase):
    def test_verify_prompt_names_n3_and_repetition(self):
        prompt = verify_post.build_prompt("POST", ["- Day 18《仲間の存在》core：…"])
        self.assertIn("N3 適切性", prompt)
        self.assertIn("重複度", prompt)
        self.assertIn("Day 18《仲間の存在》", prompt)
        self.assertIn("VERDICT:", prompt)

    def test_verify_prompt_checks_native_naturalness(self):
        prompt = verify_post.build_prompt("POST", [])
        self.assertIn("自然性", prompt)
        self.assertIn("日本人實際會說", prompt)

    def test_generate_prompt_demands_native_naturalness(self):
        row = loop_support.ThemeRow(19, "小さな勝利を祝う", "慶祝小勝利", "小さな勝利を祝う人は、長く続く。")
        prompt = generate_post.build_prompt("PERSONA", row, 19, False, "GOLD", [])
        self.assertIn("日本人實際會說", prompt)
        self.assertIn("日本人會這樣用嗎", prompt)


DIVERGENT_FINDINGS = [
    {"line": 30, "col": 24, "surface": "教室", "rt": "きょうしち", "verdict": "divergent",
     "reason": "unattested_reading",
     "evidence": {"assembled": "きょうしち", "unidic": "キョウシツ", "tokens": ["教室"],
                  "jmdict": [], "pykakasi": None}},
    {"line": 34, "col": 39, "surface": "数字", "rt": "かず", "verdict": "divergent",
     "reason": "unattested_reading",
     "evidence": {"assembled": "かず", "unidic": "スウジ", "tokens": ["数字"],
                  "jmdict": [], "pykakasi": None}},
]


class BuildRevisionPromptTest(unittest.TestCase):
    def test_prompt_carries_the_draft_and_every_finding_with_its_evidence(self):
        prompt = generate_post.build_revision_prompt("下書き本文", DIVERGENT_FINDINGS)
        self.assertIn("<<<DRAFT\n下書き本文\nDRAFT>>>", prompt)
        for expected in ("2 處", "L30", "教室", "rt=きょうしち", "unidic=キョウシツ", "tokens=教室",
                         "L34", "数字", "rt=かず", "unidic=スウジ", "unattested_reading"):
            self.assertIn(expected, prompt)

    def test_prompt_limits_the_edit_to_flagged_readings(self):
        prompt = generate_post.build_revision_prompt("下書き", DIVERGENT_FINDINGS[:1])
        for expected in ("`<rt>` 讀音", "malformed_ruby", "逐字保留", "不要任何前言"):
            self.assertIn(expected, prompt)


class FileTailTest(unittest.TestCase):
    def test_revision_takes_the_drafts_own_closing_newline(self):
        match = generate_post.match_file_tail
        self.assertEqual(match("下書き\n", "校訂後の下書き"), "校訂後の下書き\n")
        self.assertEqual(match("下書き\n", "校訂後の下書き\n\n\n"), "校訂後の下書き\n")
        self.assertEqual(match("下書き", "校訂後の下書き\n"), "校訂後の下書き")

    def test_last_line_is_left_to_the_invariant(self):
        match = generate_post.match_file_tail
        self.assertEqual(match("下書き\n", "校訂後の下書き\n以上です。\n"),
                         "校訂後の下書き\n以上です。\n")
        self.assertEqual(match("下書き  \n", "校訂後の下書き  \n"), "校訂後の下書き  \n")


def run_tool(script, args, env_extra):
    env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / script)] + args,
        capture_output=True, text=True, env=env, timeout=60,
    )


class FakeModeTest(unittest.TestCase):
    def test_verify_fake_pass_flag_fail_noverdict(self):
        post = str(FIXTURES / "day-019-chiisana-shouri-wo-iwau.md")
        expected = {
            "verify-pass.txt": 0, "verify-flag.txt": 3,
            "verify-fail.txt": 2, "verify-noverdict.txt": 4,
        }
        for name, rc in expected.items():
            with self.subTest(fixture=name):
                proc = run_tool("verify-post.py", [post], {"VERIFY_FAKE_PATH": str(FIXTURES / name)})
                self.assertEqual(proc.returncode, rc, proc.stderr)
                if rc in (0, 2, 3):
                    meta = [ln for ln in proc.stdout.splitlines() if ln.startswith("META ")][-1]
                    self.assertEqual(json.loads(meta[5:])["verdict"], {0: "PASS", 2: "FAIL", 3: "FLAG"}[rc])

    def test_verify_without_key_or_fake_is_transport_error(self):
        post = str(FIXTURES / "day-019-chiisana-shouri-wo-iwau.md")
        proc = run_tool("verify-post.py", [post], {})
        self.assertEqual(proc.returncode, 4)

    def test_model_flag_names_the_lane_model_in_meta(self):
        post = str(FIXTURES / "day-019-chiisana-shouri-wo-iwau.md")
        proc = run_tool("verify-post.py", ["--model", "z-ai/glm-5.3", post],
                        {"VERIFY_FAKE_PATH": str(FIXTURES / "verify-pass.txt"),
                         "VERIFY_MODEL": "openai/gpt-6.1-sol"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        meta = [ln for ln in proc.stdout.splitlines() if ln.startswith("META ")][-1]
        self.assertEqual(json.loads(meta[5:])["model"], "z-ai/glm-5.3")

    def test_generate_fake_writes_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "draft.md"
            proc = run_tool(
                "generate-post.py",
                ["--day", "19", "--out", str(out)],
                {"GEN_FAKE_PATH": str(FIXTURES / "day-019-chiisana-shouri-wo-iwau.md")},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("description", out.read_text(encoding="utf-8"))
            meta = [ln for ln in proc.stdout.splitlines() if ln.startswith("META ")][-1]
            self.assertTrue(json.loads(meta[5:])["fake"])

    def test_generate_without_key_or_fake_is_transport_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "draft.md"
            proc = run_tool("generate-post.py", ["--day", "19", "--out", str(out)], {})
            self.assertEqual(proc.returncode, 4)


class RevisionModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.draft = self.root / "draft.md"
        self.draft.write_text("下書き\n", encoding="utf-8")
        self.report = self.root / "report.json"
        self.report.write_text(json.dumps({"findings": DIVERGENT_FINDINGS}), encoding="utf-8")
        self.out = self.root / "out" / "revised.md"
        self.revised_fixture = FIXTURES / "day-019-chiisana-shouri-wo-iwau.md"

    def tearDown(self):
        self.tmp.cleanup()

    def revise(self, *extra, **env):
        return run_tool("generate-post.py", [
            "--revise", str(self.draft), "--findings", str(self.report), "--out", str(self.out),
            *extra,
        ], env)

    def test_revise_fake_copies_its_fixture_and_announces_the_stage(self):
        proc = self.revise(REVISE_FAKE_PATH=str(self.revised_fixture))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.out.read_text(encoding="utf-8"),
                         self.revised_fixture.read_text(encoding="utf-8"))
        meta = [ln for ln in proc.stdout.splitlines() if ln.startswith("META ")][-1]
        self.assertEqual(json.loads(meta[5:]), {
            "stage": "revise", "model": None, "usage": None, "cost_usd": None, "fake": True,
        })

    def test_revise_calls_the_configured_model_and_reports_its_cost(self):
        calls = []

        def model_reply(model, prompt, api_key):
            calls.append((model, prompt, api_key))
            return "校訂後の下書き", {"prompt_tokens": 900, "cost": 0.02}

        with unittest.mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key",
                                                    "GEN_MODEL": "vendor/revise-model"}):
            os.environ.pop("REVISE_FAKE_PATH", None)
            with unittest.mock.patch.object(generate_post, "call_openrouter", model_reply), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                rc = generate_post.main([
                    "--revise", str(self.draft), "--findings", str(self.report),
                    "--out", str(self.out)])
        self.assertEqual(rc, 0)
        model, prompt, key = calls[0]
        self.assertEqual((model, key), ("vendor/revise-model", "test-key"))
        self.assertIn("教室", prompt)
        self.assertEqual(self.out.read_text(encoding="utf-8"), "校訂後の下書き\n",
                         "a reply without the closing newline takes the draft's")
        meta = [ln for ln in stdout.getvalue().splitlines() if ln.startswith("META ")][-1]
        self.assertEqual(json.loads(meta[5:]), {
            "stage": "revise", "model": "vendor/revise-model", "fake": False,
            "cost_usd": 0.02, "usage": {"prompt_tokens": 900, "cost": 0.02},
        })

    def test_revise_without_key_or_fake_is_transport_error(self):
        proc = self.revise()
        self.assertEqual(proc.returncode, 4)
        self.assertFalse(self.out.exists())

    def test_revise_with_no_findings_to_correct_is_usage_error(self):
        self.report.write_text(json.dumps({"findings": []}), encoding="utf-8")
        proc = self.revise(REVISE_FAKE_PATH=str(self.revised_fixture))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no findings", proc.stderr)
        self.assertFalse(self.out.exists())

    def test_revise_rejects_the_generation_arguments(self):
        for flag, value in (("--day", "19"), ("--fewshot", "gold.md"), ("--digest", "digest.json")):
            with self.subTest(flag=flag):
                proc = self.revise(flag, value, REVISE_FAKE_PATH=str(self.revised_fixture))
                self.assertEqual(proc.returncode, 1)
                self.assertIn("--revise cannot be combined with", proc.stderr)
                self.assertFalse(self.out.exists())

    def test_generation_rejects_findings_and_still_requires_day(self):
        env = {"GEN_FAKE_PATH": str(self.revised_fixture)}
        with_findings = run_tool("generate-post.py", [
            "--day", "19", "--out", str(self.out), "--findings", str(self.report)], env)
        self.assertEqual(with_findings.returncode, 1)
        self.assertIn("--findings is only valid with --revise", with_findings.stderr)
        without_day = run_tool("generate-post.py", ["--out", str(self.out)], env)
        self.assertEqual(without_day.returncode, 1)
        self.assertIn("--day is required", without_day.stderr)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()


class RubyBaseRuleTest(unittest.TestCase):
    """Both prompts state the ruby-base rule a real draft broke on 2026-11-22."""

    ROW = loop_support.ThemeRow(20, "二十日の軌道修正", "檢視節奏，微調不重來",
                                "進む方向は、少しずつ直していけばいい。")

    def test_generation_prompt_keeps_each_ruby_base_to_one_word(self):
        prompt = generate_post.build_prompt("persona", self.ROW, 20, False, "gold", [])
        self.assertIn("ruby 的 base", prompt)
        self.assertIn("助詞、標點、其他詞一律放在 ruby 之外", prompt)
        self.assertIn("目標 260–275 字，超過 300 字會被退件", prompt)

    def test_revision_prompt_teaches_base_narrowing_on_repair_lines(self):
        finding = {"line": 99, "surface": "これからも、日本語", "rt": "にほんご",
                  "verdict": "divergent", "reason": "malformed_ruby",
                  "evidence": {"assembled": "にほんご", "unidic": "コレカラモ、ニッポンゴ",
                               "tokens": ["これからも、", "日本語"], "jmdict": [],
                               "pykakasi": "コレカラモ、ニホンゴ"}}
        prompt = generate_post.build_revision_prompt("draft", [finding])
        self.assertIn("base 只能是一個漢字詞", prompt)
        self.assertIn("把多餘的文字移到 ruby 之外", prompt)
        self.assertIn("寫平假名，不可出現標點或漢字", prompt)
        self.assertIn("malformed_ruby 的行沒有讀音證據，只修標記結構", prompt)
