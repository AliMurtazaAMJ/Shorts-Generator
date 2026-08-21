"""End-to-end orchestrator.

Pipeline: yt-dlp download → gateway Whisper transcription → LLM highlight
ranking → ffmpeg blur-bar vertical crop. The LLM and Whisper backends both
run through a single OpenAI-compatible gateway; clipping is fully local.
"""
import os
from typing import Dict, List, Optional

from .config import SHORTS_VIDEOS_DIR
from .highlights import get_highlights


def generate_shorts(
    youtube_url: str,
    num_clips: int = 3,
    aspect_ratio: str = "9:16",
    language: Optional[str] = None,
    out_dir: Optional[str] = None,
    detect_captions: bool = False,
    burn_captions: bool = True,
    force_captions: bool = False,
    caption_options: Optional[Dict] = None,
    focus: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Dict:
    """Run the full pipeline and return a structured result.

    Args:
        youtube_url: YouTube URL, file:// URL, or local file path.
        num_clips: how many shorts to render.
        aspect_ratio: e.g. "9:16", "1:1".
        language: ISO-639-1 to force Whisper language detection.
        out_dir: where rendered clips are written. Defaults to
            SHORTS_VIDEOS_DIR (resources/clips); concurrent callers should
            pass a per-job/per-video directory.
        detect_captions: scan frames with OCR for burned-in captions and flag
            the result with ``has_burned_captions``.
        burn_captions: split the transcript into subtitle cues and burn them
            onto every rendered short (skipped automatically when OCR found the
            source already has captions).
        force_captions: add subtitles even when the source already has them;
            when set, OCR detection is skipped to save the ~1 min scan.
        caption_options: styling dict for the burned captions — text/active-word/
            outline colors and karaoke highlight (see
            shorts_generator.captions.subtitles.DEFAULT_CAPTION_OPTIONS).
        focus: optional free-text description of the kind of clips the user
            wants (e.g. "funny moments" or "productivity tips"). Passed to the
            highlight LLM so it prioritizes matching moments.
        run_id: unique token baked into each clip filename so re-processing
            the same video never overwrites previously rendered clips.

    Returns:
        {
          "mode": "local",
          "source_video_url": str,   # local source path
          "source_title": str,       # the source video's title
          "transcript": {...},
          "highlights": [...],       # all candidates ranked
          "shorts": [...],           # top `num_clips` with clip_url / local path
          "has_burned_captions": bool,  # only when OCR ran (non-forced runs)
          "captions": "burned" | "skipped" | "off",
          "captions_forced": bool,      # only present when force_captions was used
        }
    """
    from .local.clipper import crop_highlights_local
    from .local.downloader import download_youtube_local
    from .local.gateway_transcriber import transcribe_gateway
    from .local.llm import call_local_llm

    source_path, source_title = download_youtube_local(youtube_url)

    transcript = transcribe_gateway(source_path, language=language)
    if not transcript["segments"]:
        raise RuntimeError(
            "Whisper produced no segments. The video may have no detectable speech."
        )

    highlights_result = get_highlights(
        transcript,
        num_clips=num_clips,
        llm_fn=call_local_llm,
        focus=focus,
    )
    all_highlights: List[Dict] = highlights_result.get("highlights", [])
    if not all_highlights:
        raise RuntimeError("Highlight generator returned zero clips.")

    top = sorted(all_highlights, key=lambda h: int(h.get("score", 0)), reverse=True)[:num_clips]
    print(f"[pipeline] cropping {len(top)} of {len(all_highlights)} candidates", flush=True)

    shorts = crop_highlights_local(
        source_path,
        top,
        aspect_ratio=aspect_ratio,
        out_dir=out_dir or SHORTS_VIDEOS_DIR,
        run_id=run_id,
        source_name=source_title,
    )

    result: Dict = {
        "mode": "local",
        "source_video_url": source_path,
        "source_title": source_title,
        "transcript": transcript,
        "highlights": all_highlights,
        "shorts": shorts,
    }

    need_ocr = detect_captions and burn_captions and not force_captions
    if need_ocr:
        from .local.caption_detection import detect_burned_captions
        result["has_burned_captions"] = detect_burned_captions(source_path)

    _apply_captions(
        result,
        transcript,
        burn_captions,
        force_captions,
        aspect_ratio,
        source_path,
        caption_options,
    )

    return result


def _apply_captions(
    result: Dict,
    transcript: Dict,
    burn_captions: bool,
    force_captions: bool,
    aspect_ratio: str,
    source_path: Optional[str] = None,
    caption_options: Optional[Dict] = None,
) -> None:
    """Generate subtitle cues and burn them into each rendered short.

    Skips (result["captions"] == "skipped") when OCR found the source already
    has burned-in captions — unless ``force_captions`` is set, which always
    burns. Caption lines are anchored at the top of the source's bottom blur
    band when computable. Annotation is in-place on ``result["shorts"]``.
    """
    if not burn_captions:
        result["captions"] = "off"
        return

    if not force_captions and result.get("has_burned_captions") is True:
        result["captions"] = "skipped"
        return

    from .captions import build_cues, generate_captions_for_clip
    from .local.clipper import _canvas_size, _source_dimensions, caption_anchor_y

    width, height = _canvas_size(aspect_ratio)

    anchor_y: Optional[int] = None
    if source_path and isinstance(source_path, str) and os.path.exists(source_path):
        try:
            src_w, src_h = _source_dimensions(source_path)
            anchor_y = caption_anchor_y(width, height, src_w, src_h)
        except Exception as e:
            print(f"[captions] could not compute blink band anchor: {e}", flush=True)

    lang = transcript.get("language") or "en"
    if not isinstance(lang, str) or not lang:
        lang = "en"
    cues = build_cues(transcript.get("segments", []), lang=lang)
    print(
        f"[captions] burning {len(cues)} cues ({lang}) into shorts"
        + (f" at y={anchor_y}" if anchor_y is not None else ""),
        flush=True,
    )

    rendered = 0
    for short in result.get("shorts", []):
        clip_url = short.get("clip_url")
        if not clip_url or not isinstance(clip_url, str):
            continue
        try:
            info = generate_captions_for_clip(
                cues,
                float(short["start_time"]),
                float(short["end_time"]),
                clip_url,
                width=width,
                height=height,
                burn=True,
                options=caption_options,
                anchor_y=anchor_y,
            )
            short["caption_file"] = info["ass_file"]
            short["caption_srt"] = info["srt_file"]
            short["caption_cues"] = info["cue_count"]
            rendered += 1
        except Exception as e:
            print(f"[captions] clip {clip_url} caption burn failed: {e}", flush=True)

    result["captions"] = "burned" if rendered else "off"
    if caption_options:
        result["caption_options"] = dict(caption_options)
    if force_captions and rendered:
        result["captions_forced"] = True