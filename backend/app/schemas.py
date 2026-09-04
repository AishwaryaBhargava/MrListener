"""Pydantic request/response models."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MeetingCreate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=500)


class MeetingUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=500)


class SpeakerOut(BaseModel):
    """One speaker as the detail page draws it."""

    #: Stable key, "S1"/"S2"/... - what a rename is addressed to.
    id: str
    #: Current display name: the default "Speaker 1" until the user renames it.
    name: str
    #: Seconds of speech attributed to this speaker.
    talk_time: float = 0.0
    turn_count: int = 0
    #: Slot in the frontend's speaker palette, already wrapped.
    color_index: int = 0


class MeetingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    created_at: str
    duration_seconds: Optional[float] = None
    status: str
    audio_path: Optional[str] = None
    # Which pipeline step is running: "converting" | "transcribing" |
    # "summarizing" | "identifying_speakers" | null. Only meaningful while
    # status is "processing".
    pipeline_stage: Optional[str] = None
    pipeline_error: Optional[str] = None
    #: The "Recording NN" name, when Stage 4 replaced it with a suggestion.
    auto_title: Optional[str] = None
    #: How the audio arrived: "live" (microphone) or "upload" (a file).
    source: str = "live"
    #: The name of the uploaded file, when there was one.
    source_filename: Optional[str] = None

    has_audio: bool = False
    has_transcript: bool = False
    has_notes: bool = False
    #: First ~120 characters of the transcript, for the meetings list.
    transcript_preview: Optional[str] = None
    #: First sentence of the summary; the preferred list preview.
    notes_preview: Optional[str] = None
    #: Text around the search hit, when the request carried ?q=.
    match_snippet: Optional[str] = None

    speaker_count: int = 0
    action_item_count: int = 0
    #: Only populated on the detail response; the list stays small on purpose.
    speakers: List[SpeakerOut] = Field(default_factory=list)
    #: The full notes payload, owners resolved to current speaker names.
    notes: Optional[Dict[str, Any]] = None

    @field_validator("source", mode="before")
    @classmethod
    def _default_source(cls, value: Optional[str]) -> str:
        """A row from a database the migration has not touched reads as live."""
        return value or "live"


class TranscriptSegment(BaseModel):
    id: int
    start: float
    end: float
    text: str
    #: Present once diarization has run. ``speaker`` is resolved at read time,
    #: so it always carries whatever the speaker is called right now.
    speaker: Optional[str] = None
    speaker_id: Optional[str] = None


class TranscriptOut(BaseModel):
    segments: List[TranscriptSegment] = Field(default_factory=list)
    language: Optional[str] = None
    #: "live" while recording, "final" once the full-file pass has replaced it.
    source: str = "final"


class SpeakerRename(BaseModel):
    """``{"S1": "Priya"}`` - one or more speakers at a time."""

    model_config = ConfigDict(extra="allow")


class ActionItemUpdate(BaseModel):
    done: bool


class SettingsOut(BaseModel):
    diarization_enabled: bool = True
    max_speakers: Optional[int] = None
    #: "" means auto-detect.
    language_hint: str = ""
    live_window_seconds: int = 20


class SettingsUpdate(BaseModel):
    diarization_enabled: Optional[bool] = None
    max_speakers: Optional[int] = None
    language_hint: Optional[str] = None
    live_window_seconds: Optional[int] = None


class KeyStatus(BaseModel):
    set: bool = False
    #: Last four characters only. The key itself never leaves the backend.
    last4: Optional[str] = None


class KeysOut(BaseModel):
    groq_api_key: KeyStatus
    hf_token: KeyStatus


class KeysUpdate(BaseModel):
    """A blank or absent field means "leave this key as it is"."""

    groq_api_key: Optional[str] = None
    hf_token: Optional[str] = None


class HealthOut(BaseModel):
    ok: bool
    ffmpeg: bool
    groq_key_set: bool
    hf_token_set: bool
    version: str = ""
    #: Absolute path to backend/data, shown on the Settings page.
    data_dir: str = ""
