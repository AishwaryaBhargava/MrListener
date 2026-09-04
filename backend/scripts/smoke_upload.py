"""End-to-end smoke test for uploading a recording instead of making one.

Runs against a backend that is already up, and against the real Groq API. No
microphone and no WebSocket: a short speech fixture is synthesized with the
Windows TTS voice, encoded to mp3, and POSTed to ``/api/meetings/upload`` as
ordinary ``multipart/form-data`` - exactly what the browser's FormData sends.

    backend\\.venv\\Scripts\\python backend\\scripts\\smoke_upload.py --base-url http://localhost:8001

What it proves
--------------
1. A ``.txt`` file is refused with 415 and a message naming the supported
   formats, and no meeting row is left behind.
2. A valid mp3 returns 201 with ``status: processing``, ``source: "upload"``,
   ``pipeline_stage: "converting"`` and the sequential ``Recording NN`` name.
3. The meeting passes through the ``converting`` stage on its way to ready.
4. ``duration_seconds`` matches the fixture, so the conversion decoded the
   whole file rather than a prefix of it.
5. The transcript is non-empty and contains words the fixture actually says.
6. Notes were written - or, when the daily Groq budget is gone, the meeting
   still lands on ``ready`` with a ``pipeline_error`` that says so. Either is a
   pass; the script prints which one happened.
7. ``upload.mp3`` is kept beside ``audio.wav``, and ``GET /audio`` serves the
   wav with a 206 for a ``Range`` request.
8. ``DELETE`` removes the row and the whole folder, original included.

Options
-------
    --base-url    http://localhost:8001 by default - NOT the production port
    --keep        leave the created meeting in the database
    --rebuild     regenerate the speech fixture first
    --timeout     seconds to wait for the pipeline (default 600)

Point the backend under test at a scratch library with ``MRLISTENER_DATA_DIR``
before starting it, and this script never touches the real database or audio.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# The Windows console is cp1252 and a transcript can carry typographic
# punctuation; printing it must never fail an otherwise passing run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):  # pragma: no cover - non-standard stdout
    pass

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from app import config  # noqa: E402  (needs the path tweak above)
from make_fixtures import FixtureError, make_speech_wav  # noqa: E402

FIXTURE_DIR = SCRIPT_DIR / "fixtures"
UPLOAD_WAV = FIXTURE_DIR / "upload_speech.wav"
UPLOAD_MP3 = FIXTURE_DIR / "upload_speech.mp3"

#: Meeting-shaped, and deliberately under 40 seconds: Groq's free Whisper tier
#: has a daily audio budget and this test must cost as little of it as possible.
#: No apostrophes: the Windows TTS helper passes this through a single-quoted
#: PowerShell string, where one would end the argument early.
SCRIPT = (
    "Right, the recording from the planning call is uploaded now. "
    "The two decisions were to move the migration to the second week of "
    "October, and to keep the old dashboard running until then. "
    "Sanjay is writing the rollback plan and I will book the review."
)
#: Slow enough to be clearly articulated, fast enough to stay short.
SPEECH_RATE = -2
#: Exact length of the encoded fixture; every duration assertion works off this.
TARGET_SECONDS = 30.0
#: Tolerance on the duration the backend reports. mp3 frames pad the tail a
#: little, so this is generous on the high side and tight on the low.
DURATION_LOW = 0.9
DURATION_HIGH = 1.15
#: Whisper has to return at least one of these. Several, because a single
#: mis-heard word should not fail a run that is otherwise correct.
EXPECTED_WORDS = ("migration", "dashboard", "rollback", "october", "planning")
#: How a rate-limited pipeline_error reads, so a quota failure is reported as
#: the budget running out rather than as a broken feature.
RATE_LIMIT_HINTS = ("rate limit", "rate_limit", "429", "quota", "too many requests", "tokens per")

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class SmokeFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)
    print(f"  ok  {message}")


def note(message: str) -> None:
    print(f"  ..  {message}")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def http(method: str, url: str, payload: dict | None = None, headers: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read(), _headers(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), _headers(exc.headers)


def _headers(message) -> dict[str, str]:
    """Response headers keyed in lower case - uvicorn sends them that way."""
    return {key.lower(): value for key, value in message.items()}


def body(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))


def multipart(fields: dict[str, str], filename: str, content: bytes) -> tuple[bytes, str]:
    """Encode one file part plus text parts, the way a browser's FormData does."""
    boundary = f"----MrListenerSmoke{uuid.uuid4().hex}"
    line_end = b"\r\n"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks += [
            f"--{boundary}".encode(),
            line_end,
            f'Content-Disposition: form-data; name="{name}"'.encode(),
            line_end,
            line_end,
            value.encode("utf-8"),
            line_end,
        ]
    chunks += [
        f"--{boundary}".encode(),
        line_end,
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode(),
        line_end,
        b"Content-Type: application/octet-stream",
        line_end,
        line_end,
        content,
        line_end,
        f"--{boundary}--".encode(),
        line_end,
    ]
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def post_upload(base: str, filename: str, content: bytes, title: str | None = None):
    fields = {"title": title} if title else {}
    payload, content_type = multipart(fields, filename, content)
    request = urllib.request.Request(
        f"{base}/api/meetings/upload", data=payload, method="POST"
    )
    request.add_header("Content-Type", content_type)
    request.add_header("Content-Length", str(len(payload)))
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# --------------------------------------------------------------------------
# Fixture
# --------------------------------------------------------------------------


def ensure_mp3(rebuild: bool) -> tuple[Path, float]:
    """A <=40 s spoken mp3. Built once with Windows TTS, then cached."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    if rebuild or not UPLOAD_WAV.exists() or UPLOAD_WAV.stat().st_size == 0:
        note("synthesizing the speech fixture with the Windows TTS voice")
        make_speech_wav(UPLOAD_WAV, SCRIPT, SPEECH_RATE)

    if rebuild or not UPLOAD_MP3.exists() or UPLOAD_MP3.stat().st_mtime < UPLOAD_WAV.stat().st_mtime:
        note(f"encoding {UPLOAD_MP3.name} at exactly {TARGET_SECONDS:.0f}s")
        proc = subprocess.run(
            [
                config.ffmpeg_bin(),
                "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(UPLOAD_WAV),
                # apad + -t pins the length whatever pace the voice reads at.
                "-af", "apad",
                "-t", f"{TARGET_SECONDS}",
                "-ac", "1",
                "-ar", "44100",
                "-c:a", "libmp3lame",
                "-b:a", "64k",
                str(UPLOAD_MP3),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace")[-800:]
            raise SmokeFailure(f"could not encode the mp3 fixture: {detail}")

    return UPLOAD_MP3, TARGET_SECONDS


# --------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------


def wait_for_ready(base: str, meeting_id: str, timeout: float) -> tuple[dict, list[str]]:
    """Poll the detail endpoint the way the UI does, collecting every stage."""
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        status, raw, _ = http("GET", f"{base}/api/meetings/{meeting_id}")
        if status != 200:
            raise SmokeFailure(f"GET /api/meetings/{{id}} -> {status}")
        meeting = body(raw)
        stage = meeting.get("pipeline_stage")
        label = stage or "done"
        if not seen or seen[-1] != label:
            seen.append(label)
            note(f"stage: {label}")
        if meeting["status"] in ("ready", "failed") and stage is None:
            return meeting, seen
        time.sleep(1.0)
    raise SmokeFailure(f"the pipeline did not finish within {timeout:.0f}s")


def transcript_text(base: str, meeting_id: str) -> str:
    status, raw, _ = http("GET", f"{base}/api/meetings/{meeting_id}/transcript")
    if status != 200:
        raise SmokeFailure(f"GET /transcript -> {status}")
    payload = body(raw)
    return " ".join(str(seg.get("text") or "") for seg in payload.get("segments") or [])


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8001")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    meeting_id: str | None = None
    started_at = time.monotonic()
    print(f"MrListener upload smoke test against {base}")

    try:
        # -- environment ---------------------------------------------------
        status, raw, _ = http("GET", f"{base}/api/health")
        check(status == 200, f"GET /api/health -> {status}")
        health = body(raw)
        check(health.get("ffmpeg") is True, "health reports ffmpeg available")
        check(health.get("groq_key_set") is True, "health reports GROQ_API_KEY is set")
        note(f"data dir: {health.get('data_dir')}")

        # -- a file the backend must refuse --------------------------------
        before = len(body(http("GET", f"{base}/api/meetings")[1]))
        status, raw = post_upload(base, "notes.txt", b"this is not audio at all")
        check(status == 415, f"POST /upload with a .txt -> {status}")
        detail = body(raw).get("detail") or ""
        check("mp3" in detail and ".txt" in detail, f"the 415 names the formats ({detail[:90]}...)")
        after = len(body(http("GET", f"{base}/api/meetings")[1]))
        check(after == before, "the rejected upload left no meeting behind")

        # -- the real thing --------------------------------------------------
        fixture, duration = ensure_mp3(args.rebuild)
        content = fixture.read_bytes()
        check(
            fixture.stat().st_size > 0 and duration <= 40,
            f"mp3 fixture ready ({duration:.0f}s, {len(content)} bytes)",
        )

        status, raw = post_upload(base, fixture.name, content)
        check(status == 201, f"POST /api/meetings/upload -> {status}")
        created = body(raw)
        meeting_id = created["id"]
        check(created["status"] == "processing", "the new meeting is 'processing', not 'recording'")
        check(created["source"] == "upload", "the new meeting reports source 'upload'")
        check(
            created["source_filename"] == fixture.name,
            f"source_filename is the uploaded name ({created['source_filename']!r})",
        )
        check(
            created["pipeline_stage"] == "converting",
            f"the response already says stage 'converting' ({created['pipeline_stage']!r})",
        )
        check(
            re.fullmatch(r"Recording \d{2,}", created["title"]) is not None,
            f"the backend assigned a sequential name ({created['title']!r})",
        )

        folder = config.meeting_dir(meeting_id)
        check((folder / "upload.mp3").exists(), "the original was written to upload.mp3")

        # -- the pipeline ----------------------------------------------------
        meeting, stages = wait_for_ready(base, meeting_id, args.timeout)
        check("converting" in stages, f"the run passed through 'converting' ({' -> '.join(stages)})")
        check(meeting["status"] == "ready", f"the meeting landed on 'ready' ({meeting['status']})")
        check(meeting["source"] == "upload", "it is still an upload after processing")

        reported = meeting["duration_seconds"] or 0.0
        check(
            duration * DURATION_LOW <= reported <= duration * DURATION_HIGH,
            f"duration_seconds is the fixture length ({reported:.1f}s vs {duration:.0f}s)",
        )
        check(meeting["has_audio"] is True, "audio.wav exists for the uploaded meeting")
        check(config.wav_path(meeting_id).exists(), "audio.wav is on disk")
        check(config.stored_upload(meeting_id) is not None, "the original upload was kept")

        # -- the transcript ----------------------------------------------------
        text = transcript_text(base, meeting_id).lower()
        check(bool(text.strip()), f"the transcript is not empty ({len(text)} characters)")
        hits = [word for word in EXPECTED_WORDS if word in text]
        check(bool(hits), f"the transcript carries words from the script ({hits})")
        note(f"transcript: {text.strip()[:180]}")

        # -- the notes, budget permitting --------------------------------------
        error = meeting.get("pipeline_error") or ""
        rate_limited = any(hint in error.lower() for hint in RATE_LIMIT_HINTS)
        if meeting["has_notes"]:
            check(True, "the notes model wrote a full set of notes")
            summary = str((meeting.get("notes") or {}).get("summary") or "")
            check(bool(summary.strip()), f"the notes carry a summary ({summary[:80]!r})")
        else:
            check(
                rate_limited,
                "no notes, and pipeline_error says the Groq budget is spent "
                f"({error[:140]!r})",
            )
            note("NOTES WERE NOT EXERCISED: the Groq daily budget is exhausted")

        # -- playback ------------------------------------------------------------
        status, payload, headers = http(
            "GET", f"{base}/api/meetings/{meeting_id}/audio", headers={"Range": "bytes=0-1023"}
        )
        check(status == 206, f"GET /audio with a Range header -> {status}")
        check(len(payload) == 1024, f"the partial response is 1024 bytes ({len(payload)})")
        content_range = headers.get("content-range", "")
        check(
            content_range.startswith("bytes 0-1023/"),
            f"Content-Range is set ({content_range!r})",
        )
        check(headers.get("accept-ranges") == "bytes", "the response advertises byte ranges")

        # -- the library ---------------------------------------------------------
        rows = body(http("GET", f"{base}/api/meetings")[1])
        row = next((entry for entry in rows if entry["id"] == meeting_id), None)
        check(row is not None, "the uploaded meeting shows up in the list")
        check(row["source"] == "upload", "the list row carries source 'upload'")
        check(
            row["source_filename"] == fixture.name,
            "the list row carries the filename the meta line renders",
        )

        # -- delete ---------------------------------------------------------------
        if not args.keep:
            status, _, _ = http("DELETE", f"{base}/api/meetings/{meeting_id}")
            check(status == 204, f"DELETE /api/meetings/{{id}} -> {status}")
            check(not folder.exists(), "the folder went with it, original upload included")
            status, _, _ = http("GET", f"{base}/api/meetings/{meeting_id}")
            check(status == 404, "the deleted meeting is gone")
            meeting_id = None

    except SmokeFailure as exc:
        print(f"\nFAILED: {exc}")
        return 1
    except FixtureError as exc:
        print(f"\nFAILED: could not build the fixture: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAILED: unexpected error: {type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
        return 1
    finally:
        if meeting_id and not args.keep:
            http("DELETE", f"{base}/api/meetings/{meeting_id}")

    print(f"\nAll checks passed in {time.monotonic() - started_at:.0f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
