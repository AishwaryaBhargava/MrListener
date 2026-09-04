"""Markdown export for a finished meeting.

One function, :func:`markdown`, that renders everything the detail page shows
into a single portable file: the notes, the speaker roster, and the full
transcript with speaker labels and timestamps. The notes sections come in the
same order as the detail page - summary, topics, decisions, action items, the
per-speaker breakdown, open questions and the follow-ups - and empty ones are
left out entirely.

Speaker names, action-item owners, who asked an open question and the heading
on each ``by_speaker`` block are all resolved through ``speaker_names_json``
here too, so an exported file always carries the names the user actually typed.
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

        takeaways = [str(item).strip() for item in note.get("key_takeaways") or [] if str(item).strip()]
        if takeaways:
            lines += ["## Key takeaways", ""]
            lines += [f"- {item}" for item in takeaways]
            lines.append("")

        topics = note.get("topics") or []
        if topics:
            lines += ["## Topics", ""]
            for topic in topics:
                span = f"{_stamp(topic.get('start') or 0.0)}–{_stamp(topic.get('end') or 0.0)}"
                title = str(topic.get("title") or "").strip()
                detail = str(topic.get("summary") or "").strip()
                tail = f" — {detail}" if detail else ""
                lines.append(f"- **{span}** · **{title}**{tail}")
            lines.append("")

        decisions = [str(item).strip() for item in note.get("decisions") or [] if str(item).strip()]
        if decisions:
            lines += ["## Decisions", ""]
            lines += [f"- {item}" for item in decisions]
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

        # Names here are already resolved through speaker_names_json, so a
        # renamed speaker is renamed in the export too.
        blocks = note.get("by_speaker") or []
        if blocks:
            lines += ["## By speaker", ""]
            for block in blocks:
                lines += [f"### {str(block.get('name') or block.get('speaker_id') or '').strip()}", ""]
                for label, key in (
                    ("Main points", "main_points"),
                    ("Commitments", "commitments"),
                    ("Questions raised", "questions_raised"),
                ):
                    entries = [str(e).strip() for e in block.get(key) or [] if str(e).strip()]
                    if not entries:
                        continue
                    lines += [f"**{label}**", ""]
                    lines += [f"- {entry}" for entry in entries]
                    lines.append("")

        questions = note.get("open_questions") or []
        if questions:
            lines += ["## Open questions", ""]
            for item in questions:
                text = str(item.get("question") or "").strip()
                if not text:
                    continue
                meta = []
                if item.get("asked_by"):
                    meta.append(f"asked by {item['asked_by']}")
                if item.get("time") is not None:
                    meta.append(_stamp(float(item["time"])))
                meta.append("answered" if item.get("answered") else "unanswered")
                lines.append(f"- {text} — {' · '.join(meta)}")
            lines.append("")

        for heading, key in (("Follow-ups for you", "follow_up_questions"),):
            items = [str(item).strip() for item in note.get(key) or [] if str(item).strip()]
            if items:
                lines += [f"## {heading}", ""]
                lines += [f"- {item}" for item in items]
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
