"""AI meeting notes with a Groq-hosted Llama model.

One synchronous entry point, :func:`generate`, that turns transcript segments
into the stored ``notes_json`` shape. Callers run it off the event loop with
``anyio.to_thread.run_sync``.

Stored shape::

    {
      "title_suggestion": "Weekly sync: migration and launch risk",
      "summary": "Two to four sentences.",
      "key_takeaways": ["..."],
      "decisions": ["..."],
      "action_items": [{"task": "...", "owner": "Priya" | null,
                        "owner_speaker_id": "S1" | null, "due": "Friday" | null,
                        "source_time": 84.0 | null, "done": false}],
      "open_questions": ["..."],
      "follow_up_questions": ["..."],
      "generated_at": "2026-09-03T18:22:04Z",
      "with_speakers": true,
      "model": "llama-3.3-70b-versatile"
    }

``owner_speaker_id`` is what makes a rename propagate: the owner pill resolves
through ``speaker_names_json`` at read time, exactly like a transcript label,
so renaming S1 to "Priya" relabels every action item she owns without touching
``notes_json``.

Long transcripts go through map-reduce: chunk by time, summarize each chunk,
then merge the partial notes in one final call. The threshold is an estimate at
four characters per token - deliberately conservative, since overshooting the
context window fails the whole call while an unnecessary map-reduce only costs
a few extra seconds.
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

#: The exact object the model must return. Repeated in both prompts because the
#: model follows a schema it can see far more reliably than one it must recall.
_SCHEMA = """{
  "title_suggestion": "a short specific meeting title, 3-8 words, no date",
  "summary": "2-4 sentences covering what the meeting was about and where it landed",
  "key_takeaways": ["short factual statements worth remembering"],
  "decisions": ["decisions that were actually made, each one sentence"],
  "action_items": [
    {
      "task": "what needs to be done, imperative voice",
      "owner": "the speaker label of whoever committed to it, or null",
      "owner_speaker_id": "the S-id of that speaker (S1, S2, ...), or null",
      "due": "a date or relative deadline exactly as said, or null",
      "source_time": 84.0
    }
  ],
  "open_questions": ["questions the participants themselves raised and left unanswered"],
  "follow_up_questions": ["questions the reader should ask or chase after this meeting"]
}"""

_RULES = """Rules:
- Use only what the transcript says. Never invent names, dates or numbers.
- Every array may be empty. Prefer an empty array over a padded one.
- An action item needs a real commitment ("I will...", "can you...", "we need to
  send..."). Do not turn discussion into tasks.
- Attribute an owner only when the transcript makes it clear who took the task.
  When a speaker says "I will do X", the owner is that speaker's label.
- source_time is the timestamp in SECONDS where the item was said, taken from
  the [mm:ss] marker on that line.
- open_questions and follow_up_questions are different things. open_questions
  are questions the participants asked out loud and nobody answered, quoted
  close to how they were put. follow_up_questions are what the reader should
  go and ask next: threads raised but never resolved, commitments with no owner
  or no date, numbers or claims that were never qualified, gaps the discussion
  implies. Write each follow-up question in the first person, as the reader
  could say it, and make it specific - name the vendor, date, person or system
  the transcript actually mentions. Return an empty array rather than a generic
  one.
- Respond with the JSON object and nothing else."""


class NotesError(RuntimeError):
    """A missing key, an exhausted retry budget, or an unusable model reply."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
    match = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", text)
    if not match or not any(match.groups()):
        return config.MODEL_COOLDOWN_SECONDS
    hours, minutes, seconds = match.groups()
    total = float(hours or 0) * 3600 + float(minutes or 0) * 60 + float(seconds or 0)
    return max(60.0, min(total + 5.0, 24 * 3600))


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


def _complete(
    client: Any,
    system: str,
    user: str,
    candidates: tuple[str, ...] | None = None,
    temperature: float | None = None,
) -> dict:
    """One JSON-mode chat completion.

    Transient failures are retried with backoff on the same model. A rate
    limit or a missing model moves straight on to the next candidate, and the
    exhausted model is skipped for as long as the API asked (or a default
    cooldown), so one busy model never blocks notes or suggestions.
    """
    last_error: Exception | None = None
    models = candidate_models(client, candidates)
    temp = config.NOTES_TEMPERATURE if temperature is None else temperature
    for model in models:
        short_waits = 0
        attempt = 0
        while attempt < config.NOTES_MAX_ATTEMPTS:
            attempt += 1
            try:
                response = client.chat.completions.create(
                    model=model,
                    temperature=temp,
                    max_tokens=config.NOTES_MAX_OUTPUT_TOKENS,
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


def _string_list(value: Any, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("text") or item.get("item") or ""
        text = str(item or "").strip()
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _action_items(value: Any, valid_ids: set[str]) -> list[dict]:
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

        sid = str(item.get("owner_speaker_id") or "").strip().upper() or None
        if sid and sid not in valid_ids:
            sid = None
        owner = str(item.get("owner") or "").strip() or None
        due = str(item.get("due") or "").strip() or None

        source_time: float | None
        try:
            raw = item.get("source_time")
            source_time = round(float(raw), 2) if raw is not None else None
        except (TypeError, ValueError):
            source_time = None

        out.append(
            {
                "task": task,
                "owner": owner,
                "owner_speaker_id": sid,
                "due": due,
                "source_time": source_time,
                "done": bool(item.get("done", False)),
            }
        )
        if len(out) >= 25:
            break
    return out


def normalize(payload: dict, valid_ids: set[str] | None = None) -> dict:
    """The model's object, coerced into exactly the stored shape."""
    ids = valid_ids or set()
    summary = str(payload.get("summary") or "").strip()
    return {
        "title_suggestion": str(payload.get("title_suggestion") or "").strip()[:120] or None,
        "summary": summary,
        "key_takeaways": _string_list(payload.get("key_takeaways")),
        "decisions": _string_list(payload.get("decisions")),
        "action_items": _action_items(payload.get("action_items"), ids),
        "open_questions": _string_list(payload.get("open_questions")),
        "follow_up_questions": _string_list(payload.get("follow_up_questions"), limit=8),
    }


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------


def _system(with_speakers: bool) -> str:
    who = (
        "Each line is labelled with the speaker who said it."
        if with_speakers
        else "Speaker labels are not available yet, so attribute owners only when "
        "the words themselves name someone."
    )
    return (
        "You are a meticulous meeting notetaker. You read a raw meeting "
        f"transcript and write the notes a participant would want afterwards. {who} "
        "You always answer with a single JSON object matching the schema you are "
        "given, and you never add commentary around it."
    )


def _single_prompt(body: str, roster: str, title: str | None) -> str:
    header = f"Meeting title so far: {title}\n" if title else ""
    return (
        f"{header}{roster}"
        "Write the meeting notes for the transcript below.\n\n"
        f"Return exactly this JSON shape:\n{_SCHEMA}\n\n{_RULES}\n\n"
        f"TRANSCRIPT\n----------\n{body}\n"
    )


def _chunk_prompt(body: str, roster: str, index: int, total: int) -> str:
    return (
        f"{roster}"
        f"This is part {index} of {total} of one long meeting transcript. "
        "Write notes for THIS PART ONLY; a later step merges the parts.\n\n"
        f"Return exactly this JSON shape:\n{_SCHEMA}\n\n{_RULES}\n"
        "- title_suggestion may be an empty string for a part.\n\n"
        f"TRANSCRIPT PART {index}/{total}\n----------\n{body}\n"
    )


def _merge_prompt(parts: list[dict], roster: str, title: str | None) -> str:
    header = f"Meeting title so far: {title}\n" if title else ""
    body = json.dumps(parts, ensure_ascii=False, indent=1)
    return (
        f"{header}{roster}"
        "Below are notes written separately for consecutive parts of one "
        "meeting, in order. Merge them into a single set of notes for the whole "
        "meeting: de-duplicate repeated points, drop anything a later part "
        "resolved or superseded, and keep the strongest wording. The summary "
        "must describe the whole meeting in 2-4 sentences.\n\n"
        f"Return exactly this JSON shape:\n{_SCHEMA}\n\n{_RULES}\n\n"
        f"PART NOTES\n----------\n{body}\n"
    )


def _roster(names: dict[str, str], with_speakers: bool) -> str:
    if not with_speakers or not names:
        return ""
    listed = ", ".join(f"{sid} = {name}" for sid, name in sorted(names.items()))
    return (
        f"Speakers in this meeting: {listed}. Use these exact labels for owner "
        "and the matching S-id for owner_speaker_id.\n"
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
    ``speaker_id``) is what produces speaker-attributed notes. An empty or
    speechless transcript short-circuits to a fixed set of notes rather than
    spending an API call on nothing.
    """
    segment_list = [dict(item) for item in segments]
    names = names or {}
    with_speakers = speakers.any_speakers(segment_list) and bool(names)
    lines = speakers.transcript_lines(segment_list, names)

    if not lines:
        return {
            "title_suggestion": None,
            "summary": EMPTY_SUMMARY,
            "key_takeaways": [],
            "decisions": [],
            "action_items": [],
            "open_questions": [],
            "follow_up_questions": [],
            "generated_at": _now(),
            "with_speakers": False,
            "model": None,
            "empty": True,
        }

    client = _client()
    roster = _roster(names, with_speakers)
    valid_ids = set(names)
    body = "\n".join(lines)
    started = time.perf_counter()

    if len(body) <= config.NOTES_SINGLE_PASS_CHARS:
        raw = _complete(client, _system(with_speakers), _single_prompt(body, roster, title))
        result = normalize(raw, valid_ids)
        passes = 1
    else:
        chunks = _chunk(lines, config.NOTES_CHUNK_CHARS)
        log.info("transcript is %d chars; map-reducing over %d chunks", len(body), len(chunks))
        parts: list[dict] = []
        for index, chunk in enumerate(chunks, start=1):
            raw = _complete(
                client,
                _system(with_speakers),
                _chunk_prompt("\n".join(chunk), roster, index, len(chunks)),
            )
            parts.append(normalize(raw, valid_ids))
        merged = _complete(client, _system(with_speakers), _merge_prompt(parts, roster, title))
        result = normalize(merged, valid_ids)
        passes = len(chunks) + 1

    if not result["summary"]:
        result["summary"] = (
            "The transcript was too short or too unclear for a useful summary."
        )

    result.update(
        {
            "generated_at": _now(),
            "with_speakers": with_speakers,
            "model": _last_model or config.NOTES_MODEL,
            "empty": False,
        }
    )
    log.info(
        "wrote notes in %.1fs (%d groq call(s), speakers=%s)",
        time.perf_counter() - started,
        passes,
        with_speakers,
    )
    return result


# --------------------------------------------------------------------------
# storage + read-time resolution
# --------------------------------------------------------------------------


def dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def loads(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    payload.setdefault("action_items", [])
    payload.setdefault("key_takeaways", [])
    payload.setdefault("decisions", [])
    payload.setdefault("open_questions", [])
    payload.setdefault("follow_up_questions", [])
    payload.setdefault("summary", "")
    return payload


def resolve(payload: dict | None, names: dict[str, str]) -> dict | None:
    """A copy with every action-item owner resolved through the current names."""
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
