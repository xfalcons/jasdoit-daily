#!/usr/bin/env python3
"""Daily-loop orchestrator: stages 1-7 for one day (agent-loop-design.md
section 2, as amended 2026-11-21).

One command runs select, generate, annotate, gate, verify, tts, finalize,
publish, log for a single day. All engine policy lives here.

Every run executes inside a temp sandbox (amendment 1): posts, annotations,
the theme table, tools and the ledger are copied to mkdtemp and each stage
writes only there. A dry run never touches the real tree. Live publishing
builds every commit in a throwaway worktree created from the fetched
origin/main, never the ambient checkout. The run record rides the same
commit wave as the content it describes, and every live run's ledger state
lands on main; PR branches never carry the ledger or engine state, so FLAG
merges cannot conflict on them.

Arming (amendment 4): with LOOP_EVENT set (CI), schedule runs live only
when ENGINE_LIVE == "true" and dispatch runs live only when
LOOP_DISPATCH_LIVE == "true"; explicit --live/--dry-run override and are
mutually exclusive; everything else is dry.

Exit codes: 0 pass/flag/noop, 1 usage error, 2 fail.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loop_support

TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_GEN_MODEL = "z-ai/glm-5.3"
DEFAULT_VERIFY_MODEL = "openai/gpt-6.1-sol"
RESULT_ORDER = {"pass": 0, "flag": 1, "fail": 2}
STAGE_ORDER = ["noop", "select", "generate", "annotate", "gate", "verify", "tts",
               "finalize", "publish", "log"]
STATE_SCHEMA = 1
DISARMED_BANNER = "DISARMED: engine not armed for live; nothing was committed, pushed, or uploaded."
DEGRADE_BLOCK_BANNER = ("DEGRADE: engine degraded; schedule runs stay dry until 2 manual "
                        "dispatch PASS runs recover it")
DEGRADE_ENTER_BANNER = "DEGRADE: 3 consecutive failures — switch to manual mode"
DISPATCH_EVENTS = ("dispatch", "workflow_dispatch")


def normalize_event(env) -> str:
    """GitHub sends `workflow_dispatch` for manual runs; local runs may say
    either. Both map to the one internal constant `dispatch`."""
    event = env.get("LOOP_EVENT", "").strip()
    if event == "schedule":
        return "schedule"
    if event in DISPATCH_EVENTS:
        return "dispatch"
    return ""


class UsageError(Exception):
    pass


class FailRun(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --- engine state and ledger -------------------------------------------------


def load_state(repo_root: Path) -> dict:
    path = repo_root / "data" / "engine-state.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {
                "schema": STATE_SCHEMA,
                "degraded": bool(data.get("degraded", False)),
                "recover_passes": int(data.get("recover_passes", 0)),
            }
        except (OSError, ValueError):
            pass
    return {"schema": STATE_SCHEMA, "degraded": False, "recover_passes": 0}


def state_bytes(state: dict) -> bytes:
    payload = {
        "schema": STATE_SCHEMA,
        "degraded": state["degraded"],
        "recover_passes": state["recover_passes"],
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def ledger_entries(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def day_results(entries: list[dict]) -> dict[int, str]:
    """Last live verdict per day; dry runs, noops and event lines never count."""
    results: dict[int, str] = {}
    for entry in entries:
        if not entry.get("live") or entry.get("event"):
            continue
        day, result = entry.get("day"), entry.get("result")
        if isinstance(day, int) and result in RESULT_ORDER:
            results[day] = result
    return results


def enters_degraded(results: dict[int, str]) -> bool:
    """Three fail days on consecutive theme-table dates, counted only from
    the latest tail: the scan stops at the first non-fail entry, so an older
    historical streak can never re-enter the latch once recovered. A date
    gap breaks the streak."""
    if not results:
        return False
    streak = 0
    day = max(results)
    while day in results:
        if results[day] != "fail":
            break
        streak += 1
        if streak >= 3:
            return True
        day -= 1
    return False


def append_ledger_line(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def apply_state_transitions(state: dict, entries: list[dict], *, live: bool, noop: bool,
                            manual: bool, result: str) -> tuple[dict, bool, bool, bool]:
    """(state, changed, entered_degrade, recovered). Live runs only; dry runs
    and noops never move engine state. Same-day reruns collapse via
    day_results. The latch only tightens: a FAIL never clears `degraded`
    (not even a manual one); only 2 qualifying manual dispatch PASS runs
    recover it."""
    state = dict(state)
    changed = entered = recovered = False
    if not live or noop:
        return state, changed, entered, recovered
    if result == "fail":
        if not state["degraded"] and enters_degraded(day_results(entries)):
            state["degraded"] = True
            entered = True
        state["recover_passes"] = 0
        changed = True
    elif state["degraded"] and manual:
        state["recover_passes"] = state["recover_passes"] + 1 if result == "pass" else 0
        if state["recover_passes"] >= 2:
            state["degraded"] = False
            state["recover_passes"] = 0
            recovered = True
        changed = True
    return state, changed, entered, recovered


# --- arming, day resolution, progress ----------------------------------------


def resolve_arming(args, env) -> tuple[bool, str]:
    if args.live and args.dry_run:
        raise UsageError("--live and --dry-run are mutually exclusive")
    if args.live:
        return True, "flag"
    if args.dry_run:
        return False, "flag"
    event = normalize_event(env)
    if event == "schedule":
        return env.get("ENGINE_LIVE") == "true", "arming"
    if event == "dispatch":
        return env.get("LOOP_DISPATCH_LIVE") == "true", "arming"
    return False, "default"


def resolve_day(args, env, now: datetime, anchor: tuple[int, int, int]) -> tuple[int | None, bool]:
    """(day, schedule_noop). Explicit days must already be past in Taipei
    (amendment 12); the scheduled default may be today; the schedule noops
    past day 100 or before day 1."""
    raw = args.day
    if raw is None:
        raw_str = env.get("LOOP_DAY", "").strip()
        if raw_str:
            try:
                raw = int(raw_str)
            except ValueError:
                raise UsageError(f"LOOP_DAY is not an integer: {raw_str!r}")
    anchor_date = date(*anchor)
    if raw is None:
        day = (now.date() - anchor_date).days + 1
        return (day, True) if day > 100 or day < 1 else (day, False)
    if raw < 1:
        raise UsageError(f"day must be a positive integer, got {raw}")
    if raw > 100:
        raise UsageError(f"day {raw} is beyond the 100-day table")
    day_date = anchor_date + timedelta(days=raw - 1)
    if day_date >= now.date():
        raise UsageError(
            f"day {raw} ({day_date}) is not in the past in Asia/Taipei (today {now.date()})"
        )
    return raw, False


def progress_decision(published: bool, branch_exists: bool, pr_exists: bool) -> str:
    if published:
        return "noop"
    if branch_exists and pr_exists:
        return "pending_pr"
    if branch_exists:
        return "resume_pr"
    return "generate"


def local_published(repo_root: Path, day: int) -> bool:
    return any((repo_root / "source" / "_posts").glob(f"day-{day:03d}-*.md"))


def remote_published(repo_root: Path, day: int) -> bool:
    """The day's post already on origin/main; live progress is decided
    against the fetched remote, never a stale or half-pushed local tree."""
    proc = subprocess.run(["git", "-C", str(repo_root), "ls-tree", "-r", "--name-only",
                           "origin/main", "--", "source/_posts"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise FailRun("progress_check_failed")
    return any(Path(name).name.startswith(f"day-{day:03d}-")
               for name in proc.stdout.splitlines())


# --- sandbox -------------------------------------------------------------------


def build_sandbox(repo_root: Path) -> Path:
    root = Path(tempfile.mkdtemp(prefix="daily-loop-"))
    (root / "source" / "_posts").mkdir(parents=True)
    (root / "data" / "annotations").mkdir(parents=True)
    (root / "aidd-docs" / "references").mkdir(parents=True)
    (root / "tools").mkdir(parents=True)
    for post in (repo_root / "source" / "_posts").glob("day-*.md"):
        shutil.copy2(post, root / "source" / "_posts" / post.name)
    for ann in (repo_root / "data" / "annotations").glob("day-*.json"):
        shutil.copy2(ann, root / "data" / "annotations" / ann.name)
    theme = repo_root / "aidd-docs" / "references" / "japanese-theme-rotation.md"
    if theme.is_file():
        shutil.copy2(theme, root / "aidd-docs" / "references" / theme.name)
    for tool in (repo_root / "tools").glob("*.py"):
        shutil.copy2(tool, root / "tools" / tool.name)
    persona = repo_root / "AGENTS-teacher.md"
    if persona.is_file():
        shutil.copy2(persona, root / persona.name)
    ledger = repo_root / "data" / "loop-log.jsonl"
    if ledger.is_file():
        shutil.copy2(ledger, root / "data" / "loop-log.jsonl")
    state = repo_root / "data" / "engine-state.json"
    if state.is_file():
        shutil.copy2(state, root / "data" / "engine-state.json")
    return root


# --- draft/post composition ----------------------------------------------------


def romaji_slug(title_ja: str) -> str:
    try:
        from pykakasi import kakasi

        text = "".join(w["hepburn"] for w in kakasi().convert(title_ja))
    except Exception:
        return "post"
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "post"


def split_draft(raw: str) -> tuple[str | None, str]:
    """The model owns only `description`; every other front-matter field is
    rewritten by the orchestrator (amendment 11)."""
    if not raw.startswith("---"):
        return None, raw
    lines = raw.split("\n")
    try:
        fm_end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None, raw
    description = None
    for line in lines[1:fm_end]:
        m = re.match(r"^description:\s*(.*)$", line)
        if m:
            description = m.group(1).strip().strip("\"'")
    return description, "\n".join(lines[fm_end + 1:]).lstrip("\n")


QUOTED_DOUBLE_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
QUOTED_SINGLE_RE = re.compile(r"'(?:[^']|'')*'")
FM_ITEM_RE = re.compile(r"^(\s*-\s+)(.*)$")
FM_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*:\s*(.*)$")


def yaml_double_quoted(value: str) -> str:
    """One double-quoted YAML scalar line; the model owns `description`, so
    its quotes, backslashes and tabs must not break the front-matter."""
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    escaped = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", escaped)
    return f'"{escaped}"'


def _scalar_ok(value: str) -> bool:
    stripped = value.strip()
    if stripped.startswith('"'):
        return bool(QUOTED_DOUBLE_RE.fullmatch(stripped))
    if stripped.startswith("'"):
        return bool(QUOTED_SINGLE_RE.fullmatch(stripped))
    return True


def front_matter_valid(post_text: str) -> bool:
    """Every front-matter line is `key: scalar` or a list entry whose quoted
    scalars are closed; Hexo parses this block as YAML before building."""
    lines = post_text.split("\n")
    if not lines or lines[0].strip() != "---":
        return False
    try:
        fm_end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return False
    for line in lines[1:fm_end]:
        m = FM_ITEM_RE.match(line)
        if m:
            scalar = m.group(2)
        else:
            km = FM_KEY_RE.match(line)
            if not km:
                return False
            scalar = km.group(1)
        if not _scalar_ok(scalar):
            return False
    return True


def compose_post(row: loop_support.ThemeRow, day: int, day_date: date, body: str,
                 description: str, status: str, audio_url: str | None,
                 gen_model: str, verify_model: str, cost_usd: float | None) -> str:
    fm = [
        "---",
        f"title: {yaml_double_quoted(f'Day {day}｜{row.title_ja}')}",
        f"date: {day_date} 06:00:00",
        "lang: zh-TW",
        "layout: post",
        "learning: true",
        "kind: daily",
        f"day: {day}",
        f"phase: {loop_support.phase_of_day(day)}",
        f"reading_title: {row.title_ja}",
        "level: N3",
        f"core_sentence: {row.core_sentence}",
        f"audio_url: {audio_url or ''}",
        f"status: {status}",
        f"generated_by: {gen_model}@openrouter",
        f"verified_by: {verify_model}@openrouter",
    ]
    if cost_usd is not None:
        fm.append(f"cost_usd: {cost_usd}")
    fm += [
        "categories:",
        "  - 日語學習",
        "tags:",
        "  - 抄寫日語100天",
        f"  - Day {day}",
        f"description: {yaml_double_quoted(description)}",
        "---",
        "",
    ]
    return "\n".join(fm) + body.rstrip("\n") + "\n"


def set_front_matter_fields(post_text: str, fields: dict[str, str]) -> str:
    lines = post_text.split("\n")
    fm_end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    out = []
    replaced = set()
    for line in lines[:fm_end]:
        key = line.split(":", 1)[0] if ":" in line else None
        if key in fields:
            out.append(fields[key])
            replaced.add(key)
        else:
            out.append(line)
    for key, value in fields.items():
        if key not in replaced:
            out.append(value)
    return "\n".join(out + lines[fm_end:])


def body_of(post_text: str) -> str:
    lines = post_text.split("\n")
    fm_end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    return "\n".join(lines[fm_end + 1:])


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- stage runners ---------------------------------------------------------------


@dataclass
class Runners:
    generate: object
    annotate: object
    gate: object
    verify: object
    tts: object


def _spawn(cmd: list[str], cwd: Path) -> tuple[int, str, str, dict | None]:
    proc = subprocess.run([sys.executable] + cmd, cwd=cwd, capture_output=True,
                          text=True, timeout=1200)
    meta = None
    for line in reversed(proc.stdout.splitlines()):
        meta = loop_support.parse_meta_line(line)
        if meta is not None:
            break
    return proc.returncode, proc.stdout, proc.stderr, meta


def subprocess_runners(sandbox: Path, day: int) -> Runners:
    tools = sandbox / "tools"

    def generate(draft: Path, fewshot: Path | None, digest: Path | None):
        cmd = [str(tools / "generate-post.py"), "--day", str(day), "--out", str(draft)]
        if fewshot:
            cmd += ["--fewshot", str(fewshot)]
        if digest:
            cmd += ["--digest", str(digest)]
        return _spawn(cmd, sandbox)

    def annotate(post: Path):
        return _spawn([str(tools / "annotate-post.py"), str(post)], sandbox)

    def gate(post: Path, json_out: Path):
        return _spawn([str(tools / "gate-post.py"), str(post), "--json", str(json_out)], sandbox)

    def verify(post: Path, digest: Path | None):
        cmd = [str(tools / "verify-post.py"), str(post)]
        if digest:
            cmd += ["--digest", str(digest)]
        return _spawn(cmd, sandbox)

    def tts(post: Path, dry: bool):
        cmd = [str(tools / "tts-upload.py"), str(post),
               "--out-dir", str(sandbox / "tts-out")]
        if dry:
            cmd.append("--dry-run")
        return _spawn(cmd, sandbox)

    return Runners(generate=generate, annotate=annotate, gate=gate, verify=verify, tts=tts)


def gate_soft_reasons(report_path: Path) -> list[str]:
    if not report_path.is_file():
        return []
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    reasons = []
    for post in report.get("posts", []):
        for check in post.get("checks", []):
            if check.get("state") == "issue" and check.get("severity") == "soft":
                if check["check"] not in reasons:
                    reasons.append(check["check"])
    return reasons


def annotation_hash_of(report_dir: Path, stem: str) -> str | None:
    path = report_dir / f"{stem}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("post_sha256")
    except (OSError, json.JSONDecodeError):
        return None


# --- the run ---------------------------------------------------------------------


@dataclass
class Loop:
    repo_root: Path
    live: bool
    day: int
    now: datetime
    env: dict
    runners: Runners
    out: object = sys.stdout
    degraded_forced: bool = False

    reasons: list = field(default_factory=list)
    flagged: bool = False
    stage_reached: str = "noop"
    audio_url: str | None = None
    models: dict = field(default_factory=dict)
    cost_usd: float | None = None
    stage_ms: dict = field(default_factory=dict)
    result: str = "pass"
    _stage_start: float = 0.0

    def say(self, message: str) -> None:
        print(message, file=self.out, flush=True)

    def stage(self, name: str) -> None:
        # Monotonic: finalize re-runs annotate/gate, but the run's deepest
        # pipeline stage stays what the ledger reports.
        if STAGE_ORDER.index(name) > STAGE_ORDER.index(self.stage_reached):
            self.stage_reached = name
        self._stage_start = time.monotonic()
        self.say(f"== stage: {name} ==")

    def stage_done(self, name: str) -> None:
        self.stage_ms[name] = int((time.monotonic() - self._stage_start) * 1000)

    def flag(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.flagged = True

    def record(self, ts: str) -> dict:
        return {
            "ts": ts,
            "day": self.day,
            "live": self.live,
            "result": self.result,
            "reasons": list(self.reasons),
            "model_gen": self.models.get("gen"),
            "model_verify": self.models.get("verify"),
            "stage_reached": self.stage_reached,
            "audio_url": self.audio_url,
            "cost_usd": self.cost_usd,
            "latency_ms": sum(self.stage_ms.values()),
        }

    def execute(self, theme_text: str) -> None:
        anchor = loop_support.theme_anchor(theme_text)
        day_date = date(*anchor) + timedelta(days=self.day - 1)

        self.stage("select")
        if self.live and (self.env.get("GEN_FAKE_PATH") or self.env.get("VERIFY_FAKE_PATH")):
            raise FailRun("fake_mode_in_live")
        row = loop_support.parse_theme_row(theme_text, self.day)
        if row is None:
            raise FailRun("theme_row_missing")
        self.say(f"theme: Day {self.day} {row.title_ja}｜{row.focus}")

        self.models["gen"] = self.env.get("GEN_MODEL") or DEFAULT_GEN_MODEL
        self.models["verify"] = self.env.get("VERIFY_MODEL") or DEFAULT_VERIFY_MODEL
        verify_allowed = True
        if not loop_support.vendors_distinct(self.models["gen"], self.models["verify"]):
            self.flag("verifier_vendor")
            self.say(f"preflight: {self.models['gen']} / {self.models['verify']} are not "
                     "distinct vendors; auto-PASS forbidden this run")
            verify_allowed = False
        self.stage_done("select")

        self.stage("generate")
        digest = self.build_digest()
        raw_draft = self.sandbox / "draft-raw.md"
        rc, stdout, stderr, meta = self.retry_transport(
            lambda: self.runners.generate(raw_draft, self.pick_fewshot(), digest)
        )
        self.echo(stdout, stderr)
        if rc != 0:
            raise FailRun("generate_failed")
        if self.live and meta and meta.get("fake"):
            raise FailRun("fake_mode_in_live")
        if meta and meta.get("cost_usd") is not None:
            self.cost_usd = meta["cost_usd"]
        description, body = split_draft(raw_draft.read_text(encoding="utf-8"))
        if not description:
            description = f"Day {self.day}：{row.focus}。N3 短文與中日對照。"
        self.stem = f"day-{self.day:03d}-{romaji_slug(row.title_ja)}"
        self.post = self.sandbox / "source" / "_posts" / f"{self.stem}.md"
        self.post.write_text(
            compose_post(row, self.day, day_date, body, description, "pass", None,
                         self.models["gen"], self.models["verify"], self.cost_usd),
            encoding="utf-8",
        )
        self.say(f"post composed: {self.stem} ({len(body)} chars body)")
        self.stage_done("generate")

        self.run_annotate(self.post)
        self.run_gate(self.post, "gate-draft.json")

        self.stage("verify")
        if verify_allowed:
            rc, stdout, stderr, meta = self.retry_transport(
                lambda: self.runners.verify(self.post, digest)
            )
            self.echo(stdout, stderr)
            if self.live and meta and meta.get("fake"):
                raise FailRun("fake_mode_in_live")
            if rc == 4:
                self.flag("verifier_unavailable")
            elif rc == 2:
                raise FailRun("verifier_fail")
            elif rc == 3:
                self.flag("verifier_flag")
            elif rc != 0:
                raise FailRun("verifier_error")
            if meta and meta.get("cost_usd") is not None:
                self.cost_usd = (self.cost_usd or 0) + meta["cost_usd"]
        else:
            self.say("verify skipped (vendor preflight)")
        self.stage_done("verify")
        body_sha = sha256_text(body_of(self.post.read_text(encoding="utf-8")))

        self.stage("tts")
        rc, stdout, stderr, meta = self.retry_transport(
            lambda: self.runners.tts(self.post, not self.live)
        )
        self.echo(stdout, stderr)
        if rc == 0:
            self.audio_url = meta.get("url") if meta else None
            if self.live and not self.audio_url:
                # Auto-push needs a real URL: a live run without
                # R2_PUBLIC_BASE degrades to the PR path, never a silent
                # audio-less PASS (amendment 9 ruling).
                self.flag("tts_failed")
            if meta and meta.get("unannotated"):
                self.flag("unannotated_kanji")
        elif rc == 4:
            # Phase exhaustion only: text publishes first, audio is patched
            # later (spec section 2 stage 5).
            self.flag("tts_failed")
        else:
            raise FailRun("tts_error")
        self.stage_done("tts")

        self.finalize(body_sha)

    def finalize(self, body_sha: str) -> None:
        if not front_matter_valid(self.post.read_text(encoding="utf-8")):
            raise FailRun("front_matter_invalid")
        for refreeze in (False, True):
            self.stage("finalize")
            status = "flag" if self.flagged else "pass"
            self.post.write_text(
                set_front_matter_fields(self.post.read_text(encoding="utf-8"), {
                    "status": f"status: {status}",
                    "audio_url": f"audio_url: {self.audio_url or ''}",
                }),
                encoding="utf-8",
            )
            self.say(f"finalized front-matter: status={status} audio_url={self.audio_url or ''}")
            self.run_annotate(self.post)
            rc, stdout, stderr, meta = self.runners.gate(self.post, self.sandbox / "gate-final.json")
            self.echo(stdout, stderr)
            if rc == 2:
                raise FailRun("final_gate_regression")
            if rc == 3:
                for reason in gate_soft_reasons(self.sandbox / "gate-final.json"):
                    self.flag(reason)
                if not refreeze:
                    self.say("final gate FLAG: re-entering finalize once (amendment 9)")
                    continue
            elif rc != 0:
                raise FailRun("gate_error")
            recorded = annotation_hash_of(self.sandbox / "data" / "annotations", self.stem)
            current = sha256_text(self.post.read_text(encoding="utf-8"))
            if recorded is not None and recorded != current:
                raise FailRun("annotation_stale")
            if sha256_text(body_of(self.post.read_text(encoding="utf-8"))) != body_sha:
                raise FailRun("body_changed_after_verify")
            break
        self.stage_done("finalize")

    def run_annotate(self, post: Path) -> None:
        self.stage("annotate")
        rc, stdout, stderr, meta = self.runners.annotate(post)
        self.echo(stdout, stderr)
        if rc == 2:
            raise FailRun("annotation_divergent")
        if rc != 0 and rc != 3:
            raise FailRun("annotation_error")
        if rc == 3:
            self.flag("low_confidence")
        self.stage_done("annotate")

    def run_gate(self, post: Path, report_name: str) -> None:
        self.stage("gate")
        report = self.sandbox / report_name
        rc, stdout, stderr, meta = self.runners.gate(post, report)
        self.echo(stdout, stderr)
        if rc == 2:
            raise FailRun("gate_fail")
        if rc == 3:
            for reason in gate_soft_reasons(report):
                self.flag(reason)
        elif rc != 0:
            # Exactly 0 and 3 are completed verdicts; usage errors, crashes
            # and signal kills are execution errors (amendment 9 ruling).
            raise FailRun("gate_error")
        self.stage_done("gate")

    def retry_transport(self, call):
        rc, stdout, stderr, meta = call()
        if rc == 4:
            self.say("transport/model error; retrying once (spec section 2)")
            rc, stdout, stderr, meta = call()
        return rc, stdout, stderr, meta

    def echo(self, stdout: str, stderr: str) -> None:
        for line in (stdout or "").splitlines():
            self.say(f"  {line}")
        for line in (stderr or "").splitlines():
            self.say(f"  [stderr] {line}")

    def build_digest(self) -> Path:
        posts = []
        for post in sorted((self.sandbox / "source" / "_posts").glob("day-*.md")):
            m = re.match(r"day-(\d{3})-", post.name)
            if not m or int(m.group(1)) >= self.day:
                continue
            text = post.read_text(encoding="utf-8")

            def fm_value(key, text=text):
                match = re.search(rf"^{key}:\s*(.*)$", text, re.M)
                return match.group(1).strip().strip("\"'") if match else ""

            posts.append({"day": int(m.group(1)), "title": fm_value("reading_title"),
                          "core": fm_value("core_sentence")})
        path = self.sandbox / "digest.json"
        path.write_text(json.dumps({"posts": posts}, ensure_ascii=False), encoding="utf-8")
        return path

    def pick_fewshot(self) -> Path | None:
        previous = [
            p for p in sorted((self.sandbox / "source" / "_posts").glob("day-*.md"))
            if int(re.match(r"day-(\d{3})-", p.name).group(1)) < self.day
        ]
        return previous[-1] if previous else None


# --- publish ----------------------------------------------------------------------


def git_env(pat: str | None) -> dict:
    env = dict(os.environ)
    if pat:
        token = base64.b64encode(f"x-access-token:{pat}".encode()).decode()
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {token}",
        })
    return env


class Publisher:
    """Live-only. Never commits, checks out or pushes the ambient checkout:
    every commit is built in a throwaway worktree created from the fetched
    origin/main, so a stale or dirty local tree cannot leak into a publish.

    PASS: one content commit (post, annotation, ledger with the run record)
    pushed to main, then one ledger commit (publish event, engine state).
    FLAG: the ledger commit (record, engine state) lands on main before any
    content push, so the verdict survives a crash at every later point; the
    content commit (post and annotation only, never the ledger) is pushed to
    loop/day-NNN with a PR whose body carries the publish event. FAIL:
    exactly one ledger commit with engine state (Ruling B)."""

    def __init__(self, loop: Loop):
        self.loop = loop
        self.repo = loop.repo_root

    def git(self, *args, env=None, check=True, cwd=None):
        proc = subprocess.run(["git", "-C", str(cwd or self.repo), *args],
                              capture_output=True, text=True, env=env or dict(os.environ))
        if check and proc.returncode != 0:
            raise FailRun(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
        return proc

    def gh_pr(self, branch: str, title: str, body: str) -> str | None:
        env = dict(os.environ)
        pat = self.loop.env.get("LOOP_PAT")
        if pat:
            env["GH_TOKEN"] = pat
        proc = subprocess.run(
            ["gh", "pr", "create", "--head", branch, "--title", title, "--body", body],
            capture_output=True, text=True, env=env, cwd=str(self.repo),
        )
        if proc.returncode != 0:
            self.loop.say(f"gh pr create failed: {proc.stderr.strip()[:300]}")
            return None
        lines = proc.stdout.strip().splitlines()
        return lines[-1] if lines else None

    def fetch_main(self) -> None:
        self.git("fetch", "origin", "main")

    def open_worktree(self) -> Path:
        path = Path(tempfile.mkdtemp(prefix="loop-publish-"))
        self.git("worktree", "add", "--detach", str(path), "origin/main")
        return path

    def close_worktree(self, path: Path) -> None:
        self.git("worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)

    def wt_commit(self, wt: Path, rel_files: list[str], message: str) -> str:
        # --only keeps a stale index out: the commit carries exactly these
        # paths even if something else was left staged.
        self.git("add", "--", *rel_files, cwd=wt)
        proc = self.git("-c", "user.name=jasdoit-engine", "-c", "user.email=engine@jasdoit.local",
                        "commit", "--only", "-m", message, "--", *rel_files,
                        cwd=wt, check=False)
        if proc.returncode != 0:
            self.git("reset", cwd=wt, check=False)
            raise FailRun("git_commit_failed")
        return self.git("rev-parse", "HEAD", cwd=wt).stdout.strip()

    def wt_push(self, wt: Path, refspec: str) -> None:
        self.git("push", "origin", refspec, env=git_env(self.loop.env.get("LOOP_PAT")), cwd=wt)

    def materialize(self, rel: str, wt: Path) -> None:
        dest = wt / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.loop.sandbox / rel, dest)

    def ledger_files(self, wt: Path, state: dict, state_changed: bool) -> list[str]:
        self.materialize("data/loop-log.jsonl", wt)
        files = ["data/loop-log.jsonl"]
        if state_changed:
            (wt / "data" / "engine-state.json").write_bytes(state_bytes(state))
            files.append("data/engine-state.json")
        return files

    def run(self, state: dict, state_changed: bool) -> dict:
        loop = self.loop
        loop.stage("publish")
        day_tag = f"day {loop.day:03d}"
        event = {
            "ts": datetime.now(TAIPEI).isoformat(timespec="seconds"),
            "day": loop.day, "event": "publish", "result": loop.result,
            "pushed": False, "pr_url": None, "error": None,
        }
        self.fetch_main()
        wt = self.open_worktree()
        base = self.git("rev-parse", "HEAD", cwd=wt).stdout.strip()
        try:
            if loop.result == "fail":
                files = self.ledger_files(wt, state, state_changed)
                self.wt_commit(wt, files, f"loop: {day_tag} fail ledger")
                self.wt_push(wt, "HEAD:main")
                loop.stage_done("publish")
                return event
            if local_published(wt, loop.day):
                event["error"] = "remote_already_published"
                loop.stage_done("publish")
                return event
            post_rel = f"source/_posts/{loop.stem}.md"
            ann_rel = f"data/annotations/{loop.stem}.json"
            veto = loop.env.get("AUTO_PUSH_N3") == "false"
            if loop.result == "pass" and not veto:
                self.materialize(post_rel, wt)
                self.materialize(ann_rel, wt)
                # The run record rides the content commit itself, so a crash
                # after this push can never lose the verdict (amendment 5).
                self.materialize("data/loop-log.jsonl", wt)
                self.wt_commit(wt, [post_rel, ann_rel, "data/loop-log.jsonl"],
                               f"content: {day_tag} post (engine pass)")
                self.wt_push(wt, "HEAD:main")
                event["pushed"] = True
                append_ledger_line(loop.sandbox / "data" / "loop-log.jsonl", event)
                try:
                    files = self.ledger_files(wt, state, state_changed)
                    self.wt_commit(wt, files, f"loop: {day_tag} pass ledger")
                    self.wt_push(wt, "HEAD:main")
                except FailRun as exc:
                    event["error"] = exc.reason
                loop.stage_done("publish")
                return event
            branch = f"loop/day-{loop.day:03d}"
            files = self.ledger_files(wt, state, state_changed)
            self.wt_commit(wt, files, f"loop: {day_tag} {loop.result} ledger")
            self.wt_push(wt, "HEAD:main")
            # The branch bases on the exact fetched main, not on the ledger
            # commit just pushed, so it never carries ledger changes.
            self.git("checkout", "--detach", base, cwd=wt)
            self.materialize(post_rel, wt)
            self.materialize(ann_rel, wt)
            self.wt_commit(wt, [post_rel, ann_rel],
                           f"content: {day_tag} post (engine {loop.result})")
            self.wt_push(wt, f"HEAD:refs/heads/{branch}")
            pr_url = self.gh_pr(branch, f"loop: {day_tag} {loop.result.upper()}",
                                f"engine {loop.result}: {day_tag}\n"
                                f"reasons: {', '.join(loop.reasons) or '-'}\n"
                                f"record: {json.dumps(loop.record_dict, ensure_ascii=False)}\n"
                                "publish: event line rides this PR body, not the ledger")
            event["pr_url"] = pr_url
            if pr_url is None:
                # Content is on the pushed branch; rerunning the day resumes
                # with PR creation only (amendment 7). The verdict already
                # landed on main above; the caller records this failure.
                event["error"] = "pr_create_failed"
            loop.stage_done("publish")
            return event
        finally:
            self.close_worktree(wt)

    def record_publish_failure(self, event: dict) -> None:
        """Best-effort landing of a publish-failure event line on main; the
        verdict record is either already committed or nothing published."""
        try:
            self.fetch_main()
            wt = self.open_worktree()
            try:
                append_ledger_line(self.loop.sandbox / "data" / "loop-log.jsonl", event)
                self.materialize("data/loop-log.jsonl", wt)
                self.wt_commit(wt, ["data/loop-log.jsonl"],
                               f"loop: day {self.loop.day:03d} publish event")
                self.wt_push(wt, "HEAD:main")
                self.loop.say("publish-failure event committed to the ledger")
            finally:
                self.close_worktree(wt)
        except FailRun as exc:
            self.loop.say(f"could not record publish failure ({exc.reason}); "
                          "the transcript above is the record")


# --- main ---------------------------------------------------------------------------


class Parser(argparse.ArgumentParser):
    def error(self, message):
        print(f"daily-loop: {message}", file=sys.stderr)
        raise SystemExit(1)


def run_engine(args, env, out=sys.stdout, repo_root: Path | None = None,
               runners_factory=None) -> tuple[int, object]:
    repo_root = repo_root or Path(__file__).resolve().parent.parent
    loop_support.load_dotenv(repo_root)
    env = {**dict(os.environ), **env}
    for key in ("GEN_FAKE_PATH", "VERIFY_FAKE_PATH"):
        raw_path = env.get(key)
        if raw_path and not Path(raw_path).is_absolute():
            # Stage tools run with cwd=sandbox; a relative fake path would
            # point inside the sandbox, where fixtures were never copied.
            env[key] = str((repo_root / raw_path).resolve())
            os.environ[key] = env[key]
    try:
        live, arming = resolve_arming(args, env)
    except UsageError as exc:
        print(f"daily-loop: {exc}", file=sys.stderr)
        return 1, None

    now_raw = env.get("LOOP_NOW", "")
    try:
        now = datetime.fromisoformat(now_raw) if now_raw else datetime.now(TAIPEI)
    except ValueError:
        print(f"daily-loop: LOOP_NOW is not an ISO datetime: {now_raw!r}", file=sys.stderr)
        return 1, None
    if now.tzinfo is None:
        now = now.replace(tzinfo=TAIPEI)
    now = now.astimezone(TAIPEI)

    theme_path = repo_root / "aidd-docs" / "references" / "japanese-theme-rotation.md"
    try:
        theme_text = theme_path.read_text(encoding="utf-8")
        anchor = loop_support.theme_anchor(theme_text)
    except (OSError, ValueError) as exc:
        print(f"daily-loop: {exc}", file=sys.stderr)
        return 1, None

    try:
        day, schedule_noop = resolve_day(args, env, now, anchor)
    except UsageError as exc:
        print(f"daily-loop: {exc}", file=sys.stderr)
        return 1, None
    day = day or 0

    state = load_state(repo_root)
    degraded_forced = False
    if live and state["degraded"] and normalize_event(env) == "schedule":
        live = False
        degraded_forced = True

    if live:
        dirty = subprocess.run(["git", "-C", str(repo_root), "status", "--porcelain"],
                               capture_output=True, text=True).stdout.strip()
        if dirty:
            print("daily-loop: live run requires a clean tree; commit or stash first",
                  file=sys.stderr)
            return 2, ""

    sandbox = build_sandbox(repo_root)
    runners = (runners_factory or subprocess_runners)(sandbox, day)
    loop = Loop(repo_root=repo_root, live=live, day=day, now=now, env=env,
                runners=runners, out=out, degraded_forced=degraded_forced)
    loop.sandbox = sandbox
    ts = now.isoformat(timespec="seconds")
    loop.say(f"daily-loop: day={day} live={live} arming={arming} now={ts}")

    noop = False
    try:
        if schedule_noop:
            noop = True
            loop.result = "noop"
            loop.say("schedule is past day 100 (or before day 1): noop")
        else:
            branch_exists = pr_exists = False
            if live:
                fetch = subprocess.run(["git", "-C", str(repo_root), "fetch", "origin", "main"],
                                       capture_output=True, text=True)
                if fetch.returncode != 0:
                    raise FailRun("progress_check_failed")
                published = remote_published(repo_root, day)
                branch = f"loop/day-{day:03d}"
                ls = subprocess.run(["git", "-C", str(repo_root), "ls-remote", "--heads",
                                     "origin", branch], capture_output=True, text=True)
                gh_env = dict(env)
                if env.get("LOOP_PAT"):
                    gh_env["GH_TOKEN"] = env["LOOP_PAT"]
                prl = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "open",
                                      "--json", "url"], capture_output=True, text=True,
                                     cwd=str(repo_root), env=gh_env)
                if ls.returncode != 0 or prl.returncode != 0:
                    raise FailRun("progress_check_failed")
                branch_exists = bool(ls.stdout.strip())
                try:
                    pr_exists = bool(json.loads(prl.stdout or "[]"))
                except json.JSONDecodeError:
                    raise FailRun("progress_check_failed")
            else:
                published = local_published(repo_root, day)
            progress = progress_decision(published, branch_exists, pr_exists)
            loop.say(f"progress: {progress}")
            if progress == "noop":
                loop.result = "noop"
                noop = True
            elif progress == "pending_pr":
                raise FailRun("pending_pr")
            elif progress == "resume_pr":
                publisher = Publisher(loop)
                url = publisher.gh_pr(f"loop/day-{day:03d}", f"loop: day {day:03d} FLAG",
                                      f"engine resume: PR for day {day:03d}")
                loop.say(f"resume: PR {url or 'creation failed'}")
                if url is None:
                    raise FailRun("pr_create_failed")
                loop.result = "noop"
                loop.flag("resume_pr")
                noop = True
        if not noop:
            loop.execute(theme_text)
            loop.result = "flag" if loop.flagged else "pass"
    except FailRun as exc:
        loop.flag(exc.reason)
        loop.result = "fail"

    record = loop.record(ts)
    ledger_sandbox = sandbox / "data" / "loop-log.jsonl"
    append_ledger_line(ledger_sandbox, record)
    loop.record_dict = record
    state, state_changed, entered, recovered = apply_state_transitions(
        state, ledger_entries(ledger_sandbox), live=live, noop=noop,
        manual=normalize_event(env) == "dispatch", result=loop.result,
    )
    loop.state_after = state
    if entered:
        loop.say(DEGRADE_ENTER_BANNER)
    if recovered:
        loop.say("DEGRADE: recovered; schedule live re-armed")

    publish_failed = False
    if live and loop.result != "noop":
        try:
            publish_event = Publisher(loop).run(state, state_changed)
            publish_failed = bool(publish_event.get("error"))
        except FailRun as exc:
            loop.say(f"publish failed: {exc.reason}")
            publish_failed = True
            publish_event = {"ts": ts, "day": day, "event": "publish",
                             "result": loop.result, "pushed": False, "pr_url": None,
                             "error": exc.reason}
        if publish_failed:
            # The content verdict stays as recorded; the publish failure is a
            # separate ledger event and a nonzero exit so Actions notifies.
            loop.say(f"publish event: {json.dumps(publish_event, ensure_ascii=False)}")
            Publisher(loop).record_publish_failure(publish_event)
    elif live:
        # Noop reruns and the post-day-100 schedule no-op leave no commit and
        # no dirty tree; the sandbox ledger line and transcript are the record.
        loop.say("publish: nothing to publish (noop)")
    else:
        if loop.result == "fail":
            would = "commit data/loop-log.jsonl (+engine-state) only (Ruling B)"
        elif loop.result == "flag":
            would = (f"commit the ledger on main, then branch loop/day-{day:03d} with "
                     "post+annotation and open a PR")
        else:
            would = "commit post + annotation + ledger to main, then commit the publish event"
        loop.say(f"DRY publish: result={loop.result}; would {would}")

    loop.say(f"RESULT day={day} result={loop.result} reasons={loop.reasons} "
             f"stage_reached={loop.stage_reached} audio_url={loop.audio_url}")
    if not live and loop.result != "fail":
        if degraded_forced:
            loop.say(DEGRADE_BLOCK_BANNER)
        loop.say(DISARMED_BANNER)

    shutil.rmtree(sandbox, ignore_errors=True)
    return (2 if loop.result == "fail" or publish_failed else 0), loop


def main(argv=None) -> int:
    parser = Parser(
        prog="daily-loop.py",
        description="Daily content engine: one day through stages 1-7 "
                    "(see aidd-docs/engine-runbook.md)",
    )
    parser.add_argument("--day", type=int, default=None,
                        help="backfill day; must already be past in Asia/Taipei")
    parser.add_argument("--live", action="store_true", help="publish (commit/push/upload)")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="run every stage, write nothing outside a temp sandbox")
    args = parser.parse_args(argv)
    code, _ = run_engine(args, {})
    return code


if __name__ == "__main__":
    sys.exit(main())
