#!/usr/bin/env python3
"""Stage 4 of the daily loop: deterministic rule QA over daily posts.

Usage:
    uv run --with fugashi --with unidic-lite --with jamdict --with jamdict_data \
           --with pykakasi python tools/gate-post.py POST [POST ...] [--json PATH]

Reads each post and its stage-3 annotation report (data/annotations/<stem>.json,
resolved from the tree the post lives in), runs the §3 rule QA, and prints one
summary per post plus totals. --json additionally writes one deterministic
batch artifact (UTF-8, sorted keys, no timestamps or absolute paths). The gate
never edits posts or annotations.

Gold regression: every invocation replays tools/annotate-post.py over the gold
corpus (day-001 through the highest contiguous day present in source/_posts
with a committed report) inside a temporary tree and compares the regenerated
reports with the baselines byte-identically.

Verdict vocabulary per aidd-docs/plans/agent-loop-design.md §3: PASS / FLAG /
FAIL. Deterministic scope only: the cross-model verifier and the N3 vocabulary
comparison are external inputs the orchestrator combines, listed in
required_external.

Exit codes: 0 PASS, 1 usage error, 2 FAIL, 3 FLAG, 4 execution error.
A completed run (0/2/3) emits the full verdict and artifact; 1/4 do not.

The gate itself is stdlib-only. Gold replay re-executes the stage-3 annotator
with this interpreter, so the invocation needs the stage-3 packages.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import IntEnum
from html.parser import HTMLParser
from pathlib import Path
from typing import Sequence

RUBY_RE = re.compile(r"<ruby>(?P<surf>.*?)<rp>.*?</rp><rt>(?P<rt>.*?)</rt><rp>.*?</rp></ruby>")
RP_RE = re.compile(r"<rp>.*?</rp>")
TAG_RE = re.compile(r"<[^>]+>")
RT_SCRIPT = re.compile(r"[ぁ-ゖァ-ヶー]+")
KANJI_RE = re.compile("[㐀-鿿々\U00020000-\U000323af豈-﫿]")
COUNT_RE = re.compile("[㐀-鿿々\U00020000-\U000323af豈-﫿ぁ-ゖァ-ヺー]")
DAY_STEM_RE = re.compile(r"day-(\d{3})-([a-z0-9-]+)")
MORE_MARKER = "<!-- more -->"
THEME_RE = re.compile(r"Day\s*1\s*=\s*(\d{4})-(\d{2})-(\d{2})")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
LIST_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
PROMPT_RE = re.compile(r"^\d+\.\s")

REQUIRED_EXTERNAL = ["cross_model", "n3_vocabulary"]
CONSUMED_FIELDS = (
    "layout", "learning", "kind", "day", "phase", "date", "lang",
    "reading_title", "level", "core_sentence",
)

HARD = "hard"
SOFT = "soft"

# The sole check inventory and severity ledger; §3 is the authority.
CHECKS = {
    "post.contract": HARD,
    "intro.structure": HARD,
    "sections.required": HARD,
    "sections.unrecognized": SOFT,
    "body.structure": HARD,
    "length.maximum": HARD,
    "length.target_minimum": SOFT,
    "ruby.structure": HARD,
    "ruby.coverage": HARD,
    "translations.paragraphs": HARD,
    "vocabulary.cards": HARD,
    "grammar.point": HARD,
    "writing.sunday": HARD,
    "footer.attribution": HARD,
    "annotation.present": HARD,
    "annotation.contract": HARD,
    "annotation.freshness": HARD,
    "annotation.divergent": HARD,
    "annotation.low_confidence": SOFT,
    "gold.regression": HARD,
}


class ExitCode(IntEnum):
    PASS = 0
    USAGE = 1
    FAIL = 2
    FLAG = 3
    EXECUTION = 4


EXIT_OF_VERDICT = {"PASS": ExitCode.PASS, "FLAG": ExitCode.FLAG, "FAIL": ExitCode.FAIL}


class UsageError(Exception):
    pass


class ExecutionError(Exception):
    pass


def ev(kind: str, detail: str, **fields) -> dict:
    item = {"kind": kind, "detail": detail}
    item.update(fields)
    return item


def satisfied(check: str, *evidence) -> dict:
    return {"check": check, "severity": CHECKS[check], "state": "satisfied", "evidence": list(evidence)}


def issue(check: str, *evidence) -> dict:
    return {"check": check, "severity": CHECKS[check], "state": "issue", "evidence": list(evidence)}


def blocked(check: str, because: str) -> dict:
    return {"check": check, "severity": CHECKS[check], "state": "blocked", "because": because, "evidence": []}


def na(check: str, reason: str) -> dict:
    return {"check": check, "severity": CHECKS[check], "state": "not_applicable", "reason": reason, "evidence": []}


def reduce_verdict(results: Sequence[dict]) -> str:
    failed = [r for r in results if r["state"] == "issue"]
    if any(r["severity"] == HARD for r in failed):
        return "FAIL"
    if failed:
        return "FLAG"
    return "PASS"


def length_band(day: int) -> tuple:
    if day <= 30:
        return 250, 300
    if day <= 70:
        return 300, 400
    return None, 450


# --- post parsing -----------------------------------------------------------


@dataclass
class Post:
    stem: str
    day: int
    raw: bytes
    text: str
    lines: list  # body lines after front matter
    body_start: int  # 0-based index of lines[0] in the whole file, for absolute numbering
    scalars: dict


def parse_front_matter(text: str) -> tuple:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("no opening --- for front-matter")
    try:
        fm_end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError("no closing --- for front-matter")
    key_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$")
    scalars: dict = {}
    for line in lines[1:fm_end]:
        m = key_re.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if key not in CONSUMED_FIELDS:
            continue
        if key in scalars:
            raise ValueError(f"duplicate front-matter key: {key}")
        scalars[key] = value.strip("\"'")
    return scalars, fm_end + 1


def load_post(path: Path) -> Post:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError(f"{path}: not UTF-8: {exc}")
    try:
        scalars, body_start = parse_front_matter(text)
    except ValueError as exc:
        raise UsageError(f"{path}: {exc}")
    if scalars.get("kind") != "daily":
        raise UsageError(
            f"{path}: unsupported profile kind={scalars.get('kind')!r}; this gate handles kind: daily only"
        )
    m = DAY_STEM_RE.fullmatch(path.stem)
    if not m:
        raise UsageError(f"{path}: stem does not match day-NNN-<slug>")
    return Post(
        stem=path.stem,
        day=int(m.group(1)),
        raw=raw,
        text=text,
        lines=text.splitlines()[body_start:],
        body_start=body_start,
        scalars=scalars,
    )


def contract_issues(post: Post, anchor: date) -> tuple:
    issues = []
    day = None

    def mismatch(field, expected, observed):
        issues.append(ev("observation", f"{field}: expected {expected!r}, observed {observed!r}",
                         field=field, expected=expected, observed=observed))

    scalars = post.scalars
    if scalars.get("layout") != "post":
        mismatch("layout", "post", scalars.get("layout"))
    if scalars.get("learning") != "true":
        mismatch("learning", "true", scalars.get("learning"))
    if scalars.get("lang") != "zh-TW":
        mismatch("lang", "zh-TW", scalars.get("lang"))
    if not scalars.get("reading_title"):
        mismatch("reading_title", "<nonempty>", scalars.get("reading_title"))
    if not scalars.get("core_sentence"):
        mismatch("core_sentence", "<nonempty>", scalars.get("core_sentence"))

    raw_day = scalars.get("day")
    try:
        fm_day = int(raw_day)
    except (TypeError, ValueError):
        fm_day = None
    if fm_day is None or not 1 <= fm_day <= 100:
        issues.append(ev("observation", f"day: expected 1-100, observed {raw_day!r}",
                         field="day", expected="1-100", observed=raw_day))
    elif fm_day != post.day:
        issues.append(ev("observation", f"day: expected {post.day} (filename), observed {fm_day}",
                         field="day", expected=post.day, observed=fm_day))
    else:
        day = fm_day

    raw_phase = scalars.get("phase")
    try:
        phase = int(raw_phase)
    except (TypeError, ValueError):
        phase = None
    if phase is None or phase < 1:
        issues.append(ev("observation", f"phase: expected positive integer, observed {raw_phase!r}",
                         field="phase", expected="positive integer", observed=raw_phase))

    level = scalars.get("level")
    if day is not None and day <= 30:
        if level != "N3":
            mismatch("level", "N3", level)
    elif not level:
        mismatch("level", "<nonempty>", level)

    expected_date = anchor + timedelta(days=day - 1) if day is not None else None
    raw_dt = scalars.get("date")
    observed_date = None
    if raw_dt:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                observed_date = datetime.strptime(raw_dt, fmt).date()
                break
            except ValueError:
                continue
    if expected_date is None or observed_date != expected_date:
        issues.append(ev("observation",
                         f"date: expected {expected_date} (theme Day 1 anchor + day - 1), observed {raw_dt!r}",
                         field="date", expected=str(expected_date), observed=raw_dt))
    return issues, day


# --- sections, blocks, spans -------------------------------------------------


@dataclass
class Section:
    kind: str
    heading_no: int
    heading: str
    lines: list  # (absolute line number, text)


def classify_heading(label: str):
    if re.fullmatch(r"日文文章([:：].*)?", label):
        return "japanese"
    if label == "3 個重點單字":
        return "vocabulary"
    if label == "1 個重點文法":
        return "grammar"
    if re.fullmatch(r"(週日寫作提示|寫作提示)([:：].*)?", label):
        return "writing"
    return "unrecognized"


def detect_sections(lines: Sequence[str], offset: int = 0):
    """Index H2 sections, more markers and intro. offset is the absolute number
    of the line before lines[0]; fence-internal and quoted headings are content."""
    sections: list = []
    intro: list = []
    more_markers: list = []
    fence = False
    current = None
    for idx, line in enumerate(lines):
        no = offset + idx + 1
        if FENCE_RE.match(line):
            fence = not fence
        if not fence and line.startswith("## "):
            label = re.sub(r"\s+#+\s*$", "", line[3:].strip())
            current = Section(classify_heading(label), no, label, [])
            sections.append(current)
            continue
        if not fence and line.strip() == MORE_MARKER:
            more_markers.append(no)
        if current is None:
            intro.append((no, line))
        else:
            current.lines.append((no, line))
    return sections, intro, more_markers


def split_blocks(section_lines: Sequence):
    """Split section content into blank-line-separated blocks.
    Each block: (kind, first_line_no, lines). kind: heading|quote|prose|other."""
    blocks = []
    acc: list = []
    first_no = None

    def flush():
        nonlocal acc, first_no
        if not acc:
            return
        head = acc[0][1]
        if head.startswith("#"):
            kind = "heading"
        elif head.lstrip().startswith(">"):
            kind = "quote"
        elif any(FENCE_RE.match(l) or LIST_LINE_RE.match(l) or l.lstrip().startswith("|") for _, l in acc):
            kind = "other"
        else:
            kind = "prose"
        blocks.append((kind, first_no, acc))
        acc = []
        first_no = None

    for no, line in section_lines:
        if not line.strip():
            flush()
            continue
        if first_no is None:
            first_no = no
        acc.append((no, line))
    flush()
    return blocks


class _SpanScanner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.spans = []
        self._open = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag == "span":
            d = dict(attrs)
            if d.get("lang") == "zh-Hant" and "data-translation" in d:
                self._open = True
                self._text = []

    def handle_data(self, data):
        if self._open:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "span" and self._open:
            self.spans.append("".join(self._text).strip())
            self._open = None


def translation_spans(quote_block_lines: Sequence):
    """Return the nonempty zh-Hant data-translation span texts of a blockquote block."""
    content = "\n".join(re.sub(r"^\s*>\s?", "", l) for _, l in quote_block_lines)
    scanner = _SpanScanner()
    scanner.feed(content)
    scanner.close()
    return [s for s in scanner.spans if s]


# --- counting and ruby -------------------------------------------------------


def canonical_count(section_lines: Sequence) -> int:
    """Reading-locked Japanese character count over Japanese-section lines.
    Headings, blockquote translations and punctuation contribute zero."""
    selected = [l for _, l in section_lines if l.strip() and not l.lstrip().startswith((">", "#"))]
    text = "\n".join(selected)
    # Order is load-bearing: substitute the <rt> reading for the whole ruby
    # element FIRST, then drop leftover <rp> runs, then strip other tags.
    # Removing <rp> first dismembers the RUBY_RE pattern and leaves bare bases.
    text = RUBY_RE.sub(lambda m: m.group("rt"), text)
    text = RP_RE.sub("", text)
    text = TAG_RE.sub("", text)
    return len(COUNT_RE.findall(text))


def ruby_structure_issues(post_lines: Sequence[str], offset: int = 0):
    issues = []
    for idx, line in enumerate(post_lines):
        if "<ruby" not in line and "</ruby>" not in line:
            continue
        no = offset + idx + 1
        matches = list(RUBY_RE.finditer(line))
        if line.count("<ruby") != len(matches) or line.count("</ruby>") != len(matches):
            issues.append(ev("observation", f"L{no}: unbalanced ruby markup: {line.strip()[:60]}",
                             line=no, observed=line.strip()[:60]))
            continue
        for m in matches:
            surface = re.sub(r"[ \t　]", "", m.group("surf"))
            rt = m.group("rt")
            if not surface or not rt or not RT_SCRIPT.fullmatch(rt):
                issues.append(ev("observation", f"L{no}: invalid ruby element: {m.group(0)[:60]}",
                                 line=no, observed=m.group(0)[:60]))
    return issues


def bare_kanji(section_lines: Sequence):
    out = []
    for no, line in section_lines:
        if not line.strip() or line.lstrip().startswith((">", "#")):
            continue
        text = RUBY_RE.sub("", line)
        text = RP_RE.sub("", text)
        text = TAG_RE.sub("", text)
        m = KANJI_RE.search(text)
        if m:
            out.append(ev("observation", f"L{no}: bare kanji {m.group()} outside ruby coverage",
                          line=no, character=m.group()))
    return out


# --- annotation contract -----------------------------------------------------


def decode_annotation(raw: bytes, post: Post):
    """Validate stage-3 schema 1 against this post. Returns (data, issues)."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [ev("observation", f"not valid UTF-8 JSON: {exc}")]
    issues = []

    def bad(detail, **fields):
        issues.append(ev("observation", detail, **fields))

    if not isinstance(data, dict):
        return None, [ev("observation", "report is not a JSON object")]
    if data.get("tool") != "annotate-post":
        bad(f"tool: expected 'annotate-post', observed {data.get('tool')!r}")
    schema = data.get("schema")
    if type(schema) is not int or schema != 1:
        bad(f"schema: expected 1, observed {schema!r}")
    if data.get("post") != post.stem:
        bad(f"post: expected {post.stem!r}, observed {data.get('post')!r}")
    ann_day = data.get("day")
    if type(ann_day) is not int or ann_day != post.day:
        bad(f"day: expected {post.day}, observed {ann_day!r}")
    sha = data.get("post_sha256")
    if not isinstance(sha, str) or not HASH_RE.fullmatch(sha):
        bad("post_sha256: expected 64 lowercase hex characters", observed=sha)

    counts = data.get("counts")
    count_keys = ("ruby", "regions", "ok", "ok_contextual", "ok_attested", "low_confidence", "divergent")
    if not isinstance(counts, dict) or any(type(counts.get(k)) is not int or counts.get(k) < 0 for k in count_keys):
        bad("counts: expected nonnegative integers for all shipped keys")
        counts = counts if isinstance(counts, dict) else {}
    else:
        if counts["ok"] != counts["ok_contextual"] + counts["ok_attested"]:
            bad("counts: ok != ok_contextual + ok_attested")

    findings = data.get("findings")
    if not isinstance(findings, list):
        bad("findings: expected a list")
        findings = []
    else:
        for f in findings:
            if not isinstance(f, dict) or not {"line", "surface", "verdict", "reason"} <= set(f):
                bad("findings: entry missing line/surface/verdict/reason")
                break
        if isinstance(counts, dict) and type(counts.get("low_confidence")) is int and counts["low_confidence"] > 0:
            if not any(f.get("verdict") == "low_confidence" for f in findings if isinstance(f, dict)):
                bad("counts.low_confidence > 0 but no low_confidence finding present")
        if isinstance(counts, dict) and type(counts.get("divergent")) is int and counts["divergent"] > 0:
            if not any(f.get("verdict") == "divergent" for f in findings if isinstance(f, dict)):
                bad("counts.divergent > 0 but no divergent finding present")
    if issues:
        return None, issues
    return data, []


def finding_detail(f: dict) -> str:
    at = f"@{f['col']}" if isinstance(f.get("col"), int) else ""
    rt = f.get("rt") if f.get("rt") is not None else "-"
    return f"L{f.get('line')} plain{at} {f.get('surface')} rt={rt}; {f.get('reason')}"


# --- gold corpus ---------------------------------------------------------------


@dataclass
class GoldMember:
    stem: str
    post_path: Path
    ann_path: Path
    post_bytes: bytes
    ann_bytes: bytes


def assemble_gold(repo_root: Path):
    """Membership is the contiguous day-001..N set actually present in the tree."""
    by_day: dict = {}
    ann_dir = repo_root / "data" / "annotations"
    for p in sorted((repo_root / "source" / "_posts").glob("day-*.md")):
        m = DAY_STEM_RE.fullmatch(p.stem)
        if m:
            by_day.setdefault(int(m.group(1)), []).append(p)
    members = []
    day = 1
    while day in by_day:
        group = by_day[day]
        if len(group) != 1:
            return None, [ev("observation",
                             f"day {day:03d}: {len(group)} daily posts in source/_posts",
                             day=day)]
        post_path = group[0]
        ann_path = ann_dir / f"{post_path.stem}.json"
        if not ann_path.is_file():
            return None, [ev("observation",
                             f"day {day:03d}: missing baseline report {ann_path.relative_to(repo_root)}",
                             day=day)]
        members.append(GoldMember(post_path.stem, post_path, ann_path,
                                  post_path.read_bytes(), ann_path.read_bytes()))
        day += 1
    if not members:
        return None, [ev("observation", "no gold corpus: source/_posts has no day-001 post")]
    return members, []


def corpus_digest(annotator_bytes: bytes, members: Sequence[GoldMember]) -> str:
    h = hashlib.sha256()
    h.update(b"gate-gold-v1\n")
    h.update(hashlib.sha256(annotator_bytes).hexdigest().encode())
    for m in sorted(members, key=lambda m: m.stem):
        h.update(m.stem.encode())
        h.update(hashlib.sha256(m.post_bytes).hexdigest().encode())
        h.update(hashlib.sha256(m.ann_bytes).hexdigest().encode())
    return h.hexdigest()


def replay_gold(annotator_bytes: bytes, members: Sequence[GoldMember]):
    """Re-run stage 3 in a throwaway tree; compare report bytes exactly."""
    with tempfile.TemporaryDirectory(prefix="gate-gold-") as tmpname:
        tmp = Path(tmpname)
        (tmp / "tools").mkdir()
        (tmp / "source" / "_posts").mkdir(parents=True)
        (tmp / "tools" / "annotate-post.py").write_bytes(annotator_bytes)
        post_args = []
        for m in members:
            dest = tmp / "source" / "_posts" / m.post_path.name
            dest.write_bytes(m.post_bytes)
            post_args.append(str(dest))
        proc = subprocess.run(
            [sys.executable, str(tmp / "tools" / "annotate-post.py")] + post_args,
            capture_output=True,
        )
        # 0 all ok, 2 divergent, 3 low-confidence: all completed stage-3 runs.
        if proc.returncode not in (0, 2, 3):
            tail = proc.stderr.decode("utf-8", errors="replace")[-800:]
            raise ExecutionError(
                f"gold replay annotator exited {proc.returncode} (expected 0/2/3): {tail}"
            )
        issues = []
        for m in members:
            generated = tmp / "data" / "annotations" / f"{m.stem}.json"
            if not generated.is_file():
                issues.append(ev("hash", f"{m.stem}: replay produced no report",
                                 member=m.stem, expected_sha256=hashlib.sha256(m.ann_bytes).hexdigest(),
                                 observed_sha256=None))
                continue
            observed = generated.read_bytes()
            if observed != m.ann_bytes:
                issues.append(ev(
                    "hash",
                    f"{m.stem}: baseline {hashlib.sha256(m.ann_bytes).hexdigest()[:12]}... "
                    f"!= replayed {hashlib.sha256(observed).hexdigest()[:12]}...",
                    member=m.stem,
                    expected_sha256=hashlib.sha256(m.ann_bytes).hexdigest(),
                    observed_sha256=hashlib.sha256(observed).hexdigest(),
                ))
    return issues


# --- post evaluation ----------------------------------------------------------


def evaluate_post(post: Post, ann_path: Path, anchor: date, gold_issues: Sequence[dict]) -> dict:
    results = {}

    contract, day = contract_issues(post, anchor)
    results["post.contract"] = (
        issue("post.contract", *contract) if contract else satisfied("post.contract")
    )

    sections, intro, more_markers = detect_sections(post.lines, offset=post.body_start)
    by_kind: dict = {}
    for s in sections:
        by_kind.setdefault(s.kind, []).append(s)

    first_h2 = sections[0].heading_no if sections else None
    intro_ok = True
    intro_evidence = []
    if len(more_markers) != 1:
        intro_ok = False
        intro_evidence.append(ev("observation", f"expected 1 {MORE_MARKER}, observed {len(more_markers)}",
                                 observed=len(more_markers)))
    elif first_h2 is None or more_markers[0] > first_h2:
        intro_ok = False
        at = more_markers[0]
        intro_evidence.append(ev("observation", f"{MORE_MARKER} at L{at} is not before the first section",
                                 line=at))
    if not any(l.strip() and not l.lstrip().startswith((">", "#")) for _, l in intro):
        intro_ok = False
        intro_evidence.append(ev("observation", "no prose paragraph before the more marker"))
    results["intro.structure"] = (
        issue("intro.structure", *intro_evidence) if not intro_ok else satisfied("intro.structure")
    )

    order = ["japanese", "vocabulary", "grammar"]
    section_evidence = []
    for kind, label in (("japanese", "日文文章"), ("vocabulary", "3 個重點單字"), ("grammar", "1 個重點文法")):
        found = by_kind.get(kind, [])
        if not found:
            section_evidence.append(ev("observation", f"missing ## {label} section"))
        elif len(found) > 1:
            at = ", ".join(f"L{s.heading_no}" for s in found)
            section_evidence.append(ev("observation", f"{len(found)} ## {label} sections ({at})",
                                       lines=[s.heading_no for s in found]))
    if all(len(by_kind.get(k, [])) == 1 for k in order):
        positions = [by_kind[k][0].heading_no for k in order]
        if positions != sorted(positions):
            section_evidence.append(ev("observation", "required sections out of template order"))
    results["sections.required"] = (
        issue("sections.required", *section_evidence) if section_evidence else satisfied("sections.required")
    )

    unknown = by_kind.get("unrecognized", [])
    results["sections.unrecognized"] = (
        issue("sections.unrecognized", *[
            ev("observation", f'unrecognized H2 "{s.heading}" at L{s.heading_no}', line=s.heading_no)
            for s in unknown
        ]) if unknown else satisfied("sections.unrecognized")
    )

    japanese = by_kind.get("japanese", [None])[0]
    if japanese is None:
        for check in ("body.structure", "length.maximum", "length.target_minimum",
                      "translations.paragraphs", "ruby.coverage"):
            results[check] = blocked(check, "sections.required")
    else:
        blocks = split_blocks(japanese.lines)
        body_evidence = []
        prose_blocks = [b for b in blocks if b[0] == "prose"]
        if not prose_blocks:
            body_evidence.append(ev("observation", "no prose paragraphs in the Japanese section"))
        for kind, no, _ in blocks:
            if kind == "other":
                body_evidence.append(ev("observation", f"L{no}: unsupported block structure in the Japanese section",
                                        line=no))
        prev_kind = None
        for kind, no, _ in blocks:
            if kind == "quote" and prev_kind != "prose":
                body_evidence.append(ev("observation", f"L{no}: orphan translation blockquote", line=no))
            prev_kind = kind
        results["body.structure"] = (
            issue("body.structure", *body_evidence) if body_evidence else satisfied("body.structure")
        )

        ruby_issues = ruby_structure_issues(post.lines, offset=post.body_start)
        results["ruby.structure"] = (
            issue("ruby.structure", *ruby_issues) if ruby_issues else satisfied("ruby.structure")
        )

        count = canonical_count(japanese.lines)
        minimum, maximum = length_band(day if day is not None else post.day)
        length_ev = ev("length", f"{count}/{maximum} characters", actual=count,
                       minimum=minimum, maximum=maximum, method="reading-locked-japanese-v1")
        if ruby_issues:
            results["length.maximum"] = blocked("length.maximum", "ruby.structure")
            results["length.target_minimum"] = blocked("length.target_minimum", "ruby.structure")
        else:
            results["length.maximum"] = (
                issue("length.maximum", length_ev) if count > maximum else satisfied("length.maximum", length_ev)
            )
            if minimum is None:
                results["length.target_minimum"] = na("length.target_minimum", "no target minimum for days 71-100")
            else:
                low_ev = ev("length", f"{count}/{minimum} characters", actual=count,
                            minimum=minimum, maximum=maximum, method="reading-locked-japanese-v1")
                results["length.target_minimum"] = (
                    issue("length.target_minimum", low_ev) if count < minimum
                    else satisfied("length.target_minimum", low_ev)
                )

        results["ruby.coverage"] = (
            blocked("ruby.coverage", "ruby.structure") if ruby_issues
            else (lambda bare: issue("ruby.coverage", *bare) if bare else satisfied("ruby.coverage"))(bare_kanji(japanese.lines))
        )

        translation_evidence = []
        for i, (kind, no, block_lines) in enumerate(blocks):
            if kind != "prose":
                continue
            following = blocks[i + 1] if i + 1 < len(blocks) else None
            if following is None or following[0] != "quote":
                translation_evidence.append(ev(
                    "observation",
                    f"L{no}: paragraph has no following translation blockquote",
                    line=no,
                ))
            elif len(translation_spans(following[2])) != 1:
                translation_evidence.append(ev(
                    "observation",
                    f"L{following[1]}: blockquote is not exactly one complete zh-Hant translation span",
                    line=following[1],
                ))
        results["translations.paragraphs"] = (
            issue("translations.paragraphs", *translation_evidence)
            if translation_evidence else satisfied("translations.paragraphs")
        )

    vocab = by_kind.get("vocabulary", [None])[0]
    if vocab is None:
        results["vocabulary.cards"] = blocked("vocabulary.cards", "sections.required")
    else:
        vocab_evidence = []
        cards = []
        cur = None
        for no, line in vocab.lines:
            if line.startswith("### "):
                cur = (no, re.sub(r"\s+#+\s*$", "", line[4:].strip()), [])
                cards.append(cur)
            elif cur is not None:
                cur[2].append((no, line))
        numbers = []
        for no, title, card_lines in cards:
            m = re.match(r"^(\d+)\.\s*\S", title)
            if m:
                numbers.append(int(m.group(1)))
            else:
                vocab_evidence.append(ev("observation", f'L{no}: card heading not numbered: "### {title}"', line=no))
            body = "\n".join(l for _, l in card_lines)
            for label in ("詞性", "意思", "常見搭配", "例句"):
                if label not in body:
                    vocab_evidence.append(ev("observation", f"L{no}: card missing {label}", line=no))
            if not translation_spans(card_lines):
                vocab_evidence.append(ev("observation", f"L{no}: card missing translation span", line=no))
        if numbers != [1, 2, 3] or len(cards) != 3:
            vocab_evidence.append(ev("observation",
                                     f"expected 3 cards numbered 1..3, observed {len(cards)} cards {numbers}",
                                     observed=len(cards)))
        results["vocabulary.cards"] = (
            issue("vocabulary.cards", *vocab_evidence) if vocab_evidence else satisfied("vocabulary.cards")
        )

    grammar = by_kind.get("grammar", [None])[0]
    if grammar is None:
        results["grammar.point"] = blocked("grammar.point", "sections.required")
    else:
        grammar_evidence = []
        points = [(no, line) for no, line in grammar.lines if line.startswith("### ")]
        if len(points) != 1:
            grammar_evidence.append(ev("observation",
                                       f"expected 1 grammar H3 point, observed {len(points)}",
                                       observed=len(points)))
        body = "\n".join(l for _, l in grammar.lines)
        for label in ("接續", "核心語意", "例句"):
            if label not in body:
                grammar_evidence.append(ev("observation", f"grammar section missing {label}"))
        if "比較與注意" not in body and "相似比較" not in body:
            grammar_evidence.append(ev("observation", "grammar section missing comparison material"))
        results["grammar.point"] = (
            issue("grammar.point", *grammar_evidence) if grammar_evidence else satisfied("grammar.point")
        )

    writing = by_kind.get("writing", [])
    expected_date = anchor + timedelta(days=post.day - 1)
    is_sunday = expected_date.weekday() == 6
    if day is None:
        results["writing.sunday"] = blocked("writing.sunday", "post.contract")
    elif not writing and not is_sunday:
        results["writing.sunday"] = na("writing.sunday", f"{expected_date} is not a Sunday")
    elif not writing:
        results["writing.sunday"] = issue(
            "writing.sunday",
            ev("observation", f"{expected_date} is a Sunday: writing section missing"),
        )
    else:
        prompts = [no for no, line in writing[0].lines if PROMPT_RE.match(line)]
        if 5 <= len(prompts) <= 8:
            results["writing.sunday"] = satisfied(
                "writing.sunday",
                ev("observation", f"{len(prompts)} writing prompts", prompts=len(prompts)),
            )
        else:
            results["writing.sunday"] = issue(
                "writing.sunday",
                ev("observation",
                   f"L{writing[0].heading_no}: writing section has {len(prompts)} prompts, expected 5-8",
                   line=writing[0].heading_no, prompts=len(prompts)),
            )

    nonblank = [(no, line) for no, line in enumerate(post.lines, start=post.body_start + 1) if line.strip()]
    last_no, last_line = nonblank[-1] if nonblank else (0, "")
    results["footer.attribution"] = (
        satisfied("footer.attribution")
        if "JMdict" in last_line and "EDRDG" in last_line
        else issue("footer.attribution",
                   ev("observation", f"L{last_no}: final line does not name JMdict and EDRDG", line=last_no))
    )

    annotation_raw = ann_path.read_bytes() if ann_path.is_file() else None
    if annotation_raw is None:
        results["annotation.present"] = issue(
            "annotation.present",
            ev("observation", f"annotation report not found: {ann_path.name}"),
        )
        for check in ("annotation.contract", "annotation.freshness",
                      "annotation.divergent", "annotation.low_confidence"):
            results[check] = blocked(check, "annotation.present")
    else:
        results["annotation.present"] = satisfied("annotation.present")
        data, contract = decode_annotation(annotation_raw, post)
        if data is None:
            results["annotation.contract"] = issue("annotation.contract", *contract)
            for check in ("annotation.freshness", "annotation.divergent", "annotation.low_confidence"):
                results[check] = blocked(check, "annotation.contract")
        else:
            results["annotation.contract"] = satisfied("annotation.contract")
            current = hashlib.sha256(post.raw).hexdigest()
            recorded = data["post_sha256"]
            if current != recorded:
                results["annotation.freshness"] = issue(
                    "annotation.freshness",
                    ev("hash", f"annotation post_sha256 {recorded} != sha256(post) {current}",
                       resource=post.stem, expected_sha256=recorded, observed_sha256=current),
                )
                results["annotation.divergent"] = blocked("annotation.divergent", "annotation.freshness")
                results["annotation.low_confidence"] = blocked("annotation.low_confidence", "annotation.freshness")
            else:
                results["annotation.freshness"] = satisfied(
                    "annotation.freshness",
                    ev("hash", f"post_sha256 {current}", resource=post.stem,
                       expected_sha256=current, observed_sha256=current),
                )
                counts = data["counts"]
                findings = data["findings"]
                if counts["divergent"] > 0:
                    results["annotation.divergent"] = issue(
                        "annotation.divergent",
                        *[ev("finding", finding_detail(f), finding=f) for f in findings
                          if f.get("verdict") == "divergent"],
                    )
                else:
                    results["annotation.divergent"] = satisfied("annotation.divergent")
                if counts["low_confidence"] > 0:
                    results["annotation.low_confidence"] = issue(
                        "annotation.low_confidence",
                        *[ev("finding", finding_detail(f), finding=f) for f in findings
                          if f.get("verdict") == "low_confidence"],
                    )
                else:
                    results["annotation.low_confidence"] = satisfied("annotation.low_confidence")

    if gold_issues:
        results["gold.regression"] = issue("gold.regression", *gold_issues)
    else:
        results["gold.regression"] = satisfied("gold.regression")

    ordered = [results[check] for check in CHECKS if check in results]
    return {
        "stem": post.stem,
        "day": day,
        "post_sha256": hashlib.sha256(post.raw).hexdigest(),
        "annotation_sha256": (
            hashlib.sha256(annotation_raw).hexdigest() if annotation_raw is not None else None
        ),
        "verdict": reduce_verdict(ordered),
        "checks": ordered,
    }


# --- reporting -----------------------------------------------------------------


def encode_report(report: dict) -> bytes:
    return (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def write_json(destination: Path, payload: bytes, protected: Sequence[Path]) -> None:
    resolved = destination.resolve()
    for p in protected:
        if resolved == p.resolve():
            raise ExecutionError(f"--json destination aliases an input: {destination}")
    tmp = destination.parent / (destination.name + ".gate-tmp")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, destination)
    except OSError as exc:
        raise ExecutionError(f"cannot write {destination}: {exc}")


def print_summary(report: dict) -> None:
    for post in report["posts"]:
        count = maximum = None
        for check in post["checks"]:
            if check["check"] == "length.maximum" and check["state"] in ("satisfied", "issue"):
                for e in check["evidence"]:
                    if e["kind"] == "length":
                        count, maximum = e["actual"], e["maximum"]
        suffix = f" ({count}/{maximum} characters)" if count is not None else ""
        print(f"{post['stem']}: {post['verdict']}{suffix}")
        for check in post["checks"]:
            if check["state"] != "issue":
                continue
            label = "FAIL" if check["severity"] == HARD else "FLAG"
            for e in check["evidence"]:
                print(f"  {label} {check['check']}: {e['detail']}")
    gold = report["gold"]
    if gold["state"] == "satisfied":
        print(f"gold replay: {gold['members']} posts byte-identical")
    else:
        print(f"gold replay: RED")
        for e in gold["issues"]:
            print(f"  {e['detail']}")
    counts = {"PASS": 0, "FLAG": 0, "FAIL": 0}
    for post in report["posts"]:
        counts[post["verdict"]] += 1
    n = len(report["posts"])
    print(f"TOTAL {n} post{'' if n == 1 else 's'}: "
          f"{counts['PASS']} PASS, {counts['FLAG']} FLAG, {counts['FAIL']} FAIL")
    print(f"required external: {', '.join(REQUIRED_EXTERNAL)}")


# --- CLI ------------------------------------------------------------------------


def parse_args(argv: Sequence[str]):
    posts = []
    json_path = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--json":
            i += 1
            if i >= len(argv):
                raise UsageError("--json requires a path")
            json_path = Path(argv[i])
        elif arg.startswith("--"):
            raise UsageError(f"unknown option: {arg}")
        elif arg:
            posts.append(Path(arg))
        i += 1
    if not posts:
        raise UsageError("usage: gate-post.py POST [POST ...] [--json PATH]")
    seen = {}
    for p in posts:
        seen.setdefault(str(p.resolve()), p)
    return sorted(seen.values(), key=lambda p: p.stem), json_path


def find_annotation(repo_root: Path, post_path: Path) -> Path:
    """Annotation lives in the data/annotations directory of the tree the post
    belongs to; fall back to this gate's own repository root."""
    name = f"{post_path.stem}.json"
    for base in [post_path.resolve().parent, *post_path.resolve().parents]:
        candidate = base / "data" / "annotations" / name
        if candidate.is_file():
            return candidate
    return repo_root / "data" / "annotations" / name


def main(argv: Sequence[str]) -> int:
    try:
        post_paths, json_path = parse_args(argv)
        repo_root = Path(__file__).resolve().parent.parent
        theme_path = repo_root / "aidd-docs" / "references" / "japanese-theme-rotation.md"
        try:
            theme_bytes = theme_path.read_bytes()
        except OSError as exc:
            raise ExecutionError(f"cannot read theme schedule {theme_path}: {exc}")
        m = THEME_RE.search(theme_bytes.decode("utf-8", errors="replace"))
        if not m:
            raise ExecutionError(f"no 'Day 1 = YYYY-MM-DD' anchor in {theme_path}")
        anchor = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

        posts = []
        for path in post_paths:
            if not path.is_file():
                raise UsageError(f"no such post: {path}")
            posts.append(load_post(path))

        annotator_path = repo_root / "tools" / "annotate-post.py"
        try:
            annotator_bytes = annotator_path.read_bytes()
        except OSError as exc:
            raise ExecutionError(f"cannot read {annotator_path}: {exc}")

        members, gold_assembly_issues = assemble_gold(repo_root)
        if members:
            gold_issues = gold_assembly_issues + replay_gold(annotator_bytes, members)
            digest = corpus_digest(annotator_bytes, members)
            gold_state = "satisfied" if not gold_issues else "issue"
        else:
            gold_issues = gold_assembly_issues
            digest = None
            gold_state = "issue"

        post_reports = [
            evaluate_post(post, find_annotation(repo_root, path), anchor, gold_issues)
            for post, path in zip(posts, post_paths)
        ]
        all_results = [c for p in post_reports for c in p["checks"]]
        report = {
            "tool": "gate-post",
            "schema": 1,
            "scope": "deterministic",
            "verdict": reduce_verdict(all_results),
            "gate_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "theme_sha256": hashlib.sha256(theme_bytes).hexdigest(),
            "required_external": list(REQUIRED_EXTERNAL),
            "gold": {
                "state": gold_state,
                "members": len(members) if members else 0,
                "corpus_digest": digest,
                "issues": gold_issues,
            },
            "posts": post_reports,
        }
        print_summary(report)
        if json_path is not None:
            protected = [theme_path, annotator_path, Path(__file__).resolve()]
            for post, path in zip(posts, post_paths):
                protected.append(path)
                protected.append(find_annotation(repo_root, path))
            if members:
                protected.extend(m.post_path for m in members)
                protected.extend(m.ann_path for m in members)
            write_json(json_path, encode_report(report), protected)
    except UsageError as exc:
        print(f"gate-post: {exc}", file=sys.stderr)
        return int(ExitCode.USAGE)
    except ExecutionError as exc:
        print(f"gate-post: {exc}", file=sys.stderr)
        return int(ExitCode.EXECUTION)
    return int(EXIT_OF_VERDICT[report["verdict"]])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
