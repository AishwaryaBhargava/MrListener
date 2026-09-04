"""The settings page's endpoints.

Preferences round-trip through the ``settings`` table; API keys round-trip
through the root ``.env``. A key is never returned - only whether it is set and
its last four characters, which is enough for the user to tell one key from
another without the value ever crossing the wire again.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import settings as settings_module
from ..schemas import KeysOut, KeysUpdate, SettingsOut, SettingsUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("", response_model=SettingsOut)
def get_settings() -> SettingsOut:
    return SettingsOut(**settings_module.get_all())


@router.put("", response_model=SettingsOut)
def put_settings(payload: SettingsUpdate) -> SettingsOut:
    """Store the fields that were sent. Omitted fields keep their value."""
    values = payload.model_dump(exclude_unset=True)
    # max_speakers is legitimately null ("let the model decide"), so it is only
    # skipped when the client did not mention it at all.
    try:
        return SettingsOut(**settings_module.put(values))
    except settings_module.SettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/keys", response_model=KeysOut)
def get_keys() -> KeysOut:
    return KeysOut(**settings_module.keys_status())


@router.put("/keys", response_model=KeysOut)
def put_keys(payload: KeysUpdate) -> KeysOut:
    """Write the given keys to .env and apply them to the running process."""
    values = payload.model_dump(exclude_unset=True)
    return KeysOut(**settings_module.set_keys(values))
