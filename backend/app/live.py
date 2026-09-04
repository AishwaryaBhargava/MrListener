"""Live transcription while a meeting is still recording.

One asyncio task per active meeting, started when the recording WebSocket
connects and cancelled when it closes or /stop is pressed. Every
``LIVE_CHUNK_SECONDS`` the task:

1. decodes ``raw.webm`` from ``last_end`` to EOF into a 16 kHz mono WAV,
2. sends that window to Groq Whisper,
3. offsets the returned segment times by the window start,
4. persists the running transcript and pushes the new segments to the browser,
5. asks :mod:`.suggestions` whether it is time to work out what the user should
   ask next - always as a separate task, never in line with the window.

The window is extracted by decoding the whole webm every tick rather than by
transcribing individual blobs: MediaRecorder's blobs after the first are not
independently decodable. That costs a full opus decode per tick, which is cheap
relative to a 20 s wall-clock budget but does grow with meeting length.

Sessions outlive the socket on purpose. The browser closes the socket *before*
it calls POST /stop, and the pipeline needs ``last_end`` to transcribe the tail.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import anyio
from starlette.websockets import WebSocket

from . import config, groq_client, settings, suggestions, transcripts
from .audio import AudioError, extract_window
from .db import SessionLocal
from .models import Meeting

log = logging.getLogger("mrlistener.live")


@dataclass
class LiveSession:
    meeting_id: str
    websocket: WebSocket | None = None
    #: End of the audio already transcribed, in seconds from the recording start.
    last_end: float = 0.0
    segments: list[dict] = field(default_factory=list)
    language: str | None = None
    task: asyncio.Task | None = None
    #: Serializes the periodic window and the /stop tail pass.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Only the first Groq failure is surfaced to the UI; retries are silent.
    error_sent: bool = False


_sessions: dict[str, LiveSession] = {}


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def start(meeting_id: str, websocket: WebSocket) -> LiveSession:
    """Attach a socket to a (new or resumed) session and start its loop."""
    session = _sessions.get(meeting_id)
    if session is None:
        session = LiveSession(meeting_id=meeting_id)
        _sessions[meeting_id] = session
    else:
        _cancel(session)

    session.websocket = websocket
    session.error_sent = False
    session.task = asyncio.create_task(_loop(session), name=f"live-{meeting_id}")
    return session


def detach(meeting_id: str, websocket: WebSocket | None = None) -> None:
    """The socket went away. Stop ticking but keep ``last_end`` for the pipeline."""
    session = _sessions.get(meeting_id)
    if session is None:
        return
    if websocket is not None and session.websocket is not websocket:
        # A newer socket already took over this meeting; leave it running.
        return
    _cancel(session)
    session.websocket = None


def get(meeting_id: str) -> LiveSession | None:
    """The live session for a meeting that is still recording, if any."""
    return _sessions.get(meeting_id)


def take(meeting_id: str) -> LiveSession | None:
    """Remove and return the session, so the pipeline owns it from here on."""
    session = _sessions.pop(meeting_id, None)
    if session is not None:
        _cancel(session)
        session.websocket = None
    return session


def discard(meeting_id: str) -> None:
    take(meeting_id)
    suggestions.discard(meeting_id)


def _cancel(session: LiveSession) -> None:
    task = session.task
    session.task = None
    if task is not None and not task.done():
        task.cancel()


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


async def _send(session: LiveSession, payload: dict[str, Any]) -> None:
    socket = session.websocket
    if socket is None:
        return
    try:
        await socket.send_json(payload)
    except Exception:  # noqa: BLE001 - a dead socket must never break recording
        session.websocket = None


async def send_status(session: LiveSession, stage: str) -> None:
    await _send(session, {"type": "status", "stage": stage})


async def send_suggestions(session: LiveSession, batch: dict[str, Any]) -> None:
    """Push one finished suggestion batch down the recording socket."""
    await _send(
        session,
        {
            "type": "suggestions",
            "items": batch.get("items") or [],
            "generated_at": batch.get("generated_at"),
            "transcript_end": batch.get("transcript_end"),
        },
    )


def push_suggestions(session: LiveSession):
    """An ``on_batch`` callback bound to this session's socket."""

    async def deliver(batch: dict[str, Any]) -> None:
        await send_suggestions(session, batch)

    return deliver


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


async def _loop(session: LiveSession) -> None:
    try:
        while True:
            # Read every tick, so changing it on the Settings page takes
            # effect on the next window rather than the next recording.
            await asyncio.sleep(settings.live_window_seconds())
            await transcribe_window(session, config.LIVE_MIN_WINDOW_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("live transcription loop failed for meeting %s", session.meeting_id)


async def transcribe_window(session: LiveSession, minimum: float) -> list[dict]:
    """Transcribe [last_end, EOF). Returns the new segments (possibly empty).

    Never raises: recording must survive a transcription outage. A Groq failure
    is reported once over the socket and retried on the next tick.
    """
    async with session.lock:
        raw = config.raw_path(session.meeting_id)
        window = config.live_window_path(session.meeting_id)
        start_at = session.last_end

        try:
            duration = await anyio.to_thread.run_sync(extract_window, raw, window, start_at)
        except AudioError as exc:
            await _report_error(session, str(exc))
            return []

        # None -> ffmpeg cannot decode the file yet; too short -> not worth a call.
        if duration is None or duration < minimum:
            return []

        try:
            result = await anyio.to_thread.run_sync(
                groq_client.transcribe, window, start_at, None
            )
        except groq_client.TranscriptionError as exc:
            await _report_error(session, str(exc))
            return []
        except Exception as exc:  # noqa: BLE001
            await _report_error(session, f"Transcription failed: {exc}")
            return []

        # Advance regardless of whether anything came back, so a silent window
        # is not re-sent to Groq forever.
        session.last_end = start_at + duration
        session.error_sent = False
        if result.get("language"):
            session.language = result["language"]

        new_segments = [item for item in result.get("segments") or [] if item.get("text")]
        if not new_segments:
            return []

        base = len(session.segments)
        for offset, item in enumerate(new_segments):
            item["id"] = base + offset
        session.segments.extend(new_segments)

        _persist(session)
        await _send(session, {"type": "transcript", "segments": new_segments})

        # Fire and forget: suggestions run as their own task so a slow chat
        # completion can never delay the next transcription window. Only while
        # a socket is attached, though - the tail pass the pipeline runs after
        # /stop has nobody left to tell, and its state was already frozen.
        if session.websocket is not None:
            suggestions.maybe_refresh(
                session.meeting_id, session.segments, push_suggestions(session)
            )
        return new_segments


async def _report_error(session: LiveSession, message: str) -> None:
    log.warning("meeting %s live transcription: %s", session.meeting_id, message)
    if session.error_sent:
        return
    session.error_sent = True
    await _send(session, {"type": "error", "message": message})


def _persist(session: LiveSession) -> None:
    """Write the running live transcript so a page reload does not lose it."""
    payload = transcripts.build(session.segments, session.language, transcripts.SOURCE_LIVE)
    db = SessionLocal()
    try:
        meeting = db.get(Meeting, session.meeting_id)
        if meeting is None:
            return
        meeting.transcript_json = transcripts.dumps(payload)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("could not persist live transcript for meeting %s", session.meeting_id)
    finally:
        db.close()
