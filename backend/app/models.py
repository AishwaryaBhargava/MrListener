"""ORM models.

The ``meetings`` table already carries the nullable JSON-text columns that
Stages 2-4 fill in (transcript, diarization, notes, speaker names) so those
stages need no migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Float, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Status is an enum-like string rather than a DB enum so later stages can add
# values ("transcribing", "diarizing", ...) without a migration.
STATUS_RECORDING = "recording"
STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
MEETING_STATUSES = (
    STATUS_RECORDING,
    STATUS_PROCESSING,
    STATUS_READY,
    STATUS_FAILED,
)

# ``pipeline_stage`` says which step of run_pipeline() is in flight while the
# status is "processing". It is NULL whenever nothing is running.
STAGE_TRANSCRIBING = "transcribing"
#: Stage 3. Named for what the user sees, not for the algorithm - the notes are
#: already on screen by the time this one starts and it is the slow step.
STAGE_IDENTIFYING_SPEAKERS = "identifying_speakers"
STAGE_SUMMARIZING = "summarizing"
#: Kept so a row written by an older build still reads back sensibly.
STAGE_DIARIZING = "diarizing"
PIPELINE_STAGES = (
    STAGE_TRANSCRIBING,
    STAGE_SUMMARIZING,
    STAGE_IDENTIFYING_SPEAKERS,
    STAGE_DIARIZING,
)


def new_uuid() -> str:
    return str(uuid.uuid4())


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now_iso)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=STATUS_RECORDING)
    audio_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    #: The auto-assigned "Recording NN" name, kept when Stage 4 replaces the
    #: title with the model's suggestion. NULL for a user-named meeting.
    auto_title: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Which pipeline step is running right now, and why the last one failed.
    pipeline_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pipeline_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Reserved for later stages (all nullable, all JSON encoded as text) ---
    transcript_json: Mapped[str | None] = mapped_column(Text, nullable=True)      # Stage 2
    diarization_json: Mapped[str | None] = mapped_column(Text, nullable=True)     # Stage 3
    notes_json: Mapped[str | None] = mapped_column(Text, nullable=True)           # Stage 4
    speaker_names_json: Mapped[str | None] = mapped_column(Text, nullable=True)   # Stage 3/5
    #: Written by the removed live-suggestions feature. Nothing reads or
    #: writes it any more; the column stays so an existing database opens
    #: unchanged and no migration has to drop anything.
    suggestions_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class Setting(Base):
    """Simple key/value store used by the Stage 5 settings page."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
