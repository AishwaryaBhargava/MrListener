"""Groq Whisper transcription.

One synchronous helper (:func:`transcribe`) that uploads a WAV and returns the
normalized ``{"segments": [...], "language": ...}`` shape the rest of the app
stores. Callers run it off the event loop with ``anyio.to_thread.run_sync``.

The API key is read from the environment on every call so editing ``.env`` and
restarting is the only thing needed to change it. It is never logged.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from . import config, settings

log = logging.getLogger("mrlistener.groq")


class TranscriptionError(RuntimeError):
    """Raised for a missing key, an unusable file, or an exhausted retry budget."""


class MissingKeyError(TranscriptionError):
    pass


def key_is_set() -> bool:
    return config.groq_api_key() is not None


def _client():
    key = config.groq_api_key()
    if not key:
        raise MissingKeyError(
            "GROQ_API_KEY is not set. Add it to the .env file at the project root "
            "and restart the backend."
        )
    try:
        from groq import Groq
    except ImportError as exc:  # pragma: no cover - install-time problem
        raise TranscriptionError(
            "The groq package is not installed. Run: "
            "backend\\.venv\\Scripts\\python -m pip install -r backend\\requirements.txt"
        ) from exc
    return Groq(api_key=key)


def _as_dict(payload: Any) -> dict:
    """The SDK may hand back a pydantic model, a dataclass-ish object or a dict."""
    if isinstance(payload, dict):
        return payload
    for attribute in ("model_dump", "to_dict", "dict"):
        method = getattr(payload, attribute, None)
        if callable(method):
            try:
                result = method()
            except TypeError:
                continue
            if isinstance(result, dict):
                return result
    return dict(getattr(payload, "__dict__", {}) or {})


def _normalize(payload: Any, offset: float) -> dict:
    """Whisper's verbose_json -> our stored shape, with ``offset`` added to times.

    Segment ids are renumbered by the caller once every window is assembled, so
    the ids here are only locally unique.
    """
    body = _as_dict(payload)
    raw_segments = body.get("segments") or []

    segments: list[dict] = []
    for index, item in enumerate(raw_segments):
        entry = _as_dict(item)
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(entry.get("start") or 0.0)
            end = float(entry.get("end") or 0.0)
        except (TypeError, ValueError):
            continue
        segments.append(
            {
                "id": index,
                "start": round(start + offset, 3),
                "end": round(max(end, start) + offset, 3),
                "text": text,
            }
        )

    # Some very short clips come back with text but no segment array.
    if not segments:
        whole = str(body.get("text") or "").strip()
        if whole:
            duration = float(body.get("duration") or 0.0)
            segments.append(
                {
                    "id": 0,
                    "start": round(offset, 3),
                    "end": round(offset + duration, 3),
                    "text": whole,
                }
            )

    return {"segments": segments, "language": body.get("language")}


def transcribe(path: Path, offset: float = 0.0, language: str | None = None) -> dict:
    """Transcribe a WAV file. Blocking - call from a worker thread.

    Retries transient failures three times with exponential backoff. A missing
    key fails immediately: no amount of retrying will fix it.
    """
    if not path.exists() or path.stat().st_size == 0:
        raise TranscriptionError(f"{path.name} is missing or empty")

    client = _client()
    payload = path.read_bytes()
    hint = language if language is not None else settings.language_hint()

    last_error: Exception | None = None
    for attempt in range(1, config.GROQ_MAX_ATTEMPTS + 1):
        try:
            kwargs: dict[str, Any] = {
                "file": (path.name, payload),
                "model": config.GROQ_MODEL,
                "response_format": "verbose_json",
            }
            if hint:
                kwargs["language"] = hint
            response = client.audio.transcriptions.create(**kwargs)
            return _normalize(response, offset)
        except MissingKeyError:
            raise
        except Exception as exc:  # noqa: BLE001 - SDK raises a wide family
            last_error = exc
            if attempt == config.GROQ_MAX_ATTEMPTS:
                break
            delay = config.GROQ_BACKOFF_SECONDS * (2 ** (attempt - 1))
            log.warning(
                "groq transcription attempt %d/%d failed (%s); retrying in %.1fs",
                attempt,
                config.GROQ_MAX_ATTEMPTS,
                type(last_error).__name__,
                delay,
            )
            time.sleep(delay)

    raise TranscriptionError(
        f"Groq transcription failed after {config.GROQ_MAX_ATTEMPTS} attempts: {last_error}"
    ) from last_error
