#!/usr/bin/env python3
"""Stage 4 cross-model verifier via OpenRouter.

Usage:
    python tools/verify-post.py POST [--digest PATH]

Condensed form of the manual day-15..18 review prompts, plus the
N3-appropriateness item (Ruling A: a completed verifier PASS closes the
n3_vocabulary seam for this run) and the repetition item against the
previous five titles/core sentences (amendment 17). The response's first
line must be `VERDICT: PASS|FLAG|FAIL`; anything unparseable is a model
error (exit 4), never a guessed verdict. VERIFY_FAKE_PATH reads a fixture
response instead of calling the API (tests, dry-runs).

Exit codes: 0 PASS, 1 usage error, 2 FAIL, 3 FLAG, 4 transport/model error.
The last stdout line is one META json object.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loop_support

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-6.1-sol"
TRANSPORT = 4
VERDICT_LINE_RE = re.compile(r"VERDICT:\s*(PASS|FLAG|FAIL)\s*")
EXIT_OF_VERDICT = {"PASS": 0, "FLAG": 3, "FAIL": 2}


def parse_verdict(response_text: str) -> str | None:
    """The first non-empty line must be exactly `VERDICT: PASS|FLAG|FAIL`;
    any trailing commentary makes the response unparseable (exit 4)."""
    for line in response_text.splitlines():
        if line.strip():
            m = VERDICT_LINE_RE.fullmatch(line.strip())
            return m.group(1) if m else None
    return None


def build_prompt(post_text: str, digest_lines: list[str]) -> str:
    digest = "\n".join(digest_lines) or "-（無）"
    return f"""你是日語 N3 教材的審校者（不同於產文的模型）。審下面這篇 Day 貼文，只看內容品質。

檢查項目：
1. 讀音與標音：ruby 讀音是否正確、自然。
2. N3 適切性：用字與句型是否符合 JLPT N3（不超綱、不過易）；不合適要指出。
3. 重複度：與近五篇的主題/核心句是否重複（digest 如下）。
4. 中譯：每段中譯是否正確、自然（繁體中文）。
5. 結構與長度：章節齊全、日文正文長度合理、內容連貫。

輸出格式（嚴格）：
第一行只能是 `VERDICT: PASS`、`VERDICT: FLAG` 或 `VERDICT: FAIL` 之一。
PASS＝無重大問題可自動發布；FLAG＝可疑需人工 3–5 分鐘 review；FAIL＝重大錯誤不可發布。
之後逐條列出 findings（編號、位置、問題、建議）；無問題寫「無重大 finding」。

近五篇 digest：
{digest}

待審貼文：
<<<POST
{post_text}
POST>>>"""


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


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Stage 4 cross-model verifier")
    parser.add_argument("post", type=Path)
    parser.add_argument("--digest", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    loop_support.load_dotenv(Path(__file__).resolve().parent.parent)
    try:
        post_text = args.post.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"verify-post: cannot read {args.post}: {exc}", file=sys.stderr)
        return 1
    digest_lines = []
    if args.digest and args.digest.is_file():
        for post in json.loads(args.digest.read_text(encoding="utf-8"))["posts"][-5:]:
            digest_lines.append(
                f"- Day {post['day']}《{post['title']}》core：{post['core']}"
            )

    fake = os.environ.get("VERIFY_FAKE_PATH")
    if fake:
        try:
            response = Path(fake).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"verify-post: cannot read VERIFY_FAKE_PATH {fake}: {exc}", file=sys.stderr)
            return TRANSPORT
        model = None
        usage = None
    else:
        model = os.environ.get("VERIFY_MODEL") or DEFAULT_MODEL
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            print("verify-post: OPENROUTER_API_KEY not set", file=sys.stderr)
            return TRANSPORT
        try:
            response, usage = call_openrouter(model, build_prompt(post_text, digest_lines), api_key)
        except (urllib.error.URLError, ValueError, KeyError, json.JSONDecodeError, OSError) as exc:
            print(f"verify-post: model/transport error: {exc}", file=sys.stderr)
            return TRANSPORT

    print(response.strip())
    verdict = parse_verdict(response)
    if verdict is None:
        print("verify-post: no VERDICT: PASS|FLAG|FAIL on the first response line", file=sys.stderr)
        return TRANSPORT
    cost = usage.get("cost") if isinstance(usage, dict) else None
    findings = [ln for ln in response.splitlines() if re.match(r"^\s*\d+[.、]", ln)]
    loop_support.print_meta({
        "stage": "verify", "model": model, "verdict": verdict, "usage": usage,
        "cost_usd": cost, "findings": len(findings), "fake": bool(fake),
    })
    return EXIT_OF_VERDICT[verdict]


if __name__ == "__main__":
    sys.exit(main())
