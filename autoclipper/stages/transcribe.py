"""Speech-to-text with faster-whisper (word timestamps, VAD), plus voice-over word timing."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import WhisperSettings
from ..interfaces import ProgressFn, Transcriber
from ..models import Segment, Transcript, Word


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


class WhisperTranscriber:
    """`interfaces.Transcriber` using faster-whisper. The model is loaded on first use."""

    def __init__(self, settings: WhisperSettings):
        self.settings = settings
        self._model: Any | None = None

    def _load(self) -> Any:
        if self._model is None:
            device, compute = resolve_device(self.settings)
            try:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(self.settings.model, device=device, compute_type=compute)
            except Exception as e:  # noqa: BLE001
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
        model = self._load()
        lang = language or self.settings.language or None
        try:
            seg_iter, info = model.transcribe(str(audio), language=lang, word_timestamps=True,
                                              vad_filter=True)
            total = float(getattr(info, "duration", 0.0) or 0.0)
            segments: list[Segment] = []
            for s in seg_iter:
                words = [Word(start=float(w.start), end=float(w.end), text=str(w.word).strip())
                         for w in (getattr(s, "words", None) or []) if str(w.word).strip()]
                text = str(s.text).strip()
                if text or words:
                    segments.append(Segment(start=float(s.start), end=float(s.end), text=text,
                                            words=words))
                if on_progress and total > 0:
                    on_progress(min(1.0, float(s.end) / total), "transcribing")
        except TranscribeError:
            raise
        except Exception as e:  # noqa: BLE001
            raise TranscribeError(f"transcription of {audio.name} failed: {e}") from e
        detected = getattr(info, "language", None) or lang or "en"
        if on_progress:
            on_progress(1.0, "transcribed")
        return Transcript(language=str(detected), duration=total, segments=segments)


def load_transcript(path: Path) -> Transcript:
    try:
        return Transcript.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise TranscribeError(f"could not read transcript {path}: {e}; delete it to re-transcribe") from e


def save_transcript(t: Transcript, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(t.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_transcribe(audio: Path, out_json: Path, transcriber: Transcriber, *,
                   language: str | None = None, on_progress: ProgressFn | None = None) -> Transcript:
    """Idempotent: reuse out_json if present, else transcribe `audio` and write it."""
    out_json = Path(out_json)
    if out_json.is_file() and out_json.stat().st_size > 0:
        if on_progress:
            on_progress(1.0, "transcript exists")
        return load_transcript(out_json)
    t = transcriber.transcribe(Path(audio), language=language, on_progress=on_progress)
    save_transcript(t, out_json)
    return t


def transcribe_voice(voice_wav: Path, lang: str, transcriber: Transcriber) -> list[Word]:
    """Flat word list of a voice-over (language forced to `lang`) for caption timing."""
    t = transcriber.transcribe(Path(voice_wav), language=lang)
    return [w for seg in t.segments for w in seg.words]
