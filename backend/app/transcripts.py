"""The stored transcript shape, and the helpers that read and write it.

``meetings.transcript_json`` always holds::

    {
      "segments": [{"id": 0, "start": 0.0, "end": 3.2, "text": "..."}],
      "language": "english",
      "source": "live" | "final"
    }

``source`` says where it came from: ``live`` is the concatenation of the 20 s
windows captured while recording (word boundaries can be clipped), ``final`` is
the single full-file pass run after /stop. Stage 3 should align against
``final`` whenever it is present.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

SOURCE_LIVE = "live"
SOURCE_FINAL = "final"

PREVIEW_CHARS = 120


#: Keys a segment may carry beyond the core four. Diarization adds these and
#: :func:`renumber` must not drop them, or a re-save would strip every label.
OPTIONAL_KEYS = ("speaker", "speaker_id")


def renumber(segments: Iterable[dict]) -> list[dict]:
    """Sort by start time and hand out contiguous ids from 0.

    Speaker labels, when present, are carried through untouched.
    """
    ordered = sorted(segments, key=lambda item: (item.get("start") or 0.0, item.get("end") or 0.0))
    out: list[dict] = []
    for index, item in enumerate(ordered):
        entry = {
            "id": index,
            "start": round(float(item.get("start") or 0.0), 3),
            "end": round(float(item.get("end") or 0.0), 3),
            "text": str(item.get("text") or "").strip(),
        }
        for key in OPTIONAL_KEYS:
            value = item.get(key)
            if value:
                entry[key] = str(value)
        out.append(entry)
    return out


def build(segments: Iterable[dict], language: str | None, source: str) -> dict:
    return {"segments": renumber(segments), "language": language, "source": source}


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
    payload.setdefault("segments", [])
    payload.setdefault("language", None)
    payload.setdefault("source", SOURCE_FINAL)
    return payload


def full_text(payload: dict | None) -> str:
    if not payload:
        return ""
    parts = [str(item.get("text") or "").strip() for item in payload.get("segments") or []]
    return " ".join(part for part in parts if part)


def preview(raw: str | None, limit: int = PREVIEW_CHARS) -> str | None:
    """First ~``limit`` characters of the transcript, for the meetings list."""
    text = full_text(loads(raw))
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def empty(source: str = SOURCE_LIVE) -> dict[str, Any]:
    return {"segments": [], "language": None, "source": source}
