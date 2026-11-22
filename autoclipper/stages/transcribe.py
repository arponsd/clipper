"""Speech-to-text with faster-whisper (word timestamps, VAD), plus voice-over word timing."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import wave
from pathlib import Path
from typing import Any

import numpy as np

from .. import ffmpeg
from ..config import WhisperSettings
from ..interfaces import ProgressFn, Transcriber
from ..models import Segment, Transcript, Word
from ._atomic import write_text_atomic

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
WINDOWING_THRESHOLD = 20 * 60.0  # seconds; longer audio is transcribed in windows
WINDOW_SECONDS = 15 * 60.0
SNAP_SECONDS = 10.0  # window cuts move to the quietest point within +/- this


class TranscribeError(RuntimeError):
    """Transcription failed (model load, decoding, unreadable transcript.json)."""


def _cuda_device_count() -> int:
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:  # noqa: BLE001 - no ctranslate2 / broken CUDA -> treat as no GPU
        return 0


def resolve_device(settings: WhisperSettings) -> tuple[str, str]:
    """Return (device, compute_type). "auto" -> cuda/float16 if a CUDA device exists, else cpu/int8."""
    device = (settings.device or "auto").lower()
    if device == "auto":
        device = "cuda" if _cuda_device_count() > 0 else "cpu"
    compute = (settings.compute_type or "auto").lower()
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    return device, compute


_CUDA_LIB_ERROR = re.compile(r"cuda|cublas|cudnn|\.dll\b|\.so\b|\.so\.\d", re.IGNORECASE)


def _is_cuda_library_error(exc: BaseException) -> bool:
    """True when an error looks like a missing/broken CUDA runtime library (cuBLAS, cuDNN, .dll/.so)."""
    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if _CUDA_LIB_ERROR.search(str(e)):
            return True
        e = e.__cause__ or e.__context__
    return False


def _read_pcm16_mono_16k(path: Path) -> np.ndarray | None:
    """float32 samples of a 16 kHz mono s16le wav, or None if the wav has another format."""
    try:
        with wave.open(str(path), "rb") as w:
            if (w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2
                    or w.getcomptype() != "NONE"):
                return None
            data = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return None
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def _load_audio(path: Path) -> np.ndarray:
    """Decode `path` to 16 kHz mono float32 in [-1, 1).

    faster_whisper.decode_audio (PyAV) is never used: with PyAV 19 it fails with a TypeError
    ('metadata_errors'). A 16 kHz mono s16le wav (audio.wav) is read directly; anything else
    (voice.wav is 24/44.1 kHz) is converted with ffmpeg to a temp wav first.
    """
    path = Path(path)
    try:
        arr = _read_pcm16_mono_16k(path)
        if arr is not None:
            return arr
        with tempfile.TemporaryDirectory(prefix="autoclipper-whisper-") as d:
            tmp = Path(d) / "audio16k.wav"
            ffmpeg.to_wav(path, tmp, sample_rate=SAMPLE_RATE, channels=1)
            arr = _read_pcm16_mono_16k(tmp)
    except ffmpeg.FFmpegError as e:
        raise TranscribeError(f"could not decode audio {path.name}: {e}") from e
    except OSError as e:
        raise TranscribeError(f"could not read audio {path.name}: {e}") from e
    if arr is None:
        raise TranscribeError(f"could not decode audio {path.name} to 16 kHz mono PCM")
    return arr


def _split_points(audio: np.ndarray) -> list[tuple[int, int]]:
    """Sample ranges to transcribe. Audio up to WINDOWING_THRESHOLD is one range; longer audio is cut
    about every WINDOW_SECONDS at the quietest 20 ms frame within +/- SNAP_SECONDS of the boundary."""
    n = len(audio)
    if n <= WINDOWING_THRESHOLD * SAMPLE_RATE:
        return [(0, n)]
    window = int(WINDOW_SECONDS * SAMPLE_RATE)
    snap = int(SNAP_SECONDS * SAMPLE_RATE)
    frame = SAMPLE_RATE // 50  # 20 ms
    # stop cutting once the rest fits in one window plus a third (last window 5..20 min)
    tail = int(WINDOW_SECONDS * SAMPLE_RATE * 4 / 3)
    ranges: list[tuple[int, int]] = []
    pos = 0
    while n - pos > tail:
        target = pos + window
        lo, hi = max(pos + frame, target - snap), min(n - frame, target + snap)
        cut = target
        if hi - lo >= frame:
            seg = audio[lo:hi]
            k = len(seg) // frame
            energy = np.square(seg[: k * frame].astype(np.float32)).reshape(k, frame).mean(axis=1)
            cut = lo + int(np.argmin(energy)) * frame + frame // 2
        ranges.append((pos, cut))
        pos = cut
    ranges.append((pos, n))
    return ranges


class WhisperTranscriber:
    """`interfaces.Transcriber` using faster-whisper. The model is loaded on first use.

    If whisper.device is "auto" and resolves to CUDA but the CUDA libraries are missing or broken
    (model load or the first transcription fails with a cuBLAS/cuDNN/.dll/.so error), it logs a warning
    and retries once on cpu/int8.
    """

    def __init__(self, settings: WhisperSettings):
        self.settings = settings
        self._model: Any | None = None
        self._forced: tuple[str, str] | None = None  # set after a CUDA -> CPU fallback
        self._device: tuple[str, str] | None = None  # device the loaded model uses
        self._proven = False  # one transcription finished on the current model

    def _can_fall_back(self, device: str, exc: BaseException) -> bool:
        return ((self.settings.device or "auto").lower() == "auto" and device == "cuda"
                and self._forced is None and _is_cuda_library_error(exc))

    def _fall_back_to_cpu(self, exc: BaseException) -> None:
        log.warning("Whisper on CUDA failed (%s); falling back to cpu/int8", exc)
        self._model = None
        self._device = None
        self._proven = False
        self._forced = ("cpu", "int8")

    def _load(self) -> Any:
        if self._model is None:
            device, compute = self._forced or resolve_device(self.settings)
            try:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(self.settings.model, device=device, compute_type=compute)
                self._device = (device, compute)
            except Exception as e:  # noqa: BLE001
                if self._can_fall_back(device, e):
                    self._fall_back_to_cpu(e)
                    return self._load()
                raise TranscribeError(
                    f"could not load Whisper model {self.settings.model!r} on {device}/{compute}: {e}. "
                    "Check whisper.model in settings and that the model can be downloaded once."
                ) from e
        return self._model

    def transcribe(self, audio: Path, language: str | None = None,
                   on_progress: ProgressFn | None = None) -> Transcript:
        audio = Path(audio)
        if not audio.is_file():
            raise TranscribeError(f"audio file not found: {audio}")
        self._load()
        samples = _load_audio(audio)
        try:
            return self._transcribe_samples(samples, audio.name, language, on_progress)
        except TranscribeError as e:
            device = self._device[0] if self._device else ""
            cause = e.__cause__ or e  # skip our wrapper text (it holds the file name)
            if self._proven or not self._can_fall_back(device, cause):
                raise
            self._fall_back_to_cpu(cause)
            self._load()
            return self._transcribe_samples(samples, audio.name, language, on_progress)

    def _transcribe_samples(self, samples: np.ndarray, name: str, language: str | None,
                            on_progress: ProgressFn | None) -> Transcript:
        model = self._model
        lang = language or self.settings.language or None
        total = len(samples) / SAMPLE_RATE
        ranges = _split_points(samples)
        segments: list[Segment] = []
        detected: str | None = None
        try:
            for i, (a, b) in enumerate(ranges):
                offset, chunk_end = a / SAMPLE_RATE, b / SAMPLE_RATE
                chunk = samples if len(ranges) == 1 else samples[a:b]
                # after the first window, keep the language the first window detected
                seg_iter, info = model.transcribe(chunk, language=lang or detected, word_timestamps=True,
                                                  vad_filter=True)
                if detected is None:
                    detected = getattr(info, "language", None)
                for s in seg_iter:
                    def at(t: float) -> float:
                        return min(chunk_end, max(offset, offset + float(t)))

                    words = [Word(start=at(w.start), end=at(w.end), text=str(w.word).strip())
                             for w in (getattr(s, "words", None) or []) if str(w.word).strip()]
                    text = str(s.text).strip()
                    if text or words:
                        segments.append(Segment(start=at(s.start), end=at(s.end), text=text, words=words))
                    if on_progress and total > 0:
                        msg = "transcribing" if len(ranges) == 1 else f"transcribing ({i + 1}/{len(ranges)})"
                        on_progress(min(1.0, at(s.end) / total), msg)
                del seg_iter, chunk  # free this window before the next one
                if on_progress and total > 0 and len(ranges) > 1:
                    on_progress(min(1.0, chunk_end / total), f"transcribed {i + 1}/{len(ranges)}")
        except TranscribeError:
            raise
        except Exception as e:  # noqa: BLE001
            raise TranscribeError(f"transcription of {name} failed: {e}") from e
        self._proven = True
        if on_progress:
            on_progress(1.0, "transcribed")
        return Transcript(language=str(detected or lang or "en"), duration=total, segments=segments)


def load_transcript(path: Path) -> Transcript:
    try:
        return Transcript.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise TranscribeError(f"could not read transcript {path}: {e}; delete it to re-transcribe") from e


def save_transcript(t: Transcript, path: Path) -> Path:
    text = json.dumps(t.model_dump(), ensure_ascii=False, indent=2)
    return write_text_atomic(Path(path), text)


def run_transcribe(audio: Path, out_json: Path, transcriber: Transcriber, *,
                   language: str | None = None, on_progress: ProgressFn | None = None) -> Transcript:
    """Idempotent: reuse out_json if present, else transcribe `audio` and write it.

    An unreadable out_json (e.g. truncated by a crash) is renamed to transcript.json.corrupt and the
    audio is transcribed again.
    """
    out_json = Path(out_json)
    if out_json.is_file() and out_json.stat().st_size > 0:
        try:
            t = load_transcript(out_json)
        except TranscribeError as e:
            bad = out_json.with_name(out_json.name + ".corrupt")
            os.replace(out_json, bad)
            log.warning("%s; moved it to %s and transcribing again", e, bad.name)
            if on_progress:
                on_progress(0.0, f"{out_json.name} was unreadable (moved to {bad.name}); transcribing again")
        else:
            if on_progress:
                on_progress(1.0, "transcript exists")
            return t
    t = transcriber.transcribe(Path(audio), language=language, on_progress=on_progress)
    save_transcript(t, out_json)
    return t


def transcribe_voice(voice_wav: Path, lang: str, transcriber: Transcriber) -> list[Word]:
    """Flat word list of a voice-over (language forced to `lang`) for caption timing."""
    t = transcriber.transcribe(Path(voice_wav), language=lang)
    return [w for seg in t.segments for w in seg.words]
