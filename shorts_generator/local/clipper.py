"""Local clipping: ffmpeg subclip + blurred-background vertical reframe.

Per highlight, one ffmpeg pass cuts the source to [start, end] and builds a
vertical short with the full frame centred on a blurred, darkened copy of the
same frame (the TikTok/Shorts "blur bars" look). No OpenCV involved.

Each clip gets a unique filename ``{slug}_{run_id}_{i}_{score}.mp4`` where
``slug`` is the first three words of the source title — so re-processing the
same video never overwrites a previously rendered clip.
"""
import os
import re
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from ..config import LOCAL_OUTPUT_DIR


def _slugify(name: str) -> str:
    """First three alphanumeric words of a title, lowercased and '-' joined.

    E.g. "World Order Is a Lie｜ Prof. Jiang" → "world-order-is". Non-ASCII
    titles fall back to "short".
    """
    tokens = re.findall(r"[a-zA-Z0-9]+", (name or "").lower())[:3]
    return "-".join(tokens) or "short"


def _ratio(aspect_ratio: str) -> float:
    """Parse '9:16' → 9/16, '1:1' → 1.0."""
    try:
        w, h = aspect_ratio.split(":")
        return float(w) / float(h)
    except (ValueError, ZeroDivisionError):
        return 9.0 / 16.0


def _canvas_size(aspect_ratio: str) -> Tuple[int, int]:
    """720-wide canvas, even dimensions, e.g. 9:16 → (720, 1280)."""
    width = 720
    height = int(width / _ratio(aspect_ratio))
    height = max(2, height - (height % 2))
    return width, height


def _source_dimensions(path: str) -> Tuple[int, int]:
    """Return the source video's (width, height) via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ],
        capture_output=True, text=True, check=True,
    )
    values = result.stdout.strip().split()
    if len(values) < 2:
        raise ValueError(f"could not read dimensions of {path}")
    return int(values[0]), int(values[1])


def caption_anchor_y(
    canvas_w: int,
    canvas_h: int,
    src_w: int,
    src_h: int,
    pad: int = 8,
) -> Optional[int]:
    """Y coordinate (canvas space) at the top of the bottom blur band.

    Mirrors ``crop_clip_local``'s layout math: foreground scaled to fit the
    canvas preserving aspect ratio, cropped to even dimensions, centered —
    so the bottom blurred strip starts at ``(canvas_h - fg_h)/2 + fg_h``.
    Returns ``None`` when there is no real bottom band (source fills/almost
    fills the canvas), in which case callers should use the bottom-margin
    fallback.
    """
    if not src_w or not src_h:
        return None
    scale = min(canvas_w / src_w, canvas_h / src_h)
    fg_h = int(src_h * scale) // 2 * 2
    off_y = (canvas_h - fg_h) // 2
    band_top = off_y + fg_h
    band_h = canvas_h - band_top
    if band_h < max(40, int(canvas_h * 0.03)):
        return None
    return band_top + pad


def crop_clip_local(
    source_path: str,
    start_time: float,
    end_time: float,
    aspect_ratio: str,
    out_path: str,
) -> str:
    """Cut + reframe one highlight, returning the local mp4 path."""
    width, height = _canvas_size(aspect_ratio)
    filter_graph = (
        "[0:v]split=2[fg][bg];"
        f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=20:5,"
        "eq=brightness=-0.1:saturation=1.2[bgv];"
        f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        "crop=trunc(iw/2)*2:trunc(ih/2)*2[fgv];"
        "[bgv][fgv]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p"
    )

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start_time:.3f}",
        "-to", f"{end_time:.3f}",
        "-i", source_path,
        "-vf", filter_graph,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    return out_path


def crop_highlights_local(
    source_path: str,
    highlights: List[Dict],
    aspect_ratio: str = "9:16",
    out_dir: Optional[str] = None,
    run_id: Optional[str] = None,
    source_name: Optional[str] = None,
) -> List[Dict]:
    out_dir = out_dir or LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    run_token = run_id or str(int(time.time()))
    slug = _slugify(source_name or os.path.basename(source_path))
    results: List[Dict] = []
    for i, h in enumerate(highlights, 1):
        score = h.get("score", 0)
        out_path = os.path.join(out_dir, f"{slug}_{run_token}_{i}_{score}.mp4")
        print(f"[clip/local] {i}/{len(highlights)}: {h.get('title', '(untitled)')}", flush=True)
        try:
            crop_clip_local(
                source_path,
                float(h["start_time"]),
                float(h["end_time"]),
                aspect_ratio,
                out_path,
            )
            results.append({**h, "clip_url": out_path})
        except Exception as e:
            print(f"[clip/local] {i} failed: {e}", flush=True)
            results.append({**h, "clip_url": None, "error": str(e)})
    return results