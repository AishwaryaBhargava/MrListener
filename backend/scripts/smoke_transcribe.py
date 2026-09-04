"""End-to-end smoke test for the Stage 2 transcription pipeline.

Runs against the real Groq API - GROQ_API_KEY must be set. It streams 25
seconds of synthesized speech over the recording WebSocket at real-time pace,
so at least one live window closes mid-recording, then finalizes and waits for
the background pipeline.

What it proves:

* a new meeting with no title is named "Recording NN" by the backend
* a ``transcript`` frame arrives on the socket while still recording, and the
  live text contains "onboarding"
* POST /stop leaves the meeting ``processing`` and the pipeline drives it to
  ``ready``
* the stored transcript ends up with ``source: final`` and contains "marketing"

Run the backend first, then:

    backend\\.venv\\Scripts\\python backend\\scripts\\smoke_transcribe.py

Options:
    --base-url     http://localhost:8000 by default
    --keep         leave the created meeting in the database
    --rebuild      regenerate the speech fixtures before running
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets

# The Windows console is cp1252 and the notes model reaches for typographic
# dashes and thin spaces; printing them must never fail a passing run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):  # pragma: no cover - non-standard stdout
    pass

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from app import config  # noqa: E402  (needs the path tweak above)
from make_fixtures import FixtureError, TARGET_SECONDS, ensure_speech_webm  # noqa: E402

#: One send per second, matching the browser's MediaRecorder timeslice.
CHUNK_SECONDS = 1.0
LIVE_WORD = "onboarding"
FINAL_WORD = "marketing"
#: Two Groq round trips (tail + full file) plus ffmpeg.
PIPELINE_TIMEOUT = 180.0


class SmokeFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)
    print(f"  ok  {message}")


def http(method: str, url: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def json_body(raw: bytes) -> dict:
    return json.loads(raw.decode())


def frame_text(frames: list[dict]) -> str:
    """Every word the server pushed over the socket, lower-cased."""
    words: list[str] = []
    for frame in frames:
        if frame.get("type") != "transcript":
            continue
        for segment in frame.get("segments") or []:
            words.append(str(segment.get("text") or ""))
    return " ".join(words).lower()


async def stream_realtime(ws_url: str, source: Path) -> tuple[int, list[dict]]:
    """Send the fixture in ~1 s slices, collecting every JSON frame sent back."""
    payload = source.read_bytes()
    slices = max(1, int(round(TARGET_SECONDS)))
    chunk_size = max(1, -(-len(payload) // slices))  # ceil division
    frames: list[dict] = []
    sent = 0

    async with websockets.connect(ws_url, max_size=None) as socket:

        async def receive() -> None:
            try:
                async for message in socket:
                    if isinstance(message, str):
                        try:
                            frames.append(json.loads(message))
                        except json.JSONDecodeError:
                            pass
            except websockets.exceptions.ConnectionClosed:
                pass

        reader = asyncio.create_task(receive())
        try:
            for offset in range(0, len(payload), chunk_size):
                chunk = payload[offset : offset + chunk_size]
                await socket.send(chunk)
                sent += len(chunk)
                # Real-time pace: this is what lets a 20 s live window close.
                await asyncio.sleep(CHUNK_SECONDS)
            await socket.send(json.dumps({"type": "stop"}))
            await asyncio.sleep(0.5)
        finally:
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass

    return sent, frames


def wait_for_ready(base: str, meeting_id: str) -> dict:
    """Poll the detail endpoint the way the UI does, until the pipeline lands."""
    deadline = time.monotonic() + PIPELINE_TIMEOUT
    seen_stages: list[str] = []
    while time.monotonic() < deadline:
        status, raw = http("GET", f"{base}/api/meetings/{meeting_id}")
        if status != 200:
            raise SmokeFailure(f"GET /api/meetings/{{id}} -> {status}")
        meeting = json_body(raw)
        stage = meeting.get("pipeline_stage")
        if stage and stage not in seen_stages:
            seen_stages.append(stage)
        if meeting["status"] in ("ready", "failed"):
            if seen_stages:
                print(f"  ..  pipeline stages observed: {', '.join(seen_stages)}")
            return meeting
        time.sleep(2.0)
    raise SmokeFailure(f"the pipeline did not finish within {PIPELINE_TIMEOUT:.0f}s")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    meeting_id: str | None = None

    print(f"MrListener Stage 2 smoke test against {base}")

    try:
        status, raw = http("GET", f"{base}/api/health")
        check(status == 200, f"GET /api/health -> {status}")
        health = json_body(raw)
        check(health.get("ffmpeg") is True, "health reports ffmpeg available")
        check(health.get("groq_key_set") is True, "health reports GROQ_API_KEY is set")

        fixture = ensure_speech_webm(rebuild=args.rebuild)
        check(fixture.stat().st_size > 0, f"speech fixture ready ({fixture.name})")

        # No title in the body: the backend must name it.
        status, raw = http("POST", f"{base}/api/meetings", {"title": None})
        check(status == 201, f"POST /api/meetings -> {status}")
        meeting = json_body(raw)
        meeting_id = meeting["id"]
        check(meeting["status"] == "recording", "new meeting has status 'recording'")
        import re

        check(
            re.fullmatch(r"Recording \d{2,}", meeting["title"]) is not None,
            f"backend assigned a sequential name ({meeting['title']!r})",
        )

        print(f"  ..  streaming {TARGET_SECONDS:.0f}s of speech at real-time pace")
        sent, frames = await stream_realtime(f"{ws_base}/ws/record/{meeting_id}", fixture)
        check(sent == fixture.stat().st_size, f"streamed {sent} bytes over the websocket")

        errors = [f["message"] for f in frames if f.get("type") == "error"]
        check(not errors, f"no error frames on the socket ({errors})")

        transcript_frames = [f for f in frames if f.get("type") == "transcript"]
        check(len(transcript_frames) >= 1, f"a live transcript frame arrived ({len(transcript_frames)})")

        live_text = frame_text(frames)
        print(f"  ..  live text: {live_text[:220]}")
        check(LIVE_WORD in live_text, f"the live transcript contains {LIVE_WORD!r}")

        status, raw = http("POST", f"{base}/api/meetings/{meeting_id}/stop")
        check(status == 200, f"POST /stop -> {status}")
        stopped = json_body(raw)
        check(
            stopped["status"] == "processing",
            f"meeting is 'processing' after /stop (got {stopped['status']!r})",
        )

        ready = wait_for_ready(base, meeting_id)
        check(ready["status"] == "ready", f"the pipeline finished with status {ready['status']!r}")
        check(ready.get("pipeline_stage") is None, "pipeline_stage is cleared when done")
        check(ready.get("pipeline_error") is None, f"no pipeline error ({ready.get('pipeline_error')})")

        status, raw = http("GET", f"{base}/api/meetings/{meeting_id}/transcript")
        check(status == 200, f"GET /transcript -> {status}")
        transcript = json_body(raw)
        check(transcript["source"] == "final", "the stored transcript came from the full-file pass")
        check(len(transcript["segments"]) > 0, f"{len(transcript['segments'])} segments were stored")

        ids = [segment["id"] for segment in transcript["segments"]]
        check(ids == list(range(len(ids))), "segment ids are contiguous from 0")
        check(
            all(s["end"] >= s["start"] for s in transcript["segments"]),
            "every segment ends at or after it starts",
        )

        final_text = " ".join(s["text"] for s in transcript["segments"])
        print("\n  final transcript:")
        for segment in transcript["segments"]:
            print(f"    [{segment['start']:6.2f} - {segment['end']:6.2f}]  {segment['text']}")
        print()
        check(FINAL_WORD in final_text.lower(), f"the final transcript contains {FINAL_WORD!r}")

        check(
            (ready.get("transcript_preview") or "").strip() != "",
            f"the list preview is populated ({(ready.get('transcript_preview') or '')[:60]!r})",
        )

        # Reprocess must re-run the pipeline from the wav alone.
        status, raw = http("POST", f"{base}/api/meetings/{meeting_id}/reprocess")
        check(status == 200, f"POST /reprocess -> {status}")
        check(json_body(raw)["status"] == "processing", "reprocess puts the meeting back to 'processing'")
        again = wait_for_ready(base, meeting_id)
        check(again["status"] == "ready", "reprocess drove the meeting back to 'ready'")

        if not args.keep:
            status, _ = http("DELETE", f"{base}/api/meetings/{meeting_id}")
            check(status == 204, f"DELETE /api/meetings/{{id}} -> {status}")
            check(not config.meeting_dir(meeting_id).exists(), "the audio folder was removed")
            meeting_id = None

    except SmokeFailure as exc:
        print(f"\nFAILED: {exc}")
        return 1
    except FixtureError as exc:
        print(f"\nFAILED: could not build the speech fixture ({exc})")
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"\nFAILED: could not reach {base} - is the backend running? ({exc})")
        return 1

    print("All Stage 2 smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
