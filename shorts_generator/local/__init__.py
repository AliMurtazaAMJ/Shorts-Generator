"""Local backends — download, transcribe, rank, and crop on your machine.

The default path uses a single OpenAI-compatible gateway for both the Whisper
transcription and the highlight LLM. Requires the deps in requirements.txt
(yt-dlp, openai) plus ffmpeg on PATH.
"""