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

#: Where the SQLite database and the recorded audio live. Overridable with
#: MRLISTENER_DATA_DIR so a test or a second instance can be pointed at a
#: scratch folder without ever touching the real library. A relative value is
#: resolved against the project root, not the current working directory, so it
#: means the same thing whether uvicorn starts in backend/ or at the root.
_DATA_DIR_ENV = (os.getenv("MRLISTENER_DATA_DIR") or "").strip()
DATA_DIR = (
    (Path(_DATA_DIR_ENV) if Path(_DATA_DIR_ENV).is_absolute() else ROOT_DIR / _DATA_DIR_ENV)
    if _DATA_DIR_ENV
    else BACKEND_DIR / "data"
)
AUDIO_DIR = DATA_DIR / "audio"
DB_PATH = DATA_DIR / "mrlistener.db"
DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"

# Filenames inside backend/data/audio/<meeting_id>/
RAW_FILENAME = "raw.webm"
WAV_FILENAME = "audio.wav"
#: An uploaded recording is kept beside audio.wav under this stem plus its
#: original extension, so the file the user handed over is never lost.
UPLOAD_STEM = "upload"

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

# --- Uploaded recordings ----------------------------------------------------
#: Containers ffmpeg can decode that the upload endpoint accepts. Anything else
#: is refused with a 415 before a single byte reaches the disk. Video files are
#: accepted too: only the first audio stream is kept.
UPLOAD_EXTENSIONS = (
    "mp3", "m4a", "aac", "wav", "flac", "ogg", "opus",
    "webm", "mp4", "mov", "mkv", "wma", "aiff",
)
#: Hard ceiling on one upload. Enforced while streaming to disk, so an
#: oversized file is abandoned partway rather than written out in full.
UPLOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024
#: How much of the request body is moved per write.
UPLOAD_CHUNK_BYTES = 1024 * 1024

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

#: How long a model that answered 429 is skipped before being tried again,
#: when the error message does not say.
MODEL_COOLDOWN_SECONDS = 15 * 60

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


def upload_path(meeting_id: str, extension: str) -> Path:
    """``backend/data/audio/<id>/upload.<ext>`` - the file as it was sent."""
    return meeting_dir(meeting_id) / f"{UPLOAD_STEM}.{extension.lstrip('.').lower()}"


def stored_upload(meeting_id: str) -> Path | None:
    """The kept original for this meeting, whatever container it arrived in."""
    for candidate in sorted(meeting_dir(meeting_id).glob(f"{UPLOAD_STEM}.*")):
        if candidate.is_file():
            return candidate
    return None


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
