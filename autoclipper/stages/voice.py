"""Voice stage: narration text -> Voicebox -> fitted voice.wav (never longer than the clip)."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .. import ffmpeg
from ..config import VoiceProfile
from ..interfaces import LengthenFn, ProgressFn, ShortenFn, VoiceboxAPI
from ..voicebox import VoiceboxError, VoiceboxTimeout
from ._atomic import write_text_atomic

log = logging.getLogger(__name__)

VOICE_SAMPLE_RATE = 44100
FADE_SECONDS = 0.3
# A take shorter than this is treated as "Voicebox returned nothing" (wrong profile/engine, server bug).
MIN_VOICE_SECONDS = 0.2
# Tolerance so rounding in ffprobe durations does not trigger a needless atempo pass.
_EPS = 0.02
# A take shorter than this fraction of what its word count needs (words / words-per-second) was cut short
# by the TTS engine (Chatterbox sometimes stops after a few sentences): it is not a "too short script".
TRUNCATED_BELOW = 0.5


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
    lengthened: bool = False  # a too-short take was replaced by a lengthened narration (step 2)
    # When trimmed: the estimated prefix of `narration` that is actually heard before the cut.
    # Captions use it instead of `narration`. None for untrimmed takes and older voice.json files.
    spoken_narration: str | None = None
    # Things the user should know about this take (e.g. the lengthen retry timed out); shown in the UI.
    warnings: list[str] = []
    # Voicebox stopped early: the take is far shorter than the text needs (see looks_truncated). Captions
    # then show only `spoken_narration` (the estimated spoken prefix), as for a trimmed take.
    truncated: bool = False


def narration_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def engine_for(lang: str, voice: VoiceProfile) -> str:
    """Hindi always uses Chatterbox (multilingual); other languages use the configured engine."""
    if lang == "hi":
        return "chatterbox"
    return voice.engine or "chatterbox"


def _count_words(text: str) -> int:
    return len(text.split())


def estimate_spoken_prefix(text: str, raw_duration: float, tempo: float, clip_duration: float) -> str:
    """Words of ``text`` that end before ``clip_duration`` in a take of ``raw_duration`` played at ``tempo``.

    The sped-up take (raw_duration / tempo) is spread over the words by character length, the same
    way captions.distribute_words does, and words whose estimated end is <= clip_duration are kept.
    At least the first word is kept.
    """
    words = text.split()
    if not words:
        return ""
    spoken = raw_duration / max(tempo, 1e-6)
    total = sum(max(1, len(w)) for w in words)
    kept: list[str] = []
    t = 0.0
    for w in words:
        t += spoken * max(1, len(w)) / total
        if t > clip_duration + 1e-6:
            break
        kept.append(w)
    return " ".join(kept or words[:1])


def expected_seconds(text: str, wps: float) -> float:
    """Seconds a narration of ``text`` needs at ``wps`` words per second."""
    return _count_words(text) / max(wps, 1e-6)


def looks_truncated(text: str, duration: float, wps: float | None) -> bool:
    """True when a take of ``duration`` seconds is far shorter than ``text`` needs (TTS stopped early)."""
    if not wps or _count_words(text) < 8:
        return False
    return duration < TRUNCATED_BELOW * expected_seconds(text, wps)


def truncated_prefix(text: str, duration: float, wps: float) -> str:
    """The words of ``text`` a cut-short take of ``duration`` seconds probably says (at ``wps``)."""
    return estimate_spoken_prefix(text, expected_seconds(text, wps), 1.0, duration)


def _settings_key(lang: str) -> str:
    return "hindi" if lang == "hi" else "english"


def make_voice(*, client: VoiceboxAPI, narration: str, lang: str, voice: VoiceProfile,
               clip_duration: float, out_dir: Path, max_speedup: float,
               shorten: ShortenFn | None = None, on_progress: ProgressFn | None = None,
               lengthen: LengthenFn | None = None, lengthen_below: float = 0.75,
               fill_ratio: float = 0.88, lengthen_timeout: float | None = None,
               wps: float | None = None) -> VoiceResult:
    """Generate the narration voice and fit it to ``clip_duration``.

    Fitting order: as-is if it fits -> atempo (<= max_speedup) -> shorten once via ``shorten`` and
    regenerate -> atempo max_speedup + trim to clip with a 0.3 s fade-out.

    Too short: when ``lengthen`` is given and the take is shorter than clip x ``lengthen_below``,
    the narration is lengthened once (to fill clip x ``fill_ratio``) and regenerated. The longer
    take is kept only if it fits within max_speedup; otherwise (or on any error) the original take
    is kept, with a warning in voice.json. A lengthen never leads to a shorten or a trim.
    ``lengthen_timeout`` limits the Voicebox wait of that retry (voicebox.lengthen_timeout_seconds).

    Cut short: with ``wps`` (the language's speaking rate), a take under TRUNCATED_BELOW x the time its
    words need was stopped early by the TTS engine. The same text is generated once more and the longer
    take kept; if that is still cut short the take is marked ``truncated`` (captions show only the spoken
    prefix), a warning is added, and it is never lengthened (a longer text would be cut short too).
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

    def generate(t: str, lo: float, hi: float, timeout: float | None = None) -> float:
        def sub(f: float, msg: str) -> None:
            progress(lo + (hi - lo) * min(max(f, 0.0), 1.0), msg)

        kw = {"timeout": timeout} if timeout is not None and _accepts_timeout(client) else {}
        client.synthesize(t, profile_id=voice.profile_id, language=lang, engine=engine,
                          dst=raw, on_progress=sub, **kw)
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

    lengthened = False
    warnings: list[str] = []
    truncated = False
    if looks_truncated(text, d, wps):
        d, truncated = _retry_truncated(text=text, d=d, wps=wps, generate=generate, raw=raw, norm=norm,
                                        out_dir=out_dir, progress=progress, warnings=warnings)

    if truncated:
        pass  # never lengthen a cut-short take: the longer text would be cut short as well
    elif lengthen is not None and d < clip_duration * lengthen_below:
        text, d, lengthened = _try_lengthen(
            text=text, d=d, clip_duration=clip_duration, max_speedup=max_speedup, fill_ratio=fill_ratio,
            lengthen=lengthen, generate=generate, raw=raw, norm=norm, out_dir=out_dir, progress=progress,
            timeout=lengthen_timeout, warnings=warnings)
    elif d > clip_duration * max_speedup + _EPS and shorten is not None:
        max_words = math.floor(_count_words(text) * clip_duration * max_speedup / d * 0.95)
        max_words = max(1, max_words)
        progress(0.6, f"voice {d:.1f}s > clip {clip_duration:.1f}s, shortening to {max_words} words")
        try:
            candidate = (shorten(text, max_words) or "").strip()
        except Exception as e:  # noqa: BLE001 - refusal, network, bad output: fall back to tempo + trim
            msg = f"shortening failed: {e}; speeding up and trimming instead"
            log.warning(msg)
            progress(0.6, msg)
            candidate = ""
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
        shortened=shortened, trimmed=trimmed, lengthened=lengthened,
        spoken_narration=(estimate_spoken_prefix(text, d, tempo, clip_duration) if trimmed
                          else truncated_prefix(text, d, wps) if truncated and wps else None),
        warnings=warnings, truncated=truncated)
    write_text_atomic(out_dir / "voice.json",
                      json.dumps(result.model_dump(), indent=2, ensure_ascii=False))
    progress(1.0, "voice ready")
    return result


def _retry_truncated(*, text: str, d: float, wps: float, generate, raw: Path, norm: Path, out_dir: Path,
                     progress: ProgressFn, warnings: list[str]) -> tuple[float, bool]:
    """Generate a cut-short take once more; keep the longer one. Returns (duration, still truncated)."""
    need = expected_seconds(text, wps)
    progress(0.6, f"voice {d:.1f}s is far shorter than the {_count_words(text)}-word text needs "
                  f"(~{need:.0f}s): Voicebox stopped early, generating it again")
    log.warning("voice take %.1fs for a %d-word text (~%.0fs expected): cut short, retrying once",
                d, _count_words(text), need)
    backup_raw = out_dir / "voice_raw.first.wav"
    backup_norm = out_dir / "voice_norm.first.wav"
    shutil.copyfile(raw, backup_raw)
    shutil.copyfile(norm, backup_norm)
    try:
        try:
            d2 = generate(text, 0.6, 0.8)
        except Exception as e:  # noqa: BLE001 - the first take is still usable
            log.warning("regenerating the cut-short voice failed: %s", e)
            d2 = 0.0
        if d2 > d:
            d = d2
        else:
            backup_raw.replace(raw)
            backup_norm.replace(norm)
    finally:
        backup_raw.unlink(missing_ok=True)
        backup_norm.unlink(missing_ok=True)
    if not looks_truncated(text, d, wps):
        return d, False
    warnings.append(f"Voicebox cut the narration short twice: the voice is {d:.1f}s but the "
                    f"{_count_words(text)}-word text needs about {need:.0f}s. Captions show only the part that "
                    "is spoken; shorten the narration (Review & edit) or re-render to try again")
    return d, True


def _try_lengthen(*, text: str, d: float, clip_duration: float, max_speedup: float, fill_ratio: float,
                  lengthen: LengthenFn, generate, raw: Path, norm: Path, out_dir: Path,
                  progress: ProgressFn, timeout: float | None = None,
                  warnings: list[str] | None = None) -> tuple[str, float, bool]:
    """One lengthen retry. Returns (text, duration, lengthened); the original take's files are
    restored whenever the longer take is not used (and a warning is added to ``warnings``)."""
    warnings = warnings if warnings is not None else []
    kept = f"kept the original {d:.1f}s voice ({d / clip_duration:.0%} of the {clip_duration:.1f}s clip)"
    words = _count_words(text)
    target = max(words + 1, math.ceil(words * clip_duration * fill_ratio / max(d, 1e-6)))
    progress(0.6, f"voice {d:.1f}s fills only {d / clip_duration:.0%} of the {clip_duration:.1f}s clip, "
                  f"lengthening to {target} words")
    try:
        candidate = " ".join((lengthen(text, target) or "").split())
    except Exception as e:  # noqa: BLE001 - refusal, network, bad output: keep the original take
        msg = f"lengthening failed: {e}; keeping the original voice"
        log.warning(msg)
        progress(0.6, msg)
        warnings.append(f"narration could not be lengthened ({_one_line(e)}); {kept}")
        return text, d, False
    if not candidate or candidate == " ".join(text.split()) or _count_words(candidate) <= words:
        log.warning("lengthen returned no longer narration; keeping the original voice")
        warnings.append(f"narration could not be lengthened (no longer text); {kept}")
        return text, d, False

    backup_raw = out_dir / "voice_raw.orig.wav"
    backup_norm = out_dir / "voice_norm.orig.wav"
    shutil.copyfile(raw, backup_raw)
    shutil.copyfile(norm, backup_norm)

    def restore(reason: str) -> tuple[str, float, bool]:
        log.warning("%s; keeping the original voice", reason)
        progress(0.85, f"{reason}; keeping the original voice")
        warnings.append(f"{reason}; {kept}")
        backup_raw.replace(raw)
        backup_norm.replace(norm)
        return text, d, False

    try:
        try:
            d2 = generate(candidate, 0.6, 0.85, timeout)
        except Exception as e:  # noqa: BLE001 - the original take is still good
            if isinstance(e, VoiceboxTimeout):
                return restore(f"the lengthened voice took longer than {timeout:.0f}s "
                               "(voicebox.lengthen_timeout_seconds) and was cancelled")
            return restore(f"generating the lengthened voice failed: {_one_line(e)}")
        if d2 < MIN_VOICE_SECONDS:
            return restore(f"the lengthened voice was empty ({d2:.1f}s)")
        if d2 > clip_duration * max_speedup + _EPS:
            return restore(f"the lengthened voice ({d2:.1f}s) does not fit the {clip_duration:.1f}s clip "
                           f"even at x{max_speedup:g}")
        return candidate, d2, True
    finally:
        backup_raw.unlink(missing_ok=True)
        backup_norm.unlink(missing_ok=True)


def _one_line(e: BaseException, limit: int = 200) -> str:
    text = " ".join(str(e).split()) or type(e).__name__
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _accepts_timeout(client: object) -> bool:
    """Whether client.synthesize takes ``timeout=`` (VoiceboxClient does; older fakes may not)."""
    import inspect

    try:
        params = inspect.signature(client.synthesize).parameters  # type: ignore[attr-defined]
    except (TypeError, ValueError, AttributeError):
        return False
    return "timeout" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


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
