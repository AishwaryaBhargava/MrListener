"""Builds the audio fixtures the smoke tests need. Windows only.

A sine tone transcribes to nothing, so the speech is synthesized with the
System.Speech voice that ships with Windows and then muxed into webm/opus - the
same container the browser's MediaRecorder produces.

    backend\\.venv\\Scripts\\python backend\\scripts\\make_fixtures.py

Everything lands in ``backend/scripts/fixtures/``, which is git-ignored; the
smoke tests rebuild whatever is missing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

SPEECH_WAV = FIXTURE_DIR / "stage2_speech.wav"
SPEECH_WEBM = FIXTURE_DIR / "stage2_speech.webm"

#: The live-suggestions fixture: longer, and written so a listener would have
#: obvious things to ask about.
SUGGESTIONS_WAV = FIXTURE_DIR / "suggestions_speech.wav"
SUGGESTIONS_WEBM = FIXTURE_DIR / "suggestions_speech.webm"

#: Deliberately meeting-shaped, and long enough that at least one 20 s live
#: window closes while it is still streaming.
SCRIPT = (
    "Okay so the main thing for this week is getting the onboarding flow "
    "finished before the Friday demo. I can take the email verification piece "
    "but I need the copy from marketing by Wednesday. We should ship analytics "
    "separately, bundling it puts the demo at risk."
)

#: Deliberately full of loose ends: a contract that expires with nobody named
#: as the owner of the renewal, a deadline with no date, an error spike nobody
#: investigated, and a dependency nobody confirmed. A listener paying attention
#: has several obvious questions; that is exactly what the suggestions model is
#: asked to find. No apostrophes - the script is embedded in a single-quoted
#: PowerShell string.
SUGGESTIONS_SCRIPT = (
    "Right, let us go through the vendor situation first. The Acme support "
    "contract expires at the end of next month and we have not started the "
    "renewal paperwork yet. Legal will need at least three weeks with it. "
    "Separately, the migration is about sixty percent done. We saw a spike in "
    "error rates on Tuesday, roughly four hundred failures in an hour, and I "
    "have not had time to look into what caused it. The board review is coming "
    "up soon and they will want the cost numbers by then. Marketing asked "
    "whether we can bring the launch forward, and I said probably, but that "
    "depends on whether the second data centre is ready. Nobody has confirmed "
    "that yet. One more thing. Priya mentioned that two of the integration "
    "tests are flaky and she has been rerunning them by hand. That is fine for "
    "now. Let us also remember the security review. It came back with four "
    "findings and I think two of them are still open. Okay, that is everything "
    "from me for today."
)

#: The default voice reads the script in ~18 s; -3 stretches it past 20 s.
SPEECH_RATE = -3
#: The suggestions script is three times longer, so it is read at the voice's
#: normal pace to fit inside SUGGESTIONS_SECONDS without being clipped.
SUGGESTIONS_RATE = 0
TARGET_SECONDS = 25.0
#: Long enough for a live window to close, a first suggestion batch to arrive,
#: and a forced refresh on top of it.
SUGGESTIONS_SECONDS = 70.0
#: CBR opus, so a byte offset in the file maps roughly onto a time offset and
#: the smoke test can stream it at a realistic pace.
OPUS_BITRATE = "24k"


class FixtureError(RuntimeError):
    pass


def _run(cmd: list[str], what: str) -> None:
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise FixtureError(f"{what} failed:\n{proc.stderr.decode('utf-8', 'replace')[-800:]}")


def make_speech_wav(
    target: Path = SPEECH_WAV, text: str = SCRIPT, rate: int = SPEECH_RATE
) -> Path:
    """Synthesize ``text`` with the built-in Windows TTS voice."""
    if sys.platform != "win32":
        raise FixtureError("Speech synthesis here relies on Windows System.Speech")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate = {rate}; "
        f"$s.SetOutputToWaveFile('{target}'); "
        f"$s.Speak('{text}'); "
        "$s.Dispose()"
    )
    _run(["powershell", "-NoProfile", "-Command", script], "Windows TTS")
    if not target.exists() or target.stat().st_size == 0:
        raise FixtureError(f"the TTS voice produced no audio at {target}")
    return target


def make_speech_webm(
    source: Path = SPEECH_WAV,
    target: Path = SPEECH_WEBM,
    seconds: float = TARGET_SECONDS,
) -> Path:
    """Pad to ``seconds`` and encode to webm/opus, like MediaRecorder does."""
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(source),
            # apad + -t gives an exact length whatever the voice's pace.
            "-af", "apad",
            "-t", f"{seconds}",
            "-ac", "1",
            "-ar", "48000",
            "-c:a", "libopus",
            "-b:a", OPUS_BITRATE,
            "-vbr", "off",
            str(target),
        ],
        "ffmpeg webm/opus encode",
    )
    return target


def ensure_speech_webm(rebuild: bool = False) -> Path:
    """The fixture the smoke tests stream. Built on first use, then cached."""
    if rebuild or not SPEECH_WAV.exists() or SPEECH_WAV.stat().st_size == 0:
        make_speech_wav()
    if rebuild or not SPEECH_WEBM.exists() or SPEECH_WEBM.stat().st_size == 0:
        make_speech_webm()
    return SPEECH_WEBM


def ensure_suggestions_webm(rebuild: bool = False) -> Path:
    """The ~70 s fixture the live-suggestions smoke test streams."""
    if rebuild or not SUGGESTIONS_WAV.exists() or SUGGESTIONS_WAV.stat().st_size == 0:
        make_speech_wav(SUGGESTIONS_WAV, SUGGESTIONS_SCRIPT, SUGGESTIONS_RATE)
    if rebuild or not SUGGESTIONS_WEBM.exists() or SUGGESTIONS_WEBM.stat().st_size == 0:
        make_speech_webm(SUGGESTIONS_WAV, SUGGESTIONS_WEBM, SUGGESTIONS_SECONDS)
    return SUGGESTIONS_WEBM


def main() -> int:
    try:
        wav = make_speech_wav()
        webm = make_speech_webm()
        suggestions_webm = ensure_suggestions_webm(rebuild=True)
    except FixtureError as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"wrote {wav} ({wav.stat().st_size} bytes)")
    print(f"wrote {webm} ({webm.stat().st_size} bytes)")
    print(f"wrote {suggestions_webm} ({suggestions_webm.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
