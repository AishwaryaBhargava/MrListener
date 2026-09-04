"""Domain operations shared by the REST and WebSocket layers."""

from __future__ import annotations

import re
import shutil

import anyio
from sqlalchemy.orm import Session

from . import config, live, pipeline
from .audio import AudioError, convert_to_wav, probe_duration
from .models import (
    STAGE_TRANSCRIBING,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_READY,
    STATUS_RECORDING,
    Meeting,
    new_uuid,
    utc_now_iso,
)

#: Only titles of exactly this shape count towards the sequence, so a renamed
#: meeting stops reserving its number and a deleted one never frees a number
#: that a survivor already uses.
_SEQUENCE_RE = re.compile(r"^Recording (\d+)$")


def next_default_title(db: Session) -> str:
    """"Recording 01", "Recording 02", ... - one past the highest in use."""
    rows = db.query(Meeting.title).filter(Meeting.title.like("Recording %")).all()
    highest = 0
    for (title,) in rows:
        match = _SEQUENCE_RE.match((title or "").strip())
        if match:
            highest = max(highest, int(match.group(1)))
    return f"Recording {highest + 1:02d}"


def create_meeting(db: Session, title: str | None) -> Meeting:
    # created_at is stamped here, i.e. the instant the user pressed start.
    meeting = Meeting(
        id=new_uuid(),
        title=(title or "").strip() or next_default_title(db),
        created_at=utc_now_iso(),
        status=STATUS_RECORDING,
    )
    config.meeting_dir(meeting.id).mkdir(parents=True, exist_ok=True)
    db.add(meeting)
    db.commit()
    db.refresh(meeting)
    return meeting


def delete_meeting(db: Session, meeting: Meeting) -> None:
    folder = config.meeting_dir(meeting.id)
    # Stop anything still writing into that folder before it disappears.
    pipeline.cancel(meeting.id)
    live.discard(meeting.id)
    db.delete(meeting)
    db.commit()
    # Best effort: a locked file must not leave an orphaned DB row behind.
    shutil.rmtree(folder, ignore_errors=True)


async def finalize_meeting(db: Session, meeting: Meeting) -> Meeting:
    """Convert raw.webm -> audio.wav and hand the meeting to the pipeline.

    Leaves the meeting in ``processing``; ``pipeline.run_pipeline`` is what
    marks it ``ready``. Idempotent: a meeting that already has a wav and is
    past recording is returned untouched, so a retried /stop (or a duplicated
    click) is harmless.
    """
    wav = config.wav_path(meeting.id)
    raw = config.raw_path(meeting.id)

    already_converted = wav.exists() and wav.stat().st_size > 0
    if already_converted and meeting.status in (STATUS_READY, STATUS_PROCESSING):
        return meeting

    if meeting.status == STATUS_RECORDING:
        meeting.status = STATUS_PROCESSING
        meeting.pipeline_stage = STAGE_TRANSCRIBING
        meeting.pipeline_error = None
        db.commit()

    try:
        await anyio.to_thread.run_sync(convert_to_wav, raw, wav)
        duration = await anyio.to_thread.run_sync(probe_duration, wav)
    except AudioError as exc:
        meeting.status = STATUS_FAILED
        meeting.pipeline_stage = None
        meeting.pipeline_error = str(exc)
        db.commit()
        db.refresh(meeting)
        raise exc

    meeting.audio_path = str(wav)
    meeting.duration_seconds = duration
    meeting.status = STATUS_PROCESSING
    meeting.pipeline_stage = STAGE_TRANSCRIBING
    db.commit()
    db.refresh(meeting)
    return meeting
