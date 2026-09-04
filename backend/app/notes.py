"""AI meeting notes with a Groq-hosted Llama model.

One synchronous entry point, :func:`generate`, that turns transcript segments
into the stored ``notes_json`` shape. Callers run it off the event loop with
``anyio.to_thread.run_sync``.

Stored shape::

    {
      "title_suggestion": "Weekly sync: migration and launch risk",
      "summary": "Three to five sentences.",
      "topics": [{"title": "Vendor renewal", "start": 0.0, "end": 118.0,
                  "summary": "One or two sentences."}],
      "key_takeaways": ["..."],
      "decisions": ["..."],
      "action_items": [{"task": "...", "owner": "Priya" | null,
                        "owner_speaker_id": "S1" | null, "due": "Friday" | null,
                        "source_time": 84.0 | null, "done": false}],
      "open_questions": [{"question": "...", "asked_by": "Priya" | null,
                          "asked_by_speaker_id": "S1" | null,
                          "time": 132.0 | null, "answered": false}],
      "follow_up_questions": ["..."],
      "by_speaker": [{"speaker_id": "S1", "name": "Priya",
                      "main_points": ["..."], "commitments": ["..."],
                      "questions_raised": ["..."]}],
      "generated_at": "2026-09-03T18:22:04Z",
      "with_speakers": true,
      "model": "llama-3.3-70b-versatile"
    }

``topics`` are the chapters of the meeting: chronological, non-overlapping, and
timed from the ``[mm:ss]`` markers the transcript is rendered with. The ranges
are sorted and de-overlapped in :func:`_topics` rather than trusted, so a
sloppy answer still produces a usable chapter list.

``owner_speaker_id`` (and ``asked_by_speaker_id``, and ``by_speaker[].speaker_id``)
are what make a rename propagate: those labels resolve through
``speaker_names_json`` at read time, exactly like a transcript label, so
renaming S1 to "Priya" relabels every action item she owns, every question she
asked and her own breakdown block without touching ``notes_json``.

The pipeline calls this twice. The first pass runs before diarization, with no
speaker labels: it produces everything except ``by_speaker`` and every speaker
attribution. The second pass runs once speakers exist and fills both in.

Long transcripts go through map-reduce: chunk by time, summarize each chunk,
then merge the partial notes in one final call. The threshold is an estimate at
four characters per token - deliberately conservative, since overshooting the
context window fails the whole call while an unnecessary map-reduce only costs
a few extra seconds. The chunk pass deliberately answers in a **compact** shape
(fewer items, shorter fields, no title) so the richer schema still fits inside
``NOTES_MAX_OUTPUT_TOKENS``, which is sized for Groq's free tier and must not
grow.
"""

from __future__ import annotations

import json
import re
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from . import config, speakers

log = logging.getLogger("mrlistener.notes")

EMPTY_SUMMARY = (
    "This recording had no detectable speech, so there is nothing to summarize. "
    "Check the microphone selection on the Record page and try again."
)

#: How many chapters the meeting is divided into.
MIN_TOPICS = 3
MAX_TOPICS = 8

_SID_RE = re.compile(r"^S\d+$")


class NotesError(RuntimeError):
    """A missing key, an exhausted retry budget, or an unusable model reply."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def empty_notes() -> dict:
    """Every list empty and every scalar at its default. The shape, with nothing in it."""
    return {
        "title_suggestion": None,
        "summary": "",
        "topics": [],
        "key_takeaways": [],
        "decisions": [],
        "action_items": [],
        "open_questions": [],
        "follow_up_questions": [],
        "by_speaker": [],
    }


# --------------------------------------------------------------------------
# the schema the model is shown
# --------------------------------------------------------------------------
#
# Repeated in every prompt because a model follows a schema it can see far more
# reliably than one it must recall.


def _schema(with_speakers: bool) -> str:
    """The full object, asked for by the single pass and by the merge pass."""
    owner = (
        '"the speaker label of whoever committed to it, or null"'
        if with_speakers
        else '"a name the words themselves give, or null"'
    )
    sid = (
        '"the S-id of that speaker (S1, S2, ...), or null"'
        if with_speakers
        else "null"
    )
    asked_sid = '"the S-id of that speaker, or null"' if with_speakers else "null"
    by_speaker = (
        """,
  "by_speaker": [
    {
      "speaker_id": "S1",
      "name": "that speaker's label",
      "main_points": ["what they reported or argued, at most 3, one line each"],
      "commitments": ["what they said they would do, at most 3"],
      "questions_raised": ["what they asked, at most 3"]
    }
  ]"""
        if with_speakers
        else ""
    )
    return f"""{{
  "title_suggestion": "a short specific meeting title, 3-8 words, no date",
  "summary": "3-5 sentences: what the meeting was about, what was covered, where it landed",
  "topics": [
    {{
      "title": "what this stretch of the meeting was about, 2-6 words",
      "start": 0.0,
      "end": 120.0,
      "summary": "1-2 sentences on what was actually said in it"
    }}
  ],
  "key_takeaways": ["short factual statements worth remembering, at most 5"],
  "decisions": ["decisions that were actually made, each one sentence, at most 6"],
  "action_items": [
    {{
      "task": "what needs to be done, imperative voice",
      "owner": {owner},
      "owner_speaker_id": {sid},
      "due": "a date or relative deadline exactly as said, or null",
      "source_time": 84.0
    }}
  ],
  "open_questions": [
    {{
      "question": "a question somebody asked out loud during the meeting",
      "asked_by": {'"the speaker label of whoever asked it, or null"' if with_speakers else "null"},
      "asked_by_speaker_id": {asked_sid},
      "time": 132.0,
      "answered": false
    }}
  ],
  "follow_up_questions": ["questions the reader should ask or chase afterwards, at most 5"]{by_speaker}
}}"""


def _chunk_schema(with_speakers: bool) -> str:
    """The compact shape one part of a long transcript answers with.

    Shorter than :func:`_schema` on purpose: fewer items, no title, one-line
    fields. The map pass runs once per chunk, so it is where the output-token
    budget is actually spent, and NOTES_MAX_OUTPUT_TOKENS must not grow.
    """
    by_speaker = (
        """,
  "by_speaker": [
    {"speaker_id": "S1", "main_points": ["at most 2, short"],
     "commitments": ["at most 2, short"], "questions_raised": ["at most 2, short"]}
  ]"""
        if with_speakers
        else ""
    )
    return f"""{{
  "summary": "1-2 sentences on this part only",
  "topics": [{{"title": "2-6 words", "start": 0.0, "end": 0.0, "summary": "one sentence"}}],
  "key_takeaways": ["at most 3, short"],
  "decisions": ["at most 3, short"],
  "action_items": [{{"task": "...", "owner": null, "owner_speaker_id": null,
                    "due": null, "source_time": 0.0}}],
  "open_questions": [{{"question": "...", "asked_by": null,
                      "asked_by_speaker_id": null, "time": 0.0, "answered": false}}],
  "follow_up_questions": ["at most 3, short"]{by_speaker}
}}"""


_RULES = f"""Rules:
- Use only what the transcript says. Never invent names, dates or numbers.
- Every array may be empty. Prefer an empty array over a padded one.
- topics are the chapters of the meeting, in the order they happened.
  Between {MIN_TOPICS} and {MAX_TOPICS} of them, together covering the whole
  recording. start and end are SECONDS, read from the [mm:ss] markers: [01:30]
  is 90.0. Ranges must be in chronological order and must not overlap - one
  topic ends where the next begins. The first starts at the first line, the
  last ends at the last line.
- An action item needs a real commitment ("I will...", "can you...", "we need to
  send..."). Do not turn discussion into tasks.
- Attribute an owner only when the transcript makes it clear who took the task.
  When a speaker says "I will do X", the owner is that speaker's label.
- source_time and time are the timestamp in SECONDS where the item was said,
  taken from the [mm:ss] marker on that line.
- open_questions and follow_up_questions are different things. open_questions
  are questions the participants asked out loud in the room, quoted close to
  how they were put; set answered to true only when somebody actually answers
  it later in the transcript. follow_up_questions are what the reader should go
  and ask next: threads raised but never resolved, commitments with no owner or
  no date, numbers or claims that were never qualified, gaps the discussion
  implies. Write each follow-up question in the first person, as the reader
  could say it, and make it specific - name the vendor, date, person or system
  the transcript actually mentions. Return an empty array rather than a generic
  one.
- Respond with the JSON object and nothing else."""

_SPEAKER_RULES = """- by_speaker has one entry per speaker who actually spoke, using the exact
  S-ids from the roster. main_points is what that person contributed,
  commitments what they took on, questions_raised what they asked. Leave a list
  empty rather than padding it, and drop a speaker who said nothing of
  substance."""


# --------------------------------------------------------------------------
# Groq plumbing
# --------------------------------------------------------------------------


def _client():
    key = config.groq_api_key()
    if not key:
        raise NotesError(
            "GROQ_API_KEY is not set. Add it on the Settings page (or to the .env "
            "file at the project root) and the notes step will work on the next run."
        )
    try:
        from groq import Groq
    except ImportError as exc:  # pragma: no cover - install-time problem
        raise NotesError(
            "The groq package is not installed. Run: "
            "backend\\.venv\\Scripts\\python -m pip install -r backend\\requirements.txt"
        ) from exc
    return Groq(api_key=key)


_available: set[str] | None = None
_model_lock = threading.Lock()
#: model name -> unix time until which it is skipped (answered 429 / not found).
_cooldown: dict[str, float] = {}
#: The model that answered the most recent successful completion.
_last_model: str | None = None


def _available_models(client: Any) -> set[str] | None:
    """Model ids this key can call, asked once per process. Empty if unknown."""
    global _available
    if _available is not None:
        return _available
    with _model_lock:
        if _available is not None:
            return _available
        try:
            _available = {str(item.id) for item in client.models.list().data}
        except Exception:  # noqa: BLE001 - listing is an optimization, not a gate
            log.warning("could not list Groq models; trying candidates blind")
            _available = set()
        return _available


def candidate_models(client: Any, candidates: tuple[str, ...] | None = None) -> list[str]:
    """``candidates`` filtered to what this key can see, in order, skipping any
    model currently in cooldown. Falls back to the raw list when /models is
    unavailable, and to everything when all are cooling down."""
    names = candidates or config.NOTES_MODEL_CANDIDATES
    available = _available_models(client)
    ordered = [n for n in names if not available or n in available] or list(names)
    now = time.time()
    live = [n for n in ordered if _cooldown.get(n, 0.0) <= now]
    return live or ordered


def resolve_model(client: Any) -> str:
    """The first usable notes model (kept for callers that want one name)."""
    return candidate_models(client)[0]


def _retry_after_seconds(exc: Exception) -> float:
    """Best effort parse of the API hint "Please try again in 47m9.1s"."""
    text = str(exc)
    # Forms seen: "47m9.168s", "1h29m42s", "12.5s", "60ms". The "ms" case
    # must not be read as 60 minutes.
    if re.search(r"try again in [\d.]+ms\b", text):
        return 2.0
    match = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?", text)
    if not match or not any(match.groups()):
        return config.MODEL_COOLDOWN_SECONDS
    hours, minutes, seconds = match.groups()
    total = float(hours or 0) * 3600 + float(minutes or 0) * 60 + float(seconds or 0)
    return max(2.0, min(total + 5.0, 24 * 3600))


def _is_too_large(exc: Exception) -> bool:
    """A single request over the per-minute cap. No retry or model switch
    helps; the caller has to send less text."""
    text = str(exc).lower()
    return "request too large" in text or "reduce your message size" in text


def _should_switch_model(exc: Exception) -> str | None:
    """Returns a reason when the error is about the model, not this request."""
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    if status == 429 or "rate_limit" in text or "rate limit" in text:
        return "rate limited"
    if status in (404, 400) and ("model" in text or "not found" in text or "decommission" in text):
        return "not available"
    return None


def _complete(client: Any, system: str, user: str) -> dict:
    """One JSON-mode chat completion.

    Transient failures are retried with backoff on the same model. A rate
    limit or a missing model moves straight on to the next candidate, and the
    exhausted model is skipped for as long as the API asked (or a default
    cooldown), so one busy model never blocks the notes.
    """
    last_error: Exception | None = None
    models = candidate_models(client)
    for model in models:
        short_waits = 0
        attempt = 0
        while attempt < config.NOTES_MAX_ATTEMPTS:
            attempt += 1
            try:
                response = client.chat.completions.create(
                    model=model,
                    temperature=config.NOTES_TEMPERATURE,
                    max_tokens=config.NOTES_MAX_OUTPUT_TOKENS_BY_MODEL.get(
                        model, config.NOTES_MAX_OUTPUT_TOKENS
                    ),
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                content = response.choices[0].message.content or ""
                payload = json.loads(content)
                if not isinstance(payload, dict):
                    raise ValueError("the model returned JSON that is not an object")
                global _last_model
                _last_model = model
                return payload
            except Exception as exc:  # noqa: BLE001 - SDK raises a wide family
                last_error = exc
                if _is_too_large(exc):
                    # This model cannot take a request this size on this
                    # tier; another candidate may. No cooldown: the model is
                    # fine for smaller requests.
                    log.warning("%s rejected the request as too large; trying the next model", model)
                    break
                reason = _should_switch_model(exc)
                if reason == "rate limited":
                    wait = _retry_after_seconds(exc)
                    if wait <= config.MODEL_SHORT_WAIT_SECONDS and short_waits < config.MODEL_MAX_SHORT_WAITS:
                        # Per-minute limit: pause and stay on this model.
                        short_waits += 1
                        attempt -= 1
                        log.info("%s per-minute limit; waiting %.0fs", model, wait)
                        time.sleep(wait)
                        continue
                    _cooldown[model] = time.time() + wait
                    log.warning("%s is rate limited; skipping it for %.0fs and trying the next model", model, wait)
                    break
                if reason:
                    _cooldown[model] = time.time() + config.MODEL_COOLDOWN_SECONDS
                    log.warning("%s is %s; trying the next model", model, reason)
                    break
                if attempt == config.NOTES_MAX_ATTEMPTS:
                    break
                delay = config.NOTES_BACKOFF_SECONDS * (2 ** (attempt - 1))
                log.warning(
                    "%s attempt %d/%d failed (%s); retrying in %.1fs",
                    model,
                    attempt,
                    config.NOTES_MAX_ATTEMPTS,
                    type(last_error).__name__,
                    delay,
                )
                time.sleep(delay)

    if last_error is not None and _is_too_large(last_error):
        raise NotesError(
            "Every available model rejected the request as too large for its "
            f"per-minute limit ({', '.join(models)}); the transcript needs smaller chunks: {last_error}"
        ) from last_error
    raise NotesError(
        f"Groq could not write the notes with any available model ({', '.join(models)}): {last_error}"
    ) from last_error


# --------------------------------------------------------------------------
# normalization - the model is well behaved but never trusted
# --------------------------------------------------------------------------


def _text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("text") or value.get("item") or value.get("question") or ""
    return str(value or "").strip()


def _string_list(value: Any, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(item)
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _number(value: Any) -> float | None:
    try:
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


def _speaker_id(value: Any, valid_ids: set[str] | None) -> str | None:
    """An "S1"-shaped id, or None. ``valid_ids=None`` accepts any well-formed id,
    which is what a read of stored notes wants - the roster is not at hand there
    and the id resolves to a display name anyway."""
    sid = str(value or "").strip().upper()
    if not _SID_RE.match(sid):
        return None
    if valid_ids is not None and sid not in valid_ids:
        return None
    return sid


def _topics(value: Any, limit: int = MAX_TOPICS) -> list[dict]:
    """Chapters, forced chronological and non-overlapping.

    The model is asked for ordered, abutting ranges and usually obliges, but a
    merged answer can carry a stray overlap or an out-of-order part. Sorting
    here and clamping each end to the next start is cheaper than another round
    trip and means the UI can seek to any range without checking it first.
    """
    if not isinstance(value, list):
        return []
    parsed: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("topic") or "").strip()
        if not title:
            continue
        start = _number(item.get("start"))
        end = _number(item.get("end"))
        parsed.append(
            {
                "title": title[:120],
                "start": max(0.0, start if start is not None else 0.0),
                "end": max(0.0, end if end is not None else 0.0),
                "summary": str(item.get("summary") or "").strip(),
            }
        )

    parsed.sort(key=lambda entry: entry["start"])
    # De-duplicate consecutive chapters the merge pass repeated verbatim.
    deduped: list[dict] = []
    for entry in parsed:
        if deduped and deduped[-1]["title"].lower() == entry["title"].lower():
            deduped[-1]["end"] = max(deduped[-1]["end"], entry["end"])
            continue
        deduped.append(entry)
    deduped = deduped[:limit]

    for index, entry in enumerate(deduped):
        nxt = deduped[index + 1] if index + 1 < len(deduped) else None
        if nxt is not None and entry["end"] > nxt["start"]:
            entry["end"] = nxt["start"]
        if entry["end"] < entry["start"]:
            entry["end"] = entry["start"]
    return deduped


def _action_items(value: Any, valid_ids: set[str] | None) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if isinstance(item, str):
            item = {"task": item}
        if not isinstance(item, dict):
            continue
        task = str(item.get("task") or item.get("action") or "").strip()
        if not task:
            continue

        out.append(
            {
                "task": task,
                "owner": str(item.get("owner") or "").strip() or None,
                "owner_speaker_id": _speaker_id(item.get("owner_speaker_id"), valid_ids),
                "due": str(item.get("due") or "").strip() or None,
                "source_time": _number(item.get("source_time")),
                "done": bool(item.get("done", False)),
            }
        )
        if len(out) >= 25:
            break
    return out


def _open_questions(value: Any, valid_ids: set[str] | None, limit: int = 12) -> list[dict]:
    """Questions raised in the room.

    Accepts the old shape - a plain list of strings, which is what every
    meeting recorded before this schema has stored - and lifts it into the
    object form with the attribution fields empty.
    """
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            item = {"question": item}
        if not isinstance(item, dict):
            continue
        question = _text(item.get("question") or item.get("text"))
        key = question.lower()
        if not question or key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "question": question,
                "asked_by": str(item.get("asked_by") or "").strip() or None,
                "asked_by_speaker_id": _speaker_id(item.get("asked_by_speaker_id"), valid_ids),
                "time": _number(item.get("time")),
                "answered": bool(item.get("answered", False)),
            }
        )
        if len(out) >= limit:
            break
    return out


def _by_speaker(value: Any, valid_ids: set[str] | None, names: dict[str, str] | None = None) -> list[dict]:
    """One block per speaker, ordered by speaker number, merged by id."""
    if not isinstance(value, list):
        return []
    names = names or {}
    merged: dict[str, dict] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        sid = _speaker_id(item.get("speaker_id") or item.get("id"), valid_ids)
        if sid is None:
            continue
        entry = merged.setdefault(
            sid,
            {
                "speaker_id": sid,
                "name": names.get(sid) or str(item.get("name") or "").strip() or None,
                "main_points": [],
                "commitments": [],
                "questions_raised": [],
            },
        )
        for field in ("main_points", "commitments", "questions_raised"):
            existing = {text.lower() for text in entry[field]}
            for text in _string_list(item.get(field), limit=6):
                if text.lower() not in existing:
                    existing.add(text.lower())
                    entry[field].append(text)
            entry[field] = entry[field][:6]

    ordered = sorted(merged.values(), key=lambda entry: _order(entry["speaker_id"]))
    return [
        entry
        for entry in ordered
        if entry["main_points"] or entry["commitments"] or entry["questions_raised"]
    ]


def _order(speaker_id: str) -> int:
    match = re.match(r"^S(\d+)$", speaker_id)
    return int(match.group(1)) if match else 9999


def _fallback_by_speaker(result: dict, names: dict[str, str]) -> list[dict]:
    """A breakdown assembled from what is already attributed.

    Used when the model returned no ``by_speaker`` even though speakers were
    known - most often on a long transcript, where the merge pass has only the
    part notes to work from. Nothing is invented: every line here is an action
    item that speaker took or a question they asked.
    """
    blocks: dict[str, dict] = {}
    for item in result.get("action_items") or []:
        sid = item.get("owner_speaker_id")
        if not sid:
            continue
        blocks.setdefault(sid, {"commitments": [], "questions_raised": []})
        blocks[sid]["commitments"].append(str(item.get("task") or "").strip())
    for item in result.get("open_questions") or []:
        sid = item.get("asked_by_speaker_id")
        if not sid:
            continue
        blocks.setdefault(sid, {"commitments": [], "questions_raised": []})
        blocks[sid]["questions_raised"].append(str(item.get("question") or "").strip())

    return _by_speaker(
        [
            {
                "speaker_id": sid,
                "main_points": [],
                "commitments": block["commitments"],
                "questions_raised": block["questions_raised"],
            }
            for sid, block in blocks.items()
        ],
        set(names) or None,
        names,
    )


def normalize(
    payload: dict,
    valid_ids: set[str] | None = None,
    names: dict[str, str] | None = None,
) -> dict:
    """The model's object, coerced into exactly the stored shape.

    ``valid_ids`` is the roster to check speaker ids against; ``None`` accepts
    any well-formed id, which is what reading stored notes back wants. Old
    payloads - ``open_questions`` as plain strings, no ``topics``, no
    ``by_speaker`` - come through as the current shape with those parts empty,
    so a meeting summarized by an earlier build still renders.
    """
    return {
        "title_suggestion": str(payload.get("title_suggestion") or "").strip()[:120] or None,
        "summary": str(payload.get("summary") or "").strip(),
        "topics": _topics(payload.get("topics")),
        "key_takeaways": _string_list(payload.get("key_takeaways")),
        "decisions": _string_list(payload.get("decisions")),
        "action_items": _action_items(payload.get("action_items"), valid_ids),
        "open_questions": _open_questions(payload.get("open_questions"), valid_ids),
        "follow_up_questions": _string_list(payload.get("follow_up_questions"), limit=8),
        "by_speaker": _by_speaker(payload.get("by_speaker"), valid_ids, names),
    }


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------


def _system(with_speakers: bool) -> str:
    who = (
        "Each line is labelled with the speaker who said it."
        if with_speakers
        else "Speaker labels are not available yet, so attribute owners and "
        "questions only when the words themselves name someone, and leave every "
        "S-id field null."
    )
    return (
        "You are a meticulous meeting notetaker. You read a raw meeting "
        f"transcript and write the notes a participant would want afterwards. {who} "
        "You always answer with a single JSON object matching the schema you are "
        "given, and you never add commentary around it."
    )


def _rules(with_speakers: bool) -> str:
    return f"{_RULES}\n{_SPEAKER_RULES}" if with_speakers else _RULES


def _single_prompt(body: str, roster: str, title: str | None, with_speakers: bool) -> str:
    header = f"Meeting title so far: {title}\n" if title else ""
    return (
        f"{header}{roster}"
        "Write the meeting notes for the transcript below.\n\n"
        f"Return exactly this JSON shape:\n{_schema(with_speakers)}\n\n"
        f"{_rules(with_speakers)}\n\n"
        f"TRANSCRIPT\n----------\n{body}\n"
    )


def _chunk_prompt(body: str, roster: str, index: int, total: int, with_speakers: bool) -> str:
    return (
        f"{roster}"
        f"This is part {index} of {total} of one long meeting transcript. "
        "Write notes for THIS PART ONLY; a later step merges the parts.\n\n"
        f"Return exactly this compact JSON shape:\n{_chunk_schema(with_speakers)}\n\n"
        f"{_rules(with_speakers)}\n"
        "- Keep every field short: this is raw material for a merge, not the "
        "finished notes. At most 2 topics for this part, covering only the time "
        "range this part spans.\n\n"
        f"TRANSCRIPT PART {index}/{total}\n----------\n{body}\n"
    )


def _merge_prompt(parts: list[dict], roster: str, title: str | None, with_speakers: bool) -> str:
    header = f"Meeting title so far: {title}\n" if title else ""
    # Compact separators: the part notes are the whole input here, so every
    # space saved is prompt budget the merge does not have to spend.
    body = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    speaker_line = (
        " Combine by_speaker across the parts, one entry per S-id, dropping "
        "repetitions."
        if with_speakers
        else ""
    )
    return (
        f"{header}{roster}"
        "Below are notes written separately for consecutive parts of one "
        "meeting, in order. Merge them into a single set of notes for the whole "
        "meeting: de-duplicate repeated points, drop anything a later part "
        "resolved or superseded, and keep the strongest wording. The summary "
        f"must describe the whole meeting in 3-5 sentences. Keep the topics in "
        f"time order, merging adjacent parts of the same discussion, and end up "
        f"with {MIN_TOPICS}-{MAX_TOPICS} chapters covering the meeting."
        f"{speaker_line}\n\n"
        f"Return exactly this JSON shape:\n{_schema(with_speakers)}\n\n"
        f"{_rules(with_speakers)}\n\n"
        f"PART NOTES\n----------\n{body}\n"
    )


def _roster(names: dict[str, str], with_speakers: bool) -> str:
    if not with_speakers or not names:
        return ""
    listed = ", ".join(f"{sid} = {name}" for sid, name in sorted(names.items()))
    return (
        f"Speakers in this meeting: {listed}. Use these exact labels for owner, "
        "asked_by and name, and the matching S-id for owner_speaker_id, "
        "asked_by_speaker_id and speaker_id.\n"
    )


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------


def _chunk(lines: list[str], limit: int) -> list[list[str]]:
    """Split rendered lines into time-ordered chunks under ``limit`` characters."""
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            chunks.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append(current)
    return chunks


def _part_notes(payload: dict, valid_ids: set[str] | None) -> dict:
    """One chunk's answer, trimmed to what the merge pass actually needs.

    ``done``, ``title_suggestion`` and the resolved owner names are all merge
    noise; dropping them here keeps the merge prompt small on a long meeting.
    """
    part = normalize(payload, valid_ids)
    return {
        "summary": part["summary"],
        "topics": part["topics"][:3],
        "key_takeaways": part["key_takeaways"][:4],
        "decisions": part["decisions"][:4],
        "action_items": [
            {key: item[key] for key in ("task", "owner_speaker_id", "due", "source_time")}
            for item in part["action_items"][:6]
        ],
        "open_questions": [
            {key: item[key] for key in ("question", "asked_by_speaker_id", "time", "answered")}
            for item in part["open_questions"][:5]
        ],
        "follow_up_questions": part["follow_up_questions"][:4],
        "by_speaker": part["by_speaker"],
    }



def _dedupe(items: list, key) -> list:
    seen: set[str] = set()
    out = []
    for item in items:
        text = " ".join(str(key(item) or "").lower().split())
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(item)
    return out


_PART_LIMITS = {
    "key_takeaways": 10,
    "decisions": 8,
    "action_items": 12,
    "open_questions": 10,
    "follow_up_questions": 6,
}


def _fill_from_parts(result: dict, parts: list[dict]) -> dict:
    """Backstop for the merge pass.

    A capped model (free-tier output limits) tends to spend its answer on the
    summary and topics and hand back empty lists for everything else. The
    part notes already hold those lists, so any list the merge left empty is
    rebuilt here as the de-duplicated union of the parts, in meeting order.
    """
    for key, limit in _PART_LIMITS.items():
        if result.get(key):
            continue
        pooled = [item for part in parts for item in (part.get(key) or [])]
        if key in ("action_items", "open_questions"):
            field = "task" if key == "action_items" else "question"
            merged = _dedupe(pooled, lambda it, f=field: it.get(f) if isinstance(it, dict) else it)
        else:
            merged = _dedupe(pooled, lambda it: it)
        if merged:
            result[key] = merged[:limit]
            log.info("merge left %s empty; rebuilt %d item(s) from the part notes", key, len(result[key]))

    if not result.get("by_speaker"):
        blocks: dict[str, dict] = {}
        for part in parts:
            for block in part.get("by_speaker") or []:
                sid = block.get("speaker_id")
                if not sid:
                    continue
                target = blocks.setdefault(
                    sid, {"speaker_id": sid, "name": block.get("name") or sid,
                          "main_points": [], "commitments": [], "questions_raised": []}
                )
                for field in ("main_points", "commitments", "questions_raised"):
                    target[field] = _dedupe(target[field] + list(block.get(field) or []), lambda it: it)[:5]
        if blocks:
            result["by_speaker"] = [blocks[k] for k in sorted(blocks, key=_order)]
    return result


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def generate(
    segments: Iterable[dict],
    names: dict[str, str] | None = None,
    title: str | None = None,
) -> dict:
    """Notes for a transcript. Blocking - call from a worker thread.

    ``names`` maps ``S1 -> display name``; passing it (and segments that carry
    ``speaker_id``) is what produces speaker-attributed notes and the
    ``by_speaker`` breakdown. Without it the notes are written exactly the same
    way minus every attribution, which is what the pre-diarization pass wants.
    An empty or speechless transcript short-circuits to a fixed set of notes
    rather than spending an API call on nothing.
    """
    segment_list = [dict(item) for item in segments]
    names = names or {}
    with_speakers = speakers.any_speakers(segment_list) and bool(names)
    lines = speakers.transcript_lines(segment_list, names)

    if not lines:
        return {
            **empty_notes(),
            "summary": EMPTY_SUMMARY,
            "generated_at": _now(),
            "with_speakers": False,
            "model": None,
            "empty": True,
        }

    client = _client()
    roster = _roster(names, with_speakers)
    # An empty set, not None: before diarization there is no roster, so any
    # S-id in the answer is a hallucination and is dropped rather than stored.
    valid_ids = set(names) if with_speakers else set()
    body = "\n".join(lines)
    started = time.perf_counter()

    if len(body) <= config.NOTES_SINGLE_PASS_CHARS:
        raw = _complete(
            client,
            _system(with_speakers),
            _single_prompt(body, roster, title, with_speakers),
        )
        result = normalize(raw, valid_ids, names)
        passes = 1
    else:
        chunks = _chunk(lines, config.NOTES_CHUNK_CHARS)
        log.info("transcript is %d chars; map-reducing over %d chunks", len(body), len(chunks))
        parts: list[dict] = []
        for index, chunk in enumerate(chunks, start=1):
            raw = _complete(
                client,
                _system(with_speakers),
                _chunk_prompt("\n".join(chunk), roster, index, len(chunks), with_speakers),
            )
            parts.append(_part_notes(raw, valid_ids))
        merged = _complete(
            client,
            _system(with_speakers),
            _merge_prompt(parts, roster, title, with_speakers),
        )
        result = normalize(merged, valid_ids, names)
        result = _fill_from_parts(result, parts)
        # Action items and questions rebuilt from parts carry S-ids only;
        # normalize again so owner / asked_by names are resolved.
        result = normalize(result, valid_ids, names)
        passes = len(chunks) + 1

    if not result["summary"]:
        result["summary"] = (
            "The transcript was too short or too unclear for a useful summary."
        )
    if not result["topics"]:
        # Better one honest chapter than a section that silently disappears.
        result["topics"] = [
            {
                "title": "Whole meeting",
                "start": round(float(segment_list[0].get("start") or 0.0), 2),
                "end": round(float(segment_list[-1].get("end") or 0.0), 2),
                "summary": result["summary"],
            }
        ]
    if with_speakers and not result["by_speaker"]:
        result["by_speaker"] = _fallback_by_speaker(result, names)

    result.update(
        {
            "generated_at": _now(),
            "with_speakers": with_speakers,
            "model": _last_model or config.NOTES_MODEL,
            "empty": False,
        }
    )
    log.info(
        "wrote notes in %.1fs (%d groq call(s), speakers=%s, %d topic(s))",
        time.perf_counter() - started,
        passes,
        with_speakers,
        len(result["topics"]),
    )
    return result


# --------------------------------------------------------------------------
# storage + read-time resolution
# --------------------------------------------------------------------------


def dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def loads(raw: str | None) -> dict | None:
    """Stored notes in the current shape, whatever shape they were written in.

    Everything a reader touches goes through :func:`normalize`, so notes from
    an older build (``open_questions`` as strings, no ``topics``, no
    ``by_speaker``) read back as the current object with those parts empty.
    """
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    out = normalize(payload)
    for key in ("generated_at", "with_speakers", "model", "empty"):
        if key in payload:
            out[key] = payload[key]
    return out


def resolve(payload: dict | None, names: dict[str, str]) -> dict | None:
    """A copy with every speaker reference resolved through the current names.

    Action-item owners, the person who asked an open question and the name on
    a ``by_speaker`` block all resolve from their stored ``S`` id, so one
    rename relabels all three without rewriting ``notes_json``.
    """
    if payload is None:
        return None
    out = dict(payload)

    items = []
    for item in payload.get("action_items") or []:
        entry = dict(item)
        sid = entry.get("owner_speaker_id")
        if sid:
            entry["owner"] = speakers.display_name(sid, names, entry.get("owner"))
        items.append(entry)
    out["action_items"] = items

    questions = []
    for item in payload.get("open_questions") or []:
        entry = dict(item)
        sid = entry.get("asked_by_speaker_id")
        if sid:
            entry["asked_by"] = speakers.display_name(sid, names, entry.get("asked_by"))
        questions.append(entry)
    out["open_questions"] = questions

    blocks = []
    for item in payload.get("by_speaker") or []:
        entry = dict(item)
        sid = entry.get("speaker_id")
        if sid:
            entry["name"] = speakers.display_name(sid, names, entry.get("name"))
        blocks.append(entry)
    out["by_speaker"] = blocks

    return out


def first_sentence(payload: dict | None, limit: int = 160) -> str | None:
    """Opening sentence of the summary - the meetings list preview."""
    if not payload:
        return None
    text = str(payload.get("summary") or "").strip()
    if not text:
        return None
    for stop in (". ", "? ", "! "):
        index = text.find(stop)
        if index > 20:
            text = text[: index + 1]
            break
    return text if len(text) <= limit else text[:limit].rstrip() + "…"
