# API reference

Base URL in production: `http://localhost:8000`. In development the same paths
are proxied through Vite on port 5173. Interactive docs with request and
response schemas are served at `/docs` while the backend runs.

All JSON responses for a meeting use the same shape (`MeetingOut`), which
includes `id`, `title`, `created_at`, `duration_seconds`, `status`,
`pipeline_stage`, `pipeline_error`, `has_audio`, `has_transcript`,
`has_notes`, `transcript_preview`, `notes_preview`, `speaker_count`,
`action_item_count`, `speakers[]` and `notes`.

## Health

| Method | Path | Notes |
| ------ | ---- | ----- |
| GET | `/api/health` | `{ok, ffmpeg, groq_key_set, hf_token_set, version, data_dir}`. Reports whether keys are present, does not validate them |

## Meetings

| Method | Path | Body / query | Notes |
| ------ | ---- | ------------ | ----- |
| POST | `/api/meetings` | `{title?}` | Creates a meeting in `recording` status. Empty title becomes `Recording NN` |
| GET | `/api/meetings` | `?q=` | Newest first. With `q`, searches title, transcript and notes and adds `match_snippet` |
| GET | `/api/meetings/{id}` | | Full detail; poll this while `status == processing` |
| PATCH | `/api/meetings/{id}` | `{title}` | Rename |
| DELETE | `/api/meetings/{id}` | | Removes the row and its audio folder |
| POST | `/api/meetings/{id}/stop` | | Converts audio, sets `processing`, starts the pipeline. Idempotent |
| POST | `/api/meetings/{id}/reprocess` | | Re-runs transcription, notes, diarization, notes |
| POST | `/api/meetings/{id}/notes/regenerate` | | Re-runs only the notes step |
| PATCH | `/api/meetings/{id}/speakers` | `{"S1": "Priya", ...}` | Rename speakers; segments are resolved at read time |
| PATCH | `/api/meetings/{id}/action_items/{index}` | `{done: bool}` | Tick or untick an action item |
| GET | `/api/meetings/{id}/transcript` | | `{segments[], language, source}` |
| GET | `/api/meetings/{id}/audio` | `Range` header supported | The 16 kHz mono WAV, seekable |
| GET | `/api/meetings/{id}/export.md` | | Markdown: notes then the full transcript |

## Live suggestions

| Method | Path | Body | Notes |
| ------ | ---- | ---- | ----- |
| GET | `/api/meetings/{id}/suggestions` | | `{batches[], pinned[], live, enabled, interval_seconds}` |
| POST | `/api/meetings/{id}/suggestions/refresh` | | Forces a new batch for an active recording; waits for an in-flight one instead of duplicating it |
| PATCH | `/api/meetings/{id}/suggestions/pins` | `{pinned: [{text, kind, why, based_on_time}]}` | Replaces the pinned list |

A suggestion item is `{text, kind, why, based_on_time}` where `kind` is one of
`question`, `clarify`, `follow_up`, `risk`.

## Settings

| Method | Path | Body | Notes |
| ------ | ---- | ---- | ----- |
| GET | `/api/settings` | | `{diarization_enabled, max_speakers, language_hint, live_window_seconds, suggestions_enabled, suggestions_interval_seconds}` |
| PUT | `/api/settings` | Any subset of the above | Partial update |
| GET | `/api/settings/keys` | | Status of each key: set or not, with the last four characters |
| PUT | `/api/settings/keys` | `{groq_api_key?, hf_token?}` | Writes the root `.env` and applies without restart. Blank or absent means leave unchanged |

## WebSocket `/ws/record/{id}`

Open it after `POST /api/meetings`. Send audio as **binary** frames, each one a
`MediaRecorder` blob of `audio/webm;codecs=opus`. Optional text frames:
`{"type":"stop"}` and `{"type":"ping"}`.

Frames from the server, all JSON text:

| `type` | Payload | When |
| ------ | ------- | ---- |
| `status` | `{stage}` | The backend changed what it is doing |
| `transcript` | `{segments: [{id, start, end, text}]}` | A live window finished transcribing |
| `suggestions` | `{items[], generated_at, transcript_end}` | A new "Ask next" batch |
| `error` | `{message}` | Transcription trouble; recording continues regardless |
| `stopped` | `{bytes}` | Reply to a `stop` text frame |
| `pong` | | Reply to `ping` |

Closing the socket never loses audio already written. `POST /stop` is the
source of truth for ending a recording.
