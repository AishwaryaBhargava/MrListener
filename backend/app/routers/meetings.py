from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional

import aiofiles
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session

from .. import (
    config,
    export,
    live,
    notes as notes_mod,
    pipeline,
    services,
    speakers,
    transcripts,
    settings,
)
from ..audio import AudioError, range_file_response
from ..db import get_db
from ..models import SOURCE_UPLOAD, STATUS_RECORDING, Meeting
from ..schemas import (
    ActionItemUpdate,
    MeetingCreate,
    MeetingOut,
    MeetingUpdate,
    SpeakerOut,
    TranscriptOut,
)

router = APIRouter(prefix="/api/meetings", tags=["meetings"])
log = logging.getLogger("mrlistener.meetings")

#: Characters of context on either side of a search hit.
SNIPPET_PAD = 70


def _to_out(meeting: Meeting, detail: bool = False, query: str | None = None) -> MeetingOut:
    wav = config.wav_path(meeting.id)
    out = MeetingOut.model_validate(meeting)
    out.has_audio = wav.exists() and wav.stat().st_size > 0
    out.transcript_preview = transcripts.preview(meeting.transcript_json)
    out.has_transcript = out.transcript_preview is not None

    show = settings.diarization_enabled()
    names = speakers.name_map(meeting.speaker_names_json) if show else {}
    roster = speakers.as_list(meeting.speaker_names_json) if show else []
    out.speaker_count = len(roster)

    payload = notes_mod.loads(meeting.notes_json)
    resolved = notes_mod.resolve(payload, names)
    if resolved and not show:
        resolved = speakers.strip_notes(resolved)
    out.has_notes = bool(resolved and not resolved.get("empty"))
    out.notes_preview = notes_mod.first_sentence(resolved)
    out.action_item_count = len(resolved.get("action_items") or []) if resolved else 0

    if detail:
        out.speakers = [SpeakerOut(**entry) for entry in roster]
        out.notes = resolved
    if query:
        out.match_snippet = _snippet(meeting, query)
    return out


def _snippet(meeting: Meeting, query: str) -> str | None:
    """Text around the first hit, searched in the order the user would expect."""
    needle = query.strip().lower()
    if not needle:
        return None

    haystacks = [
        transcripts.full_text(transcripts.loads(meeting.transcript_json)),
        str((notes_mod.loads(meeting.notes_json) or {}).get("summary") or ""),
        meeting.title or "",
    ]
    for text in haystacks:
        index = text.lower().find(needle)
        if index < 0:
            continue
        start = max(0, index - SNIPPET_PAD)
        end = min(len(text), index + len(needle) + SNIPPET_PAD)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        return f"{prefix}{text[start:end].strip()}{suffix}"
    return None


def _get_or_404(db: Session, meeting_id: str) -> Meeting:
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.post("", response_model=MeetingOut, status_code=201)
def create_meeting(payload: MeetingCreate | None = None, db: Session = Depends(get_db)) -> MeetingOut:
    title = payload.title if payload else None
    return _to_out(services.create_meeting(db, title))


def _upload_extension(filename: str) -> str:
    """The accepted extension of ``filename``, or a 415 explaining why not."""
    extension = Path(filename or "").suffix.lstrip(".").lower()
    if extension in config.UPLOAD_EXTENSIONS:
        return extension
    formats = ", ".join(config.UPLOAD_EXTENSIONS)
    named = f"'.{extension}' files" if extension else "files without an extension"
    raise HTTPException(
        status_code=415,
        detail=f"MrListener cannot read {named}. Supported formats: {formats}.",
    )


@router.post("/upload", response_model=MeetingOut, status_code=201)
async def upload_meeting(
    file: UploadFile = File(..., description="An audio or video file ffmpeg can decode"),
    title: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
) -> MeetingOut:
    """Process an existing recording instead of capturing one live.

    The body is streamed straight to ``backend/data/audio/<id>/upload.<ext>`` a
    megabyte at a time, so a two-hour video never sits in memory, and the size
    cap is checked as it goes rather than after the fact.

    The response comes back the moment the file is on disk, with status
    ``processing`` and stage ``converting``: ffmpeg and then the whole ordinary
    pipeline run in the background while the browser polls the detail page.
    """
    extension = _upload_extension(file.filename or "")

    meeting = services.create_upload_meeting(db, title, file.filename or f"upload.{extension}")
    destination = config.upload_path(meeting.id, extension)
    written = 0

    try:
        async with aiofiles.open(destination, "wb") as handle:
            while chunk := await file.read(config.UPLOAD_CHUNK_BYTES):
                written += len(chunk)
                if written > config.UPLOAD_MAX_BYTES:
                    gigabytes = config.UPLOAD_MAX_BYTES / (1024 ** 3)
                    raise HTTPException(
                        status_code=413,
                        detail=f"That file is larger than the {gigabytes:.0f} GB upload limit.",
                    )
                await handle.write(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="That file is empty.")
    except HTTPException:
        # Nothing was processed, so leave neither a row nor a part-written file.
        services.delete_meeting(db, meeting)
        raise
    except Exception as exc:  # noqa: BLE001 - a dropped connection, a full disk
        services.delete_meeting(db, meeting)
        log.exception("upload failed for meeting %s", meeting.id)
        raise HTTPException(status_code=400, detail=f"The upload did not finish: {exc}") from exc
    finally:
        await file.close()

    log.info("meeting %s: stored %s (%d bytes)", meeting.id, destination.name, written)
    pipeline.schedule(meeting.id, steps=pipeline.UPLOAD_STEPS)
    db.refresh(meeting)
    return _to_out(meeting, detail=True)


@router.get("", response_model=List[MeetingOut])
def list_meetings(
    q: Optional[str] = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
) -> List[MeetingOut]:
    """Newest first, optionally filtered by a full-text-ish search.

    The JSON columns are searched with a plain LIKE over their raw text. That
    matches inside the transcript and the notes without a second index or an
    FTS table, which for a personal library of a few hundred meetings is both
    fast enough and one less thing to keep in sync. SQLite's LIKE is
    case-insensitive for ASCII, which is the behaviour the search box wants.
    """
    query = db.query(Meeting)
    needle = (q or "").strip()
    if needle:
        like = f"%{needle}%"
        query = query.filter(
            or_(
                Meeting.title.like(like),
                Meeting.transcript_json.like(like),
                Meeting.notes_json.like(like),
            )
        )
    rows = query.order_by(desc(Meeting.created_at), desc(Meeting.id)).all()
    return [_to_out(row, query=needle or None) for row in rows]


@router.get("/{meeting_id}", response_model=MeetingOut)
def get_meeting(meeting_id: str, db: Session = Depends(get_db)) -> MeetingOut:
    return _to_out(_get_or_404(db, meeting_id), detail=True)


@router.patch("/{meeting_id}", response_model=MeetingOut)
def rename_meeting(meeting_id: str, payload: MeetingUpdate, db: Session = Depends(get_db)) -> MeetingOut:
    meeting = _get_or_404(db, meeting_id)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Title cannot be empty")
    meeting.title = title
    db.commit()
    db.refresh(meeting)
    return _to_out(meeting, detail=True)


@router.delete("/{meeting_id}", status_code=204)
def delete_meeting(meeting_id: str, db: Session = Depends(get_db)) -> Response:
    services.delete_meeting(db, _get_or_404(db, meeting_id))
    return Response(status_code=204)


@router.post("/{meeting_id}/stop", response_model=MeetingOut)
async def stop_meeting(meeting_id: str, db: Session = Depends(get_db)) -> MeetingOut:
    """Finalize a recording and kick off the pipeline. Idempotent.

    Returns as soon as the wav exists, with status ``processing``; the detail
    page polls until ``run_pipeline`` marks it ``ready``.
    """
    meeting = _get_or_404(db, meeting_id)
    was_recording = meeting.status == STATUS_RECORDING

    # Take the live session away from the WS layer first, so no further window
    # fires while ffmpeg rewrites the audio. Its ``last_end`` lets the pipeline
    # transcribe the tail that no window covered.
    session = live.take(meeting_id) if was_recording else None

    try:
        meeting = await services.finalize_meeting(db, meeting)
    except AudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    pipeline.schedule(meeting_id, session)
    db.refresh(meeting)
    return _to_out(meeting, detail=True)


@router.post("/{meeting_id}/reprocess", response_model=MeetingOut)
async def reprocess_meeting(
    meeting_id: str, keep_transcript: bool = False, db: Session = Depends(get_db)
) -> MeetingOut:
    """Re-run transcription, notes and diarization from the wav.

    With ``keep_transcript=true`` and a transcript already on file, the
    transcription step is skipped and only notes and speakers are redone.

    Async so ``pipeline.schedule`` has the event loop to attach its task to -
    a sync route body runs on the threadpool, where there is none.
    """
    meeting = _require_processable(db, meeting_id)
    if keep_transcript and meeting.transcript_json:
        steps = pipeline.RESUME_STEPS
    elif meeting.source == SOURCE_UPLOAD:
        # The convert step is idempotent, so this only does real work when the
        # wav is missing - which is exactly the case a failed upload leaves.
        steps = pipeline.UPLOAD_STEPS
    else:
        steps = None
    pipeline.mark_processing(meeting_id)
    pipeline.schedule(meeting_id, steps=steps)
    db.refresh(meeting)
    return _to_out(meeting, detail=True)


@router.post("/{meeting_id}/notes/regenerate", response_model=MeetingOut)
async def regenerate_notes(meeting_id: str, db: Session = Depends(get_db)) -> MeetingOut:
    """Re-run only the notes model over the transcript already on file.

    Diarization is skipped, so this is a ~10 second round trip even for a long
    meeting - and the speaker labels it needs are already stored.
    """
    from ..models import STAGE_SUMMARIZING

    meeting = _require_processable(db, meeting_id, needs_audio=False)
    if not meeting.transcript_json:
        raise HTTPException(status_code=400, detail="This meeting has no transcript to summarize")

    pipeline.mark_processing(meeting_id, STAGE_SUMMARIZING)
    pipeline.schedule(meeting_id, steps=pipeline.NOTES_STEPS, notes_only=True)
    db.refresh(meeting)
    return _to_out(meeting, detail=True)


def _require_processable(db: Session, meeting_id: str, needs_audio: bool = True) -> Meeting:
    meeting = _get_or_404(db, meeting_id)
    if meeting.status == STATUS_RECORDING:
        raise HTTPException(status_code=409, detail="This meeting is still recording")
    if pipeline.is_running(meeting_id):
        raise HTTPException(status_code=409, detail="This meeting is already being processed")
    if needs_audio:
        wav = config.wav_path(meeting.id)
        playable = wav.exists() and wav.stat().st_size > 0
        # An upload whose conversion failed has no wav yet but still has the
        # file the user sent, so Reprocess can convert it again.
        if not playable and config.stored_upload(meeting.id) is None:
            raise HTTPException(status_code=400, detail="This meeting has no audio to process")
    return meeting


@router.patch("/{meeting_id}/speakers", response_model=MeetingOut)
def rename_speakers(
    meeting_id: str,
    payload: dict = Body(..., examples=[{"S1": "Priya"}]),
    db: Session = Depends(get_db),
) -> MeetingOut:
    """Rename one or more speakers: ``{"S1": "Priya"}``.

    Only ``speaker_names_json`` changes. Transcript segments keep the ids
    diarization gave them and the names are resolved on every read, so a rename
    is a single small write that can never leave the transcript half-updated.
    """
    meeting = _get_or_404(db, meeting_id)
    updates = {str(k): v for k, v in payload.items() if isinstance(v, (str, int, float))}
    if not updates:
        raise HTTPException(status_code=422, detail='Send a body like {"S1": "Priya"}')

    try:
        renamed = speakers.rename(meeting.speaker_names_json, {k: str(v) for k, v in updates.items()})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    meeting.speaker_names_json = speakers.dumps(renamed)
    db.commit()
    db.refresh(meeting)
    return _to_out(meeting, detail=True)


@router.patch("/{meeting_id}/action_items/{index}", response_model=MeetingOut)
def set_action_item(
    meeting_id: str,
    index: int,
    payload: ActionItemUpdate,
    db: Session = Depends(get_db),
) -> MeetingOut:
    """Tick or untick one action item. The state lives inside ``notes_json``."""
    meeting = _get_or_404(db, meeting_id)
    stored = notes_mod.loads(meeting.notes_json)
    if stored is None:
        raise HTTPException(status_code=404, detail="This meeting has no notes")

    items = stored.get("action_items") or []
    if not 0 <= index < len(items):
        raise HTTPException(status_code=404, detail="No action item at that position")

    items[index]["done"] = payload.done
    stored["action_items"] = items
    meeting.notes_json = json.dumps(stored, ensure_ascii=False)
    db.commit()
    db.refresh(meeting)
    return _to_out(meeting, detail=True)


@router.get("/{meeting_id}/transcript", response_model=TranscriptOut)
def get_transcript(meeting_id: str, db: Session = Depends(get_db)) -> TranscriptOut:
    """The stored transcript. Empty (not 404) before anything is transcribed.

    Speaker labels are resolved here rather than stored, so a rename shows up
    on the very next poll without rewriting a single segment.
    """
    meeting = _get_or_404(db, meeting_id)
    payload = transcripts.loads(meeting.transcript_json) or transcripts.empty()
    payload = dict(payload)
    if settings.diarization_enabled():
        names = speakers.name_map(meeting.speaker_names_json)
        payload["segments"] = speakers.resolve_segments(payload.get("segments") or [], names)
    else:
        payload["segments"] = speakers.strip_segments(payload.get("segments") or [])
    return TranscriptOut.model_validate(payload)


@router.get("/{meeting_id}/export.md", response_class=PlainTextResponse)
def export_markdown(meeting_id: str, db: Session = Depends(get_db)) -> PlainTextResponse:
    """The whole meeting as one Markdown file: notes, speakers, transcript."""
    meeting = _get_or_404(db, meeting_id)
    body = export.markdown(meeting)
    return PlainTextResponse(
        body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{export.filename(meeting)}"'},
    )


@router.get("/{meeting_id}/audio")
def get_audio(meeting_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    meeting = _get_or_404(db, meeting_id)
    wav = config.wav_path(meeting.id)
    if not wav.exists() or wav.stat().st_size == 0:
        raise HTTPException(status_code=404, detail="Audio is not available for this meeting")
    return range_file_response(request, wav, f"{meeting.id}.wav")
