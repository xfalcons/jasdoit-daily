#!/usr/bin/env python3
"""Stage 2 of the daily loop: draft generation via OpenRouter, and the one
reading revision the orchestrator runs when annotate reports divergent ruby.

Usage:
    python tools/generate-post.py --day N --out PATH [--fewshot PATH] [--digest PATH]
    python tools/generate-post.py --revise DRAFT --findings REPORT --out PATH

Prompt = AGENTS-teacher persona + the day's theme-table row + hard
requirements + one few-shot gold post + the avoid-repeat corpus digest
(amendment 17). The model writes the whole post file; the orchestrator
replaces every front-matter field except `description`. GEN_FAKE_PATH
copies a fixture post instead of calling the API (tests, dry-runs; no
network, no key needed).

--revise is the second mode and excludes --day/--fewshot/--digest. Its
prompt carries the whole draft plus every finding of the annotate-post
report with its evidence, and asks for corrected <rt> readings only. The
revision keeps the draft's own file ending, because the orchestrator
compares the two byte for byte outside <rt> readings. REVISE_FAKE_PATH
mirrors GEN_FAKE_PATH for it.

Exit codes: 0 draft written, 1 usage error, 4 transport/model error.
The last stdout line is one META json object.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loop_support

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "z-ai/glm-5.3"
TRANSPORT = 4


class Parser(argparse.ArgumentParser):
    def error(self, message):
        print(f"generate-post: {message}", file=sys.stderr)
        raise SystemExit(1)


def build_prompt(persona: str, row: loop_support.ThemeRow, day: int, is_sunday: bool,
                 fewshot: str, digest_lines: list[str]) -> str:
    lo, hi = loop_support.length_band(day)
    target = f"{hi - 60}–{hi - 20}" if lo is None else f"{(lo + hi) // 2 - 15}–{(lo + hi) // 2}"
    writing = (
        f"\n- 今天是週日：文末必須有「## 週日寫作提示」section，5–8 條編號寫作提示（`1. ` 開頭），"
        if is_sunday else "\n- 今天不是週日：不要寫作提示 section。"
    )
    return f"""{persona}

# 任務：產出 Day {day} 的日語學習貼文（完整 Markdown 檔案）

## 主題表 row（不可換題）
- Day {day}：{row.title_ja}
- 內容重點：{row.focus}
- 核心句：{row.core_sentence}

## 硬性要求
- 輸出「一個完整 Markdown 檔案」，第一行是 `---` front-matter，其中只需正確填 `description:`（約 50 字繁體中文，說明本篇內容）；其他 front-matter 欄位引擎會重寫，內容可留最簡。
- 導言 1–2 句繁體中文＋`<!-- more -->`。
- `## 日文文章：{row.title_ja}`：正文 {lo}–{hi} 字（reading-locked 計數；目標 {target} 字，超過 {hi} 字會被退件），每個漢字一律 `<ruby>漢字<rp>（</rp><rt>かんじ</rt><rp>）</rp></ruby>` 標音；ruby 的 base（`<ruby>` 到 `<rp>` 之間）只能是一個漢字詞（可含詞內假名，如「当たり前」），助詞、標點、其他詞一律放在 ruby 之外，`<rt>` 內只寫該詞的平假名讀音；每段日文後緊跟 blockquote `<span lang="zh-Hant" data-translation>…</span>` 繁體中譯。
- 日文正文之外（標題、導言、單字卡、文法、寫作提示）的解說用繁體中文。
- `## 3 個重點單字`：三張 `### 1. `～`### 3. ` 編號卡片，各含 **詞性**、**意思**、**常見搭配**（至少 2 項）、**例句**（日文＋中譯 span）。
- `## 1 個重點文法`：一個 `### ` 文法點，含 **核心語意**、**接續**、**例句**（日文＋中譯）、**比較與注意**。
- 最後一行是授權標示：必須同時出現 `JMdict` 與 `EDRDG` 字樣。
- 難度 N3：用字與句型貼近 JLPT N3，不出戲、不超綱。
{writing}
## 不可重複（近篇 digest）
{chr(10).join(digest_lines) or "-（無）"}

## Few-shot 範例（結構與格式模仿，內容不可抄）
<<<GOLD
{fewshot}
GOLD>>>

現在輸出 Day {day} 的完整 Markdown 檔案，不要任何前言或 code fence。"""


def strip_outer_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    if len(lines) >= 2:
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
    return "\n".join(lines) + "\n"


def call_openrouter(model: str, prompt: str, api_key: str) -> tuple[str, dict | None]:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=240) as resp:
        payload = json.load(resp)
    return loop_support.completion_content(payload), payload.get("usage")


def format_finding(finding: dict) -> str:
    ev = finding["evidence"]

    def show(value):
        if isinstance(value, list):
            value = "、".join(value)
        return value or "-"

    return (f"- L{finding['line']}〈{finding['surface']}〉目前 rt={show(finding.get('rt'))}："
            f"{finding['verdict']}（{finding['reason']}）assembled={show(ev['assembled'])} "
            f"unidic={show(ev['unidic'])} tokens={show(ev['tokens'])} "
            f"jmdict={show(ev['jmdict'])} pykakasi={show(ev['pykakasi'])}")


def build_revision_prompt(draft: str, findings: list[dict]) -> str:
    return f"""你是日語讀音校訂者。讀音檢查在下面這篇 Markdown 貼文標記了 {len(findings)} 處 ruby 讀音。

唯一的任務：只更正被標記的 ruby 元素的 `<rt>` 讀音，其他任何字都不改。

- 其他 finding：只改該處的 `<rt>`，寫平假名，不可出現標點或漢字。`unidic` 是這個詞在句中的讀音（片假名，請轉成平假名），`jmdict` 列出的讀法也可以用，`tokens` 是切分讀音的提示。
- 標記為 malformed_ruby 的行沒有讀音證據，只修標記結構：把該行的 ruby 修成完整格式 `<ruby>漢字<rp>（</rp><rt>かんじ</rt><rp>）</rp></ruby>`。ruby 的 base 只能是一個漢字詞（可含詞內假名），不可含標點、助詞或其他詞：把多餘的文字移到 ruby 之外，`<rt>` 只留該詞的讀音。
- 沒被標記的 `<rt>`，以及所有 `<ruby>`、`<rp>` 標記、正文、標點、空白、換行、front-matter，逐字保留；不增刪行、不重新排版。

## 標記的讀音（每行一處）
{chr(10).join(format_finding(f) for f in findings)}

## 待校訂貼文
<<<DRAFT
{draft}
DRAFT>>>

現在輸出校訂後的完整 Markdown 檔案，不要任何前言或 code fence。"""


def parse_args(argv):
    parser = Parser(description="Stage 2 draft generation, or its one reading revision")
    parser.add_argument("--day", type=int, default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fewshot", type=Path, default=None)
    parser.add_argument("--digest", type=Path, default=None)
    parser.add_argument("--revise", type=Path, default=None,
                        help="draft whose flagged <rt> readings to correct; "
                             "excludes --day, --fewshot and --digest")
    parser.add_argument("--findings", type=Path, default=None,
                        help="annotate-post report that feeds --revise")
    args = parser.parse_args(argv)
    if args.revise is not None:
        if args.day is not None or args.fewshot or args.digest:
            parser.error("--revise cannot be combined with --day, --fewshot or --digest")
        if args.findings is None:
            parser.error("--revise requires --findings")
    elif args.findings is not None:
        parser.error("--findings is only valid with --revise")
    elif args.day is None:
        parser.error("--day is required")
    return args


def match_file_tail(draft: str, revised: str) -> str:
    """Give the revision the draft's own closing newline. Chat completions
    often drop it or pad it with blank lines, and the orchestrator compares
    the two byte for byte outside <rt> readings. The last line itself stays
    the model's, so a real edit there still fails that comparison."""
    ending = "\n" if draft.endswith("\n") else ""
    return revised.rstrip("\n") + ending


def revision_inputs(draft_path: Path, report_path: Path) -> tuple[str, str] | None:
    """(prompt, draft), or None once the usage error is printed."""
    try:
        draft = draft_path.read_text(encoding="utf-8")
        findings = json.loads(report_path.read_text(encoding="utf-8"))["findings"]
        if not findings:
            print("generate-post: the findings report carries no findings", file=sys.stderr)
            return None
        return build_revision_prompt(draft, findings), draft
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"generate-post: cannot read the draft or the findings report: {exc}",
              file=sys.stderr)
        return None


def complete(fake_key: str, prompt: str | None, out: Path, meta: dict,
             shape=strip_outer_fence) -> int:
    """With the fake env set, copy its fixture; otherwise answer `prompt` with
    one model call and turn the content into a draft with `shape`. Either way
    the draft lands at `out` behind one META line."""
    fake = os.environ.get(fake_key)
    if fake:
        try:
            draft = Path(fake).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"generate-post: cannot read {fake_key} {fake}: {exc}", file=sys.stderr)
            return TRANSPORT
        model = None
        usage = None
    else:
        model = os.environ.get("GEN_MODEL") or DEFAULT_MODEL
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            print("generate-post: OPENROUTER_API_KEY not set", file=sys.stderr)
            return TRANSPORT
        try:
            content, usage = call_openrouter(model, prompt, api_key)
        except (urllib.error.URLError, ValueError, KeyError, json.JSONDecodeError, OSError) as exc:
            print(f"generate-post: model/transport error: {exc}", file=sys.stderr)
            return TRANSPORT
        draft = shape(content)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(draft, encoding="utf-8")
    print(f"draft written: {out} ({len(draft)} chars)")
    cost = usage.get("cost") if isinstance(usage, dict) else None
    loop_support.print_meta({
        **meta, "model": model, "usage": usage, "cost_usd": cost, "fake": bool(fake),
    })
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    loop_support.load_dotenv(repo_root)

    if args.revise is not None:
        revision = revision_inputs(args.revise, args.findings)
        if revision is None:
            return 1
        prompt, draft = revision
        return complete("REVISE_FAKE_PATH", prompt, args.out, {"stage": "revise"},
                        shape=lambda content: match_file_tail(draft, strip_outer_fence(content)))

    theme_path = repo_root / "aidd-docs" / "references" / "japanese-theme-rotation.md"
    try:
        theme = theme_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"generate-post: cannot read theme table: {exc}", file=sys.stderr)
        return 1
    row = loop_support.parse_theme_row(theme, args.day)
    if row is None:
        print(f"generate-post: no filled theme row for day {args.day}", file=sys.stderr)
        return 1
    anchor = loop_support.theme_anchor(theme)
    from datetime import date, timedelta

    day_date = date(*anchor) + timedelta(days=args.day - 1)
    digest_lines = []
    if args.digest and args.digest.is_file():
        for post in json.loads(args.digest.read_text(encoding="utf-8"))["posts"]:
            digest_lines.append(
                f"- Day {post['day']}《{post['title']}》core：{post['core']}"
            )
    fewshot = args.fewshot.read_text(encoding="utf-8") if args.fewshot else ""

    prompt = None
    if not os.environ.get("GEN_FAKE_PATH"):
        persona_path = repo_root / "AGENTS-teacher.md"
        try:
            persona = persona_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"generate-post: cannot read persona {persona_path}: {exc}", file=sys.stderr)
            return TRANSPORT
        prompt = build_prompt(persona, row, args.day, day_date.weekday() == 6, fewshot, digest_lines)
    return complete("GEN_FAKE_PATH", prompt, args.out, {"stage": "generate", "day": args.day})


if __name__ == "__main__":
    sys.exit(main())
