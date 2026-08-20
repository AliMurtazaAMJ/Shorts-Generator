"""FastAPI application exposing the shorts generator as a simple HTTP API.

Endpoints:
    GET  /                      web UI (password-gated single page)
    GET  /static/*              web UI assets
    GET  /api/verify            validate the X-API-Key (used by the login form)
    POST /api/jobs              submit a video URL → {job_id, video_id, status}
    POST /api/upload            upload a video file from disk → {video_id, local_path}
    GET  /api/jobs/{job_id}     poll status and result (shorts metadata + URLs)
    GET  /api/jobs/{job_id}/logs  poll pipeline log lines
    POST /api/jobs/{job_id}/cancel   cancel a queued job
    GET  /clip/{video_id}/{filename}   serve a rendered short (the video URL)
    GET  /health                liveness probe (no auth)

All /api routes require the X-API-Key header. The key is generated on first
start and written to .env (see config.get_or_create_api_key). Rendered clips
are served publicly at /clip/... (no key) so they can be shared/played
directly.
"""
import hashlib
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import (
    DEVELOPER_CREDIT,
    SHORTS_API_KEY,
    SHORTS_MAX_UPLOAD_MB,
    SHORTS_PUBLIC_BASE,
    SHORTS_UPLOADS_DIR,
    SHORTS_VIDEOS_DIR,
    get_or_create_api_key,
)
from . import storage
from .jobs import JobManager

STATIC_DIR = Path(__file__).resolve().parent / "static"


class CaptionOptions(BaseModel):
    """Styling for burned-in captions (see subtitles.DEFAULT_CAPTION_OPTIONS)."""
    karaoke: bool = True
    text_color: str = "#FFFFFF"
    active_color: str = "#FFD700"
    outline_color: str = "#000000"
    font_size: int = Field(default=48, ge=8, le=200)


class JobRequest(BaseModel):
    url: str = Field(..., min_length=1)
    num_clips: int = Field(default=3, ge=1, le=20)
    aspect_ratio: str = Field(default="9:16")
    format: str = Field(default="720", pattern="^(360|480|720|1080)$")
    language: Optional[str] = None
    detect_captions: bool = True
    burn_captions: bool = True
    force_captions: bool = False
    caption_options: Optional[CaptionOptions] = None
    focus: Optional[str] = None
    webhook_url: Optional[str] = None


class JobResponse(BaseModel):
    job_id: str
    video_id: str
    status: str
    credit: dict = DEVELOPER_CREDIT


class LogsResponse(BaseModel):
    logs: list


def _require_api_key(x_api_key: str = Header(default="")) -> None:
    key = SHORTS_API_KEY or get_or_create_api_key()
    if not x_api_key or not secrets.compare_digest(x_api_key, key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init_db()
    key = get_or_create_api_key()
    print("=" * 60, flush=True)
    print(f"Shorts Generator API listening on {SHORTS_PUBLIC_BASE}", flush=True)
    print(f"X-API-Key: {key}", flush=True)
    print("=" * 60, flush=True)
    app.state.manager = JobManager()
    yield
    app.state.manager.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Shorts Generator API",
        description="Submit a video URL and get back rendered short clips with metadata.",
        version="1.0.0",
        lifespan=lifespan,
    )

    # -- public -------------------------------------------------------------
    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/verify", dependencies=[Depends(_require_api_key)])
    def verify_key():
        return {"status": "ok"}

    # -- jobs ---------------------------------------------------------------
    @app.post("/api/jobs", dependencies=[Depends(_require_api_key)])
    def submit_job(body: JobRequest) -> JobResponse:
        job = app.state.manager.create(
            params=body.model_dump(),
            webhook_url=body.webhook_url,
        )
        return JobResponse(job_id=job.id, video_id=job.video_id, status=job.status)

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(_require_api_key)])
    def get_job(job_id: str):
        job = app.state.manager.get(job_id)
        if job is not None:
            return job.to_dict()

        # Not in memory (e.g. server restarted) — reconstruct from SQLite.
        record = storage.find_by_job(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        return {
            "job_id": job_id,
            "video_id": record["id"],
            "status": record["status"],
            "url": record["url"],
            "created_at": record["created_at"],
            "started_at": None,
            "finished_at": record["completed_at"],
            "error": record["error"],
            "result": record["metadata_json"],
            "credit": DEVELOPER_CREDIT,
        }

    @app.get("/api/jobs/{job_id}/logs", dependencies=[Depends(_require_api_key)])
    def get_job_logs(job_id: str) -> LogsResponse:
        job = app.state.manager.get(job_id)
        video_id = job.video_id if job is not None else None
        if video_id is None:
            record = storage.find_by_job(job_id)
            if record is None:
                raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
            video_id = record["id"]
        return LogsResponse(logs=storage.get_logs(video_id))

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(_require_api_key)])
    def cancel_job(job_id: str):
        if not app.state.manager.cancel(job_id):
            raise HTTPException(status_code=409, detail="Job not cancelable")
        return {"job_id": job_id, "status": "cancelled"}

    # -- media library --------------------------------------------------------
    @app.get("/api/media", dependencies=[Depends(_require_api_key)])
    def list_media():
        return {"videos": storage.list_videos(limit=50)}

    # -- upload -------------------------------------------------------------
    @app.post("/api/upload", dependencies=[Depends(_require_api_key)])
    async def upload_video(file: UploadFile = File(...)):
        if SHORTS_MAX_UPLOAD_MB <= 0:
            raise HTTPException(status_code=403, detail="Uploads are disabled")
        max_bytes = SHORTS_MAX_UPLOAD_MB * 1024 * 1024

        ext = os.path.splitext(file.filename or "")[1].lower() or ".mp4"
        video_id = hashlib.sha256(
            f"{file.filename or 'upload'}:{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:16]
        dest_dir = Path(SHORTS_UPLOADS_DIR) / video_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / f"source_{video_id}{ext}"

        size = 0
        try:
            with dest_path.open("wb") as out:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"Upload exceeds the {SHORTS_MAX_UPLOAD_MB} MB limit "
                                f"(reached {size / (1024 * 1024):.1f} MB)"
                            ),
                        )
                    out.write(chunk)
        except HTTPException:
            dest_path.unlink(missing_ok=True)
            try:
                dest_dir.rmdir()
            except OSError:
                pass
            raise
        finally:
            await file.close()

        if size == 0:
            dest_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Uploaded file is empty")

        print(
            f"[upload] {file.filename} ({size / (1024 * 1024):.1f} MB) -> {dest_path}",
            flush=True,
        )
        return {
            "video_id": video_id,
            "filename": file.filename,
            "size": size,
            "local_path": str(dest_path),
        }

    # -- clip serving (public — videos playable without an API key) ----------
    @app.get("/clip/{video_id}/{filename}")
    def serve_clip(video_id: str, filename: str):
        root = Path(SHORTS_VIDEOS_DIR).resolve()
        name = os.path.basename(filename)
        target = (root / video_id / name).resolve()
        if not str(target).startswith(str(root)) or not target.is_file():
            raise HTTPException(status_code=404, detail="Clip not found")
        return FileResponse(target, media_type="video/mp4")

    @app.get("/uploads/{video_id}/{filename}")
    def serve_upload(video_id: str, filename: str):
        """Serve an uploaded source video (stored path from the DB record)."""
        record = storage.get_video(video_id)
        src = (record or {}).get("source_video_url") or ""
        upload_root = Path(SHORTS_UPLOADS_DIR).resolve()
        target = Path(src).resolve()
        if (
            not src
            or not str(target).startswith(str(upload_root))
            or os.path.basename(target.name) != os.path.basename(filename)
            or not target.is_file()
        ):
            raise HTTPException(status_code=404, detail="Upload not found")
        media_type = "video/mp4" if target.suffix.lower() == ".mp4" else "video/" + target.suffix.lstrip(".").lower()
        return FileResponse(str(target), media_type=media_type)

    # -- web UI ---------------------------------------------------------------
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        index_file = STATIC_DIR / "index.html"
        if not index_file.is_file():
            return {"detail": "Web UI not built"}
        return FileResponse(str(index_file), media_type="text/html")

    return app
