#!/usr/bin/env python3
"""Stage 2 of the daily loop: draft generation via OpenRouter.

Usage:
    python tools/generate-post.py --day N --out PATH [--fewshot PATH] [--digest PATH]

Prompt = AGENTS-teacher persona + the day's theme-table row + hard
requirements + one few-shot gold post + the avoid-repeat corpus digest
(amendment 17). The model writes the whole post file; the orchestrator
replaces every front-matter field except `description`. GEN_FAKE_PATH
copies a fixture post instead of calling the API (tests, dry-runs; no
network, no key needed).

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


def build_prompt(persona: str, row: loop_support.ThemeRow, day: int, is_sunday: bool,
                 fewshot: str, digest_lines: list[str]) -> str:
    lo, hi = loop_support.length_band(day)
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
- `## 日文文章：{row.title_ja}`：正文 {lo}–{hi} 字（reading-locked 計數），每個漢字一律 `<ruby>漢字<rp>（</rp><rt>かんじ</rt><rp>）</rp></ruby>` 標音；每段日文後緊跟 blockquote `<span lang="zh-Hant" data-translation>…</span>` 繁體中譯。
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
    content = payload["choices"][0]["message"]["content"]
    if not content or not content.strip():
        raise ValueError("empty completion content")
    return content, payload.get("usage")


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Stage 2 draft generation")
    parser.add_argument("--day", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fewshot", type=Path, default=None)
    parser.add_argument("--digest", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    loop_support.load_dotenv(repo_root)

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

    fake = os.environ.get("GEN_FAKE_PATH")
    if fake:
        try:
            draft = Path(fake).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"generate-post: cannot read GEN_FAKE_PATH {fake}: {exc}", file=sys.stderr)
            return TRANSPORT
        model = None
        usage = None
    else:
        model = os.environ.get("GEN_MODEL", DEFAULT_MODEL)
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            print("generate-post: OPENROUTER_API_KEY not set", file=sys.stderr)
            return TRANSPORT
        persona_path = repo_root / "AGENTS-teacher.md"
        try:
            persona = persona_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"generate-post: cannot read persona {persona_path}: {exc}", file=sys.stderr)
            return TRANSPORT
        prompt = build_prompt(persona, row, args.day, day_date.weekday() == 6, fewshot, digest_lines)
        try:
            content, usage = call_openrouter(model, prompt, api_key)
        except (urllib.error.URLError, ValueError, KeyError, json.JSONDecodeError, OSError) as exc:
            print(f"generate-post: model/transport error: {exc}", file=sys.stderr)
            return TRANSPORT
        draft = strip_outer_fence(content)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(draft, encoding="utf-8")
    print(f"draft written: {args.out} ({len(draft)} chars)")
    cost = usage.get("cost") if isinstance(usage, dict) else None
    loop_support.print_meta({
        "stage": "generate", "day": args.day, "model": model, "usage": usage,
        "cost_usd": cost, "fake": bool(fake),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
