"""Shared helpers for the daily-loop engine tools.

Owns the .env loader (real environment variables win over .env values),
the R2 audio key/URL builder (one function, spec section 4), the
gate-aligned spoken-text extractor (amendment 18), theme-table parsing,
and the model-vendor preflight (amendment 16). Stage policy lives in
daily-loop.py only; nothing here imports a stage tool's policy.
"""
from __future__ import annotations

import hashlib
import html
import importlib.util
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

R2_PREFIX = "audio/"
VOICE = "ja-JP-NanamiNeural"
ANCHOR_RE = re.compile(r"Day\s*1\s*=\s*(\d{4})-(\d{2})-(\d{2})")
DAY_FM_RE = re.compile(r"^day:\s*(\d+)\s*$", re.M)


class EmptySpokenText(ValueError):
    pass


def load_dotenv(repo_root: Path) -> None:
    """Populate os.environ from <repo_root>/.env without overwriting real
    environment variables; never prints or logs values."""
    path = repo_root / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        # Empty values stay unset so callers' `or DEFAULT` resolution wins.
        if key and value and key not in os.environ:
            os.environ[key] = value


def audio_key(day: int) -> str:
    return f"{R2_PREFIX}day-{day:03d}.mp3"


def public_audio_url(base: str | None, day: int) -> str | None:
    if not base:
        return None
    return base.rstrip("/") + "/" + audio_key(day)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def completion_content(payload: object) -> str:
    """Validate an OpenRouter completion payload's shape and return its text.
    Every malformation (no choices, wrong types, empty content) raises
    ValueError so callers map it to the controlled transport exit."""
    if not isinstance(payload, dict):
        raise ValueError("completion response is not a JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("completion response has no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty completion content")
    return content


def print_meta(payload: dict) -> None:
    print("META " + json.dumps(payload, ensure_ascii=False, sort_keys=True))


def parse_meta_line(line: str) -> dict | None:
    if not line.startswith("META "):
        return None
    try:
        return json.loads(line[len("META "):])
    except json.JSONDecodeError:
        return None


def vendor_of(model_slug: str) -> str | None:
    if "/" not in model_slug:
        return None
    head = model_slug.split("/", 1)[0].strip()
    return head or None


def vendors_distinct(gen_slug: str, verify_slug: str) -> bool:
    gen, verify = vendor_of(gen_slug), vendor_of(verify_slug)
    return gen is not None and verify is not None and gen != verify


@dataclass(frozen=True)
class ThemeRow:
    day: int
    title_ja: str
    focus: str
    core_sentence: str


def parse_theme_row(theme_text: str, day: int) -> ThemeRow | None:
    """The per-day row `| N | 日文標題 | 內容重點 | 核心句 |` of the rotation
    table. Unfilled phases have no row for the day, which select reads as
    theme_row_missing."""
    row_re = re.compile(
        r"^\|\s*" + str(day) + r"\s*\|([^|]+)\|([^|]+)\|([^|]+)\|\s*$", re.M
    )
    m = row_re.search(theme_text)
    if not m:
        return None
    cells = [c.strip() for c in m.groups()]
    if not all(cells) or any("待填" in c for c in cells):
        return None
    return ThemeRow(day, *cells)


def theme_anchor(theme_text: str) -> tuple[int, int, int]:
    m = ANCHOR_RE.search(theme_text)
    if not m:
        raise ValueError("no 'Day 1 = YYYY-MM-DD' anchor in the theme table")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def phase_of_day(day: int) -> int:
    return (day - 1) // 10 + 1


def length_band(day: int) -> tuple[int | None, int]:
    if day <= 30:
        return 250, 300
    if day <= 70:
        return 300, 400
    return None, 450


@dataclass(frozen=True)
class SpokenText:
    day: int
    spoken: str
    unannotated_kanji: list[str]


_GATE = None


def _gate():
    """gate-post.py loaded without running it; it is stdlib-only at import."""
    global _GATE
    if _GATE is None:
        path = Path(__file__).resolve().parent / "gate-post.py"
        spec = importlib.util.spec_from_file_location("gate_post_shared", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _GATE = module
    return _GATE


def extract_spoken(post_text: str) -> SpokenText:
    """Reading-locked spoken text of the 日文文章 section with the gate's own
    visibility rules (fences, comments, blockquotes and their lazy
    continuations, headings excluded), ruby replaced by its <rt> reading,
    HTML entities decoded, then the kanji the voice would still guess."""
    g = _gate()
    text = post_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("no opening --- for front-matter")
    try:
        fm_end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError("no closing --- for front-matter")
    m = DAY_FM_RE.search("\n".join(lines[1:fm_end]))
    if not m:
        raise ValueError("no day: in front-matter")
    day = int(m.group(1))

    sections, _, _ = g.detect_sections(lines[fm_end + 1:], offset=fm_end + 1)
    japanese = [s for s in sections if s.kind == "japanese"]
    if len(japanese) != 1:
        raise ValueError(f"expected 1 日文文章 section, found {len(japanese)}")
    kept = [
        sl for sl in japanese[0].lines
        if (not sl.hidden and not sl.quoted and sl.text.strip()
            and not g.HEADING_LINE_RE.match(sl.text))
    ]
    spoken = html.unescape("\n".join(g.ruby_to_reading(sl.text) for sl in kept))
    if not spoken.strip():
        raise EmptySpokenText("spoken text is empty")
    return SpokenText(day, spoken, sorted(set(g.KANJI_RE.findall(spoken))))
