"""ffmpeg/ffprobe helpers and a Range-capable file responder.

Every subprocess call runs synchronously and is invoked from a worker thread
(``anyio.to_thread.run_sync``) rather than through asyncio subprocesses, which
keeps behaviour identical across Windows event-loop policies.
"""

from __future__ import annotations

import mimetypes
import os
import re
import subprocess
import sys
import wave
from pathlib import Path
from typing import Iterator, Optional

from fastapi import Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from .config import WAV_CHANNELS, WAV_SAMPLE_RATE, ffmpeg_bin, ffprobe_bin

# Keep ffmpeg from flashing a console window on Windows.
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
_CHUNK = 1024 * 256


class AudioError(RuntimeError):
    pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
    )


def convert_to_wav(src: Path, dst: Path) -> None:
    """Transcode the accumulated browser recording to 16 kHz mono pcm_s16le."""
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        raise AudioError("ffmpeg was not found on PATH")
    if not src.exists() or src.stat().st_size == 0:
        raise AudioError(f"no audio was captured ({src.name} is missing or empty)")

    dst.parent.mkdir(parents=True, exist_ok=True)
    # Written to a .part file and renamed, so a crash never leaves a half wav
    # behind that would look complete to /stop's idempotency check.
    tmp = dst.with_suffix(".wav.part")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        # MediaRecorder chunks are concatenated clusters; be forgiving.
        "-fflags", "+genpts+discardcorrupt",
        "-i", str(src),
        "-vn",
        "-ac", str(WAV_CHANNELS),
        "-ar", str(WAV_SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        # The .part extension hides the container from ffmpeg's muxer probe.
        "-f", "wav",
        str(tmp),
    ]
    proc = _run(cmd)
    if proc.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        detail = proc.stderr.decode("utf-8", "replace").strip()[-800:]
        raise AudioError(f"ffmpeg failed ({proc.returncode}): {detail}")
    os.replace(tmp, dst)


def extract_window(src: Path, dst: Path, start: float) -> Optional[float]:
    """Decode ``src`` from ``start`` to EOF into a 16 kHz mono WAV.

    ``src`` is the growing ``raw.webm``. MediaRecorder's later blobs are not
    independently decodable, so the whole file is decoded from byte zero every
    time and ``-ss`` is applied *after* ``-i`` (an output-side, decode-accurate
    seek). The tail of a live file is usually a partial cluster, hence
    ``+discardcorrupt``.

    Returns the duration of what was written, or None when the file cannot be
    decoded yet (too little data) - the caller simply tries again next tick.
    """
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        raise AudioError("ffmpeg was not found on PATH")
    if not src.exists() or src.stat().st_size == 0:
        return None

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".wav.part")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-fflags", "+genpts+discardcorrupt",
        "-i", str(src),
        "-ss", f"{max(start, 0.0):.3f}",
        "-vn",
        "-ac", str(WAV_CHANNELS),
        "-ar", str(WAV_SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        "-f", "wav",
        str(tmp),
    ]
    proc = _run(cmd)
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return None

    duration = wav_duration(tmp)
    if duration is None or duration <= 0:
        tmp.unlink(missing_ok=True)
        return None

    os.replace(tmp, dst)
    return duration


def split_for_upload(
    src: Path, out_dir: Path, chunk_seconds: int, total_seconds: Optional[float] = None
) -> list[tuple[Path, float]]:
    """Cut ``src`` into consecutive FLAC chunks for upload-size-limited APIs.

    Returns ``[(chunk_path, start_offset_seconds), ...]`` in order. Chunks are
    16 kHz mono FLAC, so a 10-minute chunk is well under Groq's 25 MB cap. The
    caller deletes them when done.
    """
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        raise AudioError("ffmpeg was not found on PATH")
    if total_seconds is None:
        total_seconds = wav_duration(src) or probe_duration(src) or 0.0
    if total_seconds <= 0:
        raise AudioError(f"could not determine the duration of {src.name}")

    out_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[tuple[Path, float]] = []
    index = 0
    start = 0.0
    while start < total_seconds:
        dst = out_dir / f"chunk_{index:03d}.flac"
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", f"{start:.3f}",
            "-t", str(chunk_seconds),
            "-i", str(src),
            "-vn",
            "-ac", str(WAV_CHANNELS),
            "-ar", str(WAV_SAMPLE_RATE),
            "-c:a", "flac",
            str(dst),
        ]
        proc = _run(cmd)
        if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            for path, _ in chunks:
                path.unlink(missing_ok=True)
            dst.unlink(missing_ok=True)
            detail = proc.stderr.decode("utf-8", "replace").strip()[-800:]
            raise AudioError(f"ffmpeg failed while chunking ({proc.returncode}): {detail}")
        chunks.append((dst, start))
        index += 1
        start += chunk_seconds
    return chunks


def wav_duration(path: Path) -> Optional[float]:
    """Exact duration of a PCM wav, straight from its header - no ffprobe hop."""
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            if rate <= 0:
                return None
            return handle.getnframes() / float(rate)
    except (wave.Error, OSError):
        return None


def probe_duration(path: Path) -> Optional[float]:
    """Duration in seconds, or None when ffprobe cannot determine it."""
    ffprobe = ffprobe_bin()
    if not ffprobe or not path.exists():
        return None
    proc = _run([
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    if proc.returncode != 0:
        return None
    raw = proc.stdout.decode("utf-8", "replace").strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _iter_range(path: Path, start: int, end: int) -> Iterator[bytes]:
    remaining = end - start + 1
    with open(path, "rb") as handle:
        handle.seek(start)
        while remaining > 0:
            block = handle.read(min(_CHUNK, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block


def range_file_response(request: Request, path: Path, filename: str) -> Response:
    """Serve ``path`` honouring a single-range ``Range`` header.

    Starlette's FileResponse handles ranges in current versions, but the
    <audio> scrubber is central to the product so range handling is explicit
    here rather than dependent on the installed Starlette version.
    """
    size = path.stat().st_size
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    range_header = request.headers.get("range")

    if not range_header:
        response = FileResponse(path, media_type=media_type, filename=filename)
        response.headers["Accept-Ranges"] = "bytes"
        return response

    match = _RANGE_RE.fullmatch(range_header.strip())
    if not match:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    raw_start, raw_end = match.group(1), match.group(2)
    if raw_start == "":
        # Suffix range: last N bytes.
        length = int(raw_end or 0)
        if length <= 0:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        start = max(size - length, 0)
        end = size - 1
    else:
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1

    end = min(end, size - 1)
    if start > end or start >= size:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    return StreamingResponse(
        _iter_range(path, start, end),
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(end - start + 1),
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
        },
    )
