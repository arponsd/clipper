"""Injectable interfaces shared by stages, the pipeline and tests (owned by the team lead).

Concrete implementations:
    LLM          -> autoclipper.llm.AnthropicLLM
    VoiceboxAPI  -> autoclipper.voicebox.VoiceboxClient
    Transcriber  -> autoclipper.stages.transcribe.WhisperTranscriber
Fakes for tests live in tests/fakes.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from .models import ClipScript, Moment, Transcript

# on_progress(fraction 0..1, short human message)
ProgressFn = Callable[[float, str], None]
# shorten(narration, max_words) -> shorter narration (same language)
ShortenFn = Callable[[str, int], str]
# lengthen(narration, target_words) -> longer narration (same language), step 2 product fix
LengthenFn = Callable[[str, int], str]
# log(line) - pipeline/CLI progress lines
LogFn = Callable[[str], None]


def noop_progress(_fraction: float, _message: str = "") -> None:
    return None


@runtime_checkable
class LLM(Protocol):
    def pick_moments(self, transcript_text: str, *, count: int, min_seconds: float,
                     max_seconds: float, video_duration: float) -> list[Moment]:
        """Raw candidate moments (unvalidated; moments stage post-validates)."""
        ...

    def write_script(self, *, lang: str, clip_transcript: str, moment: Moment,
                     duration: float, target_words: int,
                     target_seconds: float | None = None) -> ClipScript:
        """Narration (new commentary, not a dub) + title/description/hashtags in `lang`.

        target_seconds (step 2, optional): the spoken length the narration should fill.
        """
        ...

    def shorten(self, *, lang: str, narration: str, max_words: int) -> str:
        """Return a shorter narration with at most max_words words, same language/meaning."""
        ...


@runtime_checkable
class Lengthener(Protocol):
    """Optional LLM capability (step 2). Not part of LLM so older fakes/clients stay valid;
    callers use getattr(llm, "lengthen", None)."""

    def lengthen(self, *, lang: str, narration: str, target_words: int) -> str:
        """Return a longer narration of about target_words words, same language/meaning/tone."""
        ...


@runtime_checkable
class VoiceboxAPI(Protocol):
    def health(self) -> None:
        """Raise VoiceboxError('Voicebox is not reachable at <url> - is it running?') if down."""
        ...

    def list_profiles(self) -> list[dict]:
        ...

    def synthesize(self, text: str, *, profile_id: str, language: str, engine: str,
                   dst: Path, on_progress: ProgressFn | None = None) -> Path:
        """generate -> poll history until completed -> download audio bytes to dst. Returns dst.
        A client may also accept ``timeout=`` (seconds) for one call; make_voice passes it only then."""
        ...


@runtime_checkable
class Transcriber(Protocol):
    def transcribe(self, audio: Path, language: str | None = None,
                   on_progress: ProgressFn | None = None) -> Transcript:
        """Word-timestamped transcript. language=None -> auto-detect."""
        ...


@runtime_checkable
class CaptionsOnlyLLM(Protocol):
    """Optional LLM capabilities for voice-over off (captions only). Not part of LLM so older fakes and
    clients stay valid; callers use getattr(llm, ..., None) and fall back when they are missing."""

    def write_publish_metadata(self, *, lang: str, clip_transcript: str, moment: Moment,
                               duration: float) -> ClipScript:
        """Title/description/hashtags in `lang` for a clip posted with its original audio (narration "")."""
        ...

    def translate_segments(self, *, lang: str, source_lang: str, segments: list[dict]) -> dict[int, str]:
        """Translate caption lines [{"id": int, "text": str}] into `lang`; returns {id: translated text}."""
        ...
