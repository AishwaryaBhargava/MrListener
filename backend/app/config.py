"""Application configuration and filesystem layout.

Paths are resolved relative to the repository root so the app behaves the same
whether uvicorn is launched from ``backend/`` or from the project root.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from dotenv import load_dotenv

NEWLINE = chr(10)

# backend/app/config.py -> backend/app -> backend -> <project root>
APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
ROOT_DIR = BACKEND_DIR.parent

# .env lives at the project root and is shared by every stage.
ENV_PATH = ROOT_DIR / ".env"
load_dotenv(ENV_PATH, override=False)

DATA_DIR = BACKEND_DIR / "data"
AUDIO_DIR = DATA_DIR / "audio"
DB_PATH = DATA_DIR / "mrlistener.db"
DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"

# Filenames inside backend/data/audio/<meeting_id>/
RAW_FILENAME = "raw.webm"
WAV_FILENAME = "audio.wav"

# Target format for every downstream stage (Whisper + pyannote both want this).
WAV_SAMPLE_RATE = 16000
WAV_CHANNELS = 1

# --- Stage 2: transcription -------------------------------------------------
# How much audio each live window covers. The live task wakes on this cadence
# and transcribes everything decoded since the previous window ended.
LIVE_CHUNK_SECONDS = 20

# A window shorter than this is not worth an API call; the task waits a tick.
LIVE_MIN_WINDOW_SECONDS = 1.0
# The remainder pass at /stop accepts a much shorter tail.
LIVE_MIN_TAIL_SECONDS = 0.4

GROQ_MODEL = "whisper-large-v3-turbo"
GROQ_MAX_ATTEMPTS = 3
GROQ_BACKOFF_SECONDS = 1.5
# Scratch file the live task reuses for each extracted window.
LIVE_WINDOW_FILENAME = "live_window.wav"

# Groq rejects uploads over 25 MB (free tier) with HTTP 413. A 16 kHz mono
# 16-bit WAV is ~1.9 MB per minute, so anything past ~13 minutes must be sent
# in pieces. The full-file pass therefore splits the recording into FLAC chunks
# of this many seconds (FLAC keeps the audio lossless at roughly half the size)
# and stitches the timestamps back together.
TRANSCRIBE_CHUNK_SECONDS = 600
# Files at or under this size are sent whole; larger ones are always chunked.
GROQ_MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# --- Stage 4: notes ---------------------------------------------------------
NOTES_MODEL = "llama-3.3-70b-versatile"
#: Groq retires hosted models fairly often and not every account can see every
#: one of them, so the notes step asks the API which of these it may use and
#: takes the first hit. NOTES_MODEL stays first: it is the one this app was
#: tuned against, and an account that has it keeps getting it.
NOTES_MODEL_CANDIDATES = (
    NOTES_MODEL,
    "llama-3.1-70b-versatile",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "llama-3.1-8b-instant",
    # Qwen last: on the free tier it allows only ~1000 output tokens per
    # minute, which a full set of notes can exceed.
    "qwen/qwen3.8-27b",
)
#: Upper bound on the answer. Free-tier models enforce output-token limits per
#: minute and reject a request whose expected output would exceed them, so
#: this has to be explicit and modest.
NOTES_MAX_OUTPUT_TOKENS = 1500
#: Per-model overrides for models whose free-tier output cap is lower than
#: the default; the request is refused outright when max_tokens exceeds it.
NOTES_MAX_OUTPUT_TOKENS_BY_MODEL = {
    "qwen/qwen3.8-27b": 900,
    "qwen/qwen3.6-27b": 900,
}
NOTES_TEMPERATURE = 0.2
NOTES_MAX_ATTEMPTS = 3
NOTES_BACKOFF_SECONDS = 1.5
#: Rough token budget for a single pass. Estimated at 4 characters per token,
#: so anything longer than this many characters goes through map-reduce.
#: Sized for Groq's free tier, where chat models allow only 6-8k tokens per
#: minute per request: one request must stay well under that including the
#: prompt and the answer.
NOTES_SINGLE_PASS_TOKENS = 3_000
NOTES_CHARS_PER_TOKEN = 4
NOTES_SINGLE_PASS_CHARS = NOTES_SINGLE_PASS_TOKENS * NOTES_CHARS_PER_TOKEN
#: Characters per map chunk (~3.5k tokens of transcript plus the prompt).
NOTES_CHUNK_CHARS = 9_000
#: A 429 whose retry hint is at most this long is a per-minute limit: wait it
#: out on the same model. Longer hints mean the daily budget is gone: switch.
MODEL_SHORT_WAIT_SECONDS = 120
#: How many per-minute waits one request may sit through before giving up.
MODEL_MAX_SHORT_WAITS = 6

# --- Live follow-up suggestions --------------------------------------------
#: Suggestions reuse the notes model and its fallback list, but run hotter:
#: they are a brainstorm, not a record, and 0.2 makes them repeat themselves.
SUGGESTIONS_TEMPERATURE = 0.3
#: Live suggestions run many times per meeting, so they get their own, cheaper
#: model list. Groq rate limits are per model, which keeps an hour of live
#: refreshes from exhausting the daily budget the notes need afterwards.
SUGGESTIONS_MODEL_CANDIDATES = (
    "openai/gpt-oss-20b",
    "llama-3.1-8b-instant",
    "qwen/qwen3.6-27b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
)
#: How long a model that answered 429 is skipped before being tried again,
#: when the error message does not say.
MODEL_COOLDOWN_SECONDS = 15 * 60
#: Floor for the gap between two automatic refreshes. The settings page can
#: raise it; nothing can lower it below SUGGESTIONS_INTERVAL_MIN.
SUGGESTIONS_MIN_INTERVAL = 45
#: ...and a refresh also needs this much new speech, so a quiet stretch does
#: not spend an API call re-reading the same transcript.
SUGGESTIONS_MIN_NEW_WORDS = 40
#: The tail sent verbatim. Everything older is represented by the rolling
#: context summary instead, which is what keeps the prompt bounded on a long
#: meeting.
SUGGESTIONS_VERBATIM_SECONDS = 5 * 60
#: Re-compress the older transcript into the rolling summary every N refreshes.
SUGGESTIONS_SUMMARY_EVERY = 3
#: Ring-buffer depth held in memory per active meeting.
SUGGESTIONS_HISTORY = 20
#: How many items a batch may contain, and the range the prompt asks for.
SUGGESTIONS_MIN_ITEMS = 3
SUGGESTIONS_MAX_ITEMS = 5
#: Cap on the rolling summary, so it can never grow into the prompt budget.
SUGGESTIONS_SUMMARY_CHARS = 1200

# --- Stage 5: shipping ------------------------------------------------------
APP_VERSION = "0.5.0"
#: Built frontend, served by uvicorn in production mode (npm start).
FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"

CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)


def meeting_dir(meeting_id: str) -> Path:
    return AUDIO_DIR / meeting_id


def raw_path(meeting_id: str) -> Path:
    return meeting_dir(meeting_id) / RAW_FILENAME


def wav_path(meeting_id: str) -> Path:
    return meeting_dir(meeting_id) / WAV_FILENAME


def live_window_path(meeting_id: str) -> Path:
    return meeting_dir(meeting_id) / LIVE_WINDOW_FILENAME


def ffmpeg_bin() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe_bin() -> str | None:
    return shutil.which("ffprobe")


def env_flag(name: str) -> bool:
    """True when the env var is present and non-empty. Keys are never validated."""
    return bool((os.getenv(name) or "").strip())


def groq_api_key() -> str | None:
    """The raw key, or None. Never logged - only ever handed to the SDK."""
    return (os.getenv("GROQ_API_KEY") or "").strip() or None


def hf_token() -> str | None:
    """The raw Hugging Face token, or None. Never logged."""
    for name in ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGINGFACEHUB_API_TOKEN"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return None


def transcript_language() -> str | None:
    """Optional ISO-639-1 hint from the environment.

    The settings table wins over this; see ``settings.language_hint``.
    """
    return (os.getenv("TRANSCRIPT_LANGUAGE") or "").strip() or None


def masked(value: str | None, keep: int = 4) -> str | None:
    """Last ``keep`` characters of a secret, for the settings page. Never the key."""
    if not value:
        return None
    tail = value[-keep:] if len(value) > keep else value
    return tail


def write_env_var(name: str, value: str) -> None:
    """Persist ``name=value`` into the root .env and apply it to this process.

    The file is rewritten line by line so comments, ordering and unrelated
    variables survive. The value is never logged. Applying it to ``os.environ``
    is what makes a key change take effect without a backend restart - every
    reader (:func:`groq_api_key`, :func:`hf_token`) reads the environment on
    each call rather than caching.
    """
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    pattern = re.compile(rf"^\s*(export\s+)?{re.escape(name)}\s*=")
    replaced = False
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = f"{name}={value}"
            replaced = True
            break
    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{name}={value}")

    ENV_PATH.write_text(NEWLINE.join(lines) + NEWLINE, encoding="utf-8")
    os.environ[name] = value
