"""State-machine tests for the daily-loop orchestrator on an isolated
fixture repo with injected fake runners (no subprocess, no network). The
real subprocess integration is measured by the sandbox end-to-end dry run
in the engine runbook validation."""
import argparse
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
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
        self.posts_seen.append(post.read_text(encoding="utf-8"))
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


if __name__ == "__main__":
    unittest.main()
