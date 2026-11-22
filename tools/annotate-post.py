#!/usr/bin/env python3
"""Stage 3 of the daily loop: verify ruby readings in a hexo post.

Usage:
    uv run --with fugashi --with unidic-lite --with jamdict --with jamdict_data \
           --with pykakasi python tools/annotate-post.py source/_posts/day-*.md

Reports are written to data/annotations/<post-stem>.json, one per post,
deterministic bytes for identical input and environment, committed artifacts.

Exit codes: 0 all ok, 1 usage error (bad path or front-matter, stderr message,
no report for that post), 2 any divergent reading, 3 low_confidence only.
Multiple posts return the worst outcome: 2 > 3 > 1 > 0.
"""
import hashlib
import importlib.metadata
import json
import platform
import re
import sys
from collections import Counter
from dataclasses import dataclass, asdict
from enum import IntEnum
from pathlib import Path
from typing import Iterable, Literal

RUBY_RE = re.compile(
    r"<ruby>(?P<surf>.*?)<rp>.*?</rp><rt>(?P<rt>.*?)</rt><rp>.*?</rp></ruby>"
)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"[ \t　]")
RT_SCRIPT = re.compile(r"[ぁ-ゖァ-ヶー]+")
# A ruby base may carry kanji, kana, the long-vowel mark and digits. Anything
# else in a base (punctuation, spaces, Latin) marks a phrase wrongly wrapped
# in one ruby, which only a markup repair can fix.
KANJI_CLASS = "㐀-鿿々\U00020000-\U000323af豈-﫿"
BASE_SCRIPT = re.compile(f"[{KANJI_CLASS}ぁ-ゖァ-ヶー0-9０-９]+")
# Free variants of the ten-based minute counters (十分, 二十分, 三十分 ...):
# じっぷん, じゅっぷん and じゅうふん are all standard, and JMdict has no entry
# for the compounds above 十分 to attest them. The shapes carry the limit: 一分,
# 六分 and 鬱憤 never take these forms, so a wrong reading still differs.
READING_VARIANTS = ((re.compile("じっぷん"), "じゅっぷん"),
                    (re.compile("ゅっぷん"), "ゅうふん"))
KANA_SCRIPT = re.compile(r"[ぁ-ゖァ-ヶー]")
KANA_SEQ = re.compile(r"[ぁ-ゖァ-ヶー]+")
KANJI_RE = re.compile(f"[{KANJI_CLASS}]")
DAY_RE = re.compile(r"^day:\s*(\d+)\s*$", re.M)

# Long o is written う (コー -> こう) per standard kana orthography; the o
# row's break from vowel identity below is correct, not a typo to "fix".
_KANA_ROWS = (
    ("a", "あかがさざただなはばぱまやゃらわゎぁ"),
    ("i", "いきぎしじちぢにひびぴみりゐぃ"),
    ("u", "うくぐすずつづぬふぶぷむゆゅるゔぅ"),
    ("e", "えけげせぜてでねへべぺめれゑぇ"),
    ("o", "おこごそぞとどのほぼぽもよょろをぉ"),
)
VOWEL_OF_KANA = {ch: vowel for vowel, row in _KANA_ROWS for ch in row}
LONG_VOWEL_OF = {"a": "あ", "i": "い", "u": "う", "e": "え", "o": "う"}

Verdict = Literal["ok", "divergent", "low_confidence"]
Reason = Literal[
    "contextual_match",
    "attested_variant",
    "pykakasi_only",
    "pykakasi_conflict",
    "no_support",
    "unattested_reading",
    "malformed_ruby",
]
VERDICT_OF_REASON: dict[Reason, Verdict] = {
    "contextual_match": "ok",
    "attested_variant": "ok",
    "pykakasi_only": "low_confidence",
    "pykakasi_conflict": "low_confidence",
    "no_support": "divergent",
    "unattested_reading": "divergent",
    "malformed_ruby": "divergent",
}


class ExitCode(IntEnum):
    OK = 0
    USAGE = 1
    DIVERGENT = 2
    LOW_CONFIDENCE = 3


_SEVERITY = {ExitCode.OK: 0, ExitCode.USAGE: 1, ExitCode.LOW_CONFIDENCE: 2, ExitCode.DIVERGENT: 3}


class UsageError(Exception):
    pass


@dataclass(frozen=True)
class Token:
    start: int
    end: int
    surface: str
    kana: str | None
    lemma: str | None


@dataclass(frozen=True)
class RubySpan:
    line: int
    start: int
    end: int
    surface: str
    rt: str


@dataclass(frozen=True)
class Region:
    start: int
    end: int
    surface: str
    spans: tuple[RubySpan, ...]
    tokens: tuple[Token, ...]

    @property
    def single_token(self) -> bool:
        return len(self.tokens) == 1


@dataclass(frozen=True)
class Evidence:
    assembled: str | None
    unidic: str | None
    tokens: tuple[str, ...]
    jmdict: tuple[str, ...]
    pykakasi: str | None


@dataclass(frozen=True)
class Finding:
    line: int
    col: int | None
    surface: str
    rt: str | None
    verdict: Verdict
    reason: Reason
    evidence: Evidence


@dataclass(frozen=True)
class ParsedPost:
    name: str
    day: int | None
    ruby_lines: tuple[tuple[int, str], ...]


def parse_post(raw: str, name: str) -> ParsedPost:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        raise UsageError("no opening --- for front-matter")
    try:
        fm_end = next(i for i, line in enumerate(lines) if i > 0 and line.strip() == "---")
    except StopIteration:
        raise UsageError("no closing --- for front-matter")
    head = "\n".join(lines[1:fm_end])
    day_match = DAY_RE.search(head)
    day = int(day_match.group(1)) if day_match else None
    numbered = enumerate(lines, start=1)
    ruby_lines = tuple((no, line) for no, line in numbered if no > fm_end + 1 and "<ruby" in line)
    return ParsedPost(name, day, ruby_lines)


def strip_markup(text: str) -> str:
    return SPACE_RE.sub("", TAG_RE.sub("", text))


def parse_line(line: str, line_no: int) -> tuple[str | None, list[RubySpan], Finding | None]:
    matches = list(RUBY_RE.finditer(line))
    if line.count("<ruby") != len(matches) or line.count("</ruby>") != len(matches):
        return None, [], malformed(line_no, line)
    spans: list[RubySpan] = []
    plain_parts: list[str] = []
    pos = 0
    for m in matches:
        plain_parts.append(strip_markup(line[pos : m.start()]))
        surf = SPACE_RE.sub("", m.group("surf"))
        rt = m.group("rt")
        start = sum(map(len, plain_parts))
        spans.append(RubySpan(line_no, start, start + len(surf), surf, rt))
        plain_parts.append(surf)
        pos = m.end()
    plain_parts.append(strip_markup(line[pos:]))
    for span in spans:
        if (not span.surface or not span.rt or not RT_SCRIPT.fullmatch(span.rt)
                or not BASE_SCRIPT.fullmatch(span.surface)):
            return None, [], malformed(line_no, line)
    return "".join(plain_parts), spans, None


def malformed(line_no: int, line: str) -> Finding:
    return Finding(
        line_no,
        None,
        line.strip()[:60],
        None,
        "divergent",
        "malformed_ruby",
        Evidence(None, None, (), (), None),
    )


_tagger = None


def tokenize(plain: str) -> list[Token]:
    global _tagger
    if _tagger is None:
        import fugashi

        _tagger = fugashi.Tagger()
    tokens: list[Token] = []
    pos = 0
    for word in _tagger(plain):
        surface = word.surface
        kana = getattr(word.feature, "kana", None) or None
        lemma = getattr(word.feature, "lemma", None) or None
        tokens.append(Token(pos, pos + len(surface), surface, kana, lemma))
        pos += len(surface)
    if pos != len(plain):
        raise RuntimeError(f"token surfaces do not tile the plain line: {plain!r}")
    return tokens


def build_regions(plain: str, spans: list[RubySpan], tokens: list[Token]) -> list[Region]:
    items = sorted(
        [(i.start, 0, i) for i in spans] + [(i.start, 1, i) for i in tokens],
        key=lambda t: (t[0], t[1]),
    )
    regions: list[Region] = []
    span_acc: list[RubySpan] = []
    token_acc: list[Token] = []
    start = end = None
    for _, kind, item in items:
        if start is not None and item.start < end:
            (span_acc if kind == 0 else token_acc).append(item)
            end = max(end, item.end)
            continue
        if span_acc:
            regions.append(Region(start, end, plain[start:end], tuple(span_acc), tuple(token_acc)))
        span_acc = [item] if kind == 0 else []
        token_acc = [item] if kind == 1 else []
        start, end = item.start, item.end
    if span_acc:
        regions.append(Region(start, end, plain[start:end], tuple(span_acc), tuple(token_acc)))
    return regions


def assemble_reading(plain: str, region: Region) -> str:
    rt_at = {span.start: span.rt for span in region.spans}
    inside = {i for span in region.spans for i in range(span.start, span.end)}
    out = []
    for i in range(region.start, region.end):
        if i in rt_at:
            out.append(rt_at[i])
        elif i not in inside:
            out.append(plain[i])
    return "".join(out)


def normalize(kana: str) -> str:
    out = []
    for ch in kana:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:
            ch = chr(code - 0x60)
        if ch == "ー" and out:
            vowel = VOWEL_OF_KANA.get(out[-1])
            if vowel:
                out.append(LONG_VOWEL_OF[vowel])
                continue
        out.append(ch)
    text = "".join(out)
    for variant, replacement in READING_VARIANTS:
        text = variant.sub(replacement, text)
    return text


def to_katakana(text: str) -> str:
    return "".join(
        chr(ord(ch) + 0x60) if 0x3041 <= ord(ch) <= 0x3096 else ch for ch in text
    )


def has_kanji(text: str) -> bool:
    return bool(KANJI_RE.search(text))


_jamdict = None


def jamdict_lookup(key: str):
    global _jamdict
    if _jamdict is None:
        from jamdict import Jamdict

        _jamdict = Jamdict()
    return _jamdict.lookup(key)


def strip_okurigana(form: str, key: str, surface: str) -> str:
    suffix_len = len(key) - len(surface)
    if (
        suffix_len > 0
        and key.startswith(surface)
        and suffix_len <= len(form)
        and KANA_SEQ.fullmatch(key[len(surface) :])
    ):
        return form[: len(form) - suffix_len]
    return form


def attested(keys: Iterable[str], surface: str, reading: str) -> list[str]:
    hits = []
    for key in keys:
        for entry in jamdict_lookup(key).entries:
            kanji_forms = [f.text for f in entry.kanji_forms]
            if key not in set(kanji_forms) | {f.text for f in entry.kana_forms}:
                continue
            for kform in entry.kana_forms:
                if kform.restr and key not in kform.restr:
                    continue
                if normalize(strip_okurigana(kform.text, key, surface)) == normalize(reading):
                    hits.append(kform.text)
    return list(dict.fromkeys(hits))


_kakasi = None


def pykakasi_reading(surface: str) -> str | None:
    global _kakasi
    if _kakasi is None:
        from pykakasi import kakasi

        _kakasi = kakasi()
    reading = "".join(item["kana"] for item in _kakasi.convert(surface))
    return reading or None


def agrees(candidate: str | None, reading: str) -> bool:
    return candidate is not None and normalize(candidate) == normalize(reading)


@dataclass(frozen=True)
class RegionVerdict:
    verdict: Verdict
    reason: Reason
    evidence: Evidence


def verdict(plain: str, region: Region) -> RegionVerdict:
    assembled = assemble_reading(plain, region)
    token_surfaces = tuple(t.surface for t in region.tokens)
    unidic_missing = any(t.kana is None and has_kanji(t.surface) for t in region.tokens)
    unidic_reading = None if unidic_missing else "".join(
        t.kana if t.kana is not None else to_katakana(t.surface) for t in region.tokens
    )
    if unidic_missing:
        pykakasi = pykakasi_reading(region.surface)
        reason = "pykakasi_only" if agrees(pykakasi, assembled) else "pykakasi_conflict"
        return RegionVerdict("low_confidence", reason, Evidence(assembled, None, token_surfaces, (), pykakasi))
    if agrees(unidic_reading, assembled):
        return RegionVerdict("ok", "contextual_match", Evidence(assembled, unidic_reading, token_surfaces, (), None))
    keys = [region.surface]
    head_lemma = region.tokens[0].lemma
    if head_lemma and head_lemma.startswith(region.surface) and KANA_SEQ.fullmatch(
        head_lemma[len(region.surface) :]
    ):
        keys.append(head_lemma)
    hits = attested(dict.fromkeys(keys), region.surface, assembled)
    if hits:
        return RegionVerdict("ok", "attested_variant", Evidence(assembled, unidic_reading, token_surfaces, tuple(hits), None))
    if not region.single_token:
        pykakasi = pykakasi_reading(region.surface)
        if agrees(pykakasi, assembled):
            return RegionVerdict("low_confidence", "pykakasi_only", Evidence(assembled, unidic_reading, token_surfaces, (), pykakasi))
        return RegionVerdict("divergent", "no_support", Evidence(assembled, unidic_reading, token_surfaces, (), pykakasi))
    return RegionVerdict("divergent", "unattested_reading", Evidence(assembled, unidic_reading, token_surfaces, (), None))


def verify_post(post: ParsedPost) -> tuple[dict, list[Finding]]:
    ruby = 0
    regions = 0
    by_reason: Counter = Counter()
    findings: list[Finding] = []
    for line_no, line in post.ruby_lines:
        plain, spans, bad = parse_line(line, line_no)
        if bad is not None:
            findings.append(bad)
            by_reason[bad.reason] += 1
            continue
        ruby += len(spans)
        tokens = tokenize(plain)
        for region in build_regions(plain, spans, tokens):
            regions += 1
            rv = verdict(plain, region)
            by_reason[rv.reason] += 1
            if rv.verdict != "ok":
                findings.extend(
                    Finding(s.line, s.start, s.surface, s.rt, rv.verdict, rv.reason, rv.evidence)
                    for s in region.spans
                )
    ok_contextual = by_reason["contextual_match"]
    ok_attested = by_reason["attested_variant"]
    low = sum(n for r, n in by_reason.items() if VERDICT_OF_REASON[r] == "low_confidence")
    divergent = sum(n for r, n in by_reason.items() if VERDICT_OF_REASON[r] == "divergent")
    counts = {
        "ruby": ruby,
        "regions": regions,
        "ok": ok_contextual + ok_attested,
        "ok_contextual": ok_contextual,
        "ok_attested": ok_attested,
        "low_confidence": low,
        "divergent": divergent,
    }
    return counts, findings


def source_versions() -> dict:
    versions = {"python": platform.python_version()}
    for dist in ("fugashi", "unidic-lite", "jamdict", "jamdict_data", "pykakasi"):
        try:
            versions[dist] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            versions[dist] = "not-installed"
    return versions


def finding_line(f: Finding) -> str:
    ev = f.evidence
    jmdict = "|".join(ev.jmdict) if ev.jmdict else "-"
    rt = f.rt if f.rt is not None else "-"
    unidic = ev.unidic if ev.unidic is not None else "-"
    pykakasi = ev.pykakasi if ev.pykakasi is not None else "-"
    return (
        f"  L{f.line}:{f.col} {f.surface} rt={rt} unidic={unidic} "
        f"jmdict={jmdict} pykakasi={pykakasi} -> {f.verdict} ({f.reason})"
    )


def main(argv: list[str]) -> int:
    paths = [Path(p) for p in argv[1:]]
    if not paths:
        print("usage: annotate-post.py POST [POST ...]", file=sys.stderr)
        return ExitCode.USAGE
    out_dir = Path(__file__).resolve().parent.parent / "data" / "annotations"
    out_dir.mkdir(parents=True, exist_ok=True)
    sources = source_versions()
    worst = ExitCode.OK
    totals = Counter()
    posts_ok = 0
    for path in paths:
        stem = path.stem
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
            post = parse_post(text, stem)
        except (OSError, UnicodeDecodeError, UsageError) as exc:
            print(f"annotate-post: {path}: {exc}", file=sys.stderr)
            worst = max(worst, ExitCode.USAGE, key=lambda c: _SEVERITY[c])
            continue
        counts, findings = verify_post(post)
        report = {
            "tool": "annotate-post",
            "schema": 1,
            "post": stem,
            "day": post.day,
            "post_sha256": hashlib.sha256(raw).hexdigest(),
            "sources": sources,
            "counts": counts,
            "findings": [asdict(f) for f in findings],
        }
        (out_dir / f"{stem}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        posts_ok += 1
        for key in counts:
            totals[key] += counts[key]
        print(
            f"{stem}: {counts['ruby']} ruby, {counts['regions']} regions, "
            f"{counts['ok']} ok ({counts['ok_contextual']} contextual, "
            f"{counts['ok_attested']} attested), {counts['divergent']} divergent, "
            f"{counts['low_confidence']} low_confidence"
        )
        for f in findings:
            print(finding_line(f))
        if counts["divergent"]:
            worst = max(worst, ExitCode.DIVERGENT, key=lambda c: _SEVERITY[c])
        elif counts["low_confidence"]:
            worst = max(worst, ExitCode.LOW_CONFIDENCE, key=lambda c: _SEVERITY[c])
    print(
        f"TOTAL {posts_ok} post{'' if posts_ok == 1 else 's'}: "
        f"{totals['ruby']} ruby, {totals['regions']} regions, "
        f"{totals['ok_contextual']} ok_contextual, {totals['ok_attested']} ok_attested, "
        f"{totals['divergent']} divergent, {totals['low_confidence']} low_confidence"
    )
    return int(worst)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
