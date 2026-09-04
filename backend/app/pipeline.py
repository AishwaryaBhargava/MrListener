"""The post-recording pipeline.

``run_pipeline(meeting_id)`` is the single place where everything that happens
after /stop lives. It runs as a detached asyncio task so /stop can answer
immediately, and it is re-runnable from POST /api/meetings/{id}/reprocess.

Steps, in order:

===  ==========================  ==========================================
 #   pipeline_stage              What it does
===  ==========================  ==========================================
 -   ``converting``              ffmpeg the uploaded container to audio.wav
                                 (uploads only; /stop already did this)
 0   ``transcribing``            Flush the live tail (only right after /stop)
 1   ``transcribing``            One full-file Groq pass over ``audio.wav``
 2   ``summarizing``             Notes, without speaker labels (~10 s)
 3   ``identifying_speakers``    pyannote diarization on CPU (~1x realtime)
 4   ``summarizing``             Notes again, now attributed (~10 s)
===  ==========================  ==========================================

**Why notes come before diarization.** Diarization is the only slow step: it
runs at roughly real time on a CPU, so a one hour meeting takes the best part
of an hour. Transcription and notes together take about half a minute. Running
notes first means the user has a usable summary within ~30 s of pressing stop,
and the speaker labels arrive later as an upgrade - the second notes pass is
cheap and only reruns because owners can now be attributed to real speakers.

**Why the app stays usable.** Diarization is CPU-bound Python, so it runs in a
worker thread via ``anyio.to_thread.run_sync``; the event loop keeps serving
requests and new recordings throughout. Only one diarization runs at a time,
process-wide: two pyannote passes on the same few cores are slower than two in
sequence, and the pipeline object is not built for concurrent calls.

Adding a step means writing an ``async def _step_x(context)`` below and adding
it to ``STEPS``. Each step owns its own short-lived DB session - never hold one
across an ``await`` that talks to the network. ``_set_stage`` is how a step
tells the UI what it is doing; the detail page polls for it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import anyio
from pathlib import Path

from . import audio, config, groq_client, live, notes, settings, speakers, transcripts
from .db import SessionLocal
from .models import (
    STAGE_CONVERTING,
    STAGE_IDENTIFYING_SPEAKERS,
    STAGE_SUMMARIZING,
    STAGE_TRANSCRIBING,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_READY,
    Meeting,
)

log = logging.getLogger("mrlistener.pipeline")

#: Keeps detached tasks alive; asyncio only holds weak references to them.
_tasks: dict[str, asyncio.Task] = {}

#: Process-wide gate: one diarization at a time. Meetings that arrive while one
#: is running wait here, still showing "identifying speakers" to the user.
_diarization_gate = asyncio.Lock()

#: Only a title of exactly this shape is replaced by the model's suggestion, so
#: a name the user typed is never overwritten.
_AUTO_TITLE_RE = re.compile(r"^Recording \d+$")


@dataclass
class Context:
    meeting_id: str
    #: The live session handed over by /stop, if this run follows a recording.
    session: live.LiveSession | None = None
    #: Set by the diarization step; the second notes pass runs only when true.
    diarized: bool = False
    #: True when only the notes are being regenerated (POST /notes/regenerate).
    notes_only: bool = False
    #: Set when the first notes pass failed; diarization still runs and the
    #: second pass retries, so a busy model never blocks speaker identification.
    notes_error: str | None = None
    #: Where the run lands. Almost always "ready" - a step that fails still
    #: leaves playable audio and partial output. The converting step is the
    #: exception: an upload ffmpeg cannot decode has nothing to keep.
    final_status: str = STATUS_READY


class PipelineError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Small DB helpers - each opens and closes its own session
# --------------------------------------------------------------------------


def _update(meeting_id: str, **fields) -> None:
    db = SessionLocal()
    try:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            return
        for key, value in fields.items():
            setattr(meeting, key, value)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("could not update meeting %s", meeting_id)
    finally:
        db.close()


def _read(meeting_id: str) -> dict | None:
    """The columns the steps need, copied out before the session closes."""
    db = SessionLocal()
    try:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            return None
        return {
            "title": meeting.title,
            "auto_title": meeting.auto_title,
            "transcript_json": meeting.transcript_json,
            "speaker_names_json": meeting.speaker_names_json,
            "notes_json": meeting.notes_json,
        }
    finally:
        db.close()


def _set_stage(meeting_id: str, stage: str | None) -> None:
    _update(meeting_id, pipeline_stage=stage)


def mark_processing(meeting_id: str, stage: str = STAGE_TRANSCRIBING) -> None:
    """Flip a meeting into the processing state before the task is scheduled."""
    _update(
        meeting_id,
        status=STATUS_PROCESSING,
        pipeline_stage=stage,
        pipeline_error=None,
    )


def _segments(row: dict | None) -> list[dict]:
    payload = transcripts.loads((row or {}).get("transcript_json"))
    return list(payload.get("segments") or []) if payload else []


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


async def _step_convert(context: Context) -> None:
    """Uploads only: decode the container the user sent into ``audio.wav``.

    ffmpeg on a two-hour mp4 is minutes of CPU, so it runs in a worker thread
    and the meeting sits on the ``converting`` stage meanwhile - which is what
    the detail page draws as the first step of the progress strip. A live
    recording never reaches here: /stop has already written the wav.

    Idempotent, so a Reprocess on an upload skips straight past it.
    """
    from . import services

    wav = config.wav_path(context.meeting_id)
    if wav.exists() and wav.stat().st_size > 0:
        return

    source = config.stored_upload(context.meeting_id)
    if source is None:
        context.final_status = STATUS_FAILED
        raise PipelineError("The uploaded file is no longer on disk")

    _set_stage(context.meeting_id, STAGE_CONVERTING)
    try:
        duration = await anyio.to_thread.run_sync(
            services.convert_upload, context.meeting_id, source
        )
    except audio.AudioError as exc:
        # Nothing survives a failed conversion: there is no wav to play and no
        # transcript to keep, so this is the one step that fails the meeting.
        context.final_status = STATUS_FAILED
        raise PipelineError(f"That file could not be converted: {exc}") from exc

    _update(
        context.meeting_id,
        audio_path=str(wav),
        duration_seconds=duration,
        pipeline_stage=STAGE_TRANSCRIBING,
    )
    log.info(
        "meeting %s: converted %s to audio.wav (%.1fs)",
        context.meeting_id,
        source.name,
        duration or 0.0,
    )


async def _step_live_tail(context: Context) -> None:
    """Transcribe whatever arrived after the last live window.

    Best effort: it only makes the transcript readable a few seconds earlier
    than the full pass would. A failure here is never fatal.
    """
    session = context.session
    if session is None:
        return
    _set_stage(context.meeting_id, STAGE_TRANSCRIBING)
    try:
        await live.transcribe_window(session, config.LIVE_MIN_TAIL_SECONDS)
    except Exception:  # noqa: BLE001
        log.exception("live tail pass failed for meeting %s", context.meeting_id)


def _transcribe_whole_file(wav: Path) -> dict:
    """Blocking. Sends a small file whole; splits a large one into FLAC chunks
    so no single upload exceeds Groq's size limit, then stitches the segments
    back together with their original timestamps."""
    if wav.stat().st_size <= config.GROQ_MAX_UPLOAD_BYTES:
        return groq_client.transcribe(wav, 0.0, None)

    chunk_dir = wav.parent / "chunks"
    chunks = audio.split_for_upload(wav, chunk_dir, config.TRANSCRIBE_CHUNK_SECONDS)
    log.info("%s: %d chunks of %ds for the full pass", wav.parent.name, len(chunks), config.TRANSCRIBE_CHUNK_SECONDS)
    segments: list[dict] = []
    language = None
    try:
        for path, offset in chunks:
            part = groq_client.transcribe(path, offset, None)
            language = language or part.get("language")
            for seg in part.get("segments") or []:
                seg = dict(seg)
                seg["id"] = len(segments)
                segments.append(seg)
    finally:
        for path, _ in chunks:
            path.unlink(missing_ok=True)
        try:
            chunk_dir.rmdir()
        except OSError:
            pass
    return {"segments": segments, "language": language}


async def _step_transcribe(context: Context) -> None:
    """One pass over the finished wav.

    The live windows cut words at their boundaries; this replaces them with
    clean, contiguous segments - which is also what the diarization step wants
    to align speaker turns against.
    """
    _set_stage(context.meeting_id, STAGE_TRANSCRIBING)
    wav = config.wav_path(context.meeting_id)
    if not wav.exists() or wav.stat().st_size == 0:
        raise PipelineError("There is no audio.wav to transcribe")

    try:
        result = await anyio.to_thread.run_sync(_transcribe_whole_file, wav)
    except (groq_client.TranscriptionError, audio.AudioError) as exc:
        raise PipelineError(str(exc)) from exc

    segments = result.get("segments") or []
    if not segments:
        # Silence, or speech Whisper found nothing in. Keep the live transcript
        # rather than blanking it out.
        log.info("meeting %s: the full pass returned no segments", context.meeting_id)
        return

    payload = transcripts.build(segments, result.get("language"), transcripts.SOURCE_FINAL)
    _update(context.meeting_id, transcript_json=transcripts.dumps(payload))


async def _write_notes(context: Context) -> bool:
    """Run the notes model over the current transcript. True when it wrote."""
    _set_stage(context.meeting_id, STAGE_SUMMARIZING)
    row = _read(context.meeting_id)
    if row is None:
        return False

    segment_list = _segments(row)
    names = speakers.name_map(row["speaker_names_json"])
    if not settings.diarization_enabled():
        # Speakers are switched off: write the notes as if no labels existed,
        # so the text never refers to "Speaker 3" the reader cannot see.
        names = {}
        segment_list = speakers.strip_segments(segment_list)

    try:
        payload = await anyio.to_thread.run_sync(
            notes.generate, segment_list, names, row["title"]
        )
    except notes.NotesError as exc:
        raise PipelineError(str(exc)) from exc

    fields: dict[str, object] = {"notes_json": notes.dumps(payload)}

    # Replace an auto-assigned "Recording NN" with the model's suggestion, once.
    # A title the user typed is left alone, and the original name is kept in
    # auto_title so nothing about the sequence is lost.
    suggestion = (payload.get("title_suggestion") or "").strip()
    current = (row["title"] or "").strip()
    if suggestion and _AUTO_TITLE_RE.match(current):
        fields["title"] = suggestion[:500]
        fields["auto_title"] = row["auto_title"] or current
        log.info("meeting %s: renamed %r -> %r", context.meeting_id, current, suggestion)

    _update(context.meeting_id, **fields)
    return True


async def _step_notes(context: Context) -> None:
    """First notes pass - fast, and what the user sees within ~30 s of /stop.

    A failure here (typically a rate limit) is recorded but does not stop the
    run: the transcript is safe, diarization is worth doing anyway, and the
    second notes pass gets another go.
    """
    try:
        await _write_notes(context)
        context.notes_error = None
    except PipelineError as exc:
        if context.notes_only:
            raise
        context.notes_error = str(exc)
        _update(context.meeting_id, pipeline_error=context.notes_error)
        log.warning("meeting %s: notes failed, continuing to speakers: %s", context.meeting_id, exc)


def _run_diarize(wav_path: str, limit: int | None) -> list[dict]:
    """Blocking pyannote call. Imported lazily: torch costs seconds to load."""
    from .diarization import diarize

    return diarize(wav_path, max_speakers=limit)


async def _step_diarize(context: Context) -> None:
    """Speaker turns -> ``diarization_json``, labels -> transcript segments.

    Skipped entirely when the settings page has diarization turned off, or when
    there is no transcript to attach speakers to.
    """
    if context.notes_only:
        return
    if not settings.diarization_enabled():
        log.info("meeting %s: diarization is disabled in settings", context.meeting_id)
        return

    row = _read(context.meeting_id)
    segment_list = _segments(row)
    if not segment_list:
        return

    wav = config.wav_path(context.meeting_id)
    if not wav.exists() or wav.stat().st_size == 0:
        return

    from .diarization import DiarizationError, assign_speakers, speaker_summary

    # The stage is set before queueing so a meeting waiting its turn still says
    # "identifying speakers" rather than looking finished.
    _set_stage(context.meeting_id, STAGE_IDENTIFYING_SPEAKERS)
    limit = settings.max_speakers()

    async with _diarization_gate:
        _set_stage(context.meeting_id, STAGE_IDENTIFYING_SPEAKERS)
        try:
            turns = await anyio.to_thread.run_sync(_run_diarize, str(wav), limit)
        except DiarizationError as exc:
            raise PipelineError(str(exc)) from exc

    if not turns:
        log.info("meeting %s: diarization found no speech turns", context.meeting_id)
        return

    assigned = assign_speakers(segment_list, turns)
    summary = speaker_summary(assigned)
    payload = transcripts.loads(row["transcript_json"]) or transcripts.empty()
    payload = transcripts.build(assigned, payload.get("language"), transcripts.SOURCE_FINAL)

    previous = speakers.loads(row["speaker_names_json"])
    names_payload = speakers.build(summary, previous)

    _update(
        context.meeting_id,
        transcript_json=transcripts.dumps(payload),
        diarization_json=transcripts.dumps({"turns": turns}),
        speaker_names_json=speakers.dumps(names_payload),
    )
    context.diarized = True
    log.info(
        "meeting %s: %d speaker(s) over %d turns",
        context.meeting_id,
        len(summary),
        len(turns),
    )


async def _step_notes_with_speakers(context: Context) -> None:
    """Second notes pass. Runs once speakers exist, or as a retry when the
    first pass failed."""
    if not context.diarized and context.notes_error is None:
        return
    try:
        await _write_notes(context)
        context.notes_error = None
    except PipelineError as exc:
        if context.notes_error is None:
            # The first pass succeeded; keep those notes rather than fail the run.
            log.warning("meeting %s: speaker-aware notes pass failed, keeping earlier notes: %s", context.meeting_id, exc)
            return
        context.notes_error = str(exc)


#: (stage label, coroutine). The label is only informational - a step sets its
#: own stage so it can change it midway.
STEPS: tuple[tuple[str | None, object], ...] = (
    (STAGE_TRANSCRIBING, _step_live_tail),
    (STAGE_TRANSCRIBING, _step_transcribe),
    (STAGE_SUMMARIZING, _step_notes),
    (STAGE_IDENTIFYING_SPEAKERS, _step_diarize),
    (STAGE_SUMMARIZING, _step_notes_with_speakers),
)

#: POST /api/meetings/upload: the same run as after /stop, with the conversion
#: of the uploaded container in front of it. Everything downstream is identical.
UPLOAD_STEPS: tuple[tuple[str | None, object], ...] = (
    (STAGE_CONVERTING, _step_convert),
) + STEPS

#: POST /notes/regenerate re-runs only this.
NOTES_STEPS: tuple[tuple[str | None, object], ...] = ((STAGE_SUMMARIZING, _step_notes),)

#: POST /reprocess?keep_transcript=true: everything after transcription. Used
#: when the transcript on file is fine and only notes/speakers need redoing,
#: which also spares the daily Whisper quota on long recordings.
RESUME_STEPS: tuple[tuple[str | None, object], ...] = (
    (STAGE_SUMMARIZING, _step_notes),
    (STAGE_IDENTIFYING_SPEAKERS, _step_diarize),
    (STAGE_SUMMARIZING, _step_notes_with_speakers),
)


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


async def run_pipeline(
    meeting_id: str,
    session: live.LiveSession | None = None,
    steps: tuple[tuple[str | None, object], ...] | None = None,
    notes_only: bool = False,
) -> None:
    """Run every step, then mark the meeting ready.

    A step raising ``PipelineError`` stops the run, stores the message in
    ``pipeline_error`` and still lands on ``ready`` - the audio is playable, the
    earlier steps' output is kept, and the user can retry with Reprocess. The
    one exception is a failed conversion of an upload, which leaves nothing at
    all and so sets ``failed``.
    """
    context = Context(meeting_id=meeting_id, session=session, notes_only=notes_only)
    error: str | None = None

    try:
        for _label, step in steps or STEPS:
            await step(context)  # type: ignore[operator]
    except asyncio.CancelledError:
        _set_stage(meeting_id, None)
        raise
    except PipelineError as exc:
        error = str(exc)
        log.warning("pipeline for meeting %s stopped: %s", meeting_id, error)
    except Exception as exc:  # noqa: BLE001
        error = f"Processing failed: {exc}"
        log.exception("pipeline for meeting %s crashed", meeting_id)
    finally:
        config.live_window_path(meeting_id).unlink(missing_ok=True)

    if error is None and context.notes_error:
        error = context.notes_error
    _update(
        meeting_id,
        status=context.final_status,
        pipeline_stage=None,
        pipeline_error=error,
    )
    log.info("pipeline for meeting %s finished%s", meeting_id, " with an error" if error else "")


def schedule(
    meeting_id: str,
    session: live.LiveSession | None = None,
    steps: tuple[tuple[str | None, object], ...] | None = None,
    notes_only: bool = False,
) -> bool:
    """Start ``run_pipeline`` in the background. False when one is already running."""
    existing = _tasks.get(meeting_id)
    if existing is not None and not existing.done():
        return False

    task = asyncio.create_task(
        run_pipeline(meeting_id, session, steps, notes_only),
        name=f"pipeline-{meeting_id}",
    )
    _tasks[meeting_id] = task

    def _forget(done: asyncio.Task, key: str = meeting_id) -> None:
        # Only clear the slot if a newer run has not already claimed it.
        if _tasks.get(key) is done:
            _tasks.pop(key, None)

    task.add_done_callback(_forget)
    return True


def recover_interrupted() -> int:
    """Un-stick meetings left mid-pipeline by a crash or a restart.

    Pipeline tasks live in the event loop and nothing survives a restart, so a
    row still marked ``processing`` at startup would keep the detail page
    polling for a step that will never run. Move those to ``ready`` - the audio
    is intact - and point the user at Reprocess.
    """
    db = SessionLocal()
    try:
        stranded = db.query(Meeting).filter(Meeting.status == STATUS_PROCESSING).all()
        for meeting in stranded:
            meeting.status = STATUS_READY
            meeting.pipeline_stage = None
            meeting.pipeline_error = (
                "Processing was interrupted when the backend restarted. "
                "Use Reprocess to run it again."
            )
        if stranded:
            db.commit()
            log.info("recovered %d meeting(s) left mid-pipeline", len(stranded))
        return len(stranded)
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("could not recover interrupted meetings")
        return 0
    finally:
        db.close()


def is_running(meeting_id: str) -> bool:
    task = _tasks.get(meeting_id)
    return task is not None and not task.done()


def diarization_busy() -> bool:
    """True while some meeting is holding the single diarization slot."""
    return _diarization_gate.locked()


def cancel(meeting_id: str) -> None:
    task = _tasks.pop(meeting_id, None)
    if task is not None and not task.done():
        task.cancel()
