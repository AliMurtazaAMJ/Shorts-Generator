"""Shared .srt transcript cache used by all local transcoders.

Transcription is the slowest/paid step, so every backend caches its result as
an .srt file next to the source media. If the cache exists and is newer than
the source, we reuse it instead of re-transcribing.
"""
import os
import re
from pathlib import Path
from typing import Dict

from ..config import SHORTS_DOWNLOADS_DIR


def transcript_cache_path(media_path: str) -> Path:
    """Return the .srt cache path for a media file."""
    cache_dir = Path(SHORTS_DOWNLOADS_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / (Path(media_path).stem + ".srt")


def format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt_timestamp(value: str) -> float:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value!r}")
    hours, minutes, seconds, millis = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds + (millis / 1000.0)


def write_srt_cache(media_path: str, transcript: Dict) -> Path:
    cache_path = transcript_cache_path(media_path)
    lines = []
    for idx, segment in enumerate(transcript.get("segments", []), start=1):
        start = format_srt_timestamp(float(segment["start"]))
        end = format_srt_timestamp(float(segment["end"]))
        text = str(segment.get("text", "")).strip().replace("\r", "").replace("\n", " ")
        lines.append(str(idx))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")

    cache_path.write_text("\n".join(lines), encoding="utf-8")
    return cache_path


def load_srt_cache(cache_path: Path) -> Dict:
    content = cache_path.read_text(encoding="utf-8-sig").strip()
    if not content:
        return {"duration": 0.0, "segments": []}

    segments = []
    for block in re.split(r"\n\s*\n", content):
        lines = [line.strip("\ufeff") for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if "-->" not in lines[0] and len(lines) > 1 and "-->" in lines[1]:
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        start_raw, end_raw = [part.strip() for part in lines[0].split("-->", 1)]
        text = "\n".join(lines[1:]).strip()
        segments.append(
            {
                "start": parse_srt_timestamp(start_raw),
                "end": parse_srt_timestamp(end_raw),
                "text": text,
            }
        )

    duration = segments[-1]["end"] if segments else 0.0
    return {"duration": duration, "segments": segments}


def load_cached_transcript(media_path: str):
    """Return a cached transcript or None. Treat empty/invalid caches as missing."""
    cache_path = transcript_cache_path(media_path)
    if not cache_path.exists():
        return None

    source_mtime = os.path.getmtime(media_path)
    if cache_path.stat().st_mtime < source_mtime:
        return None

    cached = load_srt_cache(cache_path)
    if not cached["segments"] or cached["duration"] <= 0.0:
        print(f"[transcribe] cache is empty/invalid, deleting: {cache_path}", flush=True)
        cache_path.unlink(missing_ok=True)
        return None

    print(
        f"[transcribe] reusing cached transcript: {cache_path} "
        f"({len(cached['segments'])} segments, {cached['duration']:.0f}s)",
        flush=True,
    )
    return cached
