#!/usr/bin/env python3
"""Manual-period TTS: render a post's Japanese body to mp3 via edge-tts.

Usage:
    uv run --with edge-tts python tools/tts-post.py source/_posts/day-001-*.md

Reading-locked input: each <ruby> element is replaced by its own <rt> reading,
so the voice never guesses a kanji reading; extraction is shared with the
engine (tools/loop_support.py, gate visibility rules and entity decoding).
Kanji left outside ruby coverage are printed for a human scan before
listening.

Output: audio-local/day-XXX.mp3, same key convention as the engine-period R2
objects (agent-loop-design.md section 4), so engine files replace these 1:1.
"""
import asyncio
import sys
from pathlib import Path

import edge_tts

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loop_support

# Same Neural voice family the engine will use on Azure TTS.
VOICE = loop_support.VOICE


def extract_japanese(post: str) -> tuple[int, str, list[str]]:
    result = loop_support.extract_spoken(post)
    return result.day, result.spoken, result.unannotated_kanji


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
