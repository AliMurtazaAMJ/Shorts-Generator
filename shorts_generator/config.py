import os
import re
import secrets
import threading
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_env_lock = threading.Lock()

# Gateway settings — a single OpenAI-compatible endpoint serving both the
# LLM (chat completions) and Whisper (audio transcriptions).
GATEWAY_API_KEY = os.getenv("API_KEY", "").strip()
GATEWAY_BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
LOCAL_LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
# Note the odd `Whisper_MODEL` casing — accepted alongside the sane `WHISPER_MODEL`.
LOCAL_WHISPER_MODEL = os.getenv(
    "Whisper_MODEL",
    os.getenv("WHISPER_MODEL", "whisper-large-v3-turbo"),
)

LOCAL_OUTPUT_DIR = os.getenv("LOCAL_OUTPUT_DIR", "resources")

# Source media cache — downloaded videos and .srt transcript caches.
SHORTS_DOWNLOADS_DIR = os.getenv(
    "SHORTS_DOWNLOADS_DIR", os.path.join(LOCAL_OUTPUT_DIR, "downloads")
)

# Server settings
SHORTS_API_KEY = os.getenv("SHORTS_API_KEY", "").strip()
SHORTS_MAX_WORKERS = int(os.getenv("SHORTS_MAX_WORKERS", "4"))

# Developer credit included in API responses and webhook callback payloads.
DEVELOPER_CREDIT = {
    "name": "AMJ",
    "linkedin": "https://www.linkedin.com/in/alimurtazaamj/",
}
SHORTS_HOST = os.getenv("SHORTS_HOST", "0.0.0.0")
SHORTS_PORT = int(os.getenv("SHORTS_PORT", "8100"))
SHORTS_DB_PATH = os.getenv("SHORTS_DB_PATH", "shorts_generator.db")
# Base URL used to build absolute, externally-addressable clip URLs (set to the
# address n8n uses to reach this server, e.g. http://192.168.1.5:8100).
SHORTS_PUBLIC_BASE = os.getenv("SHORTS_PUBLIC_BASE", "http://localhost:8100").rstrip("/")
# Clips land under resources/clips/{video_id}/ — durable, never deleted.
SHORTS_VIDEOS_DIR = os.getenv(
    "SHORTS_VIDEOS_DIR", os.path.join(LOCAL_OUTPUT_DIR, "clips")
)
# Uploaded source videos land under resources/uploads/{video_id}/.
SHORTS_UPLOADS_DIR = os.getenv(
    "SHORTS_UPLOADS_DIR", os.path.join(LOCAL_OUTPUT_DIR, "uploads")
)
# Max accepted upload size in MB (0 disables the upload endpoint).
SHORTS_MAX_UPLOAD_MB = int(os.getenv("SHORTS_MAX_UPLOAD_MB", "2000"))


def get_or_create_api_key() -> str:
    """Return SHORTS_API_KEY, auto-generating and persisting it to .env on
    first start so the API stays authenticated across restarts."""
    if SHORTS_API_KEY:
        return SHORTS_API_KEY

    dotenv_path = Path(".env")
    with _env_lock:
        if dotenv_path.exists():
            for line in dotenv_path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("SHORTS_API_KEY="):
                    candidate = line.split("=", 1)[1].strip().strip("\"'")
                    if candidate:
                        return candidate

        key = secrets.token_urlsafe(32)
        append = "\n" if dotenv_path.exists() and dotenv_path.read_text(encoding="utf-8").strip() else ""
        try:
            with dotenv_path.open("a", encoding="utf-8") as f:
                f.write(f"{append}SHORTS_API_KEY={key}\n")
        except OSError:
            pass
        os.environ["SHORTS_API_KEY"] = key
        return key


def _resolve_cookies() -> str:
    """Prefer the explicit env var; otherwise fall back to ytmusic's cookies
    (which are known to work for downloading YouTube videos)."""
    configured = os.getenv("YT_COOKIES_FILE", "").strip()
    if configured and os.path.exists(configured):
        return configured
    ytmusic_cookies = os.path.expanduser("~/.local/share/ytmusic/cookies.txt")
    if os.path.exists(ytmusic_cookies):
        return ytmusic_cookies
    return configured


YT_COOKIES_FILE = _resolve_cookies()


def require_gateway_key() -> str:
    if not GATEWAY_API_KEY:
        raise RuntimeError(
            "API_KEY is not set. Add it to your .env file or export it as an env var."
        )
    return GATEWAY_API_KEY
