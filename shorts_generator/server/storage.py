"""SQLite persistence for processed videos.

A video is identified by a deterministic id derived from its source URL
(``sha256(url)[:16]``), so re-running the same URL upserts the same record.
Records, clips, and logs are never deleted. SQLite runs in WAL mode and all
writes are serialized with a module-level lock so the web server (multiple
worker threads) can access it safely.
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..config import SHORTS_DB_PATH

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(SHORTS_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    import os
    from pathlib import Path

    parent = Path(SHORTS_DB_PATH).parent
    if str(parent):
        os.makedirs(parent, exist_ok=True)

    with _lock, _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id                  TEXT PRIMARY KEY,
                job_id              TEXT,
                url                 TEXT NOT NULL UNIQUE,
                source_video_url    TEXT,
                source_title        TEXT,
                status              TEXT NOT NULL,
                created_at          TEXT NOT NULL,
                completed_at        TEXT,
                error               TEXT,
                has_burned_captions INTEGER,
                metadata_json       TEXT
            );

            CREATE TABLE IF NOT EXISTS clips (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id         TEXT NOT NULL,
                title            TEXT,
                score            INTEGER,
                start_time       REAL,
                end_time         REAL,
                hook_sentence    TEXT,
                virality_reason  TEXT,
                filename         TEXT NOT NULL,
                served_url       TEXT NOT NULL,
                created_at       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id   TEXT,
                job_id     TEXT,
                level      TEXT NOT NULL,
                message    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_clips_video ON clips(video_id);
            CREATE INDEX IF NOT EXISTS idx_logs_video ON logs(video_id);
            """
        )
        # Migration for pre-existing databases that predate source_title.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(videos)").fetchall()}
        if "source_title" not in columns:
            conn.execute("ALTER TABLE videos ADD COLUMN source_title TEXT")


def upsert_video(video_id: str, job_id: str, url: str, source_path: str, status: str = "queued") -> None:
    now = _now()
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO videos (id, job_id, url, source_video_url, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                job_id = excluded.job_id,
                source_video_url = excluded.source_video_url,
                status = excluded.status,
                error = NULL,
                completed_at = NULL,
                metadata_json = NULL
            """,
            (video_id, job_id, url, source_path, status, now),
        )


def update_status(
    video_id: str,
    status: str,
    error: Optional[str] = None,
    has_burned_captions: Optional[bool] = None,
) -> None:
    completed_at = _now() if status in ("completed", "failed", "cancelled") else None
    sets = ["status = ?", "completed_at = ?", "error = ?"]
    values: List = [status, completed_at, error]
    if has_burned_captions is not None:
        sets.append("has_burned_captions = ?")
        values.append(1 if has_burned_captions else 0)
    values.append(video_id)
    with _lock, _connect() as conn:
        conn.execute(
            f"UPDATE videos SET {', '.join(sets)} WHERE id = ?",
            values,
        )


def save_result(video_id: str, result: Dict) -> None:
    """Persist the pipeline result: metadata blob plus per-clip rows."""
    now = _now()
    metadata_json = json.dumps(result, ensure_ascii=False)
    has_burned = result.get("has_burned_captions")
    source_title = result.get("source_title")
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE videos SET metadata_json = ?, has_burned_captions = ?, source_title = ? WHERE id = ?",
            (
                metadata_json,
                (1 if has_burned else 0) if has_burned is not None else None,
                source_title,
                video_id,
            ),
        )
        conn.execute("DELETE FROM clips WHERE video_id = ?", (video_id,))
        for clip in result.get("shorts", []):
            filename = clip.get("clip_url")
            if filename and isinstance(filename, str):
                filename = filename.rsplit("/", 1)[-1]
            else:
                continue
            conn.execute(
                """
                INSERT INTO clips
                    (video_id, title, score, start_time, end_time,
                     hook_sentence, virality_reason, filename, served_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video_id,
                    clip.get("title"),
                    clip.get("score"),
                    clip.get("start_time"),
                    clip.get("end_time"),
                    clip.get("hook_sentence"),
                    clip.get("virality_reason"),
                    filename,
                    clip.get("served_url", ""),
                    now,
                ),
            )


def append_log(video_id: Optional[str], job_id: Optional[str], level: str, message: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO logs (video_id, job_id, level, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (video_id, job_id, level, message, _now()),
        )


def _row_to_video(row: sqlite3.Row) -> Dict:
    video = dict(row)
    video["has_burned_captions"] = (
        bool(video.get("has_burned_captions")) if video.get("has_burned_captions") is not None else None
    )
    if video.get("metadata_json"):
        try:
            video["metadata_json"] = json.loads(video["metadata_json"])
        except json.JSONDecodeError:
            video["metadata_json"] = None
    return video


def get_video(video_id: str) -> Optional[Dict]:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        if row is None:
            return None
        video = _row_to_video(row)
        clips = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM clips WHERE video_id = ? ORDER BY id", (video_id,)
            ).fetchall()
        ]
        video["clips"] = clips
        return video


def find_by_job(job_id: str) -> Optional[Dict]:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM videos WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        video = _row_to_video(row)
        clips = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM clips WHERE video_id = ? ORDER BY id", (video["id"],)
            ).fetchall()
        ]
        video["clips"] = clips
        return video


def get_logs(video_id: str) -> List[Dict]:
    with _lock, _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT level, message, created_at FROM logs WHERE video_id = ? ORDER BY id",
                (video_id,),
            ).fetchall()
        ]


def list_videos(limit: int = 50) -> List[Dict]:
    """Return recent video records (newest first) with their clips attached."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM videos ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        clips = conn.execute(
            """
            SELECT * FROM clips
            WHERE video_id IN (
                SELECT id FROM (SELECT id FROM videos ORDER BY created_at DESC LIMIT ?)
            )
            ORDER BY id
            """,
            (limit,),
        ).fetchall()

    by_video: Dict[str, List[Dict]] = {}
    for c in clips:
        by_video.setdefault(c["video_id"], []).append(dict(c))

    videos = []
    for row in rows:
        video = _row_to_video(row)
        video["clips"] = by_video.get(video["id"], [])
        videos.append(video)
    return videos
