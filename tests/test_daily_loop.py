"""State-machine tests for the daily-loop orchestrator on an isolated
fixture repo with injected fake runners (no network). RevisionStageToolsTest
additionally spawns the real generate-post.py and verify-post.py in their
fake modes. The real subprocess integration is measured by the sandbox
end-to-end dry run in the engine runbook validation."""
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import date
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

_spec = importlib.util.spec_from_file_location("daily_loop", ROOT / "tools" / "daily-loop.py")
daily_loop = importlib.util.module_from_spec(_spec)
sys.modules["daily_loop"] = daily_loop
_spec.loader.exec_module(daily_loop)

NOW = "2026-11-22T07:00:00+08:00"  # day 19 morning in Taipei
RAW_DRAFT = (FIXTURES / "day-019-chiisana-shouri-wo-iwau.md").read_text(encoding="utf-8")
TTS_META = {"url": "https://pub.example/audio/day-019.mp3", "unannotated": ""}
REVISED_RT = "<rt>なおした</rt>"
DIVERGENT_FINDING = {
    "line": 30, "col": 24, "surface": "教室", "rt": "きょうしち", "verdict": "divergent",
    "reason": "unattested_reading",
    "evidence": {"assembled": "きょうしち", "unidic": "キョウシツ", "tokens": ["教室"],
                 "jmdict": [], "pykakasi": None},
}


def fix_one_rt(text: str) -> str:
    """The smallest honest revision: one reading changes, nothing else."""
    return re.sub(r"<rt>.*?</rt>", REVISED_RT, text, count=1)


def malformed_on(text: str, needle: str) -> list[dict]:
    """A malformed_ruby finding on the first line of `text` holding `needle`;
    none once the post no longer carries `needle`."""
    hits = [number for number, held in enumerate(text.split("\n"), start=1) if needle in held]
    if not hits:
        return []
    return [{"line": hits[0], "col": None, "surface": needle[:60], "rt": None,
             "verdict": "divergent", "reason": "malformed_ruby",
             "evidence": {"assembled": None, "unidic": None, "tokens": [], "jmdict": [],
                          "pykakasi": None}}]


class FixtureRepo:
    def __init__(self, tmp: Path):
        self.root = tmp
        (self.root / "aidd-docs" / "references").mkdir(parents=True)
        (self.root / "source" / "_posts").mkdir(parents=True)
        (self.root / "data" / "annotations").mkdir(parents=True)
        shutil.copy2(
            ROOT / "aidd-docs" / "references" / "japanese-theme-rotation.md",
            self.root / "aidd-docs" / "references" / "japanese-theme-rotation.md",
        )
        shutil.copy2(
            ROOT / "source" / "_posts" / "day-018-nakama-no-sonzai.md",
            self.root / "source" / "_posts" / "day-018-nakama-no-sonzai.md",
        )
        shutil.copy2(
            ROOT / "data" / "annotations" / "day-018-nakama-no-sonzai.json",
            self.root / "data" / "annotations" / "day-018-nakama-no-sonzai.json",
        )
        (self.root / "AGENTS-teacher.md").write_text("# persona\n", encoding="utf-8")

    def tree_files(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())


class FakeRunners:
    """Sequenced fakes; each entry is a return code (or 'meta' for tts).
    Records (stage, post-front-matter snapshot) per call. The reviser rewrites
    the draft it is handed with `revise_text`; the annotator writes `findings`
    (a list, or a function of the post text) into every report."""

    def __init__(self, generate=None, annotate=None, gate=None, verify=None, tts=None,
                 revise=None, findings=None):
        self.calls = []
        self.posts_seen = []
        self.reviser_inputs = []
        self.draft_text = RAW_DRAFT
        self.revise_text = fix_one_rt
        self.gate_length = None
        self.verify_seen = []
        self.findings_of = findings if callable(findings) else (lambda text: list(findings or []))
        self.generate_codes = list(generate or [0])
        self.revise_codes = list(revise or [0])
        self.annotate_codes = list(annotate or [0])
        self.gate_codes = list(gate or [0])
        self.verify_codes = list(verify or [0])
        self.tts_codes = list(tts or [0])

    def _next(self, codes, name):
        self.calls.append(name)
        return codes.pop(0) if codes else 0

    def make(self, sandbox, day):
        runners = daily_loop.Runners(
            generate=lambda draft, fewshot, digest: self.generate(draft),
            revise=lambda draft, findings, out: self.revise(draft, findings, out),
            annotate=lambda post: self.annotate(post),
            gate=lambda post, json_out: self.gate(post, json_out),
            verify=lambda post, digest: self.verify(post),
            tts=lambda post, dry: self.tts(post),
        )
        return runners

    def generate(self, draft):
        rc = self._next(self.generate_codes, "generate")
        if rc == 0:
            draft.write_text(self.draft_text, encoding="utf-8")
        return rc, "META {}" if rc == 0 else "", "", {"cost_usd": 0.012} if rc == 0 else None

    def revise(self, draft, findings, out):
        rc = self._next(self.revise_codes, "revise")
        if rc == 0:
            text = draft.read_text(encoding="utf-8")
            self.reviser_inputs.append((text, findings.read_text(encoding="utf-8")))
            out.write_text(self.revise_text(text), encoding="utf-8")
        return rc, "META {}" if rc == 0 else "", "", {"cost_usd": 0.006} if rc == 0 else None

    def annotate(self, post):
        text = post.read_text(encoding="utf-8")
        self.posts_seen.append(text)
        report = post.parents[2] / "data" / "annotations" / f"{post.stem}.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(
            {"post": post.name, "post_sha256": daily_loop.sha256_text(text),
             "findings": self.findings_of(text)}),
            encoding="utf-8")
        rc = self._next(self.annotate_codes, "annotate")
        return rc, "", "", None

    def gate(self, post, json_out):
        self.posts_seen.append(post.read_text(encoding="utf-8"))
        rc = self._next(self.gate_codes, "gate")
        checks = []
        if rc == 3:
            checks.append({"check": "sections.unrecognized", "severity": "soft",
                           "state": "issue"})
        if self.gate_length is not None:
            checks.append({"check": "length.maximum", "severity": "hard", "state": "satisfied",
                           "evidence": [{"actual": self.gate_length}]})
        if checks:
            json_out.write_text(json.dumps({"posts": [{"checks": checks}]}), encoding="utf-8")
        return rc, "", "", None

    def verify(self, post):
        self.verify_seen.append(post.read_text(encoding="utf-8"))
        rc = self._next(self.verify_codes, "verify")
        return rc, "", "", {"cost_usd": 0.004} if rc in (0, 3) else None

    def tts(self, post):
        rc = self._next(self.tts_codes, "tts")
        meta = dict(TTS_META) if rc == 0 else None
        return rc, "", "", meta


def run(repo, runners, *, day=None, env=None, live=False, dry=True, factory=None):
    args = SimpleNamespace(day=day, live=live, dry_run=dry)
    out = io.StringIO()
    merged = {"LOOP_NOW": NOW, **(env or {})}
    if not live:
        merged.setdefault("GEN_MODEL", "z-ai/glm-5.3")
        merged.setdefault("VERIFY_MODEL", "openai/gpt-4o")
    code, loop = daily_loop.run_engine(
        args, merged, out=out, repo_root=repo.root,
        runners_factory=factory or (lambda sandbox, d: runners.make(sandbox, d)),
    )
    return code, loop, out.getvalue()


class HappyPathTest(unittest.TestCase):
    def test_full_pass_run_records_all_stages_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            before = repo.tree_files()
            runners = FakeRunners()
            code, loop, transcript = run(repo, runners)
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "pass")
            self.assertEqual(loop.reasons, [])
            self.assertEqual(loop.stage_reached, "finalize")
            self.assertEqual(loop.audio_url, TTS_META["url"])
            self.assertEqual(loop.record_dict["cost_usd"], 0.016)
            self.assertFalse(loop.record_dict["revised"])
            self.assertIsNone(loop.record_dict["revision_cost_usd"])
            self.assertNotIn("revise", runners.calls)
            for stage in ("select", "generate", "annotate", "gate", "verify", "tts", "finalize"):
                self.assertIn(f"== stage: {stage} ==", transcript)
            self.assertIn("DISARMED:", transcript.splitlines()[-1])
            self.assertEqual(repo.tree_files(), before)

    def test_final_post_carries_audio_url_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners()
            run(repo, runners)
            final = runners.posts_seen[-1]
            self.assertIn("status: pass", final)
            self.assertIn(f"audio_url: {TTS_META['url']}", final)
            self.assertIn("date: 2026-11-22 03:00:00", final)
            self.assertIn("generated_by: z-ai/glm-5.3@openrouter", final)
            self.assertIn("verified_by: openai/gpt-4o@openrouter", final)


class NoopTest(unittest.TestCase):
    def test_existing_day_noops(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            before = repo.tree_files()
            runners = FakeRunners()
            code, loop, transcript = run(repo, runners, day=18)
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "noop")
            self.assertEqual(runners.calls, [])
            self.assertEqual(repo.tree_files(), before)

    def test_past_day_100_schedule_reports_noop_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners()
            code, loop, transcript = run(
                repo, runners, env={"LOOP_NOW": "2027-02-20T07:00:00+08:00"})
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "noop")
            self.assertEqual(runners.calls, [])
            self.assertIn("past day 100", transcript)


class EventNameTest(unittest.TestCase):
    def test_both_dispatch_spellings_map_to_one_constant(self):
        self.assertEqual(daily_loop.normalize_event({"LOOP_EVENT": "workflow_dispatch"}),
                         "dispatch")
        self.assertEqual(daily_loop.normalize_event({"LOOP_EVENT": "dispatch"}), "dispatch")
        self.assertEqual(daily_loop.normalize_event({"LOOP_EVENT": "schedule"}), "schedule")
        self.assertEqual(daily_loop.normalize_event({"LOOP_EVENT": ""}), "")
        self.assertEqual(daily_loop.normalize_event({}), "")

    def test_workflow_dispatch_arms_live(self):
        args = SimpleNamespace(live=False, dry_run=False)
        live, arming = daily_loop.resolve_arming(
            args, {"LOOP_EVENT": "workflow_dispatch", "LOOP_DISPATCH_LIVE": "true"})
        self.assertEqual((live, arming), (True, "arming"))
        live, _ = daily_loop.resolve_arming(
            args, {"LOOP_EVENT": "dispatch", "LOOP_DISPATCH_LIVE": "true"})
        self.assertTrue(live)

    def test_degrade_forcing_applies_to_schedule_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            (repo.root / "data" / "engine-state.json").write_text(
                json.dumps({"schema": 1, "degraded": True, "recover_passes": 0}),
                encoding="utf-8")
            runners = FakeRunners()
            code, loop, _ = run(repo, runners, live=True, dry=False,
                                env={"LOOP_EVENT": "workflow_dispatch",
                                     "LOOP_DISPATCH_LIVE": "true"})
            self.assertTrue(loop.live)
            # No git remote in this fixture: the live pre-check fails loudly
            # instead of silently degrading to a dry run.
            self.assertEqual(loop.result, "fail")
            self.assertIn("progress_check_failed", loop.reasons)


class FailTransitionTest(unittest.TestCase):
    def test_generate_fail_retries_once_then_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(generate=[4, 4])
            code, loop, transcript = run(repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.result, "fail")
            self.assertEqual(loop.reasons, ["generate_failed"])
            self.assertEqual(loop.stage_reached, "generate")
            self.assertEqual(runners.calls.count("generate"), 2)
            self.assertNotIn("DISARMED:", transcript)

    def test_gate_fail_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(gate=[2])
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.reasons, ["gate_fail"])

    def test_verifier_fail_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(verify=[2])
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.reasons, ["verifier_fail"])

    def test_verifier_unavailable_retries_then_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(verify=[4, 4])
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "flag")
            self.assertIn("verifier_unavailable", loop.reasons)
            self.assertEqual(runners.calls.count("verify"), 2)


class ExecutionTierTest(unittest.TestCase):
    """Signal kills and unexpected exits are execution errors (FAIL), never
    completed verdicts; only phase exhaustion stays a FLAG."""

    def test_gate_signal_kill_is_an_execution_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(gate=[-9])
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.reasons, ["gate_error"])

    def test_final_gate_signal_kill_is_an_execution_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(gate=[0, -9])
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.reasons, ["gate_error"])

    def test_verifier_signal_kill_fails_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(verify=[-9])
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.reasons, ["verifier_error"])
            self.assertEqual(runners.calls.count("verify"), 1)

    def test_tts_crash_is_execution_error_not_phase_exhaustion(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(tts=[1])
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.reasons, ["tts_error"])
            self.assertEqual(runners.calls.count("tts"), 1)


class RevisionTest(unittest.TestCase):
    """A divergent draft annotate gets exactly one reading revision and one
    re-check; nothing after the draft annotate ever revises again."""

    def test_divergent_draft_recovers_and_the_run_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            before = repo.tree_files()
            runners = FakeRunners(annotate=[2, 0], findings=[DIVERGENT_FINDING])
            code, loop, transcript = run(repo, runners)
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "pass")
            self.assertEqual(loop.reasons, [])
            self.assertEqual(loop.stage_reached, "finalize")
            self.assertEqual(loop.audio_url, TTS_META["url"])
            self.assertTrue(loop.record_dict["revised"])
            self.assertEqual(runners.calls.count("revise"), 1)
            draft, report = runners.reviser_inputs[0]
            self.assertEqual(draft, runners.posts_seen[0])
            self.assertEqual(json.loads(report)["findings"], [DIVERGENT_FINDING])
            self.assertEqual(runners.posts_seen[1], fix_one_rt(draft))
            final = runners.posts_seen[-1]
            self.assertIn(REVISED_RT, final)
            self.assertIn("status: pass", final)
            self.assertIn(f"audio_url: {TTS_META['url']}", final)
            self.assertIn("revising the flagged readings once", transcript)
            self.assertEqual(repo.tree_files(), before)

    def test_revision_cost_lands_in_the_record_and_sums(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(annotate=[2, 0])
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 0)
            self.assertAlmostEqual(loop.record_dict["revision_cost_usd"], 0.006)
            self.assertAlmostEqual(loop.record_dict["cost_usd"], 0.012 + 0.006 + 0.004)

    def test_a_rejected_revision_is_still_charged(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(annotate=[2, 0])
            runners.revise_text = lambda text: text.replace("百日挑戰", "二百日挑戰", 1)
            code, loop, _ = run(repo, runners)
            self.assertEqual(loop.reasons, ["revision_violation"])
            self.assertAlmostEqual(loop.record_dict["revision_cost_usd"], 0.006)
            self.assertAlmostEqual(loop.record_dict["cost_usd"], 0.012 + 0.006)

    def test_low_confidence_after_the_revision_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(annotate=[2, 3])
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "flag")
            self.assertEqual(loop.reasons, ["low_confidence"])
            self.assertTrue(loop.record_dict["revised"])

    def test_revision_touching_prose_fails_before_any_later_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            before = repo.tree_files()
            runners = FakeRunners(annotate=[2, 0])
            runners.revise_text = lambda text: text.replace("百日挑戰", "二百日挑戰", 1)
            code, loop, transcript = run(repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.result, "fail")
            self.assertEqual(loop.reasons, ["revision_violation"])
            self.assertEqual(loop.stage_reached, "annotate")
            self.assertFalse(loop.record_dict["revised"])
            for stage in ("gate", "verify", "tts"):
                self.assertNotIn(stage, runners.calls)
            self.assertIn("revision changed line", transcript)
            self.assertEqual(repo.tree_files(), before)

    def test_second_divergence_fails_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(annotate=[2, 2], findings=[DIVERGENT_FINDING])
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.reasons, ["revision_failed"])
            self.assertEqual(runners.calls.count("revise"), 1)
            self.assertEqual(runners.calls.count("annotate"), 2)
            for stage in ("gate", "verify", "tts"):
                self.assertNotIn(stage, runners.calls)

    def test_divergence_after_the_revision_never_revises_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(annotate=[2, 0, 2])
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.reasons, ["annotation_divergent"])
            self.assertEqual(runners.calls.count("revise"), 1)
            self.assertEqual(loop.stage_reached, "finalize")

    def test_annotator_execution_errors_never_revise(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(annotate=[1])
            code, loop, _ = run(repo, runners)
            self.assertEqual(loop.reasons, ["annotation_error"])
            self.assertNotIn("revise", runners.calls)
            runners = FakeRunners(annotate=[2, 1])
            code, loop, _ = run(repo, runners)
            self.assertEqual(loop.reasons, ["annotation_error"])

    def test_failed_reviser_is_revision_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(annotate=[2, 0], revise=[1])
            code, loop, _ = run(repo, runners)
            self.assertEqual(loop.reasons, ["revision_failed"])
            self.assertEqual(runners.calls.count("revise"), 1)
            runners = FakeRunners(annotate=[2, 0], revise=[4, 4])
            code, loop, _ = run(repo, runners)
            self.assertEqual(loop.reasons, ["revision_failed"])
            self.assertEqual(runners.calls.count("revise"), 2)
            self.assertEqual(runners.calls.count("annotate"), 1)

    def test_reviser_transport_error_retries_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(annotate=[2, 0], revise=[4, 0])
            code, loop, _ = run(repo, runners)
            self.assertEqual((code, loop.result), (0, "pass"))
            self.assertEqual(runners.calls.count("revise"), 2)

    BROKEN_RUBY = '<ruby>街<rt>まち</rt></ruby>'
    WHOLE_RUBY = '<ruby>街<rp>（</rp><rt>まち</rt><rp>）</rp></ruby>'

    def repair_run(self, findings):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(annotate=[2, 0], findings=findings)
            runners.draft_text = RAW_DRAFT.replace(
                "<!-- more -->", f"<!-- more -->\n\n今日は{self.BROKEN_RUBY}を歩いた。", 1)
            runners.revise_text = lambda text: text.replace(self.BROKEN_RUBY, self.WHOLE_RUBY)
            code, loop, _ = run(repo, runners)
            return code, loop, runners

    def test_malformed_ruby_line_is_repaired_and_allowed_to_differ(self):
        code, loop, runners = self.repair_run(lambda text: malformed_on(text, self.BROKEN_RUBY))
        self.assertEqual((code, loop.result), (0, "pass"))
        self.assertIn(self.WHOLE_RUBY, runners.posts_seen[-1])

    def test_the_same_repair_without_a_malformed_finding_is_a_violation(self):
        code, loop, runners = self.repair_run(None)
        self.assertEqual((code, loop.result), (2, "fail"))
        self.assertEqual(loop.reasons, ["revision_violation"])
        self.assertEqual(runners.calls.count("revise"), 1)


class RevisionStageToolsTest(unittest.TestCase):
    """The revision round through the real stage tools: generate-post.py (fake
    generation, then fake revision) and verify-post.py run as subprocesses
    from the real runners; annotate, gate and tts stay fakes."""

    def test_real_tools_carry_the_revision_round_to_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_tmp:
            repo = FixtureRepo(Path(tmp))
            shutil.copytree(ROOT / "tools", repo.root / "tools",
                            ignore=shutil.ignore_patterns("__pycache__"))
            before = repo.tree_files()
            fakes = Path(fake_tmp)
            (fakes / "draft.md").write_text(RAW_DRAFT, encoding="utf-8")
            runners = FakeRunners(annotate=[2, 0], findings=[DIVERGENT_FINDING])
            fake_annotate = runners.annotate

            def annotate_staging_the_revision(post):
                # The diverging post is also what a perfect reviser hands back,
                # with one reading corrected.
                (fakes / "revised.md").write_text(
                    fix_one_rt(post.read_text(encoding="utf-8")), encoding="utf-8")
                return fake_annotate(post)

            runners.annotate = annotate_staging_the_revision
            fake_env = {"GEN_FAKE_PATH": str(fakes / "draft.md"),
                        "REVISE_FAKE_PATH": str(fakes / "revised.md"),
                        "VERIFY_FAKE_PATH": str(FIXTURES / "verify-pass.txt")}

            def factory(sandbox, day):
                real = daily_loop.subprocess_runners(sandbox, day)
                return daily_loop.Runners(generate=real.generate, revise=real.revise,
                                          annotate=runners.annotate, gate=runners.gate,
                                          verify=real.verify,
                                          tts=lambda post, dry: runners.tts(post))

            with unittest.mock.patch.dict(os.environ, fake_env):
                code, loop, transcript = run(repo, runners, env=fake_env, factory=factory)
            self.assertEqual(code, 0, transcript)
            self.assertEqual(loop.result, "pass")
            self.assertTrue(loop.revised)
            self.assertEqual(runners.posts_seen[1], fix_one_rt(runners.posts_seen[0]))
            self.assertEqual(transcript.count("draft written:"), 2)
            for stage in ("gate", "verify", "tts", "finalize"):
                self.assertIn(f"== stage: {stage} ==", transcript)
            self.assertEqual(repo.tree_files(), before)


class FlagTransitionTest(unittest.TestCase):
    def test_verifier_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(verify=[3])
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "flag")
            self.assertIn("verifier_flag", loop.reasons)
            self.assertIn("status: flag", runners.posts_seen[-1])

    def test_tts_fail_flags_with_empty_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(tts=[4, 4])
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "flag")
            self.assertIn("tts_failed", loop.reasons)
            self.assertIsNone(loop.audio_url)
            self.assertIn("audio_url:", runners.posts_seen[-1].split("status")[0])

    def test_unannotated_kanji_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners()
            runners.tts_codes = [0]
            original = runners.tts

            def tts_with_kanji(post):
                rc, so, se, _ = original(post)
                return rc, so, se, {"url": TTS_META["url"], "unannotated": "漢"}
            runners.tts = tts_with_kanji
            code, loop, _ = run(repo, runners)
            self.assertEqual(loop.result, "flag")
            self.assertIn("unannotated_kanji", loop.reasons)

    def test_low_confidence_and_gate_flag_dedupe_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            soft_report = json.dumps({"posts": [{"checks": [
                {"check": "annotation.low_confidence", "severity": "soft", "state": "issue"}
            ]}]})
            runners = FakeRunners(annotate=[3, 3, 3], gate=[3, 3, 3])
            base_gate = runners.gate

            def gate_writes_soft(post, json_out):
                rc = base_gate(post, json_out)[0]
                json_out.write_text(soft_report, encoding="utf-8")
                return rc, "", "", None
            runners.gate = gate_writes_soft
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "flag")
            self.assertEqual(loop.reasons.count("low_confidence"), 1)
            self.assertEqual(loop.reasons.count("annotation.low_confidence"), 1)

    def test_vendor_preflight_same_vendor_blocks_auto_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(verify=[0])
            code, loop, _ = run(repo, runners, env={"GEN_MODEL": "z-ai/glm-5.3",
                                                    "VERIFY_MODEL": "z-ai/glm-4.6"})
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "flag")
            self.assertIn("verifier_vendor", loop.reasons)
            self.assertEqual(runners.calls.count("verify"), 0)


class FinalizeRefreezeTest(unittest.TestCase):
    def test_final_gate_flag_refreezes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(gate=[0, 3, 3])
            code, loop, transcript = run(repo, runners)
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "flag")
            self.assertIn("re-entering finalize once", transcript)
            self.assertEqual(runners.calls.count("gate"), 3)
            self.assertEqual(runners.calls.count("annotate"), 3)

    def test_final_gate_regression_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(gate=[0, 2])
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.reasons, ["final_gate_regression"])


class DegradeTest(unittest.TestCase):
    def entry(self, day, result, live=True):
        return {"day": day, "result": result, "live": live, "reasons": []}

    def test_enter_after_three_consecutive_fail_days(self):
        entries = [self.entry(17, "fail"), self.entry(17, "fail"), self.entry(18, "fail"),
                   self.entry(19, "fail")]
        state, changed, entered, _ = daily_loop.apply_state_transitions(
            {"schema": 1, "degraded": False, "recover_passes": 0}, entries,
            live=True, noop=False, manual=True, result="fail")
        self.assertTrue(changed and entered)
        self.assertTrue(state["degraded"])

    def test_gap_breaks_streak(self):
        entries = [self.entry(16, "fail"), self.entry(18, "fail"), self.entry(19, "fail")]
        _, changed, entered, _ = daily_loop.apply_state_transitions(
            {"schema": 1, "degraded": False, "recover_passes": 0}, entries,
            live=True, noop=False, manual=True, result="fail")
        self.assertTrue(changed)
        self.assertFalse(entered)

    def test_manual_fail_never_clears_the_latch(self):
        state = {"schema": 1, "degraded": True, "recover_passes": 1}
        entries = [self.entry(17, "pass"), self.entry(18, "fail"), self.entry(19, "fail")]
        state, changed, entered, recovered = daily_loop.apply_state_transitions(
            state, entries, live=True, noop=False, manual=True, result="fail")
        self.assertTrue(state["degraded"])
        self.assertFalse(entered)
        self.assertFalse(recovered)
        self.assertEqual(state["recover_passes"], 0)
        self.assertTrue(changed)

    def test_streak_scan_reads_only_the_latest_tail(self):
        results = {15: "fail", 16: "fail", 17: "fail", 18: "pass", 19: "fail"}
        self.assertFalse(daily_loop.enters_degraded(results))
        self.assertTrue(daily_loop.enters_degraded({17: "fail", 18: "fail", 19: "fail"}))
        entries = [self.entry(15, "fail"), self.entry(16, "fail"), self.entry(17, "fail"),
                   self.entry(18, "pass"), self.entry(19, "fail")]
        _, _, entered, _ = daily_loop.apply_state_transitions(
            {"schema": 1, "degraded": False, "recover_passes": 0}, entries,
            live=True, noop=False, manual=True, result="fail")
        self.assertFalse(entered)

    def test_pass_day_breaks_streak(self):
        entries = [self.entry(17, "fail"), self.entry(18, "pass"), self.entry(19, "fail")]
        _, _, entered, _ = daily_loop.apply_state_transitions(
            {"schema": 1, "degraded": False, "recover_passes": 0}, entries,
            live=True, noop=False, manual=True, result="fail")
        self.assertFalse(entered)

    def test_dry_and_never_count(self):
        entries = [self.entry(16, "fail", live=False), self.entry(17, "fail"),
                   self.entry(18, "fail"), {"day": 17, "result": "fail", "live": True, "event": "publish"}]
        results = daily_loop.day_results(entries)
        self.assertEqual(results, {17: "fail", 18: "fail"})
        state, changed, _, _ = daily_loop.apply_state_transitions(
            {"schema": 1, "degraded": False, "recover_passes": 0}, entries,
            live=False, noop=False, manual=True, result="fail")
        self.assertFalse(changed)

    def test_exit_after_two_manual_passes(self):
        state = {"schema": 1, "degraded": True, "recover_passes": 0}
        entries = []
        state, changed, _, recovered = daily_loop.apply_state_transitions(
            state, entries, live=True, noop=False, manual=True, result="pass")
        self.assertTrue(changed and not recovered)
        self.assertEqual(state["recover_passes"], 1)
        state, changed, _, recovered = daily_loop.apply_state_transitions(
            state, entries, live=True, noop=False, manual=True, result="pass")
        self.assertTrue(recovered)
        self.assertFalse(state["degraded"])

    def test_non_pass_resets_recovery(self):
        state = {"schema": 1, "degraded": True, "recover_passes": 1}
        state, _, _, recovered = daily_loop.apply_state_transitions(
            state, [], live=True, noop=False, manual=True, result="flag")
        self.assertFalse(recovered)
        self.assertEqual(state["recover_passes"], 0)

    def test_schedule_pass_does_not_recover(self):
        state = {"schema": 1, "degraded": True, "recover_passes": 0}
        state, changed, _, _ = daily_loop.apply_state_transitions(
            state, [], live=True, noop=False, manual=False, result="pass")
        self.assertFalse(changed)
        self.assertEqual(state["recover_passes"], 0)

    def test_schedule_live_blocked_runs_dry_with_degrade_banner(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            (repo.root / "data" / "engine-state.json").write_text(
                json.dumps({"schema": 1, "degraded": True, "recover_passes": 1}),
                encoding="utf-8")
            runners = FakeRunners()
            env = {"LOOP_EVENT": "schedule", "ENGINE_LIVE": "true"}
            code, loop, transcript = run(repo, runners, live=True, dry=False, env=env)
            self.assertEqual(code, 0)
            self.assertFalse(loop.live)
            lines = [ln for ln in transcript.splitlines() if ln.strip()]
            self.assertIn("DEGRADE:", lines[-2])
            self.assertIn("DISARMED:", lines[-1])


class ProgressTest(unittest.TestCase):
    def test_decision_table(self):
        f = daily_loop.progress_decision
        self.assertEqual(f(True, False, False), "noop")
        self.assertEqual(f(True, True, True), "noop")
        self.assertEqual(f(False, True, True), "pending_pr")
        self.assertEqual(f(False, True, False), "resume_pr")
        self.assertEqual(f(False, False, False), "generate")


class BannerOrderTest(unittest.TestCase):
    def test_disarmed_banner_after_transcript_and_suppressed_on_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            ok_code, _, ok_out = run(repo, FakeRunners())
            ok_lines = [ln for ln in ok_out.splitlines() if ln.strip()]
            self.assertEqual(ok_lines[-1], daily_loop.DISARMED_BANNER)
            positions = [ok_lines.index(f"== stage: {s} ==")
                         for s in ("select", "generate", "annotate", "gate", "verify",
                                   "tts", "finalize")]
            self.assertEqual(positions, sorted(positions))
            self.assertLess(positions[-1], ok_lines.index(daily_loop.DISARMED_BANNER))
            fail_code, _, fail_out = run(repo, FakeRunners(generate=[4, 4]))
            self.assertNotIn("DISARMED:", fail_out)
            self.assertEqual(ok_code, 0)
            self.assertEqual(fail_code, 2)


class PostCompositionTest(unittest.TestCase):
    def test_split_draft_takes_description_only(self):
        raw = "---\ntitle: x\ndescription: \"我的描述\"\nday: 999\n---\n\nbody line\n"
        description, body = daily_loop.split_draft(raw)
        self.assertEqual(description, "我的描述")
        self.assertEqual(body, "body line\n")
        self.assertEqual(daily_loop.split_draft("no front matter")[0], None)

    def test_set_front_matter_fields_replaces_and_inserts(self):
        post = "---\nstatus: pass\naudio_url:\n---\nbody"
        out = daily_loop.set_front_matter_fields(post, {"status": "status: flag",
                                                        "audio_url": "audio_url: https://x/y.mp3"})
        self.assertIn("status: flag", out)
        self.assertIn("audio_url: https://x/y.mp3", out)
        self.assertTrue(out.endswith("body"))

    def test_romaji_slug_falls_back_without_pykakasi(self):
        self.assertEqual(daily_loop.romaji_slug("小さな勝利を祝う"), "post")


class RevisionInvariantTest(unittest.TestCase):
    """The revision invariant on a hand-written post: readings may change
    anywhere, everything else only on a line flagged malformed_ruby."""

    ORIGINAL = (
        "---\nday: 19\n---\n"
        '<ruby>海<rp>（</rp><rt>うみ</rt><rp>）</rp></ruby>を見た。\n'
        "<ruby>空<rp>（</rp><rt>そら</rt><rp>）</rp></ruby>は青い。\n"
    )
    BROKEN = ORIGINAL.replace('<ruby>海<rp>（</rp><rt>うみ</rt>', '<ruby>海<rt>うみ</rt>')

    def violation(self, revised, repair_lines=frozenset()):
        return daily_loop.revision_violation(self.ORIGINAL, revised, repair_lines)

    def test_identical_text_passes(self):
        self.assertIsNone(self.violation(self.ORIGINAL))

    def test_changed_readings_pass_on_every_line(self):
        revised = self.ORIGINAL.replace("<rt>うみ</rt>", "<rt>かい</rt>").replace(
            "<rt>そら</rt>", "<rt>くう</rt>")
        self.assertIsNone(self.violation(revised))

    def test_prose_change_reports_its_line(self):
        self.assertEqual(self.violation(self.ORIGINAL.replace("青い", "赤い")), 5)

    def test_markup_change_around_a_reading_reports_its_line(self):
        without_rp = self.ORIGINAL.replace('<rp>（</rp><rt>うみ</rt>', "<rt>うみ</rt>")
        self.assertEqual(self.violation(without_rp), 4)

    def test_added_or_removed_line_is_a_violation(self):
        added = self.ORIGINAL + "追加の一行。\n"
        removed = self.ORIGINAL.replace(
            "<ruby>空<rp>（</rp><rt>そら</rt><rp>）</rp></ruby>は青い。\n", "")
        self.assertEqual(self.violation(added), 6)
        self.assertEqual(self.violation(removed), 5)

    def test_dropped_final_newline_is_a_violation(self):
        self.assertEqual(self.violation(self.ORIGINAL.rstrip("\n")), 5)

    def test_malformed_line_may_change_only_when_listed_for_repair(self):
        repaired = daily_loop.revision_violation(self.BROKEN, self.ORIGINAL, {4})
        self.assertIsNone(repaired)
        self.assertEqual(daily_loop.revision_violation(self.BROKEN, self.ORIGINAL, set()), 4)
        repaired_and_edited = daily_loop.revision_violation(
            self.BROKEN, self.ORIGINAL.replace("青い", "赤い"), {4})
        self.assertEqual(repaired_and_edited, 5)

    def test_repair_lines_come_from_malformed_findings_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            self.assertEqual(daily_loop.malformed_ruby_lines(report), set())
            report.write_text(json.dumps({"findings": [
                {"line": 4, "reason": "malformed_ruby"},
                {"line": 5, "reason": "unattested_reading"},
                {"line": 7, "reason": "malformed_ruby"},
            ]}), encoding="utf-8")
            self.assertEqual(daily_loop.malformed_ruby_lines(report), {4, 7})


class FrontMatterQuotingTest(unittest.TestCase):
    def test_yaml_double_quoted_escapes_model_owned_characters(self):
        self.assertEqual(daily_loop.yaml_double_quoted('練習 "成功" 的表達'),
                         '"練習 \\"成功\\" 的表達"')
        self.assertEqual(daily_loop.yaml_double_quoted("a\\b"), '"a\\\\b"')
        self.assertEqual(daily_loop.yaml_double_quoted("a\tb"), '"a\\tb"')

    def test_composed_post_front_matter_is_valid(self):
        row = daily_loop.loop_support.ThemeRow(19, "小さな勝利", "重點", "核心句。")
        post = daily_loop.compose_post(row, 19, date(2026, 11, 22), "body",
                                       '練習 "成功" 的表達', "pass", "https://x/y.mp3",
                                       "z-ai/glm-5.3", "openai/gpt-4o", 0.01)
        self.assertTrue(daily_loop.front_matter_valid(post),
                        msg=post.split("---")[1])
        self.assertIn('description: "練習 \\"成功\\" 的表達"', post)

    def test_validator_rejects_unclosed_quotes_and_bad_lines(self):
        unclosed = '---\ndescription: "unclosed\n---\nbody'
        self.assertFalse(daily_loop.front_matter_valid(unclosed))
        bad_line = "---\nnot a mapping line\n---\nbody"
        self.assertFalse(daily_loop.front_matter_valid(bad_line))
        self.assertTrue(daily_loop.front_matter_valid(
            "---\ncategories:\n  - 日語學習\ndescription: \"ok\"\n---\nbody"))


class StateSchemaTest(unittest.TestCase):
    def test_engine_state_bytes_shape(self):
        blob = daily_loop.state_bytes({"schema": 1, "degraded": True, "recover_passes": 1})
        data = json.loads(blob)
        self.assertEqual(data, {"schema": 1, "degraded": True, "recover_passes": 1})

    def test_ledger_line_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            _, loop, _ = run(repo, FakeRunners())
            record = loop.record_dict
            for key in ("ts", "day", "live", "result", "reasons", "model_gen",
                        "model_verify", "stage_reached", "audio_url", "cost_usd",
                        "revised", "revision_cost_usd", "latency_ms"):
                self.assertIn(key, record)
            self.assertEqual(record["day"], 19)
            self.assertFalse(record["live"])


class GhStub:
    """Executable `gh` stand-in: pr-list answers [], pr-create prints a URL
    (or fails) and logs its arguments."""

    def __init__(self, tmp: Path, fail_create: bool = False):
        self.log = tmp / "gh-calls.log"
        fail = "true" if fail_create else "false"
        bindir = tmp / "gh-bin"
        bindir.mkdir(exist_ok=True)
        self.script = bindir / "gh"
        self.script.write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> {self.log}\n'
            'if [ "$1" = "pr" ] && [ "$2" = "list" ]; then echo "[]"; exit 0; fi\n'
            "if [ \"$1\" = \"pr\" ] && [ \"$2\" = \"create\" ]; then\n"
            f"  if {fail}; then echo 'gh: create failed' >&2; exit 1; fi\n"
            "  echo 'https://github.com/example/jasdoit-daily/pull/7'\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        self.script.chmod(0o755)

    def create_calls(self):
        if not self.log.is_file():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.startswith("pr create")]


class GitLiveRepo:
    """A clone of a bare origin pre-seeded with the fixture corpus. Live
    runs publish to origin only; the ambient clone must never move."""

    def __init__(self, tmp: Path):
        self.origin = tmp / "origin.git"
        self.root = tmp / "clone"
        seed = tmp / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        for key, value in (("user.name", "seed"), ("user.email", "seed@example.test")):
            subprocess.run(["git", "-C", str(seed), "config", key, value], check=True)
        FixtureRepo(seed)
        subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "seed"], check=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
                       check=True)
        subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(self.origin)],
                       check=True)
        subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.root)], check=True)
        for key, value in (("user.name", "engine"), ("user.email", "engine@example.test")):
            subprocess.run(["git", "-C", str(self.root), "config", key, value], check=True)
        self.seed_sha = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"],
                                       capture_output=True, text=True, check=True).stdout.strip()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args],
                              capture_output=True, text=True)

    def origin_git(self, *args):
        return subprocess.run(["git", "-C", str(self.origin), *args],
                              capture_output=True, text=True)

    def origin_subjects(self, ref="main"):
        proc = self.origin_git("log", "--format=%s", ref)
        return proc.stdout.splitlines()

    def origin_show(self, ref, path):
        return self.origin_git("show", f"{ref}:{path}").stdout

    def ambient_state(self):
        head = self.git("rev-parse", "HEAD").stdout.strip()
        status = self.git("status", "--porcelain").stdout.strip()
        branches = self.git("branch", "--list", "loop/*").stdout.strip()
        worktrees = len([line for line in self.git("worktree", "list").stdout.splitlines()
                         if line.strip()])
        return head, status, branches, worktrees


def run_live(repo: GitLiveRepo, runners: FakeRunners, env=None, day=None):
    args = SimpleNamespace(day=day, live=True, dry_run=False)
    out = io.StringIO()
    merged = {"LOOP_NOW": NOW, "LOOP_EVENT": "workflow_dispatch",
              "LOOP_DISPATCH_LIVE": "true",
              "GEN_MODEL": "z-ai/glm-5.3", "VERIFY_MODEL": "openai/gpt-4o",
              **(env or {})}
    code, loop = daily_loop.run_engine(
        args, merged, out=out, repo_root=repo.root,
        runners_factory=lambda sandbox, d: runners.make(sandbox, d),
    )
    return code, loop, out.getvalue()


class gh_on_path:
    def __init__(self, stub: GhStub):
        self.stub = stub
        self.old_path = None

    def __enter__(self):
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.stub.script.parent}{os.pathsep}{self.old_path}"
        return self.stub

    def __exit__(self, *exc):
        os.environ["PATH"] = self.old_path


class LivePublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = GitLiveRepo(Path(self.tmp.name))

    def tearDown(self):
        subprocess.run(["git", "-C", str(self.repo.root), "worktree", "prune"],
                       capture_output=True)
        self.tmp.cleanup()

    def assert_ambient_untouched(self):
        head, status, branches, worktrees = self.repo.ambient_state()
        self.assertEqual(head, self.repo.seed_sha)
        self.assertEqual(status, "")
        self.assertEqual(branches, "", "ambient checkout gained a loop branch")
        self.assertEqual(worktrees, 1, "a publish worktree was left behind")

    def test_pass_pushes_record_with_content_then_event_ledger(self):
        with gh_on_path(GhStub(Path(self.tmp.name))) as stub:
            code, loop, transcript = run_live(self.repo, FakeRunners())
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "pass")
            subjects = self.repo.origin_subjects()
            self.assertEqual(subjects[0], "loop: day 019 pass ledger")
            self.assertEqual(subjects[1], "content: day 019 post (engine pass)")
            post_names = self.repo.origin_git(
                "ls-tree", "--name-only", "main:source/_posts").stdout.split()
            self.assertTrue(any(n.startswith("day-019-") for n in post_names),
                            msg=post_names)
            ledger_main = [json.loads(l) for l in
                           self.repo.origin_show("main", "data/loop-log.jsonl").splitlines()]
            self.assertEqual(ledger_main[0]["result"], "pass")
            self.assertEqual(ledger_main[1]["event"], "publish")
            self.assertTrue(ledger_main[1]["pushed"])
            ledger_content = [json.loads(l) for l in
                              self.repo.origin_show("main~1", "data/loop-log.jsonl").splitlines()]
            self.assertEqual(len(ledger_content), 1)
            self.assertEqual(ledger_content[0]["result"], "pass")
            self.assertEqual(stub.create_calls(), [])
            self.assert_ambient_untouched()

    def test_veto_routes_a_pass_to_a_pr_instead_of_main(self):
        runners = FakeRunners()
        with gh_on_path(GhStub(Path(self.tmp.name))) as stub:
            code, loop, _ = run_live(self.repo, runners, env={"AUTO_PUSH_N3": "false"})
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "pass")
            self.assertNotIn("content: day 019 post (engine pass)",
                            self.repo.origin_subjects())
            main_posts = self.repo.origin_git("ls-tree", "--name-only",
                                              "main:source/_posts").stdout.split()
            self.assertFalse(any(n.startswith("day-019-") for n in main_posts), msg=main_posts)
            branches = self.repo.origin_git("branch", "--list", "loop/day-019*").stdout.split()
            self.assertEqual(branches, ["loop/day-019"])
            self.assertEqual(len(stub.create_calls()), 1)

    def test_any_other_veto_value_still_pushes_a_pass_to_main(self):
        runners = FakeRunners()
        with gh_on_path(GhStub(Path(self.tmp.name))):
            code, loop, _ = run_live(self.repo, runners, env={"AUTO_PUSH_N3": "true"})
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "pass")
            self.assertIn("content: day 019 post (engine pass)", self.repo.origin_subjects())

    def test_verifier_exhaust_flags_and_publishes_via_pr_only(self):
        runners = FakeRunners(verify=[4, 4])
        with gh_on_path(GhStub(Path(self.tmp.name))) as stub:
            code, loop, transcript = run_live(self.repo, runners)
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "flag")
            self.assertIn("verifier_unavailable", loop.reasons)
            subjects = self.repo.origin_subjects()
            self.assertEqual(subjects[0], "loop: day 019 flag ledger")
            self.assertNotIn("content: day 019 post (engine pass)", subjects)
            self.assertEqual(self.repo.origin_show("main", "source/_posts/day-018-nakama-no-sonzai.md")[:4],
                             "---\n")
            main_posts = self.repo.origin_git("ls-tree", "--name-only",
                                              "main:source/_posts").stdout.split()
            self.assertFalse(any(n.startswith("day-019-") for n in main_posts), msg=main_posts)
            ledger_main = [json.loads(l) for l in
                           self.repo.origin_show("main", "data/loop-log.jsonl").splitlines()]
            self.assertEqual(ledger_main[0]["result"], "flag")
            self.assertEqual(ledger_main[0]["reasons"], ["verifier_unavailable"])
            branches = self.repo.origin_git("branch", "--list", "loop/day-019*").stdout.split()
            self.assertEqual(branches, ["loop/day-019"])
            branch_posts = self.repo.origin_git("ls-tree", "--name-only",
                                                "loop/day-019:source/_posts").stdout.split()
            self.assertTrue(any(n.startswith("day-019-") for n in branch_posts), msg=branch_posts)
            branch_ledger = self.repo.origin_git("ls-tree", "--name-only",
                                                 "loop/day-019", "data/loop-log.jsonl").stdout.strip()
            self.assertEqual(branch_ledger, "", "PR branch must not touch the ledger")
            self.assertEqual(len(stub.create_calls()), 1)
            self.assertIn("--head loop/day-019", stub.create_calls()[0])
            self.assert_ambient_untouched()

    def test_fail_commits_ledger_and_state_only(self):
        runners = FakeRunners(generate=[4, 4])
        with gh_on_path(GhStub(Path(self.tmp.name))):
            code, loop, transcript = run_live(self.repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.result, "fail")
            self.assertEqual(loop.reasons, ["generate_failed"])
            self.assertEqual(self.repo.origin_subjects()[0], "loop: day 019 fail ledger")
            main_posts = self.repo.origin_git("ls-tree", "--name-only",
                                              "main:source/_posts").stdout.split()
            self.assertFalse(any(n.startswith("day-019-") for n in main_posts), msg=main_posts)
            ledger = [json.loads(l) for l in
                      self.repo.origin_show("main", "data/loop-log.jsonl").splitlines()]
            self.assertEqual(ledger[0]["result"], "fail")
            self.assert_ambient_untouched()

    def test_pr_create_failure_keeps_verdict_records_event_and_exits_two(self):
        runners = FakeRunners(verify=[3])
        with gh_on_path(GhStub(Path(self.tmp.name), fail_create=True)):
            code, loop, transcript = run_live(self.repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.result, "flag")
            self.assertIn("verifier_flag", loop.reasons)
            self.assertIn("pr_create_failed", transcript)
            ledger = [json.loads(l) for l in
                      self.repo.origin_show("main", "data/loop-log.jsonl").splitlines()]
            self.assertEqual(ledger[0]["result"], "flag")
            self.assertEqual(ledger[0]["reasons"], ["verifier_flag"])
            events = [entry for entry in ledger if entry.get("event") == "publish"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["error"], "pr_create_failed")
            branches = self.repo.origin_git("branch", "--list", "loop/day-019*").stdout.split()
            self.assertEqual(branches, ["loop/day-019"])
            self.assert_ambient_untouched()

    def test_live_run_refuses_fake_env(self):
        fake = str(FIXTURES / "day-019-chiisana-shouri-wo-iwau.md")
        runners = FakeRunners()
        with gh_on_path(GhStub(Path(self.tmp.name))):
            code, loop, transcript = run_live(self.repo, runners,
                                              env={"GEN_FAKE_PATH": fake})
            self.assertEqual(code, 2)
            self.assertEqual(loop.reasons, ["fake_mode_in_live"])
            self.assertEqual(runners.calls, [])
            self.assertEqual(self.repo.origin_subjects()[0], "loop: day 019 fail ledger")

    def test_live_run_refuses_fake_stage_metadata(self):
        runners = FakeRunners()
        original_generate = runners.generate

        def generate_with_fake_meta(draft):
            rc, stdout, stderr, meta = original_generate(draft)
            return rc, stdout, stderr, {**(meta or {}), "fake": True}

        runners.generate = generate_with_fake_meta
        with gh_on_path(GhStub(Path(self.tmp.name))):
            code, loop, transcript = run_live(self.repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.reasons, ["fake_mode_in_live"])
            main_posts = self.repo.origin_git("ls-tree", "--name-only",
                                              "main:source/_posts").stdout.split()
            self.assertFalse(any(n.startswith("day-019-") for n in main_posts), msg=main_posts)

    def test_revised_draft_pass_publishes_the_revised_post_and_its_record(self):
        runners = FakeRunners(annotate=[2, 0], findings=[DIVERGENT_FINDING])
        with gh_on_path(GhStub(Path(self.tmp.name))) as stub:
            code, loop, transcript = run_live(self.repo, runners)
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "pass")
            posts = [n for n in self.repo.origin_git(
                "ls-tree", "--name-only", "main:source/_posts").stdout.split()
                if n.startswith("day-019-")]
            self.assertEqual(len(posts), 1, posts)
            self.assertIn(REVISED_RT, self.repo.origin_show("main", "source/_posts/" + posts[0]))
            record = json.loads(self.repo.origin_show(
                "main", "data/loop-log.jsonl").splitlines()[0])
            self.assertEqual(record["result"], "pass")
            self.assertTrue(record["revised"])
            self.assertAlmostEqual(record["revision_cost_usd"], 0.006)
            self.assertAlmostEqual(record["cost_usd"], 0.012 + 0.006 + 0.004)
            self.assertEqual(stub.create_calls(), [])
            self.assert_ambient_untouched()

    def test_revision_violation_commits_the_ledger_only(self):
        runners = FakeRunners(annotate=[2, 0])
        runners.revise_text = lambda text: text.replace("百日挑戰", "二百日挑戰", 1)
        with gh_on_path(GhStub(Path(self.tmp.name))) as stub:
            code, loop, transcript = run_live(self.repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.reasons, ["revision_violation"])
            self.assertEqual(self.repo.origin_subjects()[0], "loop: day 019 fail ledger")
            main_posts = self.repo.origin_git("ls-tree", "--name-only",
                                              "main:source/_posts").stdout.split()
            self.assertFalse(any(n.startswith("day-019-") for n in main_posts), msg=main_posts)
            self.assertEqual(
                self.repo.origin_git("branch", "--list", "loop/day-019*").stdout.split(), [])
            self.assertEqual(stub.create_calls(), [])
            self.assert_ambient_untouched()

    def test_live_run_refuses_revise_fake_env(self):
        runners = FakeRunners()
        with gh_on_path(GhStub(Path(self.tmp.name))):
            code, loop, transcript = run_live(
                self.repo, runners,
                env={"REVISE_FAKE_PATH": str(FIXTURES / "verify-pass.txt")})
            self.assertEqual(code, 2)
            self.assertEqual(loop.reasons, ["fake_mode_in_live"])
            self.assertEqual(runners.calls, [])

    def test_live_run_refuses_fake_revision_metadata(self):
        runners = FakeRunners(annotate=[2, 0])
        original_revise = runners.revise

        def revise_with_fake_meta(draft, findings, out):
            rc, stdout, stderr, meta = original_revise(draft, findings, out)
            return rc, stdout, stderr, {**(meta or {}), "fake": True}

        runners.revise = revise_with_fake_meta
        with gh_on_path(GhStub(Path(self.tmp.name))):
            code, loop, transcript = run_live(self.repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.reasons, ["fake_mode_in_live"])
            main_posts = self.repo.origin_git("ls-tree", "--name-only",
                                              "main:source/_posts").stdout.split()
            self.assertFalse(any(n.startswith("day-019-") for n in main_posts), msg=main_posts)

    def test_commit_failure_aborts_publish_and_exits_two(self):
        hook = self.repo.root / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        with gh_on_path(GhStub(Path(self.tmp.name))):
            code, loop, transcript = run_live(self.repo, FakeRunners())
            self.assertEqual(code, 2)
            self.assertEqual(loop.result, "pass",
                             "content verdict must survive a publish failure")
            self.assertIn("publish failed: git_commit_failed", transcript)
            self.assertIn("could not record publish failure (git_commit_failed)",
                          transcript)
            self.assertEqual(self.repo.origin_subjects(), ["seed"])
            self.assertEqual(
                self.repo.origin_git("branch", "--format=%(refname:short)").stdout.split(),
                ["main"])
            self.assert_ambient_untouched()

    def test_rerun_after_publish_noops_against_remote(self):
        with gh_on_path(GhStub(Path(self.tmp.name))):
            code, loop, transcript = run_live(self.repo, FakeRunners())
            self.assertEqual(code, 0)
            subjects_after_first = self.repo.origin_subjects()
            runners = FakeRunners()
            code, loop, transcript = run_live(self.repo, runners)
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "noop")
            self.assertEqual(runners.calls, [])
            self.assertIn("progress: noop", transcript)
            self.assertIn("nothing to publish", transcript)
            self.assertEqual(self.repo.origin_subjects(), subjects_after_first)
            self.assert_ambient_untouched()

    def test_live_publish_without_public_base_flags_tts_failed(self):
        runners = FakeRunners()
        original_tts = runners.tts

        def tts_without_url(post):
            rc, stdout, stderr, meta = original_tts(post)
            return rc, stdout, stderr, {"url": None, "unannotated": ""}

        runners.tts = tts_without_url
        with gh_on_path(GhStub(Path(self.tmp.name))) as stub:
            code, loop, transcript = run_live(self.repo, runners)
            self.assertEqual(code, 0)
            self.assertEqual(loop.result, "flag")
            self.assertIn("tts_failed", loop.reasons)
            self.assertIsNone(loop.audio_url)
            branches = self.repo.origin_git("branch", "--list", "loop/day-019*").stdout.split()
            self.assertEqual(branches, ["loop/day-019"])
            main_posts = self.repo.origin_git("ls-tree", "--name-only",
                                              "main:source/_posts").stdout.split()
            self.assertFalse(any(n.startswith("day-019-") for n in main_posts), msg=main_posts)
            self.assertEqual(len(stub.create_calls()), 1)


class LengthClaimTest(unittest.TestCase):
    POST = ('---\n'
            'title: "Day 19｜x"\n'
            'description: "Day 19：小勝利。以約 260 字的 N3 短文與三個重點單字。"\n'
            'day: 19\n---\n\n'
            '導言。\n\n'
            '> 日文正文約 260 字，不含標音與中文翻譯。\n\n'
            '<!-- more -->\n\n'
            '## 日文文章\n\n本文。\n\n> <span lang="zh-Hant" data-translation>譯</span>\n')

    def test_claims_take_the_measured_count_floored_to_tens(self):
        out = daily_loop.sync_length_claim(self.POST, 291)
        self.assertIn("以約 290 字的 N3 短文", out)
        self.assertIn("> 日文正文約 290 字，", out)
        self.assertEqual(daily_loop.sync_length_claim(out, 291), out)

    def test_without_a_claim_nothing_is_rewritten(self):
        text = self.POST.replace("約 260 字", "260 字前後")
        self.assertEqual(daily_loop.sync_length_claim(text, 291), text)

    def test_measured_length_reads_only_the_gate_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "gate.json"
            self.assertIsNone(daily_loop.measured_length(report))
            report.write_text(json.dumps({"posts": [{"checks": [
                {"check": "ruby.structure", "state": "satisfied", "evidence": []},
                {"check": "length.maximum", "state": "satisfied",
                 "evidence": [{"actual": 315, "maximum": 300}]},
            ]}]}), encoding="utf-8")
            self.assertEqual(daily_loop.measured_length(report), 315)

    def test_the_claim_is_synced_before_the_verifier_sees_the_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners()
            runners.gate_length = 291
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 0)
            self.assertEqual(runners.verify_seen, [runners.verify_seen[0]])
            self.assertIn("以約 290 字", runners.verify_seen[0])
            self.assertIn("日文正文約 290 字", runners.verify_seen[0])
            self.assertEqual(loop.reasons, [])


if __name__ == "__main__":
    unittest.main()
