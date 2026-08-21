"""Local YouTube download via yt-dlp.

Returns a (path, title) tuple so the rest of the local pipeline can read the
file directly off disk and label clips with the source video's title.
"""
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from typing import Optional, Tuple

from ..config import SHORTS_DOWNLOADS_DIR, YT_COOKIES_FILE

# Serialize downloads of the same video id so two concurrent jobs never race
# on the same yt-dlp temp/output files.
_download_locks = {}
_download_locks_guard = threading.Lock()


def _lock_for(video_id: str) -> threading.Lock:
    with _download_locks_guard:
        lock = _download_locks.get(video_id)
        if lock is None:
            lock = threading.Lock()
            _download_locks[video_id] = lock
        return lock


def _import_ytdlp():
    try:
        import yt_dlp  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "yt-dlp is required for downloading videos. Install it with:\n"
            "    pip install -r requirements.txt"
        ) from e
    return yt_dlp


def _extract_youtube_video_id(source: str) -> Optional[str]:
    """Best-effort extraction of a YouTube video id from a URL."""
    parsed = urlparse(source)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host in ("youtu.be", "www.youtu.be"):
        video_id = parsed.path.lstrip("/").split("/", 1)[0]
        return video_id or None

    if "youtube.com" in host:
        if parsed.path.startswith("/watch"):
            qs = parse_qs(parsed.query)
            video_id = qs.get("v", [""])[0]
            return video_id or None
        match = re.search(r"/(?:shorts|embed|live)/([^/?#&]+)", parsed.path)
        if match:
            return match.group(1)

    return None


def _resolve_local_path(source: str) -> Optional[str]:
    """Return a local filesystem path if the input already points at one."""
    parsed = urlparse(source)
    if parsed.scheme == "file":
        raw_path = unquote(parsed.path)
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            raw_path = f"//{parsed.netloc}{raw_path}"
        candidate = Path(raw_path).expanduser()
        if candidate.exists() and candidate.is_file():
            return str(candidate.resolve())
        raise RuntimeError(f"Local file URL does not exist: {source}")

    if parsed.scheme in ("http", "https"):
        return None

    candidate = Path(source).expanduser()
    if candidate.exists() and candidate.is_file():
        return str(candidate.resolve())

    if any(sep in source for sep in (os.sep, "/")) or source.startswith("~") or source.startswith("."):
        raise RuntimeError(f"Local file path does not exist: {source}")

    return None


def _existing_download(out_dir: str, video_id: str) -> Optional[str]:
    """Return a cached download path if we already have this YouTube id."""
    for ext in (".mp4", ".mkv", ".webm"):
        candidate = os.path.join(out_dir, f"source_{video_id}{ext}")
        if os.path.exists(candidate):
            return candidate
    return None


def _sidecar_path(media_path: str) -> str:
    return os.path.splitext(media_path)[0] + ".meta.json"


def _load_sidecar_title(media_path: str) -> Optional[str]:
    """Read the source title from the sidecar written on first download."""
    try:
        with open(_sidecar_path(media_path), encoding="utf-8") as f:
            return json.load(f).get("title") or None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _write_sidecar_title(media_path: str, title: str) -> None:
    try:
        with open(_sidecar_path(media_path), "w", encoding="utf-8") as f:
            json.dump({"title": title}, f)
    except OSError:
        pass


def _title_for(path: str) -> str:
    return _load_sidecar_title(path) or os.path.splitext(os.path.basename(path))[0]


def download_youtube_local(
    video_url: str, out_dir: Optional[str] = None
) -> Tuple[str, str]:
    """Download a remote URL or resolve a local file; returns (path, title)."""
    local_path = _resolve_local_path(video_url)
    if local_path:
        title = os.path.splitext(os.path.basename(local_path))[0]
        print(f"[download/local] using local file: {local_path}", flush=True)
        return local_path, title

    yt_dlp = _import_ytdlp()
    out_dir = out_dir or SHORTS_DOWNLOADS_DIR
    os.makedirs(out_dir, exist_ok=True)

    video_id = _extract_youtube_video_id(video_url)
    if video_id:
        cached = _existing_download(out_dir, video_id)
        if cached:
            print(f"[download/local] reusing cached download: {cached}", flush=True)
            return cached, _title_for(cached)
    else:
        video_id = "local"

    with _lock_for(video_id):
        if video_id != "local":
            cached = _existing_download(out_dir, video_id)
            if cached:
                print(f"[download/local] reusing cached download: {cached}", flush=True)
                return cached, _title_for(cached)
        path, title = _download_inner(yt_dlp, video_url, out_dir, video_id)
        _write_sidecar_title(path, title)
        return path, title


def _download_inner(
    yt_dlp, video_url: str, out_dir: str, video_id: str
) -> Tuple[str, str]:
    print(f"[download/local] {video_url} (best) → {out_dir}/", flush=True)

    base_opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": os.path.join(out_dir, "source_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "js_runtimes": {"node": {}},
    }
    if YT_COOKIES_FILE and os.path.exists(YT_COOKIES_FILE):
        base_opts["cookiefile"] = YT_COOKIES_FILE
        print(f"[download/local] using cookies: {YT_COOKIES_FILE}", flush=True)

    # Player-client fallback chain. The desktop client frequently 403s or
    # refuses downloads; android (360p h264+aac) is a reliable free route,
    # then tv / web_embedded as further alternates. The JS challenges are
    # solved with the node runtime (see js_runtimes above) + the yt-dlp-ejs
    # solver scripts.
    attempts = [
        ("default client", {}),
        (
            "android client",
            {"extractor_args": {"youtube": {"player_client": ["android"]}}},
        ),
        (
            "tv client",
            {"extractor_args": {"youtube": {"player_client": ["tv"]}}},
        ),
        (
            "web_embedded client",
            {"extractor_args": {"youtube": {"player_client": ["web_embedded"]}}},
        ),
    ]

    last_error: Optional[Exception] = None
    for label, extra in attempts:
        ydl_opts = {**base_opts, **extra}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
                path = ydl.prepare_filename(info)
                if not os.path.exists(path):
                    stem, _ = os.path.splitext(path)
                    for ext in (".mp4", ".mkv", ".webm"):
                        if os.path.exists(stem + ext):
                            path = stem + ext
                            break
            if os.path.exists(path):
                print(f"[download/local] ready ({label}): {path}", flush=True)
                return path, info.get("title") or os.path.splitext(os.path.basename(path))[0]
            last_error = RuntimeError(f"download produced no file ({label})")
        except Exception as e:
            last_error = e
            print(f"[download/local] {label} failed: {e}", flush=True)

    raise RuntimeError(
        f"all download attempts failed for {video_url}: {last_error}"
    )
