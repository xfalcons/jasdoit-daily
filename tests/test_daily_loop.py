"""State-machine tests for the daily-loop orchestrator on an isolated
fixture repo with injected fake runners (no subprocess, no network). The
real subprocess integration is measured by the sandbox end-to-end dry run
in the engine runbook validation."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
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
    Records (stage, post-front-matter snapshot) per call."""

    def __init__(self, generate=None, annotate=None, gate=None, verify=None, tts=None):
        self.calls = []
        self.posts_seen = []
        self.generate_codes = list(generate or [0])
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
            annotate=lambda post: self.annotate(post),
            gate=lambda post, json_out: self.gate(post, json_out),
            verify=lambda post, digest: self.verify(post),
            tts=lambda post, dry: self.tts(post),
        )
        return runners

    def generate(self, draft):
        rc = self._next(self.generate_codes, "generate")
        if rc == 0:
            draft.write_text(RAW_DRAFT, encoding="utf-8")
        return rc, "META {}" if rc == 0 else "", "", {"cost_usd": 0.012} if rc == 0 else None

    def annotate(self, post):
        text = post.read_text(encoding="utf-8")
        self.posts_seen.append(text)
        report = post.parents[2] / "data" / "annotations" / f"{post.stem}.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(
            {"post": post.name, "post_sha256": daily_loop.sha256_text(text)}),
            encoding="utf-8")
        rc = self._next(self.annotate_codes, "annotate")
        return rc, "", "", None

    def gate(self, post, json_out):
        self.posts_seen.append(post.read_text(encoding="utf-8"))
        rc = self._next(self.gate_codes, "gate")
        if rc == 3:
            json_out.write_text(json.dumps({"posts": [{"checks": [
                {"check": "sections.unrecognized", "severity": "soft", "state": "issue"}
            ]}]}), encoding="utf-8")
        return rc, "", "", None

    def verify(self, post):
        rc = self._next(self.verify_codes, "verify")
        return rc, "", "", {"cost_usd": 0.004} if rc in (0, 3) else None

    def tts(self, post):
        rc = self._next(self.tts_codes, "tts")
        meta = dict(TTS_META) if rc == 0 else None
        return rc, "", "", meta


def run(repo, runners, *, day=None, env=None, live=False, dry=True):
    args = SimpleNamespace(day=day, live=live, dry_run=dry)
    out = io.StringIO()
    merged = {"LOOP_NOW": NOW, **(env or {})}
    if not live:
        merged.setdefault("GEN_MODEL", "z-ai/glm-5.3")
        merged.setdefault("VERIFY_MODEL", "openai/gpt-4o")
    code, loop = daily_loop.run_engine(
        args, merged, out=out, repo_root=repo.root,
        runners_factory=lambda sandbox, d: runners.make(sandbox, d),
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
            self.assertIn("date: 2026-11-22 06:00:00", final)
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

    def test_divergent_annotation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FixtureRepo(Path(tmp))
            runners = FakeRunners(annotate=[2])
            code, loop, _ = run(repo, runners)
            self.assertEqual(code, 2)
            self.assertEqual(loop.reasons, ["annotation_divergent"])

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
                        "model_verify", "stage_reached", "audio_url", "cost_usd", "latency_ms"):
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


if __name__ == "__main__":
    unittest.main()
