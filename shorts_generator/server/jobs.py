"""Background job manager: queues pipeline runs in a thread pool.

Each job targets a video identified by its source URL. Concurrent requests are
handled by a ``ThreadPoolExecutor``; jobs for the *same* video are additionally
serialized with a per-video lock so two runs never fight over the same files
or DB record. Results are persisted to SQLite and optionally pushed to a
webhook URL on completion.
"""
import contextlib
import hashlib
import io
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..config import (
    DEVELOPER_CREDIT,
    SHORTS_MAX_WORKERS,
    SHORTS_PUBLIC_BASE,
    SHORTS_VIDEOS_DIR,
)
from . import storage


def video_id_for(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _LogBuffer(io.StringIO):
    """StringIO that streams complete log lines to the DB as they're printed.

    The pipeline writes progress via ``print(..., flush=True)`` under a
    ``contextlib.redirect_stdout``; this sink persists each full line to the
    ``logs`` table immediately so pollers see live step-by-step progress
    instead of a single dump at completion.
    """

    def __init__(self, video_id: str, job_id: str):
        super().__init__()
        self._video_id = video_id
        self._job_id = job_id
        self._carry = ""

    def write(self, s: str) -> int:
        super().write(s)
        self._carry += s
        lines = self._carry.split("\n")
        self._carry = lines.pop()
        for line in lines:
            line = line.strip()
            if line:
                self._persist(line)
        return len(s)

    def flush(self):
        line = self._carry.strip()
        if line:
            self._persist(line)
        self._carry = ""

    def _persist(self, line: str):
        try:
            storage.append_log(self._video_id, self._job_id, "INFO", line)
        except Exception:
            pass


class Job:
    __slots__ = (
        "id", "video_id", "status", "params", "result", "error",
        "webhook_url", "created_at", "started_at", "finished_at", "_cancelled",
    )

    def __init__(self, video_id: str, params: Dict, webhook_url: Optional[str]):
        self.id = uuid.uuid4().hex
        self.video_id = video_id
        self.status = "queued"
        self.params = dict(params)
        self.result: Optional[Dict] = None
        self.error: Optional[str] = None
        self.webhook_url = webhook_url
        self.created_at = _now()
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self._cancelled = False

    def to_dict(self) -> Dict:
        return {
            "job_id": self.id,
            "video_id": self.video_id,
            "status": self.status,
            "url": self.params.get("url"),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
            "credit": DEVELOPER_CREDIT,
        }

    def to_webhook_dict(self) -> Dict:
        """Slim payload for webhook delivery.

        Omits the full transcript and candidate highlights (kept in SQLite and
        still retrievable via GET /api/jobs/{job_id}); posts just the job
        summary plus the rendered shorts with their public served URLs.
        """
        result = self.result or {}
        shorts = []
        for clip in result.get("shorts", []):
            shorts.append(
                {
                    "title": clip.get("title"),
                    "score": clip.get("score"),
                    "start_time": clip.get("start_time"),
                    "end_time": clip.get("end_time"),
                    "hook_sentence": clip.get("hook_sentence"),
                    "virality_reason": clip.get("virality_reason"),
                    "filename": clip.get("filename"),
                    "served_url": clip.get("served_url"),
                }
            )
        return {
            "job_id": self.id,
            "video_id": self.video_id,
            "status": self.status,
            "url": self.params.get("url"),
            "source_title": result.get("source_title"),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "shorts": shorts,
            "credit": DEVELOPER_CREDIT,
        }


class JobManager:
    def __init__(self, max_workers: int = SHORTS_MAX_WORKERS):
        storage.init_db()
        self._jobs: Dict[str, Job] = {}
        self._jobs_lock = threading.Lock()
        self._video_locks: Dict[str, threading.Lock] = {}
        self._video_locks_guard = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="shorts-job"
        )

    # -- locking ------------------------------------------------------------
    def _lock_for_video(self, video_id: str) -> threading.Lock:
        with self._video_locks_guard:
            lock = self._video_locks.get(video_id)
            if lock is None:
                lock = threading.Lock()
                self._video_locks[video_id] = lock
            return lock

    # -- lifecycle ----------------------------------------------------------
    def create(self, params: Dict, webhook_url: Optional[str] = None) -> Job:
        url = params["url"]
        job = Job(video_id_for(url), params, webhook_url)
        with self._jobs_lock:
            self._jobs[job.id] = job
        source_path = url if os.path.isfile(url) else ""
        storage.upsert_video(job.video_id, job.id, url, source_path, status="queued")
        storage.append_log(job.video_id, job.id, "INFO", f"job {job.id} queued for {url}")
        self._executor.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._jobs_lock:
            return self._jobs.get(job_id)

    def list(self) -> List[Job]:
        with self._jobs_lock:
            return list(self._jobs.values())

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status != "queued":
            return False
        job._cancelled = True
        job.status = "cancelled"
        job.finished_at = _now()
        storage.update_status(job.video_id, "cancelled", error="cancelled before start")
        storage.append_log(job.video_id, job.id, "WARN", "job cancelled by user")
        return True

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    # -- worker -------------------------------------------------------------
    def _run(self, job: Job) -> None:
        if job._cancelled:
            return
        job.status = "running"
        job.started_at = _now()
        storage.update_status(job.video_id, "running")
        storage.append_log(job.video_id, job.id, "INFO", "job started")

        video_dir = os.path.join(SHORTS_VIDEOS_DIR, job.video_id)
        log_buffer = _LogBuffer(job.video_id, job.id)
        try:
            with self._lock_for_video(job.video_id):
                if job._cancelled:
                    return
                with contextlib.redirect_stdout(log_buffer):
                    from ..pipeline import generate_shorts

                    result = generate_shorts(
                        youtube_url=job.params["url"],
                        num_clips=int(job.params.get("num_clips", 3)),
                        aspect_ratio=job.params.get("aspect_ratio", "9:16"),
                        language=job.params.get("language") or None,
                        detect_captions=bool(job.params.get("detect_captions", True)),
                        burn_captions=bool(job.params.get("burn_captions", True)),
                        force_captions=bool(job.params.get("force_captions", False)),
                        caption_options=job.params.get("caption_options") or None,
                        focus=job.params.get("focus") or None,
                        out_dir=video_dir,
                        run_id=job.id,
                    )

                for clip in result.get("shorts", []):
                    clip_url = clip.get("clip_url")
                    if clip_url and isinstance(clip_url, str):
                        filename = clip_url.rsplit("/", 1)[-1]
                        clip["served_url"] = (
                            f"{SHORTS_PUBLIC_BASE}/clip/{job.video_id}/{filename}"
                        )
                        clip["filename"] = filename

                storage.save_result(job.video_id, result)
                storage.update_status(
                    job.video_id, "completed",
                    has_burned_captions=result.get("has_burned_captions"),
                )
                job.result = result
                job.status = "completed"
                job.finished_at = _now()
                storage.append_log(
                    job.video_id, job.id, "INFO",
                    f"job completed with {len(result.get('shorts', []))} clips",
                )
        except Exception as e:
            job.error = str(e)
            job.status = "failed"
            job.finished_at = _now()
            storage.update_status(job.video_id, "failed", error=str(e))
            storage.append_log(job.video_id, job.id, "ERROR", f"job failed: {e}")
        finally:
            try:
                log_buffer.flush()
            except Exception:
                pass

        if job.status in ("completed", "failed") and job.webhook_url:
            self._fire_webhook(job)

    def _fire_webhook(self, job: Job) -> None:
        def _post():
            try:
                import requests

                requests.post(
                    job.webhook_url,
                    json=job.to_webhook_dict(),
                    timeout=30,
                )
            except Exception as e:
                storage.append_log(
                    job.video_id, job.id, "ERROR", f"webhook delivery failed: {e}"
                )

        threading.Thread(target=_post, daemon=True).start()
