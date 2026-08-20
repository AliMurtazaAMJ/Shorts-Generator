"""Detect burned-in (hardcoded) captions in a video via OCR.

Samples one frame every `sample_interval` seconds across the whole video and
runs OCR (RapidOCR, onnxruntime-based, no system deps) on the full frame.
Captions are reported when text is seen in at least `min_frames` distinct
samples, which suppresses transient on-screen graphics/logos.

The full frame is scanned (not just the bottom band): when a video is reframed
into a vertical short the source's bottom-edge captions land near the vertical
center of the canvas, so a naive bottom-band scan would miss them.
"""
import os
import subprocess
import tempfile
from typing import List, Optional, Tuple

MIN_TEXT_CONFIDENCE = 0.5
MIN_TEXT_LENGTH = 3

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:
            raise RuntimeError(
                "rapidocr_onnxruntime is required for caption detection. Install it "
                "with:\n    pip install rapidocr_onnxruntime"
            ) from e
        _engine = RapidOCR()
    return _engine


def _probe_duration(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def _extract_frames(path: str, interval: int, out_dir: str) -> List[str]:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", path,
        "-vf", f"fps=1/{interval}",
        "-frames:v", "200",
        "-q:v", "3",
        os.path.join(out_dir, "frame_%04d.jpg"),
    ]
    subprocess.run(cmd, check=True)
    names = sorted(
        f for f in os.listdir(out_dir) if f.startswith("frame_")
    )
    return [os.path.join(out_dir, n) for n in names]


def _frame_has_text(image_path: str) -> bool:
    engine = _get_engine()
    try:
        result, _ = engine(image_path)
    except Exception:
        return False
    if not result:
        return False
    for _, text, score in result:
        try:
            if float(score) >= MIN_TEXT_CONFIDENCE and len(str(text).strip()) >= MIN_TEXT_LENGTH:
                return True
        except (TypeError, ValueError):
            continue
    return False


def detect_burned_captions(
    video_path: str,
    sample_interval: int = 10,
    min_frames: int = 3,
) -> bool:
    """Return True when burned-in captions are detected in `video_path`.

    Returns False (without raising) when OCR isn't available or the video is
    too short to sample meaningfully.
    """
    if not os.path.exists(video_path):
        return False
    try:
        duration = _probe_duration(video_path)
    except Exception:
        return False
    if duration < 5:
        return False

    with tempfile.TemporaryDirectory(prefix="captions_") as tmpdir:
        try:
            frames = _extract_frames(video_path, sample_interval, tmpdir)
        except Exception:
            return False
        if not frames:
            return False

        hits = 0
        for frame in frames:
            if _frame_has_text(frame):
                hits += 1
                if hits >= min_frames:
                    print(
                        f"[captions] burned-in captions detected "
                        f"({hits} of {len(frames)} frames sampled)",
                        flush=True,
                    )
                    return True

    print(
        f"[captions] no burned-in captions detected ({hits} of {len(frames)} frames sampled)",
        flush=True,
    )
    return False
