#!/usr/bin/env python3
"""Stage 5 of the daily loop: reading-locked TTS render + R2 upload.

Usage:
    python tools/tts-upload.py POST [--dry-run] [--out-dir DIR]

Spoken text comes from the shared gate-aligned extractor, so HTML entities
are decoded and blockquote/fence/comment lines never reach the voice.
Live mode reuses an R2 object only when it exists, has non-zero length and
carries the spoken-text sha256 as object metadata; a mismatched or empty
object re-renders and overwrites (crash resume, agent-loop-design.md
section 4). Only 404/NoSuchKey heads read as "absent"; 403/5xx/connect
failures stay retryable errors. Render and upload each get one independent
retry, and a render written into --out-dir is reused across the
orchestrator's tool-level retry via its sha256 sidecar. Dry-run renders to
a temp dir (or --out-dir), uploads nothing, and still prints the would-be
key and public URL. Missing R2 credentials are an error only in live mode.
The last stdout line is one META json object for the orchestrator;
credentials are never printed.

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
MISSING_CODES = {"404", "NoSuchKey", "NotFound"}


class TtsError(Exception):
    """Live-mode failure that survived its phase's one retry."""


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


def head_state(client, day: int, spoken_sha: str) -> str:
    """'match' the object is reusable, 'absent', 'stale' (present but zero
    length or wrong spoken hash), or 'error' for any non-404 head failure."""
    try:
        head = client.head_object(Bucket=os.environ.get("R2_BUCKET", ""),
                                  Key=loop_support.audio_key(day))
    except Exception as exc:
        response = getattr(exc, "response", None)
        code = response.get("Error", {}).get("Code", "") if isinstance(response, dict) else ""
        if code in MISSING_CODES:
            return "absent"
        print(f"tts-upload: head_object failed ({code or type(exc).__name__}): {exc}",
              file=sys.stderr)
        return "error"
    length = head.get("ContentLength") or 0
    metadata = head.get("Metadata") or {}
    if length > 0 and metadata.get("spoken-sha256") == spoken_sha:
        return "match"
    return "stale"


def render_mp3(spoken: str, day: int, out_dir: Path) -> Path:
    import edge_tts

    out = out_dir / f"day-{day:03d}.mp3"
    asyncio.run(edge_tts.Communicate(spoken, loop_support.VOICE).save(out))
    return out


def render_with_cache(day: int, spoken: str, out_dir: Path, spoken_sha: str) -> Path:
    """Render once per spoken text: an existing mp3 whose sidecar hash still
    matches the spoken text skips the render entirely."""
    mp3 = out_dir / f"day-{day:03d}.mp3"
    sidecar = out_dir / f"day-{day:03d}.mp3.sha256"
    if (mp3.is_file() and sidecar.is_file() and mp3.stat().st_size > 0
            and sidecar.read_text(encoding="utf-8").strip() == spoken_sha):
        print(f"reusing rendered {mp3}")
        return mp3
    for attempt in (1, 2):
        try:
            out = render_mp3(spoken, day, out_dir)
            sidecar.write_text(spoken_sha + "\n", encoding="utf-8")
            return out
        except Exception as exc:
            print(f"tts-upload: render attempt {attempt} failed: {exc}", file=sys.stderr)
    raise TtsError("render failed after retry")


def upload_with_retry(client, day: int, out: Path, spoken_sha: str) -> None:
    for attempt in (1, 2):
        try:
            client.upload_file(
                str(out), os.environ["R2_BUCKET"], loop_support.audio_key(day),
                ExtraArgs={"ContentType": "audio/mpeg",
                           "Metadata": {"spoken-sha256": spoken_sha}},
            )
            return
        except Exception as exc:
            print(f"tts-upload: upload attempt {attempt} failed: {exc}", file=sys.stderr)
    raise TtsError("upload failed after retry")


def publish_audio(client, day: int, spoken: str, out_dir: Path) -> tuple[str, Path | None]:
    spoken_sha = loop_support.sha256_hex(spoken)
    state = head_state(client, day, spoken_sha)
    if state == "error":
        raise TtsError("head_object failed")
    if state == "match":
        return "reused", None
    out = render_with_cache(day, spoken, out_dir, spoken_sha)
    upload_with_retry(client, day, out, spoken_sha)
    return "uploaded", out


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

    if not args.dry_run and not url:
        print("tts-upload: R2_PUBLIC_BASE is not set; the object can upload but no "
              "public URL exists (engine live runs flag tts_failed)", file=sys.stderr)
    out_dir = args.out_dir if args.out_dir else Path(tempfile.mkdtemp(prefix="tts-upload-"))

    if not args.dry_run:
        try:
            client = r2_client()
        except Exception as exc:
            print(f"tts-upload: R2 client construction failed: {exc}", file=sys.stderr)
            return RETRYABLE
        if client is None:
            return RETRYABLE
        try:
            outcome, rendered = publish_audio(client, result.day, result.spoken, out_dir)
        except TtsError as exc:
            print(f"tts-upload: {exc}", file=sys.stderr)
            return RETRYABLE
        if outcome == "reused":
            print(f"reusing existing object {key}")
            meta(reused=True, uploaded=False, rendered=None)
        else:
            print(f"uploaded {key} ({rendered.stat().st_size} bytes)")
            meta(reused=False, uploaded=True, rendered=rendered)
        return 0

    try:
        out = render_with_cache(result.day, result.spoken, out_dir,
                                loop_support.sha256_hex(result.spoken))
    except TtsError as exc:
        print(f"tts-upload: {exc}", file=sys.stderr)
        return RETRYABLE
    print(f"rendered {out} ({out.stat().st_size} bytes), upload skipped (dry-run)")
    meta(reused=False, uploaded=False, rendered=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
