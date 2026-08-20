"""Transcription via the gateway Whisper endpoint (OpenAI-compatible).

Extracts speech audio to small mono MP3 chunks with ffmpeg, uploads each to
the gateway's /audio/transcriptions route, and returns the same shape the
highlight generator expects: {duration, segments[start, end, text]}.

Chunking is by file size, not duration, because providers cap uploads (Groq
allows 19 MB, so we target 17 MB per chunk). Long videos get split into
multiple chunks whose per-chunk timestamps are shifted back onto the source
timeline.

Results are cached as .srt (see local/srt_cache.py) so re-runs skip the call.
"""
import os
import subprocess
import tempfile
from typing import Dict, List, Optional, Tuple

from ..config import (
    GATEWAY_BASE_URL,
    LOCAL_WHISPER_MODEL,
    require_gateway_key,
)

GROQ_MAX_AUDIO_BYTES = 19 * 1024 * 1024   # hard cap at the platform
CHUNK_TARGET_BYTES = 17 * 1024 * 1024     # keep comfortably under the cap
AUDIO_BITRATE = "64k"
SAFETY_FACTOR = 0.85


def _import_openai():
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "openai is required for the gateway transcriber. Install it with:\n"
            "    pip install -r requirements.txt"
        ) from e
    return OpenAI


def _extract_full_audio(media_path: str, tmpdir: str) -> str:
    """Extract the speech track as mono 16 kHz MP3 (small, Whisper-friendly)."""
    out = os.path.join(tmpdir, "audio.mp3")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", media_path,
        "-vn",
        "-ac", "1", "-ar", "16000",
        "-c:a", "libmp3lame", "-b:a", AUDIO_BITRATE,
        out,
    ]
    subprocess.run(cmd, check=True)
    return out


def _probe_duration(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def _split_audio_into_mp3(media_path: str) -> List[Tuple[str, float]]:
    """Extract audio and split into <=17 MB chunks.

    Returns a list of (chunk_path, start_offset) tuples so per-chunk timestamps
    can be shifted back onto the source timeline.
    """
    tmpdir = tempfile.mkdtemp(prefix="shorts_transcribe_")
    audio = _extract_full_audio(media_path, tmpdir)
    size = os.path.getsize(audio)

    if size <= CHUNK_TARGET_BYTES:
        return [(audio, 0.0)]

    duration = _probe_duration(audio)
    bytes_per_sec = size / duration
    segment_seconds = max(1, int(SAFETY_FACTOR * CHUNK_TARGET_BYTES / bytes_per_sec))

    template = os.path.join(tmpdir, "chunk_%04d.mp3")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", audio,
        "-f", "segment", "-segment_time", str(segment_seconds),
        "-reset_timestamps", "1",
        "-c", "copy",
        template,
    ]
    subprocess.run(cmd, check=True)
    os.remove(audio)

    names = sorted(f for f in os.listdir(tmpdir) if f.startswith("chunk_"))
    if not names:
        raise RuntimeError(f"ffmpeg produced no audio chunks from {media_path}")

    chunks = []
    for i, name in enumerate(names):
        path = os.path.join(tmpdir, name)
        chunk_size = os.path.getsize(path)
        if chunk_size > GROQ_MAX_AUDIO_BYTES:
            raise RuntimeError(
                f"audio chunk {name} is {chunk_size} bytes — exceeds the "
                f"{GROQ_MAX_AUDIO_BYTES}-byte provider limit. Lower AUDIO_BITRATE."
            )
        chunks.append((path, i * segment_seconds))
    return chunks


def _cleanup_chunks(chunks: List[Tuple[str, float]]):
    seen = set()
    for path, _ in chunks:
        directory = os.path.dirname(path)
        if directory not in seen:
            seen.add(directory)
    for directory in seen:
        try:
            for name in os.listdir(directory):
                os.remove(os.path.join(directory, name))
            os.rmdir(directory)
        except OSError:
            pass


def _transcribe_chunk(client, chunk_path: str, language: Optional[str]) -> Dict:
    kwargs = {
        "model": LOCAL_WHISPER_MODEL,
        "file": open(chunk_path, "rb"),
        "response_format": "verbose_json",
    }
    if language:
        kwargs["language"] = language

    try:
        result = client.audio.transcriptions.create(**kwargs)
    finally:
        if hasattr(kwargs.get("file"), "close"):
            kwargs["file"].close()

    duration = float(getattr(result, "duration", 0.0) or 0.0)
    language = getattr(result, "language", None) or None
    segments = []
    for s in getattr(result, "segments", []) or []:
        start = float(getattr(s, "start", 0.0) or 0.0)
        end = float(getattr(s, "end", 0.0) or 0.0)
        text = str(getattr(s, "text", "") or "").strip()
        if text:
            segments.append({"start": start, "end": end, "text": text})
    return {"duration": duration, "segments": segments, "language": language}


def transcribe_gateway(media_path: str, language: Optional[str] = None) -> Dict:
    """Transcribe a local media file via the gateway Whisper endpoint."""
    from .srt_cache import load_cached_transcript, write_srt_cache

    cached = load_cached_transcript(media_path)
    if cached is not None:
        return cached

    if not GATEWAY_BASE_URL:
        raise RuntimeError(
            "BASE_URL is not set. Add your OpenAI-compatible gateway URL to .env."
        )
    require_gateway_key()

    if not os.path.exists(media_path):
        raise RuntimeError(f"Media file does not exist: {media_path}")

    OpenAI = _import_openai()
    client = OpenAI(base_url=GATEWAY_BASE_URL, api_key=require_gateway_key())

    print(
        f"[transcribe] gateway whisper model={LOCAL_WHISPER_MODEL} on {media_path}",
        flush=True,
    )
    chunks = _split_audio_into_mp3(media_path)
    try:
        all_segments: List[Dict] = []
        max_end = 0.0
        detected_language = None
        for i, (chunk_path, offset) in enumerate(chunks, 1):
            print(f"[transcribe] chunk {i}/{len(chunks)} (offset {offset:.0f}s)", flush=True)
            result = _transcribe_chunk(client, chunk_path, language)
            max_end = max(max_end, offset + float(result.get("duration", 0.0)))
            if result.get("language"):
                detected_language = result["language"]
            for seg in result.get("segments", []):
                all_segments.append(
                    {
                        "start": seg["start"] + offset,
                        "end": seg["end"] + offset,
                        "text": seg["text"],
                    }
                )

        duration = max_end
        transcript = {
            "duration": duration,
            "segments": all_segments,
            "language": detected_language or language,
        }
        path = write_srt_cache(media_path, transcript)
        print(
            f"[transcribe] {len(all_segments)} segments, {duration:.0f}s; wrote cache: {path}",
            flush=True,
        )
        return transcript
    finally:
        _cleanup_chunks(chunks)