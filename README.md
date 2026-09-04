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
- **Writes notes worth reading.** Not just a summary: chaptered **topics** with
  a timestamp range you can click to jump the audio, the decisions, the action
  items with owners, a **per-speaker breakdown** of what each person argued,
  committed to and asked, the questions raised in the room with who asked them
  and whether anyone answered, and the follow-ups you should chase.
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
3. Every 20 seconds the live transcript updates, filling the card below the
   recorder.
4. Press **Stop and process**. You land on the meeting page, which polls while
   the pipeline runs. The notes appear within roughly half a minute; a banner
   tells you speaker identification is still going.
5. When the speaker chips appear, click a pencil to rename someone. Every label
   on the page updates.
6. Read the notes column: **Summary**, **Topics** (click a range to jump the
   audio there), **Decisions**, **Action items** — tick them as you do them —
   **By speaker**, **Open questions** and **Follow-ups for you**. Open questions
   are what the people in the room asked, with who asked and whether it was
   answered; follow-ups are what *you* should still chase. Empty sections are
   hidden. **Export Markdown**, **Copy notes** and **Regenerate notes** are in
   the header. Space plays and pauses the audio; clicking any transcript line
   seeks there.
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
| 2 | `summarizing` | Notes, without speaker labels or the per-speaker breakdown | 2-10 s |
| 3 | `identifying_speakers` | pyannote diarization, locally, on CPU | **~0.5-1x realtime** |
| 4 | `summarizing` | Notes again, attributed, with **By speaker** filled in | 2-10 s |

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

## What the notes contain

The notes step sends the transcript to a Groq chat model in JSON mode and
stores the answer in `notes_json`:

| Field | What it holds |
| ----- | ------------- |
| `summary` | Three to five sentences on the whole meeting |
| `topics` | The chapters: `{title, start, end, summary}`, in time order and never overlapping. The detail page shows each as an `mm:ss-mm:ss` button that seeks the player |
| `key_takeaways` | Short facts worth remembering |
| `decisions` | What was actually decided |
| `action_items` | `{task, owner, owner_speaker_id, due, source_time, done}` |
| `open_questions` | Questions asked **in** the room: `{question, asked_by, asked_by_speaker_id, time, answered}` |
| `follow_up_questions` | What *you* should ask or chase afterwards |
| `by_speaker` | Per person: `main_points`, `commitments`, `questions_raised`. Empty until speakers are known |

**Two passes.** The first runs before diarization and fills everything except
`by_speaker` and the speaker attributions; the second runs once speakers exist
and fills those in. Until it lands, the **By speaker** section shows
"Speaker breakdown arrives once speakers are identified".

**Long meetings are map-reduced.** Past `NOTES_SINGLE_PASS_CHARS` the
transcript is cut into `NOTES_CHUNK_CHARS` pieces, each answered in a
deliberately compact shape (fewer items, shorter fields), and one merge pass
combines and dedupes them. The compact chunk shape is what keeps the richer
schema inside `NOTES_MAX_OUTPUT_TOKENS`, which is sized for Groq's free tier
and deliberately not raised. Topic ranges are sorted and de-overlapped in code
afterwards, so the chapter list is always usable even when the merge is sloppy.

**Renames propagate.** `owner`, `asked_by` and each `by_speaker` name are
resolved from their stored `S`-id through `speaker_names_json` at read time, so
renaming S1 relabels the action items she owns, the questions she asked and her
own block — in the UI, in **Copy notes** and in the Markdown export.

Notes written by an earlier build still render: `open_questions` stored as
plain strings are read back as objects with empty attribution, and `topics` and
`by_speaker` come back empty and their sections stay hidden.

## Where your data lives

`backend/data` is the default; set `MRLISTENER_DATA_DIR` to point the database
and the audio somewhere else, which is how a smoke run stays away from your real
library.

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
| `notes_json` | the notes object above, including per-item `done` state |
| `suggestions_json` | a dead column from the removed live-suggestions feature; nothing reads or writes it, and it is kept only so an existing database opens without a migration |
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

# Stages 1-5 end to end: record, transcribe, notes (topics, open questions and
# the per-speaker breakdown included), speakers, rename, search, export,
# regenerate, delete - and it starts a second recording mid-diarization to prove
# the app stays usable. Takes about two minutes.
backend\.venv\Scripts\python backend\scripts\smoke_full.py
```

Point a smoke run at a scratch database rather than your own library:

```powershell
$env:MRLISTENER_DATA_DIR = "$env:TEMP\mrlistener-smoke"
backend\.venv\Scripts\python -m uvicorn app.main:app --port 8001   # from backend\
backend\.venv\Scripts\python backend\scripts\smoke_full.py --base-url http://localhost:8001
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
  for the notes) and, on first use, to Hugging Face to download the speaker
  models. Speaker identification itself runs locally.

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
