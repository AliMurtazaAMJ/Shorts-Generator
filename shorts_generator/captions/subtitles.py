"""Caption generation for the shorts pipeline.

Produces captions from plain ``{start, end, text}`` transcript segments — the
exact shape the gateway transcriber returns — without any external caption
library or torch dependency.

Word-level timing is estimated proportionally when the transcript has no word
timings (see ``determine_advanced_split_points`` / ``generate_subtitles_from_split_points``).
"""
import math
import os
import re
import subprocess
from typing import Dict, List, Optional

from .conjunctions import get_conjunctions, get_comma


def normal_round(n):
    if n - math.floor(n) < 0.5:
        return math.floor(n)
    return math.ceil(n)


def format_timestamp(seconds: float, is_vtt: bool = False):

    assert seconds >= 0, "non-negative timestamp expected"
    milliseconds = round(seconds * 1000.0)

    hours = milliseconds // 3_600_000
    milliseconds -= hours * 3_600_000

    minutes = milliseconds // 60_000
    milliseconds -= minutes * 60_000

    seconds = milliseconds // 1_000
    milliseconds -= seconds * 1_000

    separator = '.' if is_vtt else ','

    hours_marker = f"{hours:02d}:"
    return (
        f"{hours_marker}{minutes:02d}:{seconds:02d}{separator}{milliseconds:03d}"
    )


class SubtitlesProcessor:
    def __init__(self, segments, lang, max_line_length = 45, min_char_length_splitter = 30, is_vtt = False):
        self.comma = get_comma(lang)
        self.conjunctions = set(get_conjunctions(lang))
        self.segments = segments
        self.lang = lang
        self.max_line_length = max_line_length
        self.min_char_length_splitter = min_char_length_splitter
        self.is_vtt = is_vtt
        complex_script_languages = ['th', 'lo', 'my', 'km', 'am', 'ko', 'ja', 'zh', 'ti', 'ta', 'te', 'kn', 'ml', 'hi', 'ne', 'mr', 'ar', 'fa', 'ur', 'ka']
        if self.lang in complex_script_languages:
            self.max_line_length = 30
            self.min_char_length_splitter = 20

    def estimate_timestamp_for_word(self, words, i, next_segment_start_time=None):
        k = 0.25
        has_prev_end = i > 0 and 'end' in words[i - 1]
        has_next_start = i < len(words) - 1 and 'start' in words[i + 1]

        if has_prev_end:
            words[i]['start'] = words[i - 1]['end']
            if has_next_start:
                words[i]['end'] = words[i + 1]['start']
            else:
                if next_segment_start_time:
                    words[i]['end'] = next_segment_start_time if next_segment_start_time - words[i - 1]['end'] <= 1 else next_segment_start_time - 0.5
                else:
                    words[i]['end'] = words[i]['start'] + len(words[i]['word']) * k

        elif has_next_start:
            words[i]['start'] = words[i + 1]['start'] - len(words[i]['word']) * k
            words[i]['end'] = words[i + 1]['start']

        else:
            if next_segment_start_time:
                words[i]['start'] = next_segment_start_time - 1
                words[i]['end'] = next_segment_start_time - 0.5
            else:
                words[i]['start'] = 0
                words[i]['end'] = 0



    def process_segments(self, advanced_splitting=True):
        subtitles = []
        for i, segment in enumerate(self.segments):
            next_segment_start_time = self.segments[i + 1]['start'] if i + 1 < len(self.segments) else None

            if advanced_splitting:

                split_points = self.determine_advanced_split_points(segment, next_segment_start_time)
                subtitles.extend(self.generate_subtitles_from_split_points(segment, split_points, next_segment_start_time))
            else:
                words = segment.get('words', segment['text'].split())
                for i, word in enumerate(words):
                    if 'start' not in word or 'end' not in word:
                        self.estimate_timestamp_for_word(words, i, next_segment_start_time)

                subtitles.append({
                'start': segment['start'],
                'end': segment['end'],
                'text': segment['text']
            })

        return subtitles

    def determine_advanced_split_points(self, segment, next_segment_start_time=None):
        split_points = []
        last_split_point = 0
        char_count = 0

        words = segment.get('words', segment['text'].split())
        add_space = 0 if self.lang in ['zh', 'ja'] else 1

        total_char_count = sum(len(word['word']) if isinstance(word, dict) else len(word) + add_space for word in words)
        char_count_after = total_char_count

        for i, word in enumerate(words):
            word_text = word['word'] if isinstance(word, dict) else word
            word_length = len(word_text) + add_space
            char_count += word_length
            char_count_after -= word_length

            char_count_before = char_count - word_length

            if isinstance(word, dict) and ('start' not in word or 'end' not in word):
                self.estimate_timestamp_for_word(words, i, next_segment_start_time)

            if char_count >= self.max_line_length:
                midpoint = normal_round((last_split_point + i) / 2)
                if char_count_before >= self.min_char_length_splitter:
                    split_points.append(midpoint)
                    last_split_point = midpoint + 1
                    char_count = sum(len(words[j]['word']) if isinstance(words[j], dict) else len(words[j]) + add_space for j in range(last_split_point, i + 1))

            elif word_text.endswith(self.comma) and char_count_before >= self.min_char_length_splitter and char_count_after >= self.min_char_length_splitter:
                split_points.append(i)
                last_split_point = i + 1
                char_count = 0

            elif word_text.lower() in self.conjunctions and char_count_before >= self.min_char_length_splitter and char_count_after >= self.min_char_length_splitter:
                split_points.append(i - 1)
                last_split_point = i
                char_count = word_length

        return split_points


    def generate_subtitles_from_split_points(self, segment, split_points, next_start_time=None):
        subtitles = []

        words = segment.get('words', segment['text'].split())
        total_word_count = len(words)
        total_time = segment['end'] - segment['start']
        elapsed_time = segment['start']
        prefix = ' ' if self.lang not in ['zh', 'ja'] else ''
        start_idx = 0
        for split_point in split_points:

            fragment_words = words[start_idx:split_point + 1]
            current_word_count = len(fragment_words)


            if isinstance(fragment_words[0], dict):
                start_time = fragment_words[0]['start']
                end_time = fragment_words[-1]['end']
                next_start_time_for_word = words[split_point + 1]['start'] if split_point + 1 < len(words) else None
                if next_start_time_for_word and (next_start_time_for_word - end_time) <= 0.8:
                    end_time = next_start_time_for_word
            else:
                fragment = prefix.join(fragment_words).strip()
                current_duration = (current_word_count / total_word_count) * total_time
                start_time = elapsed_time
                end_time = elapsed_time + current_duration
                elapsed_time += current_duration


            subtitles.append({
                'start': start_time,
                'end': end_time,
                'text': fragment if not isinstance(fragment_words[0], dict) else prefix.join(word['word'] for word in fragment_words)
            })

            start_idx = split_point + 1

        # Handle the last fragment
        if start_idx < len(words):
            fragment_words = words[start_idx:]
            current_word_count = len(fragment_words)

            if isinstance(fragment_words[0], dict):
                start_time = fragment_words[0]['start']
                end_time = fragment_words[-1]['end']
            else:
                fragment = prefix.join(fragment_words).strip()
                current_duration = (current_word_count / total_word_count) * total_time
                start_time = elapsed_time
                end_time = elapsed_time + current_duration

            if next_start_time and (next_start_time - end_time) <= 0.8:
                end_time = next_start_time

            subtitles.append({
                'start': start_time,
                'end': end_time if end_time is not None else segment['end'],
                'text': fragment if not isinstance(fragment_words[0], dict) else prefix.join(word['word'] for word in fragment_words)
            })

        return subtitles


    def save(self, filename="subtitles.srt", advanced_splitting=True):

        subtitles = self.process_segments(advanced_splitting)

        def write_subtitle(file, idx, start_time, end_time, text):

            file.write(f"{idx}\n")
            file.write(f"{start_time} --> {end_time}\n")
            file.write(text + "\n\n")

        with open(filename, 'w', encoding='utf-8') as file:
            if self.is_vtt:
                file.write("WEBVTT\n\n")

            if advanced_splitting:
                for idx, subtitle in enumerate(subtitles, 1):
                    start_time = format_timestamp(subtitle['start'], self.is_vtt)
                    end_time = format_timestamp(subtitle['end'], self.is_vtt)
                    text = subtitle['text'].strip()
                    write_subtitle(file, idx, start_time, end_time, text)

        return len(subtitles)


def build_cues(
    segments: List[Dict],
    lang: str = "en",
    max_line_length: int = 45,
    min_char_length_splitter: int = 30,
) -> List[Dict]:
    """Turn transcript segments into word-split caption cues.

    ``segments`` items are ``{"start", "end", "text"}`` — word timings are
    estimated proportionally when absent. Returns ``[{start, end, text}]`` on
    the source timeline.
    """
    if not segments:
        return []
    processor = SubtitlesProcessor(
        segments,
        lang,
        max_line_length=max_line_length,
        min_char_length_splitter=min_char_length_splitter,
    )
    return processor.process_segments(advanced_splitting=True)


def filter_cues(
    cues: List[Dict],
    clip_start: float,
    clip_end: float,
    min_visible: float = 0.15,
) -> List[Dict]:
    """Restrict and time-shift cues to a clip's window.

    Drops cues that overlap the window by less than ``min_visible`` seconds and
    clamps the remainder so ``start``/``end`` are clip-relative and never
    exceed the clip duration.
    """
    shifted: List[Dict] = []
    for cue in cues:
        s, e = float(cue["start"]), float(cue["end"])
        visible_s, visible_e = max(s, clip_start), min(e, clip_end)
        if visible_e - visible_s < min_visible:
            continue
        start = visible_s - clip_start
        end = visible_e - clip_start
        if end <= start or start < 0:
            continue
        shifted.append(
            {"start": start, "end": end, "text": str(cue.get("text", "")).strip()}
        )
    return shifted


def write_srt(cues: List[Dict], path: str) -> str:
    lines = []
    for idx, cue in enumerate(cues, 1):
        text = re.sub(r"\s*\n\s*", "\n", str(cue.get("text", "")).strip())
        lines.append(str(idx))
        lines.append(
            f"{format_timestamp(float(cue['start']))} --> "
            f"{format_timestamp(float(cue['end']))}"
        )
        lines.append(text)
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def _format_ass_time(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    hours, cs = divmod(cs, 360000)
    minutes, cs = divmod(cs, 6000)
    secs, cs = divmod(cs, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _escape_ass_text(text: str) -> str:
    """Make runner text safe for an ASS Dialogue line.

    Braces are override-tag delimiters; strip them so transcript quirks
    can't inject styling. Newlines become hard backslash-N line breaks.
    """
    text = re.sub(r"[{}]", "", text)
    text = re.sub(r"\s*\n\s*", lambda m: "\\N", text)
    return text


DEFAULT_CAPTION_OPTIONS: Dict = {
    "karaoke": True,          # highlight the currently-spoken word
    "text_color": "#FFFFFF",  # normal word color
    "active_color": "#FFD700",  # currently-spoken word color
    "outline_color": "#000000",  # outline color (always on)
    "font_size": 48,
}


def _hex_to_ass_color(hex_color: str, default: str = "#FFFFFF") -> str:
    """Convert '#RRGGBB' to an ASS colour ('&H00BBGGRR')."""
    color = (str(hex_color or "")).strip().lstrip("#")
    if len(color) != 6:
        color = str(default).strip().lstrip("#")
    try:
        r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
    except ValueError:
        r, g, b = 255, 255, 255
    return f"&H00{b:02X}{g:02X}{r:02X}"


def _word_timings(text: str, start: float, end: float) -> List:
    """Approximate per-word timings by proportional character length.

    Returns ``[(word, start, end), ...]`` with contiguous windows spanning the
    cue; the last word stretches to the cue end.
    """
    words = text.split()
    if not words:
        return []
    total = sum(len(w) for w in words) or 1
    out, t = [], start
    for w in words:
        dur = (end - start) * (len(w) / total)
        out.append((w, t, t + dur))
        t += dur
    out[-1] = (out[-1][0], out[-1][1], end)
    return out


def _styled_line(
    words_active: List,
    active_index: int,
    text_color: str,
    active_color: str,
) -> str:
    """Build one ASS line; the word at ``active_index`` gets the active color."""
    parts = []
    for i, (w, _, _) in enumerate(words_active):
        color = active_color if i == active_index else text_color
        parts.append(f"{{\\1c{color}}}{_escape_ass_text(w)}")
    return " ".join(parts)


def write_ass(
    cues: List[Dict],
    path: str,
    width: int = 720,
    height: int = 1280,
    options: Optional[Dict] = None,
    anchor_y: Optional[int] = None,
) -> str:
    """Write an ASS subtitle file with active-word highlighting.

    Each cue is expanded into one Dialogue per word: the full line stays
    visible and the currently-spoken word is recolored with ``active_color``
    (all other words keep ``text_color``). ``anchor_y`` positions the line's
    top edge in canvas coordinates (e.g. the bottom blur band); otherwise it
    falls back to a bottom-centered margin.
    """
    opts = dict(DEFAULT_CAPTION_OPTIONS)
    if options:
        opts.update({k: v for k, v in options.items() if v is not None})

    fontsize = max(8, int(opts.get("font_size", 48)))
    outline = max(1, int(height * 0.0016))
    text_color = _hex_to_ass_color(opts.get("text_color"), "#FFFFFF")
    active_color = _hex_to_ass_color(opts.get("active_color"), "#FFD700")
    outline_color = _hex_to_ass_color(opts.get("outline_color"), "#000000")
    karaoke = bool(opts.get("karaoke", True))
    margin_v = max(16, int(height * 0.03))

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Arial,{fontsize},{text_color},{text_color},"
        f"{outline_color},{outline_color},0,0,0,0,100,100,0,0,1,{outline},1,8,"
        f"0,0,{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    pos_tag = (
        f"{{\\an8\\pos({width // 2},{int(anchor_y)})}}"
        if anchor_y is not None
        else ""
    )

    lines = []
    for cue in cues:
        text = str(cue.get("text", "")).strip()
        if not text:
            continue
        cstart, cend = float(cue["start"]), float(cue["end"])

        if karaoke:
            tws = _word_timings(text, cstart, cend)
            if len(tws) > 1:
                for i, (w, ws, we) in enumerate(tws):
                    end = tws[i + 1][1] if i + 1 < len(tws) else we
                    body = _styled_line(tws, i, text_color, active_color)
                    lines.append(
                        f"Dialogue: 0,{_format_ass_time(ws)},{_format_ass_time(end)},"
                        f"Default,,0,0,0,,{pos_tag}{body}"
                    )
                continue

        body = _styled_line(
            _word_timings(text, cstart, cend) or [(text, cstart, cend)],
            -1,
            text_color,
            active_color,
        )
        lines.append(
            f"Dialogue: 0,{_format_ass_time(cstart)},{_format_ass_time(cend)},"
            f"Default,,0,0,0,,{pos_tag}{body}"
        )

    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines) + "\n")
    return path


def burn_subtitles_into_video(clip_path: str, ass_path: str) -> str:
    """Burn an ASS file into a clip with a second, fast ffmpeg pass.

    Re-encodes video (libx264, CRF 20) and copies the audio track untouched.
    Runs with cwd set to the clip's directory so the filter path stays simple
    and safe under libavfilter parsing.
    """
    clip_dir = os.path.dirname(clip_path) or "."
    basename = os.path.basename(clip_path)
    tmp_path = os.path.join(clip_dir, f".{basename}.captioned.mp4")
    ass_name = os.path.basename(ass_path)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", basename,
        "-vf", f"ass='{ass_name}'",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "copy",
        os.path.basename(tmp_path),
    ]
    subprocess.run(cmd, check=True, cwd=clip_dir)
    os.replace(tmp_path, clip_path)
    return clip_path


def generate_captions_for_clip(
    cues: List[Dict],
    clip_start: float,
    clip_end: float,
    clip_path: str,
    width: int = 720,
    height: int = 1280,
    burn: bool = True,
    options: Optional[Dict] = None,
    anchor_y: Optional[int] = None,
) -> Dict:
    """Write .srt/.ass sidecars for one clip and optionally burn them in.

    ``options`` tunes the ASS styling (colors, karaoke active-word highlight,
    outline); ``anchor_y`` positions the caption line's
    top edge in canvas coordinates. Returns ``{"srt_file", "ass_file", "cue_count"}``.
    """
    local = filter_cues(cues, clip_start, clip_end)
    stem = os.path.splitext(clip_path)[0]
    srt_path = write_srt(local, stem + ".srt")
    ass_path = write_ass(
        local, stem + ".ass", width=width, height=height,
        options=options, anchor_y=anchor_y,
    )
    if burn:
        burn_subtitles_into_video(clip_path, ass_path)
    return {
        "srt_file": os.path.basename(srt_path),
        "ass_file": os.path.basename(ass_path),
        "cue_count": len(local),
    }