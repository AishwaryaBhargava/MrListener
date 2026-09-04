"""Markdown export for a finished meeting.

One function, :func:`markdown`, that renders everything the detail page shows
into a single portable file: the notes, the speaker roster, and the full
transcript with speaker labels and timestamps. Speaker names and action-item
owners are resolved through ``speaker_names_json`` here too, so an exported
file always carries the names the user actually typed.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from . import notes as notes_mod
from . import speakers as speakers_mod
from . import transcripts
from .models import Meeting

_UNSAFE = re.compile(r"[^A-Za-z0-9 ._-]+")


def filename(meeting: Meeting) -> str:
    """A download name derived from the title, safe on Windows and POSIX."""
    stem = _UNSAFE.sub("", meeting.title or "meeting").strip() or "meeting"
    return f"{stem[:80]}.md"


def _stamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _duration(seconds: float | None) -> str:
    if not seconds:
        return "unknown length"
    if seconds < 60:
        return f"{int(round(seconds))} sec"
    return f"{int(round(seconds / 60))} min"


def _date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def markdown(meeting: Meeting) -> str:
    names = speakers_mod.name_map(meeting.speaker_names_json)
    roster = speakers_mod.as_list(meeting.speaker_names_json)
    payload = transcripts.loads(meeting.transcript_json) or transcripts.empty()
    segments = speakers_mod.resolve_segments(payload.get("segments") or [], names)
    note = notes_mod.resolve(notes_mod.loads(meeting.notes_json), names)

    lines: list[str] = [f"# {meeting.title}", ""]

    meta = [_date(meeting.created_at), _duration(meeting.duration_seconds)]
    if roster:
        meta.append(f"{len(roster)} speaker{'s' if len(roster) != 1 else ''}")
    lines += [" · ".join(part for part in meta if part), ""]

    if roster:
        lines += ["## Speakers", ""]
        for entry in roster:
            talk = _stamp(entry["talk_time"]) if entry["talk_time"] else "--"
            lines.append(f"- **{entry['name']}** — {talk} of talk time")
        lines.append("")

    if note:
        summary = str(note.get("summary") or "").strip()
        if summary:
            lines += ["## Summary", "", summary, ""]

        for heading, key in (("Key takeaways", "key_takeaways"), ("Decisions", "decisions")):
            items = [str(item).strip() for item in note.get(key) or [] if str(item).strip()]
            if items:
                lines += [f"## {heading}", ""]
                lines += [f"- {item}" for item in items]
                lines.append("")

        actions = note.get("action_items") or []
        if actions:
            lines += ["## Action items", ""]
            for item in actions:
                box = "x" if item.get("done") else " "
                text = str(item.get("task") or "").strip()
                extras = []
                if item.get("owner"):
                    extras.append(str(item["owner"]))
                if item.get("due"):
                    extras.append(f"due {item['due']}")
                if item.get("source_time") is not None:
                    extras.append(_stamp(float(item["source_time"])))
                tail = f" ({' · '.join(extras)})" if extras else ""
                lines.append(f"- [{box}] {text}{tail}")
            lines.append("")

        for heading, key in (
            ("Open questions", "open_questions"),
            ("Follow-up questions", "follow_up_questions"),
        ):
            questions = [str(q).strip() for q in note.get(key) or [] if str(q).strip()]
            if questions:
                lines += [f"## {heading}", ""]
                lines += [f"- {question}" for question in questions]
                lines.append("")

    lines += ["## Transcript", ""]
    if not segments:
        lines += ["_No transcript was produced for this recording._", ""]
    else:
        # Consecutive segments by one speaker are collapsed under a single
        # heading, the same grouping the detail page uses on screen.
        current: str | None = None
        buffer: list[str] = []

        def flush() -> None:
            if not buffer:
                return
            lines.append(" ".join(buffer))
            lines.append("")
            buffer.clear()

        for segment in segments:
            who = segment.get("speaker")
            key = f"{who}" if who else ""
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            if key != current:
                flush()
                current = key
                head = f"**{who}** · {_stamp(segment.get('start') or 0.0)}" if who else f"**{_stamp(segment.get('start') or 0.0)}**"
                lines.append(head)
                lines.append("")
            buffer.append(text)
        flush()

    return "\n".join(lines).rstrip() + "\n"
