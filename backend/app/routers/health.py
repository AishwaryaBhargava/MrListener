from __future__ import annotations

from fastapi import APIRouter

from ..config import APP_VERSION, DATA_DIR, env_flag, ffmpeg_bin
from ..schemas import HealthOut

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    """Reports tool/key availability. Keys are checked for presence only."""
    return HealthOut(
        ok=True,
        ffmpeg=ffmpeg_bin() is not None,
        groq_key_set=env_flag("GROQ_API_KEY"),
        hf_token_set=env_flag("HF_TOKEN"),
        version=APP_VERSION,
        data_dir=str(DATA_DIR),
    )
