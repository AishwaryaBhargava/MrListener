"""End-to-end smoke test for the Stage 1 recording pipeline (no browser needed).

It creates a meeting, streams a real webm/opus file over the WebSocket in
~1 second chunks the way MediaRecorder does, finalizes through POST /stop and
asserts the resulting wav, its duration, and Range support on GET /audio.

Transcription is covered separately by ``smoke_transcribe.py`` - a sine tone
has nothing to transcribe. This test only waits for the Stage 2 pipeline to
finish so the status settles on "ready".

Run the backend first, then:

    backend\\.venv\\Scripts\\python backend\\scripts\\smoke_ws.py

Options:
    --base-url   http://localhost:8000 by default
    --keep       leave the created meeting in the database
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import websockets

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app import config  # noqa: E402  (needs the path tweak above)

TONE_SECONDS = 5
CHUNK_SECONDS = 1.0
DURATION_TOLERANCE = 0.35
# /stop now hands off to the Stage 2 pipeline, so "ready" arrives a moment later.
PIPELINE_TIMEOUT = 120.0


class SmokeFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)
    print(f"  ok  {message}")


def http(method: str, url: str, payload: dict | None = None, headers: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def json_body(raw: bytes) -> dict:
    return json.loads(raw.decode())


def make_test_webm(target: Path) -> None:
    """A 5 second 440 Hz tone in a webm/opus container, like the browser sends."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={TONE_SECONDS}",
        "-c:a", "libopus",
        str(target),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise SmokeFailure(f"ffmpeg could not build the test tone: {proc.stderr.decode(errors='replace')}")


async def stream_chunks(ws_url: str, source: Path) -> int:
    payload = source.read_bytes()
    # Split into TONE_SECONDS slices so the server sees a realistic frame cadence.
    chunk_size = max(1, len(payload) // TONE_SECONDS)
    sent = 0
    async with websockets.connect(ws_url, max_size=None) as socket:
        for offset in range(0, len(payload), chunk_size):
            chunk = payload[offset : offset + chunk_size]
            await socket.send(chunk)
            sent += len(chunk)
            await asyncio.sleep(CHUNK_SECONDS / TONE_SECONDS)
        await socket.send(json.dumps({"type": "stop"}))
        try:
            await asyncio.wait_for(socket.recv(), timeout=5)
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
            pass
    return sent


def wait_for_ready(base: str, meeting_id: str) -> dict:
    """/stop returns while the pipeline is still running; wait it out."""
    deadline = time.monotonic() + PIPELINE_TIMEOUT
    meeting: dict = {}
    while time.monotonic() < deadline:
        status, _, raw = http("GET", f"{base}/api/meetings/{meeting_id}")
        if status != 200:
            raise SmokeFailure(f"GET /api/meetings/{{id}} -> {status}")
        meeting = json_body(raw)
        if meeting["status"] in ("ready", "failed"):
            return meeting
        time.sleep(1.5)
    raise SmokeFailure(f"the pipeline did not finish within {PIPELINE_TIMEOUT:.0f}s")


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    meeting_id: str | None = None

    print(f"MrListener smoke test against {base}")

    try:
        status, _, raw = http("GET", f"{base}/api/health")
        check(status == 200, f"GET /api/health -> {status}")
        health = json_body(raw)
        check(health.get("ffmpeg") is True, "health reports ffmpeg available")

        status, _, raw = http("POST", f"{base}/api/meetings", {"title": "Smoke test meeting"})
        check(status == 201, f"POST /api/meetings -> {status}")
        meeting = json_body(raw)
        meeting_id = meeting["id"]
        check(meeting["status"] == "recording", "new meeting has status 'recording'")

        with tempfile.TemporaryDirectory() as tmp:
            tone = Path(tmp) / "test.webm"
            make_test_webm(tone)
            check(tone.stat().st_size > 0, f"built a {TONE_SECONDS}s webm/opus test tone")

            sent = await stream_chunks(f"{ws_base}/ws/record/{meeting_id}", tone)
            check(sent == tone.stat().st_size, f"streamed {sent} bytes over the websocket")

        raw_file = config.raw_path(meeting_id)
        check(raw_file.exists() and raw_file.stat().st_size == sent, "raw.webm holds every streamed byte")

        status, _, raw = http("POST", f"{base}/api/meetings/{meeting_id}/stop")
        check(status == 200, f"POST /stop -> {status}")
        stopped = json_body(raw)
        check(stopped["status"] == "processing", "meeting status is 'processing' after /stop")
        stopped = wait_for_ready(base, meeting_id)
        check(stopped["status"] == "ready", "the pipeline drove the meeting to 'ready'")

        wav_file = config.wav_path(meeting_id)
        check(wav_file.exists() and wav_file.stat().st_size > 0, "audio.wav was written")

        with wave.open(str(wav_file), "rb") as handle:
            check(handle.getframerate() == 16000, "wav sample rate is 16 kHz")
            check(handle.getnchannels() == 1, "wav is mono")
            check(handle.getsampwidth() == 2, "wav is 16-bit pcm")

        reported = stopped["duration_seconds"]
        actual = wav_duration(wav_file)
        check(
            abs(actual - TONE_SECONDS) < DURATION_TOLERANCE,
            f"wav duration {actual:.2f}s is within {DURATION_TOLERANCE}s of {TONE_SECONDS}s",
        )
        check(
            reported is not None and abs(reported - TONE_SECONDS) < DURATION_TOLERANCE,
            f"stored duration_seconds {reported} matches the tone length",
        )

        # /stop must be safe to call twice (double click, retry after a timeout).
        status, _, raw = http("POST", f"{base}/api/meetings/{meeting_id}/stop")
        check(status == 200 and json_body(raw)["status"] == "ready", "POST /stop is idempotent")

        status, headers, body = http("GET", f"{base}/api/meetings/{meeting_id}/audio")
        check(status == 200, f"GET /audio -> {status}")
        check(headers.get("Accept-Ranges") == "bytes", "GET /audio advertises Accept-Ranges: bytes")
        full_size = len(body)

        status, headers, body = http(
            "GET", f"{base}/api/meetings/{meeting_id}/audio", headers={"Range": "bytes=0-1023"}
        )
        check(status == 206, f"ranged GET /audio -> {status} (expected 206)")
        check(len(body) == 1024, "ranged response returned exactly 1024 bytes")
        check(
            headers.get("Content-Range") == f"bytes 0-1023/{full_size}",
            f"Content-Range header is correct ({headers.get('Content-Range')})",
        )

        status, _, body = http(
            "GET", f"{base}/api/meetings/{meeting_id}/audio", headers={"Range": f"bytes={full_size - 100}-"}
        )
        check(status == 206 and len(body) == 100, "open-ended range request returns the tail")

        status, _, raw = http("GET", f"{base}/api/meetings")
        check(status == 200, f"GET /api/meetings -> {status}")
        check(
            any(row["id"] == meeting_id for row in json_body(raw)),
            "the meeting appears in the list",
        )

        if not args.keep:
            status, _, _ = http("DELETE", f"{base}/api/meetings/{meeting_id}")
            check(status == 204, f"DELETE /api/meetings/{{id}} -> {status}")
            check(not config.meeting_dir(meeting_id).exists(), "the audio folder was removed")
            status, _, _ = http("GET", f"{base}/api/meetings/{meeting_id}")
            check(status == 404, "the deleted meeting is gone")
            meeting_id = None

    except SmokeFailure as exc:
        print(f"\nFAILED: {exc}")
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"\nFAILED: could not reach {base} - is the backend running? ({exc})")
        return 1

    print("\nAll smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
