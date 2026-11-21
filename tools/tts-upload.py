#!/usr/bin/env python3
"""Stage 5 of the daily loop: reading-locked TTS render + R2 upload.

Usage:
    python tools/tts-upload.py POST [--dry-run] [--out-dir DIR]

Spoken text comes from the shared gate-aligned extractor, so HTML entities
are decoded and blockquote/fence/comment lines never reach the voice.
Live mode first checks the R2 key and reuses an existing object instead of
re-rendering (crash resume, agent-loop-design.md section 4). Dry-run renders
to a temp dir (or --out-dir), uploads nothing, and still prints the
would-be key and public URL. Missing R2 credentials are an error only in
live mode. The last stdout line is one META json object for the
orchestrator; credentials are never printed.

Exit codes: 0 rendered/reused/uploaded, 1 usage error, 4 render or upload
failure (retryable).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loop_support

RETRYABLE = 4


def r2_client():
    import boto3

    required = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT", "R2_BUCKET")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print("tts-upload: R2 credentials missing: " + ",".join(missing), file=sys.stderr)
        return None
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def key_exists(client, day: int) -> bool:
    try:
        client.head_object(Bucket=os.environ["R2_BUCKET"], Key=loop_support.audio_key(day))
        return True
    except client.exceptions.ClientError:
        return False


def render_mp3(spoken: str, day: int, out_dir: Path) -> Path:
    import edge_tts

    out = out_dir / f"day-{day:03d}.mp3"
    asyncio.run(edge_tts.Communicate(spoken, loop_support.VOICE).save(out))
    return out


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Stage 5 TTS render + R2 upload")
    parser.add_argument("post", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    loop_support.load_dotenv(Path(__file__).resolve().parent.parent)
    try:
        text = args.post.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"tts-upload: cannot read {args.post}: {exc}", file=sys.stderr)
        return 1
    try:
        result = loop_support.extract_spoken(text)
    except (ValueError, loop_support.EmptySpokenText) as exc:
        print(f"tts-upload: {args.post}: {exc}", file=sys.stderr)
        return 1

    key = loop_support.audio_key(result.day)
    url = loop_support.public_audio_url(os.environ.get("R2_PUBLIC_BASE"), result.day)
    unannotated = "".join(result.unannotated_kanji)
    print(f"spoken: {len(result.spoken)} chars, key {key}, url {url}")
    print("UNANNOTATED KANJI (TTS still guesses): " + unannotated if unannotated
          else "all kanji reading-locked")

    def meta(*, reused: bool, uploaded: bool, rendered: Path | None) -> None:
        loop_support.print_meta({
            "day": result.day, "key": key, "url": url, "chars": len(result.spoken),
            "unannotated": unannotated, "reused": reused, "uploaded": uploaded,
            "rendered": str(rendered) if rendered else None,
        })

    if not args.dry_run:
        client = r2_client()
        if client is None:
            return RETRYABLE
        if key_exists(client, result.day):
            print(f"reusing existing object {key}")
            meta(reused=True, uploaded=False, rendered=None)
            return 0
        out_dir = args.out_dir if args.out_dir else Path(tempfile.mkdtemp(prefix="tts-upload-"))
        try:
            out = render_mp3(result.spoken, result.day, out_dir)
            client.upload_file(
                str(out), os.environ["R2_BUCKET"], key, ExtraArgs={"ContentType": "audio/mpeg"}
            )
        except Exception as exc:
            print(f"tts-upload: render/upload failed: {exc}", file=sys.stderr)
            return RETRYABLE
        print(f"uploaded {key} ({out.stat().st_size} bytes)")
        meta(reused=False, uploaded=True, rendered=out)
        return 0

    out_dir = args.out_dir if args.out_dir else Path(tempfile.mkdtemp(prefix="tts-upload-"))
    try:
        out = render_mp3(result.spoken, result.day, out_dir)
    except Exception as exc:
        print(f"tts-upload: render failed: {exc}", file=sys.stderr)
        return RETRYABLE
    print(f"rendered {out} ({out.stat().st_size} bytes), upload skipped (dry-run)")
    meta(reused=False, uploaded=False, rendered=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
