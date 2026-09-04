# Architecture

MrListener is two processes in development (FastAPI on 8000, Vite on 5173) and
one in production (FastAPI serving the built frontend). Everything is local
except two outbound calls to Groq: Whisper for speech-to-text and a chat model
for the notes.

```
Browser (React)                         Backend (FastAPI)                     External
─────────────────                       ─────────────────                     ────────
MediaRecorder ──webm/opus 1 s blobs──►  /ws/record/{id} ──► raw.webm
                                        every 20 s: ffmpeg slice ──────────►  Groq Whisper
   Live transcript ◄──{"type":"transcript"}──┘
Stop ──POST /stop──►  ffmpeg → audio.wav (16 kHz mono) → run_pipeline()
                                          1. transcribe full file ──────────►  Groq Whisper
                                          2. notes ───────────────────────────►  Groq chat
                                          3. diarize (pyannote, local CPU, in a worker thread)
                                          4. notes again with speaker labels ►  Groq chat
Detail page ◄──polls GET /api/meetings/{id} every 2 s while status == processing
```

## Repository layout

```
backend/
  app/
    main.py          FastAPI app, CORS, static serving + SPA fallback, startup recovery
    config.py        Paths, model names, tunables (LIVE_CHUNK_SECONDS, NOTES_MODEL, ...)
    db.py            SQLAlchemy engine/session, additive ALTER TABLE migrations
    models.py        Meeting and Setting tables
    schemas.py       Pydantic request/response models
    services.py      Meeting CRUD, sequential "Recording NN" naming, search
    audio.py         ffmpeg/ffprobe helpers (webm → wav, duration, window slicing)
    groq_client.py   Groq SDK wrapper with retry/backoff and missing-key errors
    transcripts.py   Transcript JSON shape, merging live windows, previews
    live.py          Per-recording asyncio task: window transcription and WS push
    pipeline.py      run_pipeline(): transcribe → notes → diarize → notes; stage/error bookkeeping
    diarization.py   pyannote wrapper: diarize(), assign_speakers(), speaker_summary()
    speakers.py      Read-time speaker name resolution and colour indices
    notes.py         Notes generation (JSON mode, map-reduce for long transcripts), model fallback
    export.py        Markdown export
    settings.py      Settings table access and .env key writes
    routers/
      health.py      GET /api/health
      meetings.py    Everything under /api/meetings
      record.py      WebSocket /ws/record/{id}
      settings.py    GET/PUT /api/settings and /api/settings/keys
  scripts/           Smoke tests that run against the real APIs (see README "Smoke tests")
  requirements.txt   All Python deps, including the CPU torch/pyannote block at the bottom
  requirements-ml.txt  The ML block on its own, for reference
frontend/
  src/
    pages/           RecordPage, MeetingsPage, MeetingDetailPage, SettingsPage
    components/      Sidebar, Transcript, NotesPanel, SpeakerChips,
                     PipelineProgress, AudioPlayerBar, StatusPill, Toast, Icons
    hooks/           useRecorder (mic + MediaRecorder + WS), useAudioMeter, useHealth
    lib/             api.ts (fetch wrappers), format.ts, speakers.ts
    styles/          tokens.css (design tokens), app.css
scripts/
  start.cmd          Double-click launcher: build + serve on 8000 + open browser
  dev.ps1            Backend + frontend with hot reload
docs/                This folder
```

## Data model

One SQLite database at `backend/data/mrlistener.db`.

**meetings**

| Column | Purpose |
| ------ | ------- |
| `id` | UUID |
| `title` | User-editable. Auto-named `Recording NN`, then replaced by the notes model's suggestion unless the user already renamed it |
| `auto_title` | The original `Recording NN` name, kept for reference |
| `created_at` | UTC ISO timestamp of the start click |
| `duration_seconds` | From ffprobe after stop |
| `status` | `recording` → `processing` → `ready` (`failed` is reserved) |
| `audio_path` | Path to `audio.wav` |
| `pipeline_stage` | `converting` (uploads only), `transcribing`, `summarizing`, `identifying_speakers`, or null |
| `source` | `live` for a microphone recording, `upload` for a file. Added by the additive migration with `DEFAULT 'live'`, so every pre-existing row reads back as a live recording |
| `source_filename` | The name the uploaded file arrived under; null for a live recording |
| `pipeline_error` | Last user-visible pipeline failure; the meeting still lands on `ready` so audio stays playable |
| `transcript_json` | `{"segments":[{id,start,end,text,speaker?,speaker_id?}], "language", "source": "live"\|"final"}` |
| `diarization_json` | Raw turns `[{start,end,speaker}]` from pyannote |
| `speaker_names_json` | `{"S1": "Priya", "S2": "Speaker 2", ...}` plus talk-time summary. Segments are never rewritten on rename; names resolve at read time |
| `notes_json` | The notes object: summary, `topics` (chapters with `start`/`end`), key takeaways, decisions, action items (with `done`, `owner`, `owner_speaker_id`), `open_questions` (objects with `asked_by_speaker_id`, `time`, `answered`), `follow_up_questions`, `by_speaker`, `with_speakers`, `model` |
| `suggestions_json` | Dead column from the removed live-suggestions feature. Never read or written; kept so an existing database opens without a migration |

**settings**: key/value rows for `diarization_enabled`, `max_speakers`,
`language_hint` and `live_window_seconds`. API keys are not stored here; they
live in the root `.env`. The data folder itself defaults to `backend/data` and
can be moved with the `MRLISTENER_DATA_DIR` environment variable, which is how
a smoke run keeps away from the real library.

Audio lives in `backend/data/audio/<meeting_id>/`: `raw.webm` (what the browser
sent), `audio.wav` (16 kHz mono PCM used by everything downstream), a transient
`live_window.wav`, and for an uploaded meeting `upload.<ext>` - the file the
user handed over, kept as it arrived. Only `audio.wav` is ever listed, played
or processed; the original is there because it is the user's and is removed
with the folder on delete.

## The recording path

1. The browser acquires the microphone first, then `POST /api/meetings`, so a
   denied permission never creates an orphan row.
2. `MediaRecorder` emits one `audio/webm;codecs=opus` blob per second; each is
   sent as a binary WebSocket frame and appended to `raw.webm`. Pause stops the
   blobs; resume continues the same file.
3. `live.py` runs one asyncio task per active recording. Every
   `live_window_seconds` it asks ffmpeg to decode `raw.webm` from the last
   transcribed offset to the end into a small WAV, sends it to Whisper, offsets
   the returned segment times, and pushes `{"type":"transcript"}` to the socket.
   Later blobs from MediaRecorder are not independently decodable, which is why
   the whole file is re-read from the start each time.
4. `POST /stop` converts `raw.webm` to `audio.wav`, records the duration, sets
   `status = processing`, and schedules `run_pipeline()`.

## The upload path

`POST /api/meetings/upload` is the alternative to recording. The extension is
checked before anything is written (415 otherwise), the meeting row is created
straight into `processing` with `source = "upload"` and stage `converting`, and
the request body is streamed a megabyte at a time into
`backend/data/audio/<id>/upload.<ext>` - the 2 GB cap is enforced as it goes, so
an oversized file is abandoned partway rather than written out and measured
afterwards. A failed or rejected upload deletes the row it created, leaving
nothing behind. The response is sent the moment the file is on disk, so the
browser can navigate to the detail page and poll while the work happens.

That work is `run_pipeline()` itself, with one extra step in front:
`_step_convert` runs `audio.convert_to_wav` in a worker thread (ffmpeg on a long
video is minutes of CPU, and the event loop still has recordings to serve),
records the duration, and hands over to the ordinary transcription step. The
conversion is strict rather than lenient - an uploaded file is a complete
container, unlike the concatenated MediaRecorder clusters in `raw.webm` - and
`-map 0:a:0` takes the first audio stream, which is what makes video containers
work. The step is idempotent, so `POST /reprocess` on an upload skips it once a
wav exists and retries it when one does not. It is also the only step that can
fail the meeting outright (`status = failed`): every other failure still leaves
playable audio, and a failed conversion leaves nothing.

## The pipeline

`pipeline.py` runs these steps in order, updating `pipeline_stage` before each:

1. **Transcribe** the full WAV once more. Live windows can clip words at the
   boundaries; the full pass is cleaner and is what diarization aligns to.
2. **Notes**. Groq chat model in JSON mode: summary, chaptered `topics`,
   takeaways, decisions, action items, the questions raised in the room and the
   follow-ups for the reader. A transcript over `NOTES_SINGLE_PASS_CHARS` is
   map-reduced - each chunk answers in a deliberately compact shape (fewer
   items, shorter fields, no per-chapter prose) so the richer schema still fits
   inside `NOTES_MAX_OUTPUT_TOKENS`, and one merge pass combines and dedupes
   them. Topic ranges are sorted and de-overlapped in `notes._topics` rather
   than trusted.
3. **Diarize** with pyannote `speaker-diarization-3.1` on CPU in a worker
   thread behind a process-wide lock, so only one diarization runs at a time
   and the event loop stays free. Whisper segments get the speaker with the
   largest time overlap; speakers are numbered by first appearance.
4. **Notes again** with speaker labels. This is the pass that attributes
   action-item owners and question askers and fills `by_speaker`; only it sets
   `with_speakers: true`. If the model returns no `by_speaker` (most likely on
   a long, map-reduced transcript) one is assembled from the attributions
   already in hand rather than invented.

Ordering notes before diarization is deliberate: diarization runs at roughly
real time on a laptop CPU, and users should not wait an hour for a summary.

A failure in any step stores `pipeline_error` and still finishes with
`status = ready`, so the audio and whatever was produced remain usable.
`POST /reprocess` re-runs the whole chain; `POST /notes/regenerate` re-runs only
the notes. On startup, `recover_interrupted()` marks rows stranded in
`processing` by a crash as `ready` with an error pointing at Reprocess.

## Model selection

`config.NOTES_MODEL_CANDIDATES` lists chat models in order of preference. On
first use the backend checks which of them the API key can actually see and
picks the first available, so an account without access to one model degrades
to the next rather than failing.

## Frontend conventions

- No component library. `tokens.css` holds the palette (soft teal ground, one
  accent, three speaker tints at matched lightness) and `app.css` the rest.
- Icons are inline stroke SVGs in `Icons.tsx`; no emoji anywhere in the UI.
- Pages poll the meeting every 2 seconds while `status == processing`; there is
  no server-sent event stream.
- The Record page keeps the WebSocket open for the whole recording and handles
  `transcript`, `status`, `error` and `stopped` frames.
- The detail page's two columns each scroll inside themselves with hidden
  scrollbars, so neither the transcript nor the notes drags the page past the
  fixed player bar.
