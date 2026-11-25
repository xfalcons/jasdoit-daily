#!/usr/bin/env python3
"""Stage 4 cross-model verifier via OpenRouter.

Usage:
    python tools/verify-post.py POST [--digest PATH]

Condensed form of the manual day-15..18 review prompts, plus the
N3-appropriateness item (Ruling A: a completed verifier PASS closes the
n3_vocabulary seam for this run), the repetition item against the
previous five titles/core sentences (amendment 17), and the naturalness
item (2026-11-24: would a Japanese native actually use this sentence?).
The response's first line must be `VERDICT: PASS|FLAG|FAIL`; anything
unparseable is a model error (exit 4), never a guessed verdict.
VERIFY_FAKE_PATH reads a fixture response instead of calling the API
(tests, dry-runs).

The orchestrator runs this tool once per configured verifier
(VERIFY_MODEL, then optional VERIFY_MODEL2), passing --model each time;
a bare invocation still resolves VERIFY_MODEL from the environment.

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
2. 自然性：每個日文句子是否是日本人實際會說、會寫的說法（語感、詞語搭配、語域）。中文直譯腔、生硬湊句、只有課本才出現的假句子都要指出，並建議自然的講法。
3. N3 適切性：用字與句型是否符合 JLPT N3（不超綱、不過易）；不合適要指出。
4. 重複度：與近五篇的主題/核心句是否重複（digest 如下）。
5. 中譯：每段中譯是否正確、自然（繁體中文）。
6. 結構與長度：章節齊全、日文正文長度合理、內容連貫。

輸出格式（嚴格）：
第一行只能是 `VERDICT: PASS`、`VERDICT: FLAG` 或 `VERDICT: FAIL` 之一。
之後逐條列出 findings（編號、位置、問題、建議）；無問題寫「無重大 finding」。

verdict 判準（照此分級，不要自行加嚴）：
- FAIL＝不可發布的重大錯誤：ruby 讀音錯誤、明確文法錯誤、誤譯或漏譯改變語意、結構缺漏、與近五篇主題或核心句重複。
- FLAG＝會讓學習者學到錯誤內容或明顯誤導的問題：中文詞誤標日語讀音、教學解說與例句或常見辭典用法矛盾、同形異義詞比較未標明語言而造成混淆、例句示範錯誤用法。判準是「讀者照著學會學錯」，不是「寫得不夠好」。
- PASS＝可自動發布。以下一律 PASS，只寫進 findings 供人工參考：JLPT 級別疑慮（JLPT 無官方詞表，稍進階詞彙只標「延伸詞彙」建議）、語感或更自然講法建議、稱謂語域補充、字數小幅差異、中譯可更精準。findings 多條不影響 PASS。

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
    parser.add_argument("--model", default=None,
                        help="model slug for this call; overrides VERIFY_MODEL")
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

    # Resolved for both modes so a fake run can still assert which model a
    # --model call would use; only the real path needs the key.
    model = args.model or os.environ.get("VERIFY_MODEL") or DEFAULT_MODEL
    fake = os.environ.get("VERIFY_FAKE_PATH")
    if fake:
        try:
            response = Path(fake).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"verify-post: cannot read VERIFY_FAKE_PATH {fake}: {exc}", file=sys.stderr)
            return TRANSPORT
        usage = None
    else:
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
