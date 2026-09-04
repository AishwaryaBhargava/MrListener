"""Speaker diarization on CPU with pyannote.audio 3.x.

The pipeline (``pyannote/speaker-diarization-3.1``) is gated on Hugging Face:
the account owning ``HF_TOKEN`` must have accepted the terms for both
``pyannote/speaker-diarization-3.1`` and ``pyannote/segmentation-3.0``.

Design notes
------------
* Building the pipeline costs several seconds, so it is cached at module level
  and constructed under a lock: one instance per process, safe to call from a
  thread pool.
* Everything runs on CPU. ``torch.set_num_threads`` is raised to the core count
  because the default can be a single thread, which makes segmentation several
  times slower.
* Nothing here imports FastAPI, so the module can be exercised from a plain
  script or a worker thread.

Public API
----------
``diarize(wav_path, num_speakers=None, min_speakers=None, max_speakers=None,
          progress_cb=None) -> list[dict]``
``assign_speakers(segments, turns) -> list[dict]``
``speaker_summary(segments) -> list[dict]``
"""

from __future__ import annotations

import inspect
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from dotenv import load_dotenv

log = logging.getLogger(__name__)

# backend/app/diarization.py -> backend/app -> backend -> <project root>
APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
ROOT_DIR = BACKEND_DIR.parent
ENV_PATH = ROOT_DIR / ".env"

load_dotenv(ENV_PATH, override=False)

#: Gated pipeline used for diarization.
PIPELINE_NAME = "pyannote/speaker-diarization-3.1"
#: Second gated repo the pipeline pulls in. Named in errors so the user knows
#: both licences have to be accepted.
SEGMENTATION_NAME = "pyannote/segmentation-3.0"

#: Adjacent turns of the same speaker closer than this are fused.
MERGE_GAP = 0.3

_TOKEN_ENV_VARS = ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGINGFACEHUB_API_TOKEN")

_pipeline: Any = None
_pipeline_lock = threading.Lock()


class DiarizationError(RuntimeError):
    """Raised for a missing token, denied model access or a bad input file."""


# --------------------------------------------------------------------------
# pipeline construction
# --------------------------------------------------------------------------

def _hf_token() -> str:
    for name in _TOKEN_ENV_VARS:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    raise DiarizationError(
        "HF_TOKEN is not set. Create a read token at "
        "https://huggingface.co/settings/tokens, accept the terms for "
        f"{PIPELINE_NAME} and {SEGMENTATION_NAME} while signed in as that "
        f"token owner, then add HF_TOKEN=<token> to {ENV_PATH}."
    )


def _access_error(exc: BaseException) -> DiarizationError:
    return DiarizationError(
        f"Could not load {PIPELINE_NAME}. The most common cause is that the "
        f"HF_TOKEN owner has not accepted the model terms: visit "
        f"https://huggingface.co/{PIPELINE_NAME} and "
        f"https://huggingface.co/{SEGMENTATION_NAME} while signed in, accept "
        f"both, then retry. Also check that the token is a valid read token "
        f"and that this machine can reach huggingface.co. Underlying error: "
        f"{type(exc).__name__}: {exc}"
    )


def _configure_threads() -> int:
    import torch

    try:
        torch.set_num_threads(os.cpu_count() or 1)
    except Exception:  # pragma: no cover - torch always accepts this
        pass
    return torch.get_num_threads()


def load_pipeline() -> Any:
    """Build (or return the cached) diarization pipeline pinned to CPU."""
    global _pipeline
    if _pipeline is not None:
        return _pipeline

    with _pipeline_lock:
        if _pipeline is not None:
            return _pipeline

        token = _hf_token()
        threads = _configure_threads()

        import torch
        from pyannote.audio import Pipeline

        started = time.perf_counter()
        try:
            pipeline = Pipeline.from_pretrained(PIPELINE_NAME, use_auth_token=token)
        except Exception as exc:  # network / auth / gated repo
            raise _access_error(exc) from exc

        # pyannote returns None instead of raising when the repo is gated and
        # the token cannot see it.
        if pipeline is None:
            raise _access_error(RuntimeError("Pipeline.from_pretrained returned None"))

        pipeline.to(torch.device("cpu"))
        _pipeline = pipeline
        log.info(
            "loaded %s on cpu (%d torch threads) in %.1fs",
            PIPELINE_NAME,
            threads,
            time.perf_counter() - started,
        )
        return _pipeline


# --------------------------------------------------------------------------
# progress plumbing
# --------------------------------------------------------------------------

# Rough share of total runtime per pipeline step, used to turn the per-step
# counters pyannote emits into one monotonic 0..1 fraction.
_STEP_WEIGHTS = (
    ("segmentation", 0.45),
    ("speaker_counting", 0.05),
    ("embeddings", 0.45),
    ("discrete_diarization", 0.05),
)


def _callback_arity(fn: Callable) -> int:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return 1
    count = 0
    for param in params:
        if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            count += 1
        elif param.kind is param.VAR_POSITIONAL:
            return 2
    return count


class _ProgressHook:
    """Adapts the pyannote hook protocol to ``progress_cb``.

    ``progress_cb`` may take one argument, in which case it receives the raw
    event dict ``{"step", "fraction", "completed", "total"}``, or two or more,
    in which case it is called as ``cb(step_name: str, fraction: float)``.
    Callback failures are logged and swallowed: progress reporting must never
    abort a diarization run.
    """

    def __init__(self, progress_cb: Callable):
        self._cb = progress_cb
        self._arity = _callback_arity(progress_cb)
        self._fraction = 0.0

    def __enter__(self) -> "_ProgressHook":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._emit("done", 1.0, None, None)

    def __call__(
        self,
        step_name: str,
        step_artifact: Any = None,
        file: Any = None,
        total: Optional[int] = None,
        completed: Optional[int] = None,
    ) -> None:
        base = 0.0
        weight = 0.0
        for name, share in _STEP_WEIGHTS:
            if name == step_name:
                weight = share
                break
            base += share
        else:
            # Unknown step: hold position rather than jumping to the end.
            base = self._fraction
            weight = 0.0

        within = 0.0
        if total:
            within = max(0.0, min(1.0, (completed or 0) / total))
        elif completed is not None:
            within = 1.0

        fraction = min(1.0, base + weight * within)
        # Never let the bar go backwards when steps report out of order.
        self._fraction = max(self._fraction, fraction)
        self._emit(step_name, self._fraction, completed, total)

    def _emit(
        self,
        step: str,
        fraction: float,
        completed: Optional[int],
        total: Optional[int],
    ) -> None:
        try:
            if self._arity >= 2:
                self._cb(step, fraction)
            else:
                self._cb(
                    {
                        "step": step,
                        "fraction": fraction,
                        "completed": completed,
                        "total": total,
                    }
                )
        except Exception:
            log.warning("diarization progress callback failed", exc_info=True)


# --------------------------------------------------------------------------
# diarization
# --------------------------------------------------------------------------

def _merge_turns(turns: list[dict], gap: float = MERGE_GAP) -> list[dict]:
    """Sort by start time and fuse same-speaker turns separated by < ``gap``."""
    ordered = sorted(turns, key=lambda t: (t["start"], t["end"]))
    merged: list[dict] = []
    for turn in ordered:
        if merged:
            last = merged[-1]
            if last["speaker"] == turn["speaker"] and turn["start"] - last["end"] < gap:
                last["end"] = max(last["end"], turn["end"])
                continue
        merged.append(dict(turn))
    return merged


def diarize(
    wav_path: str,
    num_speakers: Optional[int] = None,
    min_speakers: Optional[int] = None,
    max_speakers: Optional[int] = None,
    progress_cb: Optional[Callable] = None,
) -> list[dict]:
    """Diarize a 16 kHz mono WAV.

    Returns ``[{"start": float, "end": float, "speaker": "SPEAKER_00"}, ...]``
    sorted by start time, with same-speaker turns closer than 0.3 s fused.

    ``num_speakers`` pins the count exactly; ``min_speakers``/``max_speakers``
    bound it. Passing none of them lets the pipeline decide.
    """
    path = Path(wav_path)
    if not path.exists():
        raise DiarizationError(f"audio file not found: {path}")
    if path.stat().st_size == 0:
        raise DiarizationError(f"audio file is empty: {path}")

    pipeline = load_pipeline()

    params: dict[str, int] = {}
    if num_speakers is not None:
        params["num_speakers"] = int(num_speakers)
    else:
        if min_speakers is not None:
            params["min_speakers"] = int(min_speakers)
        if max_speakers is not None:
            params["max_speakers"] = int(max_speakers)

    started = time.perf_counter()
    hook = _ProgressHook(progress_cb) if progress_cb is not None else None
    try:
        if hook is not None:
            with hook:
                annotation = pipeline(str(path), hook=hook, **params)
        else:
            annotation = pipeline(str(path), **params)
    except DiarizationError:
        raise
    except Exception as exc:
        raise DiarizationError(
            f"diarization failed on {path.name}: {type(exc).__name__}: {exc}"
        ) from exc

    raw = [
        {"start": float(seg.start), "end": float(seg.end), "speaker": str(label)}
        for seg, _track, label in annotation.itertracks(yield_label=True)
    ]
    turns = _merge_turns(raw)

    elapsed = time.perf_counter() - started
    speakers = sorted({t["speaker"] for t in turns})
    log.info(
        "diarized %s in %.1fs: %d turns, %d speaker(s) %s",
        path.name,
        elapsed,
        len(turns),
        len(speakers),
        speakers,
    )
    return turns


# --------------------------------------------------------------------------
# transcript / diarization alignment
# --------------------------------------------------------------------------

def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _nearest_turn(mid: float, turns: Sequence[dict]) -> Optional[dict]:
    best = None
    best_distance = None
    for turn in turns:
        if turn["start"] <= mid <= turn["end"]:
            distance = 0.0
        else:
            distance = min(abs(turn["start"] - mid), abs(turn["end"] - mid))
        if best_distance is None or distance < best_distance:
            best, best_distance = turn, distance
    return best


def assign_speakers(segments: list[dict], turns: list[dict]) -> list[dict]:
    """Label Whisper segments with diarization speakers.

    ``segments`` are ``{"id", "start", "end", "text"}`` dicts; ``turns`` come
    from :func:`diarize`. Each segment takes the raw speaker it overlaps most,
    falling back to the nearest turn by midpoint when there is no overlap at
    all. Speakers are then renumbered by first appearance so the transcript
    reads "Speaker 1", "Speaker 2", ...

    Every returned segment is a shallow copy carrying two extra keys:

    * ``speaker``    - display label, e.g. ``"Speaker 2"``
    * ``speaker_id`` - stable key, e.g. ``"S2"``. Use it when the user renames
      a speaker so the mapping survives a re-render of the transcript.
    """
    out = [dict(segment) for segment in segments]
    if not out:
        return out

    for segment in out:
        start = float(segment.get("start", 0.0) or 0.0)
        end = float(segment.get("end", start) or start)
        if end < start:
            start, end = end, start

        raw: Optional[str] = None
        if turns:
            totals: dict[str, float] = {}
            for turn in turns:
                overlap = _overlap(start, end, turn["start"], turn["end"])
                if overlap > 0:
                    totals[turn["speaker"]] = totals.get(turn["speaker"], 0.0) + overlap
            if totals:
                raw = max(totals.items(), key=lambda kv: kv[1])[0]
            else:
                nearest = _nearest_turn((start + end) / 2.0, turns)
                raw = nearest["speaker"] if nearest else None
        segment["_raw_speaker"] = raw

    # Renumber by first appearance in time order, but keep the ordering the
    # caller handed us in the result.
    order: dict[str, int] = {}
    for segment in sorted(
        out, key=lambda s: (float(s.get("start", 0.0) or 0.0), s.get("id", 0))
    ):
        raw = segment["_raw_speaker"]
        if raw is not None and raw not in order:
            order[raw] = len(order) + 1

    for segment in out:
        raw = segment.pop("_raw_speaker")
        index = order.get(raw, 1) if raw is not None else 1
        segment["speaker_id"] = f"S{index}"
        segment["speaker"] = f"Speaker {index}"

    return out


def speaker_summary(segments: list[dict]) -> list[dict]:
    """Per-speaker talk time and turn count, ordered by speaker number.

    A turn is a run of consecutive segments (in time order) by the same
    speaker, so back-and-forth dialogue counts more turns than one monologue
    split across many Whisper segments.
    """
    ordered = sorted(
        segments, key=lambda s: (float(s.get("start", 0.0) or 0.0), s.get("id", 0))
    )
    stats: dict[str, dict] = {}
    previous: Optional[str] = None

    for segment in ordered:
        speaker = segment.get("speaker") or "Speaker 1"
        entry = stats.setdefault(
            speaker,
            {
                "speaker": speaker,
                "speaker_id": segment.get("speaker_id") or "S1",
                "talk_time": 0.0,
                "turn_count": 0,
                "segment_count": 0,
            },
        )
        start = float(segment.get("start", 0.0) or 0.0)
        end = float(segment.get("end", start) or start)
        entry["talk_time"] += max(0.0, end - start)
        entry["segment_count"] += 1
        if speaker != previous:
            entry["turn_count"] += 1
        previous = speaker

    def sort_key(entry: dict) -> tuple:
        sid = str(entry.get("speaker_id") or "")
        return (int(sid[1:]) if sid[1:].isdigit() else 9999, entry["speaker"])

    result = sorted(stats.values(), key=sort_key)
    for entry in result:
        entry["talk_time"] = round(entry["talk_time"], 3)
    return result
