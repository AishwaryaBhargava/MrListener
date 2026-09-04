from __future__ import annotations

import json
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
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
    settings as settings_mod,
    speakers,
    suggestions as suggestions_mod,
    transcripts,
)
from ..audio import AudioError, range_file_response
from ..db import get_db
from ..models import STATUS_RECORDING, Meeting
from ..schemas import (
    ActionItemUpdate,
    MeetingCreate,
    MeetingOut,
    MeetingUpdate,
    PinsUpdate,
    SpeakerOut,
    SuggestionBatchOut,
    SuggestionsOut,
    TranscriptOut,
)

router = APIRouter(prefix="/api/meetings", tags=["meetings"])

#: Characters of context on either side of a search hit.
SNIPPET_PAD = 70


def _to_out(meeting: Meeting, detail: bool = False, query: str | None = None) -> MeetingOut:
    wav = config.wav_path(meeting.id)
    out = MeetingOut.model_validate(meeting)
    out.has_audio = wav.exists() and wav.stat().st_size > 0
    out.transcript_preview = transcripts.preview(meeting.transcript_json)
    out.has_transcript = out.transcript_preview is not None

    names = speakers.name_map(meeting.speaker_names_json)
    roster = speakers.as_list(meeting.speaker_names_json)
    out.speaker_count = len(roster)

    payload = notes_mod.loads(meeting.notes_json)
    resolved = notes_mod.resolve(payload, names)
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
    # Freeze the batches produced while recording into suggestions_json before
    # the in-memory state goes away with the session.
    suggestions_mod.persist(meeting_id)

    try:
        meeting = await services.finalize_meeting(db, meeting)
    except AudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    pipeline.schedule(meeting_id, session)
    db.refresh(meeting)
    return _to_out(meeting, detail=True)


@router.post("/{meeting_id}/reprocess", response_model=MeetingOut)
async def reprocess_meeting(meeting_id: str, db: Session = Depends(get_db)) -> MeetingOut:
    """Re-run transcription, notes and diarization from the wav.

    Async so ``pipeline.schedule`` has the event loop to attach its task to -
    a sync route body runs on the threadpool, where there is none.
    """
    meeting = _require_processable(db, meeting_id)
    pipeline.mark_processing(meeting_id)
    pipeline.schedule(meeting_id)
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
        if not wav.exists() or wav.stat().st_size == 0:
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


# --------------------------------------------------------------------------
# Live follow-up suggestions
# --------------------------------------------------------------------------


def _live_segments(meeting: Meeting) -> list[dict]:
    """The transcript a suggestion refresh should read.

    While recording, the live session holds segments the database may be a
    window behind on, so it wins; otherwise the stored transcript is all there
    is.
    """
    session = live.get(meeting.id)
    if session is not None and session.segments:
        return list(session.segments)
    payload = transcripts.loads(meeting.transcript_json)
    return list(payload.get("segments") or []) if payload else []


def _suggestions_out(meeting: Meeting) -> SuggestionsOut:
    payload = suggestions_mod.stored(meeting)
    return SuggestionsOut(
        batches=[SuggestionBatchOut(**item) for item in payload["batches"]],
        pinned=payload["pinned"],
        live=suggestions_mod.peek(meeting.id) is not None,
        enabled=settings_mod.suggestions_enabled(),
        interval_seconds=settings_mod.suggestions_interval_seconds(),
    )


@router.get("/{meeting_id}/suggestions", response_model=SuggestionsOut)
def get_suggestions(meeting_id: str, db: Session = Depends(get_db)) -> SuggestionsOut:
    """Every batch this meeting produced, oldest first, plus the pinned items."""
    return _suggestions_out(_get_or_404(db, meeting_id))


@router.post("/{meeting_id}/suggestions/refresh", response_model=SuggestionBatchOut)
async def refresh_suggestions(meeting_id: str, db: Session = Depends(get_db)) -> SuggestionBatchOut:
    """Work out what to ask next, right now, and return the batch.

    Async so the refresh task attaches to the running event loop. A refresh
    already in flight is awaited rather than duplicated.
    """
    meeting = _get_or_404(db, meeting_id)
    if not settings_mod.suggestions_enabled():
        raise HTTPException(status_code=409, detail="Live suggestions are off in Settings")

    segment_list = _live_segments(meeting)
    if not segment_list:
        raise HTTPException(
            status_code=409, detail="There is nothing transcribed to suggest from yet"
        )

    session = live.get(meeting_id)
    on_batch = live.push_suggestions(session) if session is not None else None
    batch = await suggestions_mod.refresh_now(meeting_id, segment_list, on_batch)
    if batch is None:
        raise HTTPException(
            status_code=502, detail="The model did not return any suggestions this time"
        )
    if meeting.status != STATUS_RECORDING:
        # Not a live recording, so nothing will persist this later.
        suggestions_mod.persist(meeting_id)
    return SuggestionBatchOut(**batch)


@router.patch("/{meeting_id}/suggestions/pins", response_model=SuggestionsOut)
def set_suggestion_pins(
    meeting_id: str,
    payload: PinsUpdate,
    db: Session = Depends(get_db),
) -> SuggestionsOut:
    """Replace the pinned set. Pinned items survive every later refresh."""
    meeting = _get_or_404(db, meeting_id)
    suggestions_mod.save_pins(meeting_id, [item.model_dump() for item in payload.pinned])
    db.refresh(meeting)
    return _suggestions_out(meeting)


@router.get("/{meeting_id}/transcript", response_model=TranscriptOut)
def get_transcript(meeting_id: str, db: Session = Depends(get_db)) -> TranscriptOut:
    """The stored transcript. Empty (not 404) before anything is transcribed.

    Speaker labels are resolved here rather than stored, so a rename shows up
    on the very next poll without rewriting a single segment.
    """
    meeting = _get_or_404(db, meeting_id)
    payload = transcripts.loads(meeting.transcript_json) or transcripts.empty()
    names = speakers.name_map(meeting.speaker_names_json)
    payload = dict(payload)
    payload["segments"] = speakers.resolve_segments(payload.get("segments") or [], names)
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
