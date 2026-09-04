"""End-to-end smoke test for the whole product: Stages 1 through 5.

Runs against a backend that is already up, and against the real Groq and
Hugging Face APIs. No microphone is needed - the two-speaker fixture built by
``diarize_smoke.py`` is streamed over the recording WebSocket at real-time
pace, exactly the way the browser sends MediaRecorder chunks.

    backend\\.venv\\Scripts\\python backend\\scripts\\smoke_full.py

What it proves
--------------
1. A meeting with no title is named "Recording NN" by the backend.
2. Live transcript frames arrive over the socket while still recording.
3. /stop drives the pipeline through transcribing -> summarizing ->
   identifying_speakers -> summarizing -> done, and the **notes land before
   diarization starts** (the timings printed at the end show the gap).
4. The transcript has segments, at least 80% of them carry a speaker, and
   exactly two speakers were found.
5. The notes have a real summary and at least one action item.
6. The auto title "Recording NN" was replaced by the model's suggestion.
7. **The app stays usable during diarization**: while the first meeting is in
   the identifying_speakers stage, a second meeting is created and 5 seconds
   are streamed into it over a second WebSocket.
8. Renaming S1 shows up in GET detail, in the transcript and in export.md.
9. ?q= finds the meeting by a word that was only ever spoken.
10. Notes regenerate on demand, and delete removes the row and the folder.

Options
-------
    --base-url    http://localhost:8000 by default
    --keep        leave the created meetings in the database
    --rebuild     regenerate the speech fixture first
    --timeout     seconds to wait for the pipeline (default 900)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
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
from diarize_smoke import FIXTURE_META, FIXTURE_WAV, build_fixture, ffmpeg_bin  # noqa: E402

FIXTURE_WEBM = SCRIPT_DIR / "fixtures" / "two_speakers.webm"

#: One send per second, matching the browser's MediaRecorder timeslice.
CHUNK_SECONDS = 1.0
#: A word only the fixture says, used for the search assertion.
SEARCH_WORD = "migration"
#: Share of segments that must carry a speaker label.
MIN_SPEAKER_COVERAGE = 0.8
#: Seconds of the fixture to stream into the concurrency-check meeting.
SECOND_MEETING_SECONDS = 5

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


def http(method: str, url: str, payload: dict | None = None, raw: bool = False):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            return response.status, (body.decode("utf-8") if raw else body)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, (body.decode("utf-8", "replace") if raw else body)


def body(payload: bytes) -> dict:
    return json.loads(payload.decode())


# --------------------------------------------------------------------------
# Fixture
# --------------------------------------------------------------------------


def ensure_webm(rebuild: bool) -> tuple[Path, float]:
    """The two-speaker fixture, encoded the way MediaRecorder would send it."""
    meta = build_fixture(rebuild)
    duration = float(meta["duration"])

    if rebuild or not FIXTURE_WEBM.exists() or FIXTURE_WEBM.stat().st_mtime < FIXTURE_WAV.stat().st_mtime:
        note(f"encoding {FIXTURE_WEBM.name} from the fixture wav")
        proc = subprocess.run(
            [
                ffmpeg_bin(),
                "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(FIXTURE_WAV),
                "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1",
                "-f", "webm",
                str(FIXTURE_WEBM),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace")[-800:]
            raise SmokeFailure(f"could not encode the webm fixture: {detail}")

    return FIXTURE_WEBM, duration


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------


async def stream(ws_url: str, source: Path, seconds: float, limit: float | None = None):
    """Send the fixture in ~1 s slices, collecting every JSON frame sent back.

    ``limit`` cuts the stream short (used for the concurrency check, which only
    needs to prove that a second recording still works).
    """
    payload = source.read_bytes()
    slices = max(1, int(round(seconds)))
    chunk_size = max(1, -(-len(payload) // slices))  # ceil division
    frames: list[dict] = []
    sent = 0
    stop_after = None if limit is None else max(1, int(round(limit)))

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
            for index, offset in enumerate(range(0, len(payload), chunk_size)):
                if stop_after is not None and index >= stop_after:
                    break
                await socket.send(payload[offset : offset + chunk_size])
                sent += len(payload[offset : offset + chunk_size])
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


def live_text(frames: list[dict]) -> str:
    words: list[str] = []
    for frame in frames:
        if frame.get("type") != "transcript":
            continue
        for segment in frame.get("segments") or []:
            words.append(str(segment.get("text") or ""))
    return " ".join(words).lower()


# --------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------


class StageTimer:
    """Records how long the meeting spent in each pipeline stage."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.elapsed: dict[str, float] = {}
        self._current: str | None = None
        self._since = time.monotonic()

    def observe(self, stage: str | None) -> bool:
        """Feed a polled stage. True when it changed."""
        label = stage or "done"
        if label == self._current:
            return False
        now = time.monotonic()
        if self._current is not None:
            self.elapsed[self._current] = self.elapsed.get(self._current, 0.0) + (now - self._since)
        self._since = now
        self._current = label
        self.order.append(label)
        return True

    def report(self) -> str:
        lines = []
        for stage in dict.fromkeys(self.order):
            if stage == "done":
                continue
            lines.append(f"    {stage:<22} {self.elapsed.get(stage, 0.0):6.1f}s")
        return "\n".join(lines)


async def wait_for_ready(base: str, meeting_id: str, timeout: float, on_stage=None):
    """Poll the detail endpoint the way the UI does, until the pipeline lands."""
    deadline = time.monotonic() + timeout
    timer = StageTimer()
    while time.monotonic() < deadline:
        status, raw = http("GET", f"{base}/api/meetings/{meeting_id}")
        if status != 200:
            raise SmokeFailure(f"GET /api/meetings/{{id}} -> {status}")
        meeting = body(raw)
        stage = meeting.get("pipeline_stage")
        if timer.observe(stage):
            note(f"stage: {stage or 'done'}")
            if on_stage is not None:
                await on_stage(stage, meeting)
        if meeting["status"] in ("ready", "failed") and stage is None:
            return meeting, timer
        await asyncio.sleep(1.0)
    raise SmokeFailure(f"the pipeline did not finish within {timeout:.0f}s")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    meeting_id: str | None = None
    second_id: str | None = None

    print(f"MrListener full smoke test against {base}")
    started_at = time.monotonic()

    try:
        # -- environment ---------------------------------------------------
        status, raw = http("GET", f"{base}/api/health")
        check(status == 200, f"GET /api/health -> {status}")
        health = body(raw)
        check(health.get("ffmpeg") is True, "health reports ffmpeg available")
        check(health.get("groq_key_set") is True, "health reports GROQ_API_KEY is set")
        check(health.get("hf_token_set") is True, "health reports HF_TOKEN is set")

        status, raw = http("GET", f"{base}/api/settings")
        check(status == 200, f"GET /api/settings -> {status}")
        check(body(raw)["diarization_enabled"] is True, "diarization is enabled in settings")

        fixture, duration = ensure_webm(args.rebuild)
        check(fixture.stat().st_size > 0, f"two-speaker fixture ready ({duration:.1f}s)")

        # -- record --------------------------------------------------------
        status, raw = http("POST", f"{base}/api/meetings", {"title": None})
        check(status == 201, f"POST /api/meetings -> {status}")
        meeting = body(raw)
        meeting_id = meeting["id"]
        auto_name = meeting["title"]
        import re

        check(
            re.fullmatch(r"Recording \d{2,}", auto_name) is not None,
            f"backend assigned a sequential name ({auto_name!r})",
        )

        note(f"streaming {duration:.0f}s of two-speaker audio at real-time pace")
        sent, frames = await stream(f"{ws_base}/ws/record/{meeting_id}", fixture, duration)
        check(sent == fixture.stat().st_size, f"streamed {sent} bytes over the websocket")
        errors = [f["message"] for f in frames if f.get("type") == "error"]
        check(not errors, f"no error frames on the socket ({errors})")
        check(
            len([f for f in frames if f.get("type") == "transcript"]) >= 1,
            "a live transcript frame arrived while recording",
        )
        note(f"live text: {live_text(frames)[:160]}")

        status, raw = http("POST", f"{base}/api/meetings/{meeting_id}/stop")
        check(status == 200, f"POST /stop -> {status}")
        check(body(raw)["status"] == "processing", "meeting is 'processing' after /stop")

        # -- the app stays usable while diarization runs --------------------
        concurrency = {"tested": False, "notes_before_diarization": False}

        async def on_stage(stage: str | None, snapshot: dict) -> None:
            if stage in ("identifying_speakers", "diarizing"):
                if snapshot.get("has_notes"):
                    concurrency["notes_before_diarization"] = True
                    elapsed = time.monotonic() - started_at
                    note(f"notes were ready before diarization started ({elapsed:.0f}s in)")
                if concurrency["tested"]:
                    return
                concurrency["tested"] = True
                nonlocal second_id
                note("diarization is running - creating a second meeting now")
                code, payload = http("POST", f"{base}/api/meetings", {"title": None})
                if code != 201:
                    raise SmokeFailure(f"POST /api/meetings during diarization -> {code}")
                second_id = body(payload)["id"]
                bytes_sent, _ = await stream(
                    f"{ws_base}/ws/record/{second_id}",
                    fixture,
                    duration,
                    limit=SECOND_MEETING_SECONDS,
                )
                if bytes_sent <= 0:
                    raise SmokeFailure("the second websocket accepted no audio")
                code, _ = http("POST", f"{base}/api/meetings/{second_id}/stop")
                if code != 200:
                    raise SmokeFailure(f"POST /stop on the second meeting -> {code}")
                note(f"second recording accepted {bytes_sent} bytes while diarizing")

        ready, timer = await wait_for_ready(base, meeting_id, args.timeout, on_stage)
        check(ready["status"] == "ready", f"the pipeline finished ({ready['status']!r})")
        check(ready.get("pipeline_error") is None, f"no pipeline error ({ready.get('pipeline_error')})")
        check(concurrency["tested"], "a second meeting was created and streamed during diarization")
        check(
            concurrency["notes_before_diarization"],
            "the notes were already written when diarization started",
        )

        # The polled sequence, deduplicated. A stage that takes less than one
        # poll interval can be missed entirely - notes on this fixture take
        # under two seconds - so the ordering assertion only looks at the
        # stages that were actually seen. "Notes before diarization" is proven
        # above by has_notes being true the moment the slow step started.
        seen = list(dict.fromkeys(timer.order))
        check(
            "identifying_speakers" in seen,
            f"the diarization stage was reported (stages: {' -> '.join(seen)})",
        )
        if "summarizing" in seen:
            check(
                seen.index("summarizing") < seen.index("identifying_speakers"),
                f"notes ran before speakers (stages: {' -> '.join(seen)})",
            )
        check(
            "transcribing" not in seen or seen.index("transcribing") == 0,
            f"transcription ran first (stages: {' -> '.join(seen)})",
        )

        # -- transcript + speakers -----------------------------------------
        status, raw = http("GET", f"{base}/api/meetings/{meeting_id}/transcript")
        check(status == 200, f"GET /transcript -> {status}")
        transcript = body(raw)
        segments = transcript["segments"]
        check(len(segments) > 0, f"{len(segments)} transcript segments were stored")

        labelled = [s for s in segments if s.get("speaker_id")]
        coverage = len(labelled) / len(segments)
        check(
            coverage >= MIN_SPEAKER_COVERAGE,
            f"{coverage:.0%} of segments carry a speaker (>= {MIN_SPEAKER_COVERAGE:.0%})",
        )

        speakers = ready["speakers"]
        check(len(speakers) == 2, f"exactly 2 speakers were found ({len(speakers)})")
        check(
            [s["color_index"] for s in speakers] == [0, 1],
            "speakers got distinct palette slots",
        )

        # -- notes ----------------------------------------------------------
        notes = ready["notes"]
        check(notes is not None, "notes_json was written")
        check(len(str(notes["summary"]).strip()) > 40, "the summary is non-empty")
        check(len(notes["action_items"]) >= 1, f"{len(notes['action_items'])} action item(s)")
        check(notes["with_speakers"] is True, "the notes were written with speaker labels")

        check(
            ready["title"] != auto_name,
            f"the auto title was replaced ({auto_name!r} -> {ready['title']!r})",
        )
        check(ready["auto_title"] == auto_name, "the original name is kept in auto_title")

        # -- rename a speaker ------------------------------------------------
        status, raw = http("PATCH", f"{base}/api/meetings/{meeting_id}/speakers", {"S1": "Priya"})
        check(status == 200, f"PATCH /speakers -> {status}")
        renamed = body(raw)
        check(
            any(s["id"] == "S1" and s["name"] == "Priya" for s in renamed["speakers"]),
            "GET detail reports the new speaker name",
        )

        status, raw = http("GET", f"{base}/api/meetings/{meeting_id}/transcript")
        after = body(raw)["segments"]
        check(
            any(s.get("speaker") == "Priya" for s in after),
            "the transcript resolves S1 to the new name",
        )

        status, markdown = http("GET", f"{base}/api/meetings/{meeting_id}/export.md", raw=True)
        check(status == 200, f"GET /export.md -> {status}")
        check("Priya" in markdown, "export.md carries the new speaker name")
        check("## Transcript" in markdown, "export.md includes the full transcript")
        check(
            "## Action items" in markdown and "- [ ]" in markdown,
            "export.md renders the action items as checkboxes",
        )

        # -- action item state ------------------------------------------------
        status, raw = http(
            "PATCH", f"{base}/api/meetings/{meeting_id}/action_items/0", {"done": True}
        )
        check(status == 200, f"PATCH /action_items/0 -> {status}")
        check(body(raw)["notes"]["action_items"][0]["done"] is True, "the action item stayed ticked")

        # -- search -----------------------------------------------------------
        status, raw = http("GET", f"{base}/api/meetings?q={SEARCH_WORD}")
        check(status == 200, f"GET /api/meetings?q= -> {status}")
        hits = body(raw)
        check(
            any(row["id"] == meeting_id for row in hits),
            f"searching for {SEARCH_WORD!r} finds the meeting",
        )
        hit = next(row for row in hits if row["id"] == meeting_id)
        snippet = hit["match_snippet"] or ""
        check(bool(snippet), f"the hit carries a snippet ({snippet[:60]!r})")
        check(hit["speaker_count"] == 2, "the list row reports the speaker count")
        check(hit["action_item_count"] >= 1, "the list row reports the action item count")

        status, raw = http("GET", f"{base}/api/meetings?q=zzzznotawordzzzz")
        check(not any(row["id"] == meeting_id for row in body(raw)), "a miss returns nothing")

        # -- regenerate --------------------------------------------------------
        status, raw = http("POST", f"{base}/api/meetings/{meeting_id}/notes/regenerate")
        check(status == 200, f"POST /notes/regenerate -> {status}")
        again, _ = await wait_for_ready(base, meeting_id, 180.0)
        check(again["status"] == "ready", "regenerating the notes finished")
        check(again["notes"] is not None, "the regenerated notes were stored")
        check(
            again["notes"]["with_speakers"] is True,
            "the regenerated notes still carry speaker attribution",
        )
        check(
            any(s["id"] == "S1" and s["name"] == "Priya" for s in again["speakers"]),
            "regenerating notes did not lose the renamed speaker",
        )

        # -- report -------------------------------------------------------------
        print("\n  pipeline stage timings:")
        print(timer.report())
        print("\n  notes JSON:")
        print(json.dumps(again["notes"], indent=2, ensure_ascii=False))
        print()

        # -- delete ---------------------------------------------------------------
        if not args.keep:
            for victim in [meeting_id, second_id]:
                if not victim:
                    continue
                status, _ = http("DELETE", f"{base}/api/meetings/{victim}")
                check(status == 204, f"DELETE /api/meetings/{victim[:8]} -> {status}")
                check(
                    not config.meeting_dir(victim).exists(),
                    f"the audio folder for {victim[:8]} was removed",
                )
            status, _ = http("GET", f"{base}/api/meetings/{meeting_id}")
            check(status == 404, "the deleted meeting is gone")
            meeting_id = second_id = None

    except SmokeFailure as exc:
        print(f"\nFAILED: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAILED: unexpected error: {type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
        return 1
    finally:
        if meeting_id and not args.keep:
            http("DELETE", f"{base}/api/meetings/{meeting_id}")
        if second_id and not args.keep:
            http("DELETE", f"{base}/api/meetings/{second_id}")

    print(f"\nAll checks passed in {time.monotonic() - started_at:.0f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
