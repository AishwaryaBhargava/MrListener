"""Live follow-up suggestions: what to ask next, while the meeting runs.

The user is sitting in a meeting with MrListener listening beside them. Every
so often this module reads the transcript so far and hands back three to five
things worth saying out loud next - a question nobody asked, a number that went
unqualified, an owner that was never named.

Shape of one batch, both on the wire and in ``meetings.suggestions_json``::

    {
      "items": [
        {"text": "Who owns the Acme renewal before it lapses?",
         "kind": "question" | "clarify" | "follow_up" | "risk",
         "why": "The contract expiry came up but no owner was named.",
         "based_on_time": 41.2}
      ],
      "generated_at": "2026-09-03T18:22:04Z",
      "transcript_end": 96.4
    }

**Bounded prompt.** Only the last :data:`config.SUGGESTIONS_VERBATIM_SECONDS`
of transcript go in verbatim. Everything older is represented by a rolling
"context so far" summary that the model itself compresses into 3-5 lines every
few refreshes, so an hour-long meeting costs the same per refresh as a five
minute one.

**Never blocks transcription.** :func:`maybe_refresh` starts an asyncio task
and returns immediately; a refresh that arrives while one is already running is
dropped rather than queued. The Groq call itself runs in a worker thread, like
every other blocking call in this app.

The in-memory state lives here, keyed by meeting id, for exactly as long as the
recording does; :func:`persist` freezes it into the meeting row at /stop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import anyio

from . import config, notes, settings, speakers
from .db import SessionLocal
from .models import Meeting

log = logging.getLogger("mrlistener.suggestions")

KIND_QUESTION = "question"
KIND_CLARIFY = "clarify"
KIND_FOLLOW_UP = "follow_up"
KIND_RISK = "risk"
KINDS = (KIND_QUESTION, KIND_CLARIFY, KIND_FOLLOW_UP, KIND_RISK)

#: Spellings the model reaches for that mean one of the four kinds above.
_KIND_ALIASES = {
    "q": KIND_QUESTION,
    "ask": KIND_QUESTION,
    "question": KIND_QUESTION,
    "clarify": KIND_CLARIFY,
    "clarification": KIND_CLARIFY,
    "confirm": KIND_CLARIFY,
    "follow_up": KIND_FOLLOW_UP,
    "follow-up": KIND_FOLLOW_UP,
    "followup": KIND_FOLLOW_UP,
    "follow up": KIND_FOLLOW_UP,
    "risk": KIND_RISK,
    "concern": KIND_RISK,
    "warning": KIND_RISK,
    "blocker": KIND_RISK,
}


class SuggestionsError(RuntimeError):
    """A missing key, an exhausted retry budget, or an unusable model reply."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------
# In-memory state, one per active meeting
# --------------------------------------------------------------------------


@dataclass
class SuggestionState:
    meeting_id: str
    #: The rolling "context so far" - everything older than the verbatim tail,
    #: compressed by the model into a few lines.
    context_summary: str = ""
    #: Newest last, capped at ``config.SUGGESTIONS_HISTORY``.
    batches: list[dict] = field(default_factory=list)
    #: Items the user pinned. They survive every refresh and are stored with
    #: the history at /stop.
    pinned: list[dict] = field(default_factory=list)
    #: How many refreshes have completed; drives the compression cadence.
    refreshes: int = 0
    #: ``time.monotonic()`` when the last refresh *started*, so a slow call
    #: cannot be followed immediately by another.
    last_started: float = 0.0
    #: Transcript word count as of the last refresh.
    words_at_refresh: int = 0
    #: The refresh in flight, if any. One at a time, per meeting.
    task: asyncio.Task | None = None


_states: dict[str, SuggestionState] = {}


def state_for(meeting_id: str) -> SuggestionState:
    state = _states.get(meeting_id)
    if state is None:
        state = SuggestionState(meeting_id=meeting_id)
        _states[meeting_id] = state
    return state


def peek(meeting_id: str) -> SuggestionState | None:
    return _states.get(meeting_id)


def running(meeting_id: str) -> bool:
    state = _states.get(meeting_id)
    return state is not None and state.task is not None and not state.task.done()


def discard(meeting_id: str) -> SuggestionState | None:
    """Drop the state and cancel any refresh in flight."""
    state = _states.pop(meeting_id, None)
    if state is not None and state.task is not None and not state.task.done():
        state.task.cancel()
        state.task = None
    return state


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def empty() -> dict:
    return {"batches": [], "pinned": [], "context_summary": ""}


def loads(raw: str | None) -> dict:
    """The stored payload, normalized. Missing or corrupt reads as empty."""
    if not raw:
        return empty()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return empty()
    if not isinstance(payload, dict):
        return empty()

    batches = payload.get("batches")
    pinned = payload.get("pinned")
    return {
        "batches": [_clean_batch(item) for item in batches if isinstance(item, dict)]
        if isinstance(batches, list)
        else [],
        "pinned": normalize_items(pinned) if isinstance(pinned, list) else [],
        "context_summary": str(payload.get("context_summary") or "").strip(),
    }


def _clean_batch(payload: dict) -> dict:
    end = payload.get("transcript_end")
    try:
        transcript_end = round(float(end), 2) if end is not None else None
    except (TypeError, ValueError):
        transcript_end = None
    return {
        "items": normalize_items(payload.get("items")),
        "generated_at": str(payload.get("generated_at") or "") or None,
        "transcript_end": transcript_end,
    }


def stored(meeting: Meeting) -> dict:
    """Whatever is on the row, with the live state layered on when recording."""
    payload = loads(meeting.suggestions_json)
    state = _states.get(meeting.id)
    if state is not None and (state.batches or state.pinned):
        payload = {
            "batches": list(state.batches),
            "pinned": list(state.pinned),
            "context_summary": state.context_summary,
        }
    return payload


def persist(meeting_id: str) -> None:
    """Freeze the in-memory history into the meeting row, then drop it.

    Called from /stop. A meeting that never produced a suggestion writes
    nothing, so ``suggestions_json`` stays NULL rather than holding an empty
    shell.
    """
    state = discard(meeting_id)
    if state is None or (not state.batches and not state.pinned):
        return

    payload = {
        "batches": state.batches[-config.SUGGESTIONS_HISTORY :],
        "pinned": state.pinned,
        "context_summary": state.context_summary,
    }
    db = SessionLocal()
    try:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            return
        meeting.suggestions_json = dumps(payload)
        db.commit()
        log.info(
            "meeting %s: stored %d suggestion batch(es)", meeting_id, len(payload["batches"])
        )
    except Exception:  # noqa: BLE001 - /stop must never fail over this
        db.rollback()
        log.exception("could not persist suggestions for meeting %s", meeting_id)
    finally:
        db.close()


def save_pins(meeting_id: str, items: list[dict]) -> list[dict]:
    """Replace the pinned list, in memory while live and on the row always."""
    cleaned = normalize_items(items, limit=config.SUGGESTIONS_HISTORY)
    state = _states.get(meeting_id)
    if state is not None:
        state.pinned = cleaned

    db = SessionLocal()
    try:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            return cleaned
        payload = loads(meeting.suggestions_json)
        payload["pinned"] = cleaned
        if state is not None and not payload["batches"]:
            # A pin arriving mid-recording should not lose the batches that
            # only exist in memory yet.
            payload["batches"] = list(state.batches)
        meeting.suggestions_json = dumps(payload)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("could not save suggestion pins for meeting %s", meeting_id)
    finally:
        db.close()
    return cleaned


# --------------------------------------------------------------------------
# Normalization - the model is well behaved but never trusted
# --------------------------------------------------------------------------


def _kind(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "_")
    return _KIND_ALIASES.get(text, _KIND_ALIASES.get(text.replace("_", " "), KIND_QUESTION))


def normalize_items(value: Any, limit: int = config.SUGGESTIONS_MAX_ITEMS) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for entry in value:
        if isinstance(entry, str):
            entry = {"text": entry}
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or entry.get("question") or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)

        raw_time = entry.get("based_on_time")
        try:
            based_on = round(float(raw_time), 2) if raw_time is not None else None
        except (TypeError, ValueError):
            based_on = None
        if based_on is not None and based_on < 0:
            based_on = None

        out.append(
            {
                "text": text[:400],
                "kind": _kind(entry.get("kind")),
                "why": str(entry.get("why") or "").strip()[:400],
                "based_on_time": based_on,
            }
        )
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

_SCHEMA = """{
  "suggestions": [
    {
      "text": "the question or follow-up, phrased exactly as the user could say it out loud",
      "kind": "question" | "clarify" | "follow_up" | "risk",
      "why": "one short sentence tying it to what was actually said",
      "based_on_time": 84.0
    }
  ],
  "context_summary": "3-5 lines covering what this meeting has been about so far"
}"""

_SYSTEM = (
    "You are sitting in a live meeting next to one participant - the user - "
    "listening to the transcript as it arrives. Your job is to tell them what "
    "is worth asking next: the question nobody has asked, the number that went "
    "unqualified, the commitment with no owner or no date, the risk that was "
    "mentioned and dropped. You always answer with a single JSON object "
    "matching the schema you are given, and you never add commentary around it."
)

_RULES = f"""Rules:
- Return {config.SUGGESTIONS_MIN_ITEMS} to {config.SUGGESTIONS_MAX_ITEMS} suggestions, best first.
- Every suggestion must be specific to THIS conversation. Name the thing: the
  vendor, the number, the date, the person, the system that was actually
  discussed. A suggestion that would fit any meeting is worthless - do not
  return it.
- Phrase "text" as the user could say it aloud, in the first person, in one
  sentence. No preamble, no "you could ask".
- "why" is one short sentence explaining what in the transcript prompts it.
- "based_on_time" is the timestamp in SECONDS of the line that prompted it,
  taken from the [mm:ss] marker on that line, or null.
- Never suggest something the transcript shows has already been answered or
  covered. Generic prompts such as "what are the next steps?" are acceptable
  only when nobody has touched next steps at all.
- kind: "question" for something not yet asked, "clarify" for something said
  but vague or ambiguous, "follow_up" for a thread that was dropped or needs
  chasing, "risk" for something that could go wrong if left alone.
- Use only what the transcript says. Never invent names, dates or numbers.
- Respond with the JSON object and nothing else."""

_COMPRESS_SYSTEM = (
    "You compress the earlier part of a live meeting transcript into a short "
    "running context note. You always answer with a single JSON object."
)


def _suggest_prompt(recent: str, context_summary: str, elapsed: float) -> str:
    head = f"The meeting has been running for {int(elapsed) // 60} min {int(elapsed) % 60} s.\n"
    context = (
        f"CONTEXT SO FAR (earlier in this meeting, already compressed)\n"
        f"----------\n{context_summary}\n\n"
        if context_summary
        else ""
    )
    return (
        f"{head}{context}"
        "Read the most recent transcript below and tell the user what to ask "
        "or follow up on next.\n\n"
        f"Return exactly this JSON shape:\n{_SCHEMA}\n\n{_RULES}\n\n"
        f"RECENT TRANSCRIPT\n----------\n{recent}\n"
    )


def _compress_prompt(previous: str, older: str) -> str:
    carry = (
        f"PREVIOUS CONTEXT NOTE\n----------\n{previous}\n\n" if previous else ""
    )
    return (
        f"{carry}"
        "Below is the earlier part of a meeting transcript. Fold it, and the "
        "previous context note if there is one, into a single running note of "
        "3 to 5 short lines: the topics covered, the decisions and numbers "
        "that came up, who committed to what, and anything raised but left "
        "hanging. Keep names, figures and dates exactly as said.\n\n"
        'Return exactly this JSON shape:\n{"context_summary": "line\\nline\\nline"}\n\n'
        f"EARLIER TRANSCRIPT\n----------\n{older}\n"
    )


# --------------------------------------------------------------------------
# Groq plumbing - the notes client and its model fallback, run hotter
# --------------------------------------------------------------------------


def _complete(client: Any, system: str, user: str) -> dict:
    """One JSON-mode completion on the suggestions model list, with the same
    per-model fallback and cooldown the notes use."""
    return notes._complete(  # noqa: SLF001 - shared Groq plumbing
        client,
        system,
        user,
        candidates=config.SUGGESTIONS_MODEL_CANDIDATES,
        temperature=config.SUGGESTIONS_TEMPERATURE,
    )


# --------------------------------------------------------------------------
# Generation - blocking, called from a worker thread
# --------------------------------------------------------------------------


def _split(segments: list[dict], cutoff: float) -> tuple[list[dict], list[dict]]:
    older = [item for item in segments if float(item.get("end") or 0.0) <= cutoff]
    recent = [item for item in segments if float(item.get("end") or 0.0) > cutoff]
    # Never send an empty tail: if every segment is old, the newest few still go
    # in verbatim so the model has something concrete to react to.
    if not recent and segments:
        older, recent = segments[:-6], segments[-6:]
    return older, recent


def generate(
    segments: Iterable[dict],
    context_summary: str = "",
    compress: bool = False,
) -> dict:
    """Suggestions for the transcript so far. Blocking - use a worker thread.

    ``compress`` asks for an extra, cheap call that folds everything older than
    the verbatim window into the rolling context note. Returns
    ``{"items": [...], "context_summary": str}``.
    """
    segment_list = [dict(item) for item in segments]
    if not segment_list:
        return {"items": [], "context_summary": context_summary}

    end = max(float(item.get("end") or 0.0) for item in segment_list)
    cutoff = max(0.0, end - config.SUGGESTIONS_VERBATIM_SECONDS)
    older, recent = _split(segment_list, cutoff)

    client = notes._client()  # noqa: SLF001 - same client, same fallback logic
    summary = str(context_summary or "").strip()

    if older and (compress or not summary):
        try:
            reply = _complete(
                client,
                _COMPRESS_SYSTEM,
                _compress_prompt(summary, "\n".join(speakers.transcript_lines(older))),
            )
            compressed = str(reply.get("context_summary") or "").strip()
            if compressed:
                summary = compressed[: config.SUGGESTIONS_SUMMARY_CHARS]
        except SuggestionsError:
            # A failed compression only costs context, never the suggestions.
            log.warning("could not refresh the rolling context summary; keeping the old one")

    recent_body = "\n".join(speakers.transcript_lines(recent))
    raw = _complete(client, _SYSTEM, _suggest_prompt(recent_body, summary, end))

    items = normalize_items(raw.get("suggestions") or raw.get("items"))
    returned = str(raw.get("context_summary") or "").strip()
    if returned and not older:
        # Nothing has aged out yet, so the model's own running note is the best
        # context we have for the next refresh.
        summary = returned[: config.SUGGESTIONS_SUMMARY_CHARS]

    return {"items": items, "context_summary": summary}


# --------------------------------------------------------------------------
# Refresh scheduling
# --------------------------------------------------------------------------


def word_count(segments: Iterable[dict]) -> int:
    return sum(len(str(item.get("text") or "").split()) for item in segments)


def due(state: SuggestionState, words: int, now: float | None = None) -> bool:
    """True when an automatic refresh is warranted right now.

    Two gates, both of which must open: enough wall-clock time since the last
    refresh *started*, and enough new speech to be worth reading. The first
    batch only waits on the word count, so a meeting that opens with a dense
    minute gets its suggestions on the first window.
    """
    clock = time.monotonic() if now is None else now
    new_words = words - state.words_at_refresh
    if new_words < config.SUGGESTIONS_MIN_NEW_WORDS:
        return False
    if state.last_started == 0.0:
        return True
    return (clock - state.last_started) >= settings.suggestions_interval_seconds()


def _record(state: SuggestionState, batch: dict, summary: str, words: int) -> None:
    state.batches.append(batch)
    del state.batches[: max(0, len(state.batches) - config.SUGGESTIONS_HISTORY)]
    state.context_summary = summary
    state.refreshes += 1
    state.words_at_refresh = words


async def refresh(
    meeting_id: str,
    segments: list[dict],
    on_batch=None,
) -> dict | None:
    """Run one refresh to completion and return the batch. Never raises.

    ``on_batch`` is awaited with the finished batch, which is how the live
    socket pushes it to the browser.
    """
    state = state_for(meeting_id)
    if not segments:
        return None

    words = word_count(segments)
    state.last_started = time.monotonic()
    compress = state.refreshes > 0 and state.refreshes % config.SUGGESTIONS_SUMMARY_EVERY == 0
    started = time.perf_counter()

    try:
        result = await anyio.to_thread.run_sync(
            generate, segments, state.context_summary, compress
        )
    except SuggestionsError as exc:
        log.warning("meeting %s: suggestions failed (%s)", meeting_id, exc)
        return None
    except Exception:  # noqa: BLE001 - a suggestion must never break recording
        log.exception("meeting %s: suggestion refresh crashed", meeting_id)
        return None

    items = result.get("items") or []
    if not items:
        log.info("meeting %s: the model returned no suggestions", meeting_id)
        state.words_at_refresh = words
        return None

    batch = {
        "items": items,
        "generated_at": _now(),
        "transcript_end": round(
            max(float(item.get("end") or 0.0) for item in segments), 2
        ),
    }
    _record(state, batch, result.get("context_summary") or state.context_summary, words)
    log.info(
        "meeting %s: %d suggestion(s) in %.1fs",
        meeting_id,
        len(items),
        time.perf_counter() - started,
    )

    if on_batch is not None:
        try:
            await on_batch(batch)
        except Exception:  # noqa: BLE001 - a dead socket is not our problem
            log.debug("could not deliver the suggestion batch for %s", meeting_id)
    return batch


def maybe_refresh(meeting_id: str, segments: list[dict], on_batch=None) -> bool:
    """Start a refresh in the background when one is due. Never blocks.

    Returns True when a task was actually started. A refresh already in flight,
    a disabled setting, or an unopened gate all return False - suggestions are
    a nicety and must never hold up the transcription loop.
    """
    if not settings.suggestions_enabled():
        return False
    if running(meeting_id):
        return False
    state = state_for(meeting_id)
    if not due(state, word_count(segments)):
        return False
    return _spawn(state, list(segments), on_batch)


def _spawn(state: SuggestionState, segments: list[dict], on_batch=None) -> bool:
    async def _run() -> None:
        try:
            await refresh(state.meeting_id, segments, on_batch)
        finally:
            state.task = None

    state.task = asyncio.create_task(_run(), name=f"suggestions-{state.meeting_id}")
    return True


async def refresh_now(meeting_id: str, segments: list[dict], on_batch=None) -> dict | None:
    """Force a refresh and wait for it - what the Refresh button calls.

    A refresh already in flight is awaited rather than duplicated, so mashing
    the button costs one API call, not five.
    """
    state = state_for(meeting_id)
    existing = state.task
    if existing is not None and not existing.done():
        try:
            await existing
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        return state.batches[-1] if state.batches else None

    task = asyncio.create_task(
        refresh(meeting_id, segments, on_batch), name=f"suggestions-now-{meeting_id}"
    )
    state.task = task
    try:
        return await task
    finally:
        state.task = None
