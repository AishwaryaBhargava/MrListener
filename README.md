# MrListener

A meeting notetaker for the "second laptop" setup: a meeting plays on your main
machine, MrListener runs on a laptop beside it and records the room through that
laptop's microphone. It transcribes as you go, works out who said what, and
writes the notes — summary, takeaways, decisions, action items — on its own.

Everything except the two API calls it makes (Groq for speech-to-text and for
the notes) happens on your machine. The audio, the database and the speaker
models never leave it.

| Stage | Scope | Status |
| ----- | ----- | ------ |
| 1 | Recording, storage, playback, meeting list | **Done** |
| 2 | Live transcription (Groq Whisper) | **Done** |
| 3 | Speaker diarization (local pyannote) | **Done** |
| 4 | Summary, takeaways, action items (Groq LLM) | **Done** |
| 5 | Search, settings, production build, polish | **Done** |

## What it does

- **Records** from any microphone the browser can see, with a live level meter,
  pause/resume and a running timer. Space toggles pause.
- **Transcribes live** — every 20 seconds (configurable) the text so far appears
  on the Record page, so you can see it is working.
- **Tells you what to ask next.** While it listens, an **Ask next** column beside
  the live transcript fills with three to five concrete things worth saying out
  loud — a question nobody asked, a commitment with no owner, a number that went
  unqualified. Copy one to the clipboard, or pin it so it survives the next
  refresh.
- **Writes the notes first, then the speakers.** After you press stop you have a
  summary in about half a minute. Speaker labels arrive a few minutes later and
  the notes are quietly rewritten with real attribution.
- **Names speakers.** Click the pencil on a speaker chip, type "Priya", and
  every transcript header and action-item owner follows — instantly, everywhere,
  including the Markdown export.
- **Searches** across titles, transcripts *and* notes, and shows you the line
  the hit came from.
- **Exports** the whole meeting as one Markdown file, or copies the notes to the
  clipboard.

## Prerequisites

- **Python 3.10+** on PATH
- **Node 20+** and npm 10+
- **ffmpeg and ffprobe** on PATH (`ffmpeg -version` must work) — used to convert
  the browser recording into a 16 kHz mono WAV
- A browser that supports `MediaRecorder` with `audio/webm;codecs=opus`
  (Chrome, Edge or Firefox — Safari does not)
- A **Groq API key** — [console.groq.com/keys](https://console.groq.com/keys)
- A **Hugging Face token**, for speaker identification only. You must accept the
  terms for both gated models while signed in as that token's owner:
  [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
  and [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)

## Setup

From the project root:

```powershell
npm install          # installs concurrently for the dev script
npm run setup        # creates backend\.venv, pip installs, npm installs the frontend
```

`npm run setup` installs the speaker-diarization stack too (torch, torchaudio
and pyannote.audio — around 1.5 GB of CPU-only wheels), so the first run takes
a while. The app works without them; turn **Identify speakers** off on the
Settings page and you still get transcripts and notes.

Then add your keys. Either copy the template and edit it:

```powershell
Copy-Item .env.example .env
```

...or start the app and paste them into **Settings → API keys**, which writes
the same file and applies them without a restart.

| Variable | Needed for | Notes |
| -------- | ---------- | ----- |
| `GROQ_API_KEY` | Transcription and notes | Required |
| `HF_TOKEN` | Speaker identification | Optional; accept both model licences |
| `TRANSCRIPT_LANGUAGE` | optional | ISO-639-1 hint, e.g. `en`. The Settings page overrides it |

## Run

### Everyday use

```powershell
npm run build     # once, and again after any frontend change
npm start
```

That runs **one** process — uvicorn on port 8000 serving both the API and the
built UI — and opens your browser at <http://localhost:8000>. Ctrl+C stops it.

Double-clicking **`scripts\start.cmd`** does the build and the start in one go,
which is the easiest way to launch it day to day.

### Development

```powershell
npm run dev
```

Two processes: uvicorn with `--reload` on 8000 and Vite on 5173. Open
<http://localhost:5173>; Vite proxies `/api` and `/ws` to the backend, so the
browser only ever talks to 5173.

| Command | What it runs |
| ------- | ------------ |
| `npm run dev` | backend + frontend, both with hot reload |
| `npm run dev:backend` | uvicorn only, with `--reload` |
| `npm run dev:frontend` | Vite only |
| `npm run build` | type-checks and builds `frontend/dist` |
| `npm start` | production: build output + API on 8000, opens the browser |
| `npm run serve` | the same, without opening the browser |

| URL | |
| --- | --- |
| <http://localhost:8000> | the app (production mode) |
| <http://localhost:5173> | the app (dev mode) |
| <http://localhost:8000/docs> | API docs |
| <http://localhost:8000/api/health> | health check |

## Using it

1. Open **Record** and pick your microphone. Leave the title empty and the
   backend names the meeting `Recording 01`, `Recording 02`, ...
2. Press **Start recording**. Audio streams to the backend over a WebSocket in
   1 second chunks. **Pause** freezes the timer and the bars; **Resume**
   continues the same file. Space does the same thing when you are not typing.
3. Every 20 seconds the live transcript updates. About every 45 seconds — and
   only once roughly forty new words have been said — the **Ask next** column
   refreshes with what is worth asking now. The refresh icon forces one
   immediately; the copy icon puts a suggestion on the clipboard, and the pin
   icon moves it to a **Pinned** group that later batches never replace. Both
   are off if you turn **Live suggestions** off in Settings.
4. Press **Stop and process**. You land on the meeting page, which polls while
   the pipeline runs. The notes appear within roughly half a minute; a banner
   tells you speaker identification is still going.
5. When the speaker chips appear, click a pencil to rename someone. Every label
   on the page updates.
6. Tick action items as you do them. **Follow-up questions** lists what you
   should still chase — separate from **Open questions**, which is what the
   people in the room asked and nobody answered. **Suggestions during the
   meeting**, below the notes, replays what the Ask next column offered. **Export Markdown**, **Copy notes** and
   **Regenerate notes** are in the header. Space plays and pauses the audio;
   clicking any transcript line seeks there.
7. **Meetings** lists everything, newest first, and the search box looks inside
   transcripts and notes, not just titles.

## How the pipeline works

`POST /api/meetings/{id}/stop` converts the recording to a 16 kHz mono WAV and
hands it to `backend/app/pipeline.py`, which runs as a detached asyncio task and
walks a list of steps:

| # | `pipeline_stage` | What happens | Typical cost |
| - | ---------------- | ------------ | ------------ |
| 0 | `transcribing` | Transcribe the tail no live window covered | 1-3 s |
| 1 | `transcribing` | One clean Groq Whisper pass over the whole WAV | ~5 s per 10 min |
| 2 | `summarizing` | Notes, without speaker labels | 2-10 s |
| 3 | `identifying_speakers` | pyannote diarization, locally, on CPU | **~0.5-1x realtime** |
| 4 | `summarizing` | Notes again, now attributed to speakers | 2-10 s |

Then `pipeline_stage` goes null and the status becomes `ready`.

**Why the notes come before the speakers.** Diarization is the only slow step —
a 60 minute meeting takes 30 to 60 minutes on a laptop CPU. Everything else
finishes in well under a minute. Running the notes first means you read your
summary almost immediately, and the speaker labels arrive later as an upgrade;
the second notes pass is cheap and only exists so owners can be attributed to
real people.

**The app stays usable throughout.** Diarization is CPU-bound Python, so it runs
in a worker thread (`anyio.to_thread.run_sync`) and the event loop keeps serving
requests — you can browse, play back old meetings and *start a new recording*
while a previous one is still being processed. Only one diarization runs at a
time process-wide: two pyannote passes fighting over the same cores are slower
than two in sequence. A meeting waiting its turn still shows
"Identifying speakers".

A step that fails stores its message in `pipeline_error`, stops the run, and the
meeting still lands on `ready` — the audio and everything the earlier steps
produced are kept, and **Reprocess** retries the whole chain.

### Measured on the reference machine

Windows 11, Python 3.10, Intel i7-1165G7 (4 cores / 8 threads), a 34 second
two-speaker fixture:

| Step | Time |
| ---- | ---- |
| Full transcription pass | ~3 s |
| Notes (first pass) | 1.7 s |
| Diarization | 17 s |
| Notes (with speakers) | 1.8 s |

Scale the diarization row linearly with meeting length; the others barely move.

## How live suggestions work

While a meeting is recording, `backend/app/suggestions.py` runs alongside the
live transcription loop. After a live window produces new text the loop asks it
whether a refresh is due; it is when **both** gates are open — at least
`suggestions_interval_seconds` (default 45) since the last refresh started, and
at least ~40 new words. The Groq call then runs as its own asyncio task, so a
slow completion can never delay the next transcription window, and a second
refresh is dropped rather than queued while one is in flight.

**The prompt stays bounded.** Only the last 8 minutes of transcript go in
verbatim. Everything older is represented by a rolling "context so far" note,
which the model itself re-compresses into 3-5 lines every third refresh. An
hour-long meeting therefore costs the same per refresh as a five minute one.

Each batch arrives over the existing recording WebSocket as
`{"type": "suggestions", "items": [...], "generated_at": ..., "transcript_end": ...}`.
The last 20 batches are kept in memory and written into `suggestions_json` at
`/stop`, which is what the detail page reads back.

| Endpoint | |
| -------- | --- |
| `POST /api/meetings/{id}/suggestions/refresh` | force a batch now; returns it |
| `GET /api/meetings/{id}/suggestions` | the batch history and the pinned set |
| `PATCH /api/meetings/{id}/suggestions/pins` | replace the pinned set |

One refresh costs a single chat completion — around 2-3 seconds on the
reference machine, or about 4-5 seconds end to end through the refresh
endpoint. A failure is logged and skipped: recording, transcription and the
notes never depend on it.

## Where your data lives

```text
backend/data/
├── mrlistener.db            SQLite (meetings + settings tables)
└── audio/
    └── <meeting-id>/
        ├── raw.webm         the browser's original opus stream, kept as-is
        ├── audio.wav        16 kHz mono PCM, what every later step consumes
        └── live_window.wav  scratch file for the live window, removed at /stop
```

`meetings` columns worth knowing:

| Column | Holds |
| ------ | ----- |
| `transcript_json` | `{"segments": [{id, start, end, text, speaker?, speaker_id?}], "language", "source"}` |
| `diarization_json` | `{"turns": [{start, end, speaker}]}` straight from pyannote |
| `speaker_names_json` | `{"names": {"S1": "Priya"}, "summary": [talk time per speaker]}` |
| `notes_json` | the notes object, including per-item `done` state |
| `suggestions_json` | `{"batches": [{items, generated_at, transcript_end}], "pinned": [...], "context_summary"}` — the live suggestions, frozen at `/stop` |
| `auto_title` | the original `Recording NN` name, when the notes step renamed the meeting |

Two design notes that matter if you extend this:

- **Speaker names are resolved at read time, never stored on segments.**
  Renaming S1 rewrites one entry in `speaker_names_json`; the transcript,
  the notes owners and the Markdown export all resolve through it. A rename can
  never leave a half-updated transcript behind.
- **The title is only auto-replaced when it still matches `^Recording \d+$`.**
  A title you typed is never overwritten by the model's suggestion.

## Settings

Everything on the Settings page is stored in the `settings` table and picked up
on the next call — no restart.

| Setting | Default | Notes |
| ------- | ------- | ----- |
| Groq API key | — | Written to `.env`, applied immediately |
| Hugging Face token | — | Same |
| Language hint | Auto-detect | `auto`/`en`/`hi`/`es`/`fr`/`de`, or any ISO code |
| Live window | 20 s | 10-60 s. Shorter feels more live and costs more API calls |
| Identify speakers | On | Turn off to skip the slow step entirely |
| Maximum speakers | Auto | Set it when you know how many people were in the room; it makes diarization more accurate |
| Live suggestions | On | The **Ask next** column on the Record page. Off means no suggestion API calls at all |
| Refresh every | 45 s | 30-180 s. The shortest gap between two automatic refreshes; a refresh also waits for ~40 new words |

Keys are never returned by the API — only "Set" plus the last four characters.

## Smoke tests

All of them run against a backend that is already up, and none needs a
microphone. They call the real Groq and Hugging Face APIs.

```powershell
# Stage 1: streams a generated 5 second tone, checks the WAV and Range support
backend\.venv\Scripts\python backend\scripts\smoke_ws.py

# Stage 2: 25 seconds of synthesized speech at real-time pace; live + final
backend\.venv\Scripts\python backend\scripts\smoke_transcribe.py

# Stage 3: the diarization engine on its own, against known boundaries
backend\.venv\Scripts\python backend\scripts\diarize_smoke.py

# Live suggestions: streams a 70 s script full of loose ends, checks the
# suggestion frames on the socket, the refresh endpoint, follow_up_questions
# in the notes, the stored history and the pins. Takes about three minutes.
backend\.venv\Scripts\python backend\scripts\smoke_suggestions.py

# Stages 1-5 end to end: record, transcribe, notes, speakers, rename, search,
# export, regenerate, delete - and it starts a second recording mid-diarization
# to prove the app stays usable. Takes about two minutes.
backend\.venv\Scripts\python backend\scripts\smoke_full.py
```

Speech fixtures are built on first use from the Windows System.Speech voices
plus ffmpeg, and land in the git-ignored `backend/scripts/fixtures/`. Pass
`--rebuild` to regenerate them.

## Project structure

```
backend/app/       FastAPI app: routers, pipeline, Groq client, pyannote wrapper
backend/scripts/   Smoke tests that exercise the real APIs end to end
frontend/src/      React + Vite app: pages, components, hooks, design tokens
scripts/           start.cmd (double-click launcher) and dev.ps1
docs/              ARCHITECTURE.md and API.md
```

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how recording, live
  transcription, the processing pipeline and the data model fit together.
- [docs/API.md](docs/API.md): every REST endpoint and WebSocket frame.
- <http://localhost:8000/docs> while the app runs: interactive OpenAPI docs.

## What stays on your machine

Nothing sensitive is committed to this repository, and the app is built so it
stays that way:

- `.env` (your Groq key and Hugging Face token) is git-ignored. Only
  `.env.example` with empty values is tracked.
- `backend/data/` (recordings and the SQLite database) is git-ignored.
- Generated test audio under `backend/scripts/fixtures/` is git-ignored.
- The only network calls are to Groq (audio for transcription, transcript text
  for notes and suggestions) and, on first use, to Hugging Face to download the
  speaker models. Speaker identification itself runs locally.

## Troubleshooting

**"Groq key missing" in the sidebar.** Click the pill — it goes to Settings.
Paste the key there and it takes effect immediately. Recording works without
it; transcription and notes do not.

**The browser never asks for the microphone, or the dropdown is empty.**
Browsers only reveal device labels after permission is granted once. Start a
recording, allow it, then reopen the list. On Windows also check
Settings → Privacy → Microphone. Note that `http://localhost` counts as a
secure origin, so no certificate is needed.

**A Groq call fails.** Transient failures are retried three times with backoff.
A `model_not_found` for the notes model means your account cannot see it; the
backend automatically falls back to another chat model it *can* see (check the
log line "writing notes with ..."). A 401 means the key is wrong. Recording
never stops because of a Groq error — the transcript catches up at `/stop`, and
**Reprocess** re-runs everything from the stored WAV.

**Speaker identification fails with a model-access error.** The Hugging Face
token owner has to accept the terms for *both*
`pyannote/speaker-diarization-3.1` and `pyannote/segmentation-3.0`, while signed
in as that account. Accepting one is the usual mistake. The models download on
first use (a few hundred MB) and are cached afterwards, so the first meeting is
slower than the rest.

**Speaker identification is slow.** It is: roughly real time on a laptop CPU,
so a one hour meeting takes the best part of an hour. This is expected and the
reason the notes are written first. You can watch progress in the backend log,
keep using the app while it runs, or turn it off on the Settings page. Setting
**Maximum speakers** speeds up the clustering slightly and improves accuracy.

**A meeting is stuck on "Processing".** Pipeline tasks live in the event loop
and do not survive a backend restart; the next startup moves stranded meetings
to `ready` with a note, and **Reprocess** re-runs them.

**Stop fails with an ffmpeg error.** Check `ffmpeg -version`. If no audio was
captured at all, `raw.webm` is empty and the meeting is marked `failed`.

**Wrong speaker count.** Diarization decides on its own by default. If it splits
one person into two, or merges two into one, set **Maximum speakers** and press
**Reprocess**. Renaming speakers survives a reprocess.

**Port already in use.** The backend is fixed to 8000 and the dev frontend to
5173. Stop whatever else is using them.

**The production build shows an old UI.** `npm start` serves whatever is in
`frontend/dist`. Run `npm run build` again after changing the frontend —
`scripts\start.cmd` always does.
