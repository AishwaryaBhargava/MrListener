# API reference

Base URL in production: `http://localhost:8000`. In development the same paths
are proxied through Vite on port 5173. Interactive docs with request and
response schemas are served at `/docs` while the backend runs.

All JSON responses for a meeting use the same shape (`MeetingOut`), which
includes `id`, `title`, `created_at`, `duration_seconds`, `status`,
`pipeline_stage`, `pipeline_error`, `source`, `source_filename`, `has_audio`,
`has_transcript`, `has_notes`, `transcript_preview`, `notes_preview`,
`speaker_count`, `action_item_count`, `speakers[]` and `notes`.

`source` is `"live"` for a microphone recording or `"upload"` for a file sent
to `POST /api/meetings/upload`; `source_filename` carries the name the file
arrived under and is `null` for a live recording. `pipeline_stage` is one of
`converting`, `transcribing`, `summarizing`, `identifying_speakers` or null -
`converting` only ever appears on an upload.

## Health

| Method | Path | Notes |
| ------ | ---- | ----- |
| GET | `/api/health` | `{ok, ffmpeg, groq_key_set, hf_token_set, version, data_dir}`. Reports whether keys are present, does not validate them |

## Meetings

| Method | Path | Body / query | Notes |
| ------ | ---- | ------------ | ----- |
| POST | `/api/meetings` | `{title?}` | Creates a meeting in `recording` status. Empty title becomes `Recording NN` |
| POST | `/api/meetings/upload` | `multipart/form-data`: `file` (required), `title` (optional) | Processes an existing recording. Returns 201 with the meeting in `processing` status, stage `converting`, `source: "upload"` |
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

### POST `/api/meetings/upload`

Send `multipart/form-data` with a `file` part and, optionally, a `title` part.
An empty or absent title gets the same sequential `Recording NN` name as a live
recording, so the notes model's title suggestion still replaces it.

Accepted extensions, audio or video - anything ffmpeg can decode:

`mp3`, `m4a`, `aac`, `wav`, `flac`, `ogg`, `opus`, `webm`, `mp4`, `mov`, `mkv`, `wma`, `aiff`

| Status | When |
| ------ | ---- |
| 201 | Stored. The body is the meeting as created: `status: "processing"`, `pipeline_stage: "converting"`, `source: "upload"`. Poll `GET /api/meetings/{id}` from there |
| 400 | The file was empty, or the request body did not finish |
| 413 | Larger than the 2 GB cap. Detected while streaming, so nothing is kept |
| 415 | An extension that is not on the list above |

The body is streamed to `backend/data/audio/<id>/upload.<ext>` a megabyte at a
time and never held in memory. The response returns as soon as that file is on
disk: ffmpeg converts it to `audio.wav` in a worker thread while the meeting
sits on the `converting` stage, and then the ordinary pipeline runs unchanged.
A conversion that fails is the one pipeline error that lands on `status:
"failed"` rather than `ready` - there is no audio to keep. `POST /reprocess`
converts again from the stored original.

The original upload is kept beside `audio.wav`, which stays the source of truth
for playback (`GET /audio`) and every processing step. `DELETE
/api/meetings/{id}` removes both with the folder.

## The notes object

`MeetingOut.notes` (and `notes_json`) carries:

| Field | Shape |
| ----- | ----- |
| `title_suggestion` | `string \| null` - replaces a `Recording NN` title, once |
| `summary` | 3-5 sentences on the whole meeting |
| `topics` | `[{title, start, end, summary}]` - chapters in time order, never overlapping, times in seconds |
| `key_takeaways` | `string[]` |
| `decisions` | `string[]` |
| `action_items` | `[{task, owner, owner_speaker_id, due, source_time, done}]` |
| `open_questions` | `[{question, asked_by, asked_by_speaker_id, time, answered}]` - questions asked **in** the meeting |
| `follow_up_questions` | `string[]` - what the reader should chase afterwards |
| `by_speaker` | `[{speaker_id, name, main_points[], commitments[], questions_raised[]}]` - empty until diarization has run |
| `generated_at`, `with_speakers`, `model` | provenance |

`owner`, `asked_by` and `by_speaker[].name` are resolved from their stored
`S`-ids through `speaker_names_json` on every read, so `PATCH
/api/meetings/{id}/speakers` relabels all three at once.

Notes written by an older build are read back in this shape too:
`open_questions` given as plain strings become objects with empty attribution,
and `topics` and `by_speaker` come back empty.

## Settings

| Method | Path | Body | Notes |
| ------ | ---- | ---- | ----- |
| GET | `/api/settings` | | `{diarization_enabled, max_speakers, language_hint, live_window_seconds}` |
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
| `error` | `{message}` | Transcription trouble; recording continues regardless |
| `stopped` | `{bytes}` | Reply to a `stop` text frame |
| `pong` | | Reply to `ping` |

Closing the socket never loses audio already written. `POST /stop` is the
source of truth for ending a recording.
