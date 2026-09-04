"""Typed access to the ``settings`` key/value table, and .env key management.

Two different kinds of configuration live here and they are stored differently
on purpose:

* **Preferences** (language hint, live window, diarization on/off, max
  speakers) go in the ``settings`` table. They are per-installation, not
  secret, and every reader picks them up on the next call - no restart.
* **Secrets** (``GROQ_API_KEY``, ``HF_TOKEN``) go in the root ``.env`` *and*
  into ``os.environ``. They never enter the database and are never returned by
  the API; only a "set / not set" flag and the last four characters are.

Every getter opens and closes its own short-lived session, so a caller inside
the pipeline never has to hold one across an ``await``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import config
from .db import SessionLocal
from .models import Setting

log = logging.getLogger("mrlistener.settings")

DIARIZATION_ENABLED = "diarization_enabled"
MAX_SPEAKERS = "max_speakers"
LANGUAGE_HINT = "language_hint"
LIVE_WINDOW_SECONDS = "live_window_seconds"
SUGGESTIONS_ENABLED = "suggestions_enabled"
SUGGESTIONS_INTERVAL_SECONDS = "suggestions_interval_seconds"

#: Everything the settings page can change, with the value used when the row is
#: absent. Keys not in here are rejected by :func:`put`.
DEFAULTS: dict[str, Any] = {
    DIARIZATION_ENABLED: True,
    MAX_SPEAKERS: None,
    LANGUAGE_HINT: "",
    LIVE_WINDOW_SECONDS: config.LIVE_CHUNK_SECONDS,
    SUGGESTIONS_ENABLED: True,
    SUGGESTIONS_INTERVAL_SECONDS: config.SUGGESTIONS_MIN_INTERVAL,
}

LIVE_WINDOW_MIN = 10
LIVE_WINDOW_MAX = 60
MAX_SPEAKERS_MIN = 2
MAX_SPEAKERS_MAX = 20
SUGGESTIONS_INTERVAL_MIN = 30
SUGGESTIONS_INTERVAL_MAX = 180


class SettingsError(ValueError):
    """A value the settings page sent that we refuse to store."""


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def get_all() -> dict[str, Any]:
    """Every known setting, defaults filled in for rows that do not exist."""
    values = dict(DEFAULTS)
    db = SessionLocal()
    try:
        for row in db.query(Setting).all():
            if row.key not in DEFAULTS:
                continue
            try:
                values[row.key] = json.loads(row.value) if row.value is not None else None
            except (json.JSONDecodeError, TypeError):
                log.warning("setting %s holds unreadable JSON; using the default", row.key)
    except Exception:  # noqa: BLE001 - a settings read must never break a request
        log.exception("could not read the settings table")
    finally:
        db.close()
    return values


def get(key: str) -> Any:
    return get_all().get(key, DEFAULTS.get(key))


def put(values: dict[str, Any]) -> dict[str, Any]:
    """Validate and store the given subset. Returns the full settings dict."""
    cleaned = {key: _coerce(key, value) for key, value in values.items() if key in DEFAULTS}
    unknown = sorted(set(values) - set(DEFAULTS))
    if unknown:
        raise SettingsError(f"Unknown setting(s): {', '.join(unknown)}")

    db = SessionLocal()
    try:
        for key, value in cleaned.items():
            row = db.get(Setting, key)
            encoded = json.dumps(value)
            if row is None:
                db.add(Setting(key=key, value=encoded))
            else:
                row.value = encoded
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise SettingsError(f"Could not save settings: {exc}") from exc
    finally:
        db.close()
    return get_all()


def _coerce(key: str, value: Any) -> Any:
    if key in (DIARIZATION_ENABLED, SUGGESTIONS_ENABLED):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    if key == MAX_SPEAKERS:
        if value in (None, "", "auto"):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise SettingsError("max_speakers must be a whole number or empty") from exc
        if not MAX_SPEAKERS_MIN <= number <= MAX_SPEAKERS_MAX:
            raise SettingsError(
                f"max_speakers must be between {MAX_SPEAKERS_MIN} and {MAX_SPEAKERS_MAX}"
            )
        return number

    if key == LANGUAGE_HINT:
        text = str(value or "").strip().lower()
        if text in ("auto", "automatic"):
            return ""
        if len(text) > 12:
            raise SettingsError("The language hint must be an ISO code, not a sentence")
        if text and not text.replace("-", "").isalpha():
            raise SettingsError("The language hint may only contain letters and hyphens")
        return text

    if key == LIVE_WINDOW_SECONDS:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise SettingsError("live_window_seconds must be a whole number") from exc
        return max(LIVE_WINDOW_MIN, min(LIVE_WINDOW_MAX, number))

    if key == SUGGESTIONS_INTERVAL_SECONDS:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise SettingsError("suggestions_interval_seconds must be a whole number") from exc
        return max(SUGGESTIONS_INTERVAL_MIN, min(SUGGESTIONS_INTERVAL_MAX, number))

    return value


# --------------------------------------------------------------------------
# typed accessors used by the rest of the app
# --------------------------------------------------------------------------


def diarization_enabled() -> bool:
    return bool(get(DIARIZATION_ENABLED))


def max_speakers() -> int | None:
    value = get(MAX_SPEAKERS)
    return int(value) if value else None


def language_hint() -> str | None:
    """The stored hint, falling back to ``TRANSCRIPT_LANGUAGE`` in .env."""
    stored = str(get(LANGUAGE_HINT) or "").strip()
    return stored or config.transcript_language()


def live_window_seconds() -> int:
    try:
        return int(get(LIVE_WINDOW_SECONDS))
    except (TypeError, ValueError):
        return config.LIVE_CHUNK_SECONDS


def suggestions_enabled() -> bool:
    return bool(get(SUGGESTIONS_ENABLED))


def suggestions_interval_seconds() -> int:
    """Seconds between two automatic suggestion refreshes, clamped to range."""
    try:
        number = int(get(SUGGESTIONS_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        return config.SUGGESTIONS_MIN_INTERVAL
    return max(SUGGESTIONS_INTERVAL_MIN, min(SUGGESTIONS_INTERVAL_MAX, number))


# --------------------------------------------------------------------------
# API keys (.env, never the database)
# --------------------------------------------------------------------------

#: Field name in the API -> environment variable it maps onto.
KEY_FIELDS = {
    "groq_api_key": "GROQ_API_KEY",
    "hf_token": "HF_TOKEN",
}


def keys_status() -> dict[str, dict[str, Any]]:
    """"Set" plus the last four characters, or "not set". Never the key."""
    return {
        "groq_api_key": {
            "set": config.groq_api_key() is not None,
            "last4": config.masked(config.groq_api_key()),
        },
        "hf_token": {
            "set": config.hf_token() is not None,
            "last4": config.masked(config.hf_token()),
        },
    }


def set_keys(values: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Write the given keys to the root .env and apply them to this process.

    An absent or blank field leaves the stored key untouched - the settings
    page shows a masked placeholder rather than the real value, so a submitted
    empty box means "I did not change this", never "delete it".
    """
    for field, env_name in KEY_FIELDS.items():
        raw = values.get(field)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        config.write_env_var(env_name, text)
        log.info("updated %s in .env", env_name)
    return keys_status()
