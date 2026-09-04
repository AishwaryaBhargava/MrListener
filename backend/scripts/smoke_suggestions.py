"""End-to-end smoke test for the live follow-up suggestions.

Runs against the real Groq API - GROQ_API_KEY must be set. It streams ~70
seconds of synthesized speech over the recording WebSocket at real-time pace.
The script is deliberately full of loose ends (a support contract that expires
with nobody named as the owner of the renewal, a deadline with no date, an
error spike nobody looked into), so a listener paying attention has obvious
questions - which is exactly what the model is asked to find.

What it proves
--------------
1. A ``suggestions`` frame arrives on the socket *before* /stop, carrying 3-5
   items that each have non-empty text, kind and why.
2. POST /suggestions/refresh returns a batch on demand while recording, and the
   round trip is timed.
3. After /stop the notes carry a non-empty ``follow_up_questions`` list.
4. GET /suggestions returns at least one persisted batch.
5. PATCH /suggestions/pins stores pins and GET reads them back.

Run the backend first, then:

    backend\\.venv\\Scripts\\python backend\\scripts\\smoke_suggestions.py

Options:
    --base-url     http://localhost:8000 by default
    --keep         leave the created meeting in the database
    --rebuild      regenerate the speech fixture before running
    --timeout      seconds to wait for the pipeline (default 600)
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

# The Windows console is cp1252; the model happily returns typographic dashes.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):  # pragma: no cover - non-standard stdout
    pass

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from app import config  # noqa: E402  (needs the path tweak above)
from make_fixtures import (  # noqa: E402
    FixtureError,
    SUGGESTIONS_SECONDS,
    ensure_suggestions_webm,
)

#: One send per second, matching the browser's MediaRecorder timeslice.
CHUNK_SECONDS = 1.0
#: When to hit the refresh endpoint, in seconds from the start of the stream.
#: Late enough that a first automatic batch has usually already landed.
FORCE_REFRESH_AT = 48.0
KINDS = ("question", "clarify", "follow_up", "risk")
MIN_ITEMS = 3
MAX_ITEMS = 5


class SmokeFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)
    print(f"  ok  {message}")


def note(message: str) -> None:
    print(f"  ..  {message}")


def http(method: str, url: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def body(raw: bytes) -> dict:
    return json.loads(raw.decode())


def show_batch(batch: dict, heading: str) -> None:
    print(f"\n  {heading}")
    stamp = batch.get("generated_at") or "?"
    end = batch.get("transcript_end")
    print(f"    generated_at {stamp} | transcript_end {end}")
    for item in batch.get("items") or []:
        at = item.get("based_on_time")
        at_text = f"{float(at):.1f}s" if at is not None else "--"
        print(f"    [{item.get('kind'):<9}] {item.get('text')}")
        print(f"                why: {item.get('why')}  (at {at_text})")
    print()


def validate_batch(batch: dict, where: str) -> None:
    items = batch.get("items") or []
    check(
        MIN_ITEMS <= len(items) <= MAX_ITEMS,
        f"{where}: the batch has {len(items)} item(s), expected {MIN_ITEMS}-{MAX_ITEMS}",
    )
    for index, item in enumerate(items):
        check(
            bool(str(item.get("text") or "").strip()),
            f"{where}: item {index} has text",
        )
        check(
            item.get("kind") in KINDS,
            f"{where}: item {index} has a known kind ({item.get('kind')!r})",
        )
        check(
            bool(str(item.get("why") or "").strip()),
            f"{where}: item {index} explains itself",
        )


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------


async def stream_and_refresh(
    ws_url: str, source: Path, base: str, meeting_id: str
) -> tuple[list[dict], dict | None, float]:
    """Stream the fixture in ~1 s slices, forcing one refresh partway through.

    Returns every JSON frame the server pushed, the batch the refresh endpoint
    answered with, and how long that call took.
    """
    payload = source.read_bytes()
    slices = max(1, int(round(SUGGESTIONS_SECONDS)))
    chunk_size = max(1, -(-len(payload) // slices))  # ceil division
    frames: list[dict] = []
    forced: dict | None = None
    forced_seconds = 0.0

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

        async def force_refresh() -> None:
            nonlocal forced, forced_seconds
            await asyncio.sleep(FORCE_REFRESH_AT)
            note("calling POST /suggestions/refresh mid-recording")
            started = time.perf_counter()
            status, raw = await asyncio.to_thread(
                http, "POST", f"{base}/api/meetings/{meeting_id}/suggestions/refresh"
            )
            forced_seconds = time.perf_counter() - started
            if status != 200:
                raise SmokeFailure(f"POST /suggestions/refresh -> {status} {raw[:200]!r}")
            forced = body(raw)

        reader = asyncio.create_task(receive())
        refresher = asyncio.create_task(force_refresh())
        try:
            for offset in range(0, len(payload), chunk_size):
                await socket.send(payload[offset : offset + chunk_size])
                # Real-time pace: this is what lets a live window close and a
                # suggestion refresh become due while the meeting is running.
                await asyncio.sleep(CHUNK_SECONDS)
            await refresher
            await socket.send(json.dumps({"type": "stop"}))
            await asyncio.sleep(0.5)
        finally:
            for task in (reader, refresher):
                task.cancel()
            for task in (reader, refresher):
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    return frames, forced, forced_seconds


def wait_for_ready(base: str, meeting_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        status, raw = http("GET", f"{base}/api/meetings/{meeting_id}")
        if status != 200:
            raise SmokeFailure(f"GET /api/meetings/{{id}} -> {status}")
        meeting = body(raw)
        stage = meeting.get("pipeline_stage")
        if stage and stage not in seen:
            seen.append(stage)
        if meeting["status"] in ("ready", "failed"):
            if seen:
                note(f"pipeline stages observed: {', '.join(seen)}")
            return meeting
        time.sleep(2.0)
    raise SmokeFailure(f"the pipeline did not finish within {timeout:.0f}s")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    meeting_id: str | None = None

    print(f"MrListener live-suggestions smoke test against {base}")

    try:
        status, raw = http("GET", f"{base}/api/health")
        check(status == 200, f"GET /api/health -> {status}")
        health = body(raw)
        check(health.get("ffmpeg") is True, "health reports ffmpeg available")
        check(health.get("groq_key_set") is True, "health reports GROQ_API_KEY is set")

        status, raw = http("PUT", f"{base}/api/settings", {"suggestions_enabled": True})
        check(status == 200, f"PUT /api/settings -> {status}")
        settings_now = body(raw)
        check(settings_now["suggestions_enabled"] is True, "live suggestions are enabled")
        note(f"refresh interval is {settings_now['suggestions_interval_seconds']}s")

        fixture = ensure_suggestions_webm(rebuild=args.rebuild)
        check(fixture.stat().st_size > 0, f"speech fixture ready ({fixture.name})")

        status, raw = http("POST", f"{base}/api/meetings", {"title": None})
        check(status == 201, f"POST /api/meetings -> {status}")
        meeting_id = body(raw)["id"]

        note(f"streaming {SUGGESTIONS_SECONDS:.0f}s of speech at real-time pace")
        started = time.perf_counter()
        frames, forced, forced_seconds = await stream_and_refresh(
            f"{ws_base}/ws/record/{meeting_id}", fixture, base, meeting_id
        )
        note(f"stream finished in {time.perf_counter() - started:.0f}s")

        errors = [f["message"] for f in frames if f.get("type") == "error"]
        check(not errors, f"no error frames on the socket ({errors})")

        pushed = [f for f in frames if f.get("type") == "suggestions"]
        check(
            len(pushed) >= 1,
            f"a suggestions frame arrived over the socket before /stop ({len(pushed)})",
        )
        for index, frame in enumerate(pushed):
            validate_batch(frame, f"socket batch {index + 1}")
        check(
            all(f.get("transcript_end") is not None for f in pushed),
            "every pushed batch carries transcript_end",
        )

        check(forced is not None, "POST /suggestions/refresh returned a batch")
        assert forced is not None
        validate_batch(forced, "forced refresh")
        note(f"the forced refresh took {forced_seconds:.1f}s end to end")

        show_batch(pushed[0], "first suggestions batch pushed over the socket:")

        status, raw = http("POST", f"{base}/api/meetings/{meeting_id}/stop")
        check(status == 200, f"POST /stop -> {status}")

        ready = wait_for_ready(base, meeting_id, args.timeout)
        check(ready["status"] == "ready", f"the pipeline finished ({ready['status']!r})")
        check(ready.get("pipeline_error") is None, f"no pipeline error ({ready.get('pipeline_error')})")

        notes = ready.get("notes") or {}
        follow_ups = notes.get("follow_up_questions")
        check(isinstance(follow_ups, list), "notes carry a follow_up_questions list")
        check(bool(follow_ups), f"follow_up_questions is not empty ({len(follow_ups or [])})")
        print("\n  follow-up questions from the notes:")
        for question in follow_ups or []:
            print(f"    - {question}")
        print()

        status, raw = http("GET", f"{base}/api/meetings/{meeting_id}/suggestions")
        check(status == 200, f"GET /suggestions -> {status}")
        history = body(raw)
        check(
            len(history["batches"]) >= 1,
            f"the persisted history has {len(history['batches'])} batch(es)",
        )
        check(history.get("live") is False, "the in-memory state was released at /stop")
        show_batch(history["batches"][-1], "last persisted batch:")

        # Pins: PATCH the set, then read it straight back.
        pins = [dict(item) for item in history["batches"][-1]["items"][:2]]
        status, raw = http(
            "PATCH", f"{base}/api/meetings/{meeting_id}/suggestions/pins", {"pinned": pins}
        )
        check(status == 200, f"PATCH /suggestions/pins -> {status}")
        check(len(body(raw)["pinned"]) == len(pins), "the PATCH response echoes the pins")

        status, raw = http("GET", f"{base}/api/meetings/{meeting_id}/suggestions")
        check(status == 200, f"GET /suggestions -> {status}")
        stored = body(raw)
        check(
            [item["text"] for item in stored["pinned"]] == [item["text"] for item in pins],
            "GET /suggestions returns the pinned items that were just stored",
        )
        check(
            len(stored["batches"]) >= 1,
            "pinning did not disturb the stored batches",
        )

        markdown_status, markdown = http("GET", f"{base}/api/meetings/{meeting_id}/export.md")
        check(markdown_status == 200, f"GET /export.md -> {markdown_status}")
        check(
            "## Follow-up questions" in markdown.decode("utf-8", "replace"),
            "export.md carries a Follow-up questions heading",
        )

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

    print("All live-suggestions smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
