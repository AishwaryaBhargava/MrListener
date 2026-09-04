"""End-to-end smoke test for backend/app/diarization.py.

Builds a two-speaker fixture from two different Windows TTS voices, diarizes
it, and checks the result against the boundaries we know by construction.

    python backend/scripts/diarize_smoke.py            # reuse fixture if present
    python backend/scripts/diarize_smoke.py --rebuild  # regenerate the audio

What it asserts
---------------
1. ``diarize()`` finds exactly two speakers (no ``num_speakers`` hint given -
   the pipeline has to work it out).
2. Collapsing the turns into speaker runs yields four runs whose boundaries sit
   within 1.0 s of the known utterance boundaries.
3. ``assign_speakers()`` labels fake Whisper segments A/B/A/B, numbering by
   first appearance, and ``speaker_summary()`` reports two speakers with two
   turns each.

Fixture construction
--------------------
System.Speech renders each utterance to its own WAV. ffmpeg converts to 16 kHz
mono and trims leading/trailing silence, so the known boundaries are tight
against actual speech. The pieces are then concatenated sample-accurately with
Python stdlib ``wave`` (0.5 s of digital silence between utterances), which
means the expected boundaries are exact rather than ffmpeg-estimated.

If the machine only exposes one voice, speaker B is produced by pitch-shifting
speaker A with ``asetrate=16000*0.8,aresample=16000``.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
FIXTURE_DIR = SCRIPT_DIR / "fixtures"
FIXTURE_WAV = FIXTURE_DIR / "two_speakers.wav"
FIXTURE_META = FIXTURE_DIR / "two_speakers.json"

# Import backend.app.diarization without needing the package installed.
sys.path.insert(0, str(BACKEND_DIR))

SAMPLE_RATE = 16000
GAP_SECONDS = 0.5
BOUNDARY_TOLERANCE = 1.0

# Four utterances, alternating A / B / A / B. Roughly 7 s each at the default
# SAPI rate, which lands the fixture near 30 s. No apostrophes: the texts are
# embedded in single-quoted PowerShell literals.
UTTERANCES = [
    (
        "A",
        "Good morning everyone, thanks for joining the weekly sync. I want to "
        "start with the migration status and then the open bugs.",
    ),
    (
        "B",
        "Thanks. The migration finished on the staging cluster last night and "
        "every integration test passed. One slow query still needs a look.",
    ),
    (
        "A",
        "That sounds good. How risky is the slow query for launch? I would "
        "rather delay a day than ship something that falls over.",
    ),
    (
        "B",
        "It is low risk in my opinion. The index is already building and "
        "should be ready this afternoon. I will send an update.",
    ),
]

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class SmokeFailure(AssertionError):
    pass


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
        **kwargs,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[-1500:]
        raise SmokeFailure(f"command failed ({proc.returncode}): {cmd[0]}\n{detail}")
    return proc


def ffmpeg_bin() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SmokeFailure("ffmpeg was not found on PATH")
    return exe


def powershell_bin() -> str:
    exe = shutil.which("powershell") or shutil.which("powershell.exe")
    if not exe:
        raise SmokeFailure("powershell was not found on PATH (Windows only test)")
    return exe


def ps_quote(text: str) -> str:
    """Single-quoted PowerShell literal; the only escape needed is '' for '."""
    return "'" + text.replace("'", "''") + "'"


# --------------------------------------------------------------------------
# fixture generation
# --------------------------------------------------------------------------

def list_voices() -> list[str]:
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.GetInstalledVoices() | Where-Object { $_.Enabled } | "
        "ForEach-Object { $_.VoiceInfo.Name }"
    )
    proc = run([powershell_bin(), "-NoProfile", "-Command", script])
    return [line.strip() for line in proc.stdout.decode("utf-8", "replace").splitlines() if line.strip()]


def pick_voices(voices: list[str]) -> tuple[str, str, bool]:
    """Return (voice_a, voice_b, pitch_shift_b).

    Prefers two voices of different gender-ish names (David/Zira on a stock
    Windows install). Falls back to pitch shifting when only one voice exists.
    """
    if not voices:
        raise SmokeFailure("no enabled System.Speech voices are installed")
    if len(voices) == 1:
        return voices[0], voices[0], True

    # David + Zira are the stock pair and are acoustically far apart.
    preferred_a = next((v for v in voices if "David" in v), voices[0])
    preferred_b = next((v for v in voices if "Zira" in v and v != preferred_a), None)
    if preferred_b is None:
        preferred_b = next(v for v in voices if v != preferred_a)
    return preferred_a, preferred_b, False


def synthesize(voice_a: str, voice_b: str, out_dir: Path) -> list[Path]:
    """Render each utterance to <out_dir>/raw_<i>.wav with System.Speech."""
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "Add-Type -AssemblyName System.Speech",
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
    ]
    paths = []
    for index, (who, text) in enumerate(UTTERANCES):
        path = out_dir / f"raw_{index}.wav"
        paths.append(path)
        voice = voice_a if who == "A" else voice_b
        lines.append(f"$s.SelectVoice({ps_quote(voice)})")
        lines.append(f"$s.SetOutputToWaveFile({ps_quote(str(path))})")
        lines.append(f"$s.Speak({ps_quote(text)})")
    lines.append("$s.SetOutputToNull()")
    lines.append("$s.Dispose()")

    script_path = out_dir / "_tts.ps1"
    script_path.write_text("\r\n".join(lines), encoding="utf-8")
    run([powershell_bin(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)])
    script_path.unlink(missing_ok=True)

    missing = [p.name for p in paths if not p.exists() or p.stat().st_size == 0]
    if missing:
        raise SmokeFailure(f"System.Speech produced no audio for: {missing}")
    return paths


def normalize(src: Path, dst: Path, pitch_shift: bool) -> None:
    """16 kHz mono PCM, silence trimmed at both ends, optional pitch shift."""
    trim = (
        "silenceremove=start_periods=1:start_duration=0:start_threshold=-45dB:"
        "detection=peak,areverse,"
        "silenceremove=start_periods=1:start_duration=0:start_threshold=-45dB:"
        "detection=peak,areverse"
    )
    chain = f"asetrate={SAMPLE_RATE}*0.8,aresample={SAMPLE_RATE}," + trim if pitch_shift else trim
    run([
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-af", chain,
        "-c:a", "pcm_s16le", "-f", "wav",
        str(dst),
    ])
    if not dst.exists() or dst.stat().st_size == 0:
        raise SmokeFailure(f"ffmpeg produced no output for {src.name}")


def build_fixture(rebuild: bool) -> dict:
    """Create (or reuse) the fixture WAV; returns its metadata."""
    if not rebuild and FIXTURE_WAV.exists() and FIXTURE_META.exists():
        meta = json.loads(FIXTURE_META.read_text(encoding="utf-8"))
        print(f"reusing fixture {FIXTURE_WAV} ({meta['duration']:.2f}s)")
        return meta

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    voices = list_voices()
    voice_a, voice_b, pitch_shift = pick_voices(voices)
    print(f"installed voices: {voices}")
    print(f"speaker A voice: {voice_a}")
    print(f"speaker B voice: {voice_b}" + (" (pitch shifted 0.8x)" if pitch_shift else ""))

    raw_paths = synthesize(voice_a, voice_b, FIXTURE_DIR)
    clips = []
    for index, raw in enumerate(raw_paths):
        clip = FIXTURE_DIR / f"utt_{index}.wav"
        normalize(raw, clip, pitch_shift and UTTERANCES[index][0] == "B")
        clips.append(clip)
        raw.unlink(missing_ok=True)

    silence = b"\x00\x00" * int(SAMPLE_RATE * GAP_SECONDS)
    frames: list[bytes] = []
    boundaries = []
    cursor = 0  # in frames

    for index, clip in enumerate(clips):
        with wave.open(str(clip), "rb") as handle:
            if handle.getframerate() != SAMPLE_RATE or handle.getnchannels() != 1:
                raise SmokeFailure(f"{clip.name} is not 16 kHz mono")
            data = handle.readframes(handle.getnframes())
        count = len(data) // 2
        boundaries.append(
            {
                "speaker": UTTERANCES[index][0],
                "text": UTTERANCES[index][1],
                "start": cursor / SAMPLE_RATE,
                "end": (cursor + count) / SAMPLE_RATE,
            }
        )
        frames.append(data)
        cursor += count
        if index < len(clips) - 1:
            frames.append(silence)
            cursor += len(silence) // 2
        clip.unlink(missing_ok=True)

    with wave.open(str(FIXTURE_WAV), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(b"".join(frames))

    meta = {
        "duration": cursor / SAMPLE_RATE,
        "sample_rate": SAMPLE_RATE,
        "voice_a": voice_a,
        "voice_b": voice_b,
        "pitch_shifted": pitch_shift,
        "utterances": boundaries,
    }
    FIXTURE_META.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"built fixture {FIXTURE_WAV} ({meta['duration']:.2f}s)")
    return meta


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def speaker_runs(turns: list[dict]) -> list[dict]:
    """Collapse consecutive turns by the same speaker into one run.

    ``diarize`` only fuses gaps under 0.3 s; a speaker pausing mid-sentence for
    longer legitimately produces two turns. For boundary checking we want the
    span of each contiguous stretch of one voice.
    """
    runs: list[dict] = []
    for turn in turns:
        if runs and runs[-1]["speaker"] == turn["speaker"]:
            runs[-1]["end"] = max(runs[-1]["end"], turn["end"])
        else:
            runs.append(dict(turn))
    return runs


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true", help="regenerate the fixture audio")
    parser.add_argument("--verbose", action="store_true", help="enable INFO logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    meta = build_fixture(args.rebuild)
    expected = meta["utterances"]
    duration = meta["duration"]

    print("\nexpected utterance boundaries:")
    for item in expected:
        print(f"  {item['speaker']}  {item['start']:6.2f} -> {item['end']:6.2f}")

    from app.diarization import assign_speakers, diarize, speaker_summary

    print("\nloading pipeline and diarizing (no speaker-count hint) ...")
    load_started = time.perf_counter()
    events: list[tuple[str, float]] = []
    turns = diarize(
        str(FIXTURE_WAV),
        progress_cb=lambda step, fraction: events.append((step, fraction)),
    )
    total_elapsed = time.perf_counter() - load_started

    print(f"\n{len(turns)} turn(s):")
    for turn in turns:
        print(f"  {turn['speaker']:<12} {turn['start']:6.2f} -> {turn['end']:6.2f}")
    if events:
        print(f"progress callback fired {len(events)} time(s), last = {events[-1]}")

    # --- assertion 1: exactly two speakers ---------------------------------
    found = sorted({turn["speaker"] for turn in turns})
    check(len(found) == 2, f"expected exactly 2 speakers, got {len(found)}: {found}")

    # --- assertion 2: boundaries line up -----------------------------------
    runs = speaker_runs(turns)
    check(
        len(runs) == len(expected),
        f"expected {len(expected)} speaker runs, got {len(runs)}: "
        + ", ".join(f"{r['speaker']}@{r['start']:.2f}-{r['end']:.2f}" for r in runs),
    )
    worst = 0.0
    for index, (run_, want) in enumerate(zip(runs, expected)):
        for edge in ("start", "end"):
            delta = abs(run_[edge] - want[edge])
            worst = max(worst, delta)
            check(
                delta <= BOUNDARY_TOLERANCE,
                f"utterance {index} {edge} off by {delta:.2f}s "
                f"(got {run_[edge]:.2f}, want {want[edge]:.2f}, "
                f"tolerance {BOUNDARY_TOLERANCE}s)",
            )
    print(f"\nboundary check passed: worst deviation {worst:.2f}s "
          f"(tolerance {BOUNDARY_TOLERANCE}s)")

    # --- assertion 3: the A/B/A/B pattern ----------------------------------
    pattern = [run_["speaker"] for run_ in runs]
    check(
        pattern[0] == pattern[2] and pattern[1] == pattern[3] and pattern[0] != pattern[1],
        f"expected an A/B/A/B pattern, got {pattern}",
    )

    # --- assertion 4: assign_speakers over fake Whisper segments -----------
    segments = [
        {"id": index, "start": item["start"], "end": item["end"], "text": item["text"]}
        for index, item in enumerate(expected)
    ]
    labelled = assign_speakers(segments, turns)
    print("\nassign_speakers:")
    for segment in labelled:
        print(
            f"  [{segment['id']}] {segment['speaker']} ({segment['speaker_id']}) "
            f"{segment['start']:6.2f} -> {segment['end']:6.2f}  "
            f"{segment['text'][:48]}..."
        )

    labels = [segment["speaker"] for segment in labelled]
    ids = [segment["speaker_id"] for segment in labelled]
    check(labels == ["Speaker 1", "Speaker 2", "Speaker 1", "Speaker 2"],
          f"expected A/B/A/B labels numbered by first appearance, got {labels}")
    check(ids == ["S1", "S2", "S1", "S2"], f"expected S1/S2/S1/S2, got {ids}")
    check(
        all(segment["text"] == expected[segment["id"]]["text"] for segment in labelled),
        "assign_speakers must not alter segment text",
    )
    check("speaker" not in segments[0], "assign_speakers must not mutate its input")

    # --- assertion 5: speaker_summary --------------------------------------
    summary = speaker_summary(labelled)
    print("\nspeaker_summary:")
    for entry in summary:
        print(
            f"  {entry['speaker']} ({entry['speaker_id']}): "
            f"{entry['talk_time']:.2f}s over {entry['turn_count']} turn(s), "
            f"{entry['segment_count']} segment(s)"
        )
    check(len(summary) == 2, f"expected 2 speakers in the summary, got {len(summary)}")
    check(
        all(entry["turn_count"] == 2 for entry in summary),
        f"expected 2 turns per speaker, got {[e['turn_count'] for e in summary]}",
    )
    check(
        all(entry["talk_time"] > 0 for entry in summary),
        "every speaker should have non-zero talk time",
    )

    print(
        f"\nPASS  {duration:.1f}s of audio diarized in {total_elapsed:.1f}s "
        f"(includes one-off pipeline load) "
        f"-> {total_elapsed / duration:.2f}x realtime"
    )

    # Second run: the pipeline is cached, so this is the steady-state cost the
    # backend will actually pay per meeting.
    warm_started = time.perf_counter()
    diarize(str(FIXTURE_WAV))
    warm_elapsed = time.perf_counter() - warm_started
    print(
        f"WARM  same file with the pipeline already loaded: {warm_elapsed:.1f}s "
        f"-> {warm_elapsed / duration:.2f}x realtime "
        f"(~{warm_elapsed / duration * 3600 / 60:.0f} min for a 1-hour meeting)"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SmokeFailure as exc:
        print(f"\nFAIL  {exc}", file=sys.stderr)
        sys.exit(1)
