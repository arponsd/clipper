"""Voice stage: narration text -> Voicebox -> fitted voice.wav (never longer than the clip)."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .. import ffmpeg
from ..config import VoiceProfile
from ..interfaces import ProgressFn, ShortenFn, VoiceboxAPI
from ..voicebox import VoiceboxError

VOICE_SAMPLE_RATE = 44100
FADE_SECONDS = 0.3
# A take shorter than this is treated as "Voicebox returned nothing" (wrong profile/engine, server bug).
MIN_VOICE_SECONDS = 0.2
# Tolerance so rounding in ffprobe durations does not trigger a needless atempo pass.
_EPS = 0.02


class VoiceError(RuntimeError):
    """Voice stage failure that is not a Voicebox transport error."""


class VoiceResult(BaseModel):
    path: str  # absolute path of the fitted voice.wav
    narration: str  # text actually spoken (after any shortening)
    narration_hash: str
    raw_duration: float  # duration of the final generated take before fitting
    final_duration: float
    clip_duration: float
    tempo: float
    shortened: bool
    trimmed: bool


def narration_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def engine_for(lang: str, voice: VoiceProfile) -> str:
    """Hindi always uses Chatterbox (multilingual); other languages use the configured engine."""
    if lang == "hi":
        return "chatterbox"
    return voice.engine or "chatterbox"


def _count_words(text: str) -> int:
    return len(text.split())


def _settings_key(lang: str) -> str:
    return "hindi" if lang == "hi" else "english"


def make_voice(*, client: VoiceboxAPI, narration: str, lang: str, voice: VoiceProfile,
               clip_duration: float, out_dir: Path, max_speedup: float,
               shorten: ShortenFn | None = None, on_progress: ProgressFn | None = None) -> VoiceResult:
    """Generate the narration voice and fit it to ``clip_duration``.

    Fitting order: as-is if it fits -> atempo (<= max_speedup) -> shorten once via ``shorten`` and
    regenerate -> atempo max_speedup + trim to clip with a 0.3 s fade-out.
    """
    progress = on_progress or (lambda _f, _m="": None)
    text = narration.strip()
    if not text:
        raise VoiceError("narration is empty - edit script.json and add narration text")
    if not voice.profile_id:
        key = _settings_key(lang)
        raise VoiceboxError(
            f"no Voicebox profile set for {lang}: run `autoclipper profiles` then "
            f"`autoclipper settings set voicebox.{key}.profile_id=<id>`")
    if clip_duration <= 0:
        raise VoiceError(f"clip duration must be positive, got {clip_duration}")
    max_speedup = max(1.0, max_speedup)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / "voice_raw.wav"
    norm = out_dir / "voice_norm.wav"
    work = out_dir / "voice_work.wav"
    final = out_dir / "voice.wav"
    engine = engine_for(lang, voice)

    def generate(t: str, lo: float, hi: float) -> float:
        def sub(f: float, msg: str) -> None:
            progress(lo + (hi - lo) * min(max(f, 0.0), 1.0), msg)

        client.synthesize(t, profile_id=voice.profile_id, language=lang, engine=engine,
                          dst=raw, on_progress=sub)
        ffmpeg.to_wav(raw, norm, sample_rate=VOICE_SAMPLE_RATE, channels=1)
        return ffmpeg.duration(norm)

    def check_not_empty(d: float) -> None:
        if d < MIN_VOICE_SECONDS:
            for tmp in (norm, raw, work):
                tmp.unlink(missing_ok=True)
            raise VoiceError(f"Voicebox returned empty audio ({d:.1f}s) for {lang}; check the profile/engine")

    progress(0.0, "generating voice")
    d = generate(text, 0.0, 0.6)
    check_not_empty(d)
    shortened = False

    if d > clip_duration * max_speedup + _EPS and shorten is not None:
        max_words = math.floor(_count_words(text) * clip_duration * max_speedup / d * 0.95)
        max_words = max(1, max_words)
        progress(0.6, f"voice {d:.1f}s > clip {clip_duration:.1f}s, shortening to {max_words} words")
        candidate = (shorten(text, max_words) or "").strip()
        if candidate and candidate != text:
            text = candidate
            shortened = True
            d = generate(text, 0.6, 0.85)
            check_not_empty(d)

    progress(0.85, "fitting voice to clip")
    tempo = 1.0
    trimmed = False
    if d <= clip_duration + _EPS:
        shutil.copyfile(norm, final)
    elif d / clip_duration <= max_speedup:
        tempo = d / clip_duration
        ffmpeg.change_tempo(norm, work, tempo)
        work.replace(final)  # os.replace: overwrites on Windows too
    else:
        tempo = max_speedup
        if tempo > 1.0:
            ffmpeg.change_tempo(norm, work, tempo)
            src = work
        else:
            src = norm
        ffmpeg.trim_with_fade(src, final, clip_duration, FADE_SECONDS)
        trimmed = True

    for tmp in (norm, work):
        tmp.unlink(missing_ok=True)

    result = VoiceResult(
        path=str(final.resolve()), narration=text, narration_hash=narration_hash(text),
        raw_duration=round(d, 3), final_duration=round(ffmpeg.duration(final), 3),
        clip_duration=round(clip_duration, 3), tempo=round(tempo, 4),
        shortened=shortened, trimmed=trimmed)
    (out_dir / "voice.json").write_text(
        json.dumps(result.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8")
    progress(1.0, "voice ready")
    return result


def load_voice_result(out_dir: Path) -> VoiceResult | None:
    """voice.json if present, valid and its voice.wav still exists; else None."""
    p = Path(out_dir) / "voice.json"
    if not p.is_file():
        return None
    try:
        result = VoiceResult.model_validate_json(p.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError):
        return None
    wav = Path(out_dir) / "voice.wav"
    if not wav.is_file() or wav.stat().st_size == 0:
        return None
    return result
