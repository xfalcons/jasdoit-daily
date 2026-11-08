#!/usr/bin/env python3
"""Manual-period TTS: render a post's Japanese body to mp3 via edge-tts.

Usage:
    uv run --with edge-tts python tools/tts-post.py source/_posts/day-001-*.md

Reading-locked input: each <ruby> element is replaced by its own <rt> reading,
so the voice never guesses a kanji reading. Kanji left outside ruby coverage
are printed for a human scan before listening.

Output: audio-local/day-XXX.mp3, same key convention as the engine-period R2
objects (agent-loop-design.md section 4), so engine files replace these 1:1.
"""
import asyncio
import re
import sys
from pathlib import Path

import edge_tts

# Same Neural voice family the engine will use on Azure TTS.
VOICE = "ja-JP-NanamiNeural"

KANJI_RE = re.compile(r"[㐀-鿿々]")


def extract_japanese(post: str) -> tuple[int, str, list[str]]:
    day = int(re.search(r"^day:\s*(\d+)", post, re.M).group(1))
    section = post.split("## 日文文章", 1)[1]
    section = section.split("\n", 1)[1].split("\n## ", 1)[0]
    lines = [
        line.strip()
        for line in section.splitlines()
        if line.strip() and not line.lstrip().startswith((">", "#"))
    ]
    text = re.sub(r"<rp>.*?</rp>", "", "\n".join(lines))
    spoken = re.sub(r"<ruby>.*?<rt>(.*?)</rt>.*?</ruby>", r"\1", text)
    spoken = re.sub(r"<[^>]+>", "", spoken)
    return day, spoken, sorted(set(KANJI_RE.findall(spoken)))


async def render(day: int, spoken: str, out_dir: Path) -> Path:
    out = out_dir / f"day-{day:03d}.mp3"
    await edge_tts.Communicate(spoken, VOICE).save(out)
    return out


def main() -> None:
    day, spoken, unannotated = extract_japanese(Path(sys.argv[1]).read_text())
    out_dir = Path(__file__).resolve().parent.parent / "audio-local"
    out_dir.mkdir(exist_ok=True)
    out = asyncio.run(render(day, spoken, out_dir))
    print(f"{out} ({len(spoken)} chars)")
    print(spoken)
    if unannotated:
        print(f"UNANNOTATED KANJI (TTS still guesses): {''.join(unannotated)}")
    else:
        print("all kanji reading-locked")


if __name__ == "__main__":
    main()
