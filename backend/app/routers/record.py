"""WebSocket ingest for live MediaRecorder chunks, and the live transcript feed.

Client -> server: binary frames, appended to ``raw.webm`` and flushed
immediately, so a dropped connection never costs more than the frame in flight.

Server -> client: JSON text frames.

===================  ==================================================
``{"type": "status", "stage": "..."}``   what the backend is doing
``{"type": "transcript", "segments": [...]}``   a finished live window
``{"type": "suggestions", "items": [...],
   "generated_at": "...", "transcript_end": 96.4}``   what to ask next
``{"type": "error", "message": "..."}``  transcription trouble; recording
                                         carries on regardless
===================  ==================================================

The REST ``/stop`` endpoint - not this socket - remains the source of truth for
finalizing a meeting.
"""

from __future__ import annotations

import json
import logging

import aiofiles
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import config, live
from ..db import SessionLocal
from ..models import Meeting

router = APIRouter()
log = logging.getLogger("mrlistener.record")


def _meeting_exists(meeting_id: str) -> bool:
    db = SessionLocal()
    try:
        return db.get(Meeting, meeting_id) is not None
    finally:
        db.close()


@router.websocket("/ws/record/{meeting_id}")
async def record_socket(websocket: WebSocket, meeting_id: str) -> None:
    if not _meeting_exists(meeting_id):
        await websocket.close(code=4404, reason="Unknown meeting")
        return

    await websocket.accept()
    folder = config.meeting_dir(meeting_id)
    folder.mkdir(parents=True, exist_ok=True)
    raw = config.raw_path(meeting_id)

    # Ticks every LIVE_CHUNK_SECONDS and pushes transcript frames down this
    # same socket. It outlives the socket: the browser closes here *before* it
    # calls POST /stop, which needs the session to transcribe the tail.
    session = live.start(meeting_id, websocket)
    await live.send_status(session, "listening")

    bytes_written = 0
    # "ab" so a reconnect mid-meeting continues the same file.
    handle = await aiofiles.open(raw, "ab")
    try:
        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            chunk = message.get("bytes")
            if chunk:
                await handle.write(chunk)
                await handle.flush()
                bytes_written += len(chunk)
                continue

            text = message.get("text")
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "stop":
                # Optional courtesy signal; the client still calls POST /stop.
                await websocket.send_json({"type": "stopped", "bytes": bytes_written})
                break
            if payload.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - never lose audio because of a socket error
        log.exception("record socket error for meeting %s", meeting_id)
    finally:
        await handle.close()
        # Stop ticking, but leave the session in the registry for POST /stop.
        live.detach(meeting_id, websocket)
        log.info("meeting %s: wrote %d bytes to %s", meeting_id, bytes_written, raw.name)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - already closed
            pass
