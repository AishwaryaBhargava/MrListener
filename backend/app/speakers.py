"""Speaker identity: the stored shape, renaming, and read-time resolution.

``meetings.speaker_names_json`` holds::

    {
      "names":   {"S1": "Priya", "S2": "Speaker 2"},
      "summary": [{"speaker_id": "S1", "speaker": "Speaker 1",
                   "talk_time": 91.4, "turn_count": 2, "segment_count": 7}]
    }

``names`` is the only mutable part: renaming a speaker rewrites one entry there
and nothing else. Transcript segments keep the ``speaker`` label diarization
gave them and are **never** rewritten - the display name is resolved from
``speaker_id`` every time something is read. That way a rename is one small
write instead of a rewrite of every segment, and it can never leave a
half-renamed transcript behind.

``summary`` is the talk-time table produced by ``diarization.speaker_summary``
and is refreshed only when diarization runs.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

#: How many distinct speaker colours the UI cycles through. Matches the
#: palette in frontend/src/lib/speakers.ts.
PALETTE_SIZE = 5

_ID_RE = re.compile(r"^S(\d+)$")


def dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def loads(raw: str | None) -> dict:
    """The stored payload, normalized. Missing/corrupt reads as empty."""
    if not raw:
        return {"names": {}, "summary": []}
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"names": {}, "summary": []}
    if not isinstance(payload, dict):
        return {"names": {}, "summary": []}

    names = payload.get("names")
    summary = payload.get("summary")
    return {
        "names": {str(k): str(v) for k, v in names.items()} if isinstance(names, dict) else {},
        "summary": [item for item in summary if isinstance(item, dict)]
        if isinstance(summary, list)
        else [],
    }


def build(summary: list[dict], previous: dict | None = None) -> dict:
    """A fresh payload from a diarization summary, keeping custom names.

    Re-running diarization must not throw away the names the user typed, so any
    ``S<n>`` that already had a non-default name keeps it.
    """
    kept = (previous or {}).get("names", {}) if previous else {}
    names: dict[str, str] = {}
    for entry in summary:
        sid = str(entry.get("speaker_id") or "")
        if not sid:
            continue
        default = str(entry.get("speaker") or sid)
        existing = kept.get(sid)
        names[sid] = existing if existing and not _is_default(existing) else default
    return {"names": names, "summary": summary}


def _is_default(name: str) -> bool:
    return re.fullmatch(r"Speaker \d+", name.strip()) is not None


def _order(speaker_id: str) -> int:
    match = _ID_RE.match(speaker_id)
    return int(match.group(1)) if match else 9999


def color_index(speaker_id: str) -> int:
    """Palette slot for a speaker. S1 -> 0, S2 -> 1, ... then wraps."""
    return (max(_order(speaker_id), 1) - 1) % PALETTE_SIZE


def name_map(raw: str | None) -> dict[str, str]:
    """``{"S1": "Priya"}`` - what every read-time resolution needs."""
    return loads(raw)["names"]


def display_name(speaker_id: str | None, names: dict[str, str], fallback: str | None = None) -> str | None:
    """The current name for a speaker id, or the fallback the caller stored."""
    if not speaker_id:
        return fallback
    return names.get(speaker_id) or fallback or f"Speaker {_order(speaker_id)}"


def rename(raw: str | None, updates: dict[str, str]) -> dict:
    """Apply ``{"S1": "Priya"}`` to the stored payload and return the new one."""
    payload = loads(raw)
    for sid, value in updates.items():
        key = str(sid).strip()
        if not _ID_RE.match(key):
            raise ValueError(f"{key!r} is not a speaker id (expected S1, S2, ...)")
        name = str(value or "").strip()
        if not name:
            raise ValueError("A speaker name cannot be empty")
        if len(name) > 80:
            raise ValueError("A speaker name must be 80 characters or fewer")
        payload["names"][key] = name
    return payload


def as_list(raw: str | None) -> list[dict]:
    """``[{id, name, talk_time, turn_count, color_index}]`` for the API.

    Ordered by speaker number so the colours are stable between page loads.
    """
    payload = loads(raw)
    names = payload["names"]
    by_id = {str(entry.get("speaker_id") or ""): entry for entry in payload["summary"]}

    ids = sorted((set(names) | set(by_id)) - {""}, key=_order)
    out: list[dict] = []
    for sid in ids:
        if not sid:
            continue
        entry = by_id.get(sid, {})
        out.append(
            {
                "id": sid,
                "name": display_name(sid, names, str(entry.get("speaker") or "") or None),
                "talk_time": round(float(entry.get("talk_time") or 0.0), 3),
                "turn_count": int(entry.get("turn_count") or 0),
                "color_index": color_index(sid),
            }
        )
    return out


def resolve_segments(segments: Iterable[dict], names: dict[str, str]) -> list[dict]:
    """Copy segments with ``speaker`` replaced by the current display name."""
    out: list[dict] = []
    for segment in segments:
        item = dict(segment)
        sid = item.get("speaker_id")
        if sid:
            item["speaker"] = display_name(sid, names, item.get("speaker"))
        out.append(item)
    return out


def transcript_lines(segments: Iterable[dict], names: dict[str, str] | None = None) -> list[str]:
    """"[mm:ss] Speaker: text" lines - the format the notes model is fed."""
    names = names or {}
    lines: list[str] = []
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start = float(segment.get("start") or 0.0)
        stamp = f"[{int(start) // 60:02d}:{int(start) % 60:02d}]"
        sid = segment.get("speaker_id")
        who = display_name(sid, names, segment.get("speaker")) if sid else segment.get("speaker")
        lines.append(f"{stamp} {who}: {text}" if who else f"{stamp} {text}")
    return lines


def any_speakers(segments: Iterable[dict]) -> bool:
    return any(segment.get("speaker_id") for segment in segments)


def coverage(segments: list[dict]) -> float:
    """Share of segments carrying a speaker label. 0.0 for an empty transcript."""
    if not segments:
        return 0.0
    labelled = sum(1 for segment in segments if segment.get("speaker_id"))
    return labelled / len(segments)


def summary_lookup(raw: str | None) -> dict[str, Any]:
    return {str(e.get("speaker_id")): e for e in loads(raw)["summary"]}
