# 🎬 AI YouTube Shorts Generator

Turn any YouTube video into scroll-stopping vertical shorts — automatically.
The pipeline downloads the source, transcribes it with Whisper, ranks the most
viral-worthy moments with an LLM, crops them to vertical format with blur bars,
and burns in word-level karaoke captions that track the spoken word.

> **Developed by [AMJ](https://www.linkedin.com/in/alimurtazaamj/)** ·
> <a href="https://www.linkedin.com/in/alimurtazaamj/"><img src="https://img.shields.io/badge/-LinkedIn-0A66C2?logo=linkedin&logoColor=white" alt="LinkedIn" height="18"></a>

---

## ✨ Features

- **Full automated pipeline** — download → transcribe → score → clip → caption, zero manual editing.
- **Virality ranking** — an LLM scores the transcript for hook moments, emotional peaks, opinion bombs and revelations.
- **Vertical clipping with blur bars** — 9:16 (and any custom ratio) with a blurred-background band in pure `ffmpeg`.
- **Karaoke captions** — caption lines stay visible while the currently-spoken word is highlighted; fully optional styling (text / active-word / outline colors, font size).
- **Burned-caption detection** — OCR scans the source so subtitles are only added when the video doesn't already have them.
- **Force-burn mode** — add subtitles even over existing ones (skips OCR for speed).
- **WebUI** — password-gated control panel with live job progress, a media library, and upload support.
- **REST API + webhooks** — submit jobs programmatically (n8n / Zapier ready) and get notified on completion.
- **Concurrent + resumable** — per-video locking, SQLite persistence, and media that is never auto-deleted.

---

## 🎤 Captions

Captions are a first-class part of the pipeline, not an afterthought. A
dedicated, self-contained caption engine turns any transcript into word-timed,
stylable subtitles that get burned straight into the video (no external
subtitle library or dependency).

### Word-level karaoke highlighting

- Every caption line stays **fully visible** on screen.
- The **currently-spoken word** is recolored in `active_color` while it is being
  said, then returns to `text_color` — classic karaoke tracking, computed from
  Whisper segment timings using proportional per-word windows.
- Each word keeps a solid `outline_color` stroke for readability over footage.

### Styling

| Option | Default | Description |
|---|---|---|
| `karaoke` | `true` | Highlight the spoken word as it happens |
| `text_color` | `#FFFFFF` | Normal word color |
| `active_color` | `#FFD700` | Currently-spoken word color |
| `outline_color` | `#000000` | Outline stroke (always on) |
| `font_size` | `48` | Caption size (8–200) |

All options are settable per request via `caption_options` (see the API
reference) or live in the WebUI with an instant preview.

### Clever placement

Captions are anchored to the **top of the blur-bar band** — not the canvas
edge — so they sit right on the blurred strip and never overlap the foreground.
The anchor is computed per source video from its real dimensions (ffprobe),
with a bottom-margin fallback for full-canvas sources.

### Burned-caption awareness

1. **OCR detection (optional)** — RapidOCR scans the source frames; subtitles
   are only burned when the source does *not* already have them.
2. **Force mode** — burn subtitles anyway, and skip OCR entirely to save ~1 min
   per run.
3. **Smart skip** — cleanly skips the burn and reports `captions: "skipped"`.

### Output artifacts

Each rendered short gets `.srt` and `.ass` sidecar files (tracked in SQLite)
and a final video where the captions are burned in via `ffmpeg` + libass (a
single re-encode pass, audio copied losslessly). The per-short result exposes
`caption_srt`, `caption_file` and `caption_cues`, and the job-level result
reports a `captions` status: `"burned"` / `"skipped"` / `"off"`.

### Transcript caching

Downloaded videos and their Whisper transcripts are cached on disk, so
reprocessing the same video skips re-transcription.

> **Note on non-space scripts** — for languages written without spaces
> (Japanese, Chinese, Thai), line splitting can exceed the max line length;
> this matches the upstream conjunction-based splitting behaviour.

## 🔧 How it works

```
YouTube URL / local file
        │
        ▼
 ┌────────────────┐   ┌──────────────────────┐   ┌─────────────────────────┐
 │  yt-dlp        │   │  Whisper (gateway)    │   │  LLM highlight ranking  │
 │  download 720p │──▶│  transcription + time │──▶│  (hook / emotion /      │
 └────────────────┘   │  alignment            │   │   opinion / revelation) │
                      └──────────────────────┘   └────────────┬────────────┘
                                                               ▼
                                        ┌─────────────────────────────────┐
                                        │  ffmpeg vertical crop 9:16      │
                                        │  + blur-bar background          │
                                        └───────────────┬─────────────────┘
                                                        ▼
                               ┌──────────────────────────────────────────┐
                               │  Karaoke caption burn (optional)         │
                               │  + OCR caption detection (optional)      │
                               └───────────────────┬──────────────────────┘
                                                   ▼
                                        Rendered shorts + public URLs
                                        (+ webhook callback when set)
```

Speech-to-text and the highlight LLM both run through a **single OpenAI-compatible
gateway**; every video-processing step is fully local.

---

## 🗂️ Project layout

```
main.py                        # Server entry point (uvicorn)
requirements.txt
.env.example                   # Copy to .env and fill in
shorts_generator/
├── config.py                  # Environment-variable configuration
├── pipeline.py                # End-to-end orchestrator
├── highlights.py              # Virality scoring prompts + logic
├── captions/                  # Self-contained caption engine
│   ├── subtitles.py           #   cue building, .srt/.ass, burn, karaoke
│   └── conjunctions.py        #   per-language line-splitting tables
└── server/
    ├── app.py                 # FastAPI app & routes
    ├── jobs.py                # Background queue, locking, webhooks
    ├── storage.py             # SQLite persistence
    ├── static/                # Web UI (index.html / app.js / styles.css)
    └── shorts_generator/local/  downloader · transcriber · clipper ·
                                  caption_detection · llm · srt_cache
resources/         # downloads/ (source videos + transcript caches),
                   # clips/ (rendered shorts — never deleted), uploads/
shorts_generator.db# SQLite records (videos / clips / logs)
```

---

## 🧰 Tech stack

| Layer | Technology |
|---|---|
| API / server | Python · FastAPI · uvicorn |
| Timecodes + cutting | ffmpeg / ffprobe (subprocess) |
| Download | yt-dlp |
| Transcription | OpenAI-compatible Whisper endpoint (gateway) |
| Highlight LLM | OpenAI-compatible chat-completions endpoint (gateway) |
| Caption detection | RapidOCR (ONNX Runtime) |
| Storage | SQLite |
| Frontend | Plain HTML / CSS / JS (no build step) |

---

## 📦 Prerequisites

- **Python 3.10+**
- **ffmpeg + ffprobe** on `PATH`
  - Ubuntu/Debian: `sudo apt install ffmpeg`
  - macOS: `brew install ffmpeg`
  - Windows: download from ffmpeg.org or `winget install Gyan.FFmpeg`
- An **OpenAI-compatible gateway** exposing both chat completions and
  audio transcriptions (Whisper) — e.g. a hosted LLM + Whisper provider or a
  local one like vLLM / llama.cpp-server.

---

## 🚀 Setup

```bash
# 1. Clone and enter the project
git clone https://github.com/AliMurtazaAMJ/Shorts-Generator && cd Shorts-Generator

# 2. Create a virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate            # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
#      fill in the gateway settings (API_KEY / BASE_URL / LLM_MODEL / Whisper_MODEL)

# 4. Run the server
python main.py
```

On first start the server prints the `X-API-Key` (auto-generated and persisted
to `.env`) — use it to log into the WebUI or call the API.

```
Shorts Generator API listening on http://your-host:8100
X-API-Key: abc123...
```

---

## ⚙️ Configuration

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | — | (required) Gateway key (Whisper + LLM) |
| `BASE_URL` | — | (required) Gateway base URL, e.g. `https://your-gateway.com/v1` |
| `LLM_MODEL` | `gpt-4o-mini` | Highlight-ranking model id |
| `Whisper_MODEL` | `whisper-large-v3-turbo` | Transcription model id |
| `SHORTS_API_KEY` | auto-generated | X-API-Key header for the API |
| `SHORTS_HOST` | `0.0.0.0` | Bind address |
| `SHORTS_PORT` | `8100` | Bind port |
| `SHORTS_MAX_WORKERS` | `4` | Videos processed in parallel |
| `SHORTS_PUBLIC_BASE` | `http://localhost:8100` | Public base URL for served clips (set to what n8n/clients can reach) |
| `SHORTS_DB_PATH` | `shorts_generator.db` | SQLite file location |
| `LOCAL_OUTPUT_DIR` | `resources` | Media root |
| `SHORTS_DOWNLOADS_DIR` | `resources/downloads` | Source videos + transcript caches |
| `SHORTS_VIDEOS_DIR` | `resources/clips` | Rendered shorts (durable) |
| `SHORTS_UPLOADS_DIR` | `resources/uploads` | Uploaded source videos |
| `SHORTS_MAX_UPLOAD_MB` | `2000` | Upload size cap (0 = disabled) |
| `YT_COOKIES_FILE` | auto | Cookies for age-restricted videos |

---

## 🔌 API reference

All `/api/*` routes require the `X-API-Key` header. Rendered clips are served
publicly at `/clip/...` so they can be played/shared directly.

### Authentication

```
X-API-Key: <key from .env / server startup>
```

### `POST /api/verify`
Validates the API key → `200 {"status": "ok"}`.

### `POST /api/jobs`
Submit a video. Example payload:

```json
{
  "url": "https://www.youtube.com/watch?v=BqSxjmvXzzY",
  "num_clips": 2,
  "aspect_ratio": "9:16",
  "format": "720",
  "burn_captions": true,
  "detect_captions": true,
  "force_captions": false,
  "caption_options": {
    "karaoke": true,
    "text_color": "#FFFFFF",
    "active_color": "#FFD700",
    "outline_color": "#000000",
    "font_size": 48
  },
  "focus": "best productivity tips",
  "webhook_url": "https://your-n8n.example/webhook/shorts-done"
}
```

| Body field | Type | Default | Notes |
|---|---|---|---|
| `url` | string | — | YouTube URL or local path |
| `num_clips` | int | `3` | 1–20 |
| `aspect_ratio` | string | `"9:16"` | Any CSS ratio, e.g. `1:1`, `4:5`, `16:9` |
| `format` | string | `"720"` | `360` / `480` / `720` / `1080` |
| `burn_captions` | bool | `true` | Master switch for burning subtitles |
| `detect_captions` | bool | `true` | OCR before burn (only when burning) |
| `force_captions` | bool | `false` | Burn even if captions already exist (skips OCR) |
| `caption_options` | object | defaults | `karaoke`, `text_color`, `active_color`, `outline_color`, `font_size` (8–200) |
| `focus` | string|null | `null` | What the highlights should prioritize |
| `webhook_url` | string|null | `null` | Completion callback URL |

Response:

```json
{
  "job_id": "3f2a…",
  "video_id": "9b1c…",
  "status": "queued",
  "credit": { "name": "AMJ", "linkedin": "https://www.linkedin.com/in/alimurtazaamj/" }
}
```

### `GET /api/jobs/{job_id}`
Poll status + result. Returns job metadata, the full result (`source_title`,
`shorts[]` with served clip URLs), `status` (`queued` / `running` / `completed` /
`failed` / `cancelled`), and the same `credit` object.

### `GET /api/jobs/{job_id}/logs`
Live step-by-step log lines.

### `POST /api/jobs/{job_id}/cancel`
Cancel a queued job.

### `GET /api/media`
Media library — every processed video, its clips, and process data.

### `POST /api/upload`
Upload a local video file (`multipart/form-data`, field `file`) → `{video_id,
filename, size, local_path}`.

### `GET /health`
Unauthenticated liveness probe.

---

## 📡 Webhook callback

When `webhook_url` is set, the server POSTs this payload on completion (and on
failure, with `status: "failed"`):

```json
{
  "job_id": "3f2a…",
  "video_id": "9b1c…",
  "status": "completed",
  "url": "https://www.youtube.com/watch?v=BqSxjmvXzzY",
  "source_title": "Original video title",
  "created_at": "2026-08-21T12:00:00+00:00",
  "started_at": "2026-08-21T12:00:01+00:00",
  "finished_at": "2026-08-21T12:03:42+00:00",
  "error": null,
  "shorts": [
    {
      "title": "Clip title",
      "score": 92,
      "start_time": 125.4,
      "end_time": 145.8,
      "hook_sentence": "The secret is…",
      "virality_reason": "Strong hook — creates immediate curiosity",
      "filename": "slug_3f2a…_0_92.mp4",
      "served_url": "http://your-host:8100/clip/9b1c…/slug_3f2a…_0_92.mp4"
    }
  ],
  "credit": {
    "name": "AMJ",
    "linkedin": "https://www.linkedin.com/in/alimurtazaamj/"
  }
}
```

---

## 🎞️ Captioning decision matrix (API)

How the burner behaves at the field level (see also the [Captions](#-captions)
section for everything the engine does):

| `burn_captions` | `detect_captions` | `force_captions` | Behaviour |
|---|---|---|---|
| `true` | `true` | `false` | OCR the source; burn only if no captions already exist |
| `true` | `false` | — | Burn without the OCR check |
| `true` | — | `true` | Always burn; OCR skipped (~1 min faster), result marked `captions_forced` |
| `false` | — | — | No captions added (`captions: "off"`) |

The job-level result additionally carries `has_burned_captions` when OCR ran,
and `caption_options` echoes back whatever styling was applied so you can
inspect exactly what was produced.

---

## 🖥️ Web UI

Open `http://your-host:8100`, paste the API key, and you get:
- URL or local-file **upload** source tabs
- Clip count, aspect ratio, quality and caption style controls
- **Live progress** with step-by-step logs
- A **media library** with played/downloadable shorts and per-job logs
- Your choices are remembered in the browser between sessions

---

## 📄 License

This project is provided as-is for personal / educational use. All rights
reserved by the author.

---

## 👤 Credits

**Developed by [AMJ](https://www.linkedin.com/in/alimurtazaamj/)** —
an AI Video Shorts automation built with FastAPI, Whisper + LLM gateways,
ffmpeg and yt-dlp.

<p align="center">
  <a href="https://www.linkedin.com/in/alimurtazaamj/">
    <img src="https://img.shields.io/badge/LinkedIn-AMJ-0A66C2?style=for-the-badge&logo=linkedin" alt="LinkedIn">
  </a>
</p>