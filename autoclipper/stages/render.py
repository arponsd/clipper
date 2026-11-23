"""Render stage: cut.mp4 + voice + burned captions -> final.mp4, plus per-language metadata.json."""
from __future__ import annotations

import json
import logging
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import config, ffmpeg
from ..ffmpeg import _AAC, _LIMITER, _MONO_TO_STEREO, _SILENCE, _X264, _AtomicOutput
from ..config import AudioSettings, Settings
from ..interfaces import ProgressFn
from ..models import ClipScript
from ._atomic import write_text_atomic

log = logging.getLogger(__name__)

_FONT_SUFFIXES = {".ttf", ".otf", ".ttc"}


class RenderError(RuntimeError):
    """Final render failed before ffmpeg ran (missing inputs)."""


def resolve_fonts_dir() -> Path | None:
    """assets/fonts if it holds any font file, else None (libass then uses system fontconfig)."""
    d = Path(config.FONTS_DIR)
    if not d.is_dir():
        return None
    if any(p.is_file() and p.suffix.lower() in _FONT_SUFFIXES for p in d.rglob("*")):
        return d
    return None


# silencedetect parameters used to find where the speech in voice.wav really ends
SILENCE_NOISE_DB = -45
SILENCE_MIN_SECONDS = 0.3
_SILENCE_RE = re.compile(r"silence_(start|end):\s*(-?[0-9.]+)")


def _detect_trailing_silence_start(voice: Path, total: float) -> float | None:
    """Start of the silence that runs to the end of ``voice`` (silencedetect), or None."""
    cmd = [ffmpeg.ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "info", "-i", str(voice),
           "-af", f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_SECONDS}", "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("silencedetect on %s failed (%s); using the full voice duration", voice.name, e)
        return None
    if proc.returncode != 0:
        log.warning("silencedetect on %s failed; using the full voice duration", voice.name)
        return None
    last_start: float | None = None
    last_end: float | None = None
    for kind, value in _SILENCE_RE.findall(proc.stderr):
        if kind == "start":
            last_start, last_end = float(value), None
        else:
            last_end = float(value)
    if last_start is None:
        return None
    # silence still open at EOF, or closed at (about) the end of the file
    if last_end is None or last_end >= total - 0.05:
        return max(0.0, last_start)
    return None


def speech_end(voice: Path, words_json: Path | None = None) -> float:
    """Seconds into ``voice`` where the narration ends.

    The last word end in words.json (next to the voice by default) when present and valid, else
    the voice duration with trailing silence removed. Never more than the voice duration.
    """
    voice = Path(voice)
    total = ffmpeg.duration(voice)
    wj = Path(words_json) if words_json is not None else voice.parent / "words.json"
    if wj.is_file():
        try:
            words = json.loads(wj.read_text(encoding="utf-8"))
            ends = [float(w["end"]) for w in words if isinstance(w, dict) and "end" in w]
            ends = [e for e in ends if math.isfinite(e) and e > 0]
            if ends:
                return min(total, max(ends))
        except (ValueError, TypeError, KeyError, OSError) as e:
            log.warning("could not read %s (%s); detecting the speech end from the audio", wj.name, e)
    silent_from = _detect_trailing_silence_start(voice, total)
    if silent_from is not None and silent_from > 0:
        return min(total, silent_from)
    return total


def duck_envelope(vs: float, ve: float, *, during: float, after: float, ramp: float) -> str:
    """Piecewise-linear gain expression of ``t`` for the volume filter (eval=frame).

    ``after`` outside the voice window, ``during`` inside [vs, ve]; the ramp down ENDS at vs (skipped
    when vs == 0) and the ramp up STARTS at ve, each ``ramp`` seconds long (0 = hard switch).
    """
    n = ffmpeg._num
    vs, ve, ramp = max(0.0, vs), max(0.0, ve), max(0.0, ramp)
    if ve <= vs:
        return n(after)
    d, a = n(during), n(after)
    # build from the end backwards: ... if(lt(t,X), <before X>, <from X on>)
    if ramp > 0:
        tail = f"if(lt(t,{n(ve + ramp)}),{d}+({n(after - during)})*(t-{n(ve)})/{n(ramp)},{a})"
    else:
        tail = a
    expr = f"if(lt(t,{n(ve)}),{d},{tail})"
    if vs > 0:
        rs = max(0.0, vs - ramp)
        if vs - rs > 0:
            down = f"{a}+({n(during - after)})*(t-{n(rs)})/{n(vs - rs)}"
            expr = f"if(lt(t,{n(rs)}),{a},if(lt(t,{n(vs)}),{down},{expr}))"
        else:
            expr = f"if(lt(t,{n(vs)}),{a},{expr})"
    return expr


def _post_chain(audio: AudioSettings, fade_out: tuple[float, float] | None = None) -> str:
    chain = []
    if audio.loudnorm:
        chain.append(f"loudnorm=I={ffmpeg._num(audio.target_lufs)}:TP=-1.5:LRA=11")
        chain.append("aresample=48000")
    chain.append(_LIMITER)
    if fade_out is not None and fade_out[1] > 0:  # after loudnorm, so the fade is not undone
        chain.append(f"afade=t=out:st={ffmpeg._num(fade_out[0])}:d={ffmpeg._num(fade_out[1])}")
    return ",".join(chain)


@dataclass
class MixGains:
    """Linear gains of the loudness mix (see loudness_gains)."""
    voice: float
    during: float  # background while the narration plays
    after: float  # background before / after the narration


def build_audio_graph(*, orig: str, orig_stereo: str, voice_label: str | None, audio: AudioSettings,
                      voice_start: float = 0.0, voice_end: float = 0.0, gains: MixGains | None = None,
                      duck_to_end: bool = False, fade_out: tuple[float, float] | None = None,
                      bed_pad: bool = False, outside: str | None = None, outside_stereo: str = "") -> str:
    """filter_complex audio part producing ``[a]`` (see CONTRACT_WEB section 9.1).

    ``gains`` (loudness mix) replaces the fixed audio.original_volume / original_volume_after /
    voice_volume factors. ``duck_to_end``: the background stays at the under-voice level to the end (a clip
    trimmed after a short narration). ``fade_out`` = (start, seconds) on the final mix. ``bed_pad``: pad
    the background with silence (a separated stem can be a few samples shorter than the video).
    ``outside`` (+ ``outside_stereo``): a second background used only OUTSIDE the narration (the clip's own
    audio when ``orig`` is a vocals-removed stem): the stem plays under the voice, the original before and
    after it, crossfaded with the same ramps.
    """
    post = _post_chain(audio, fade_out)
    during = gains.during if gains is not None else audio.original_volume
    after = gains.after if gains is not None else audio.original_volume_after
    voice_gain = gains.voice if gains is not None else audio.voice_volume
    if duck_to_end:
        after = during
    pad = ",apad" if bed_pad else ""
    if voice_label is None or voice_end <= voice_start:
        parts = [f"{orig}{orig_stereo}{pad},volume={ffmpeg._num(after)}"]
        if voice_label is not None:  # an empty voice still has to be consumed by the graph
            parts[0] += "[a0]"
            parts.append(f"{voice_label}{_MONO_TO_STEREO},volume={ffmpeg._num(voice_gain)}[a1]")
            parts.append(f"[a0][a1]amix=inputs=2:normalize=0:duration=first,{post}[a]")
        else:
            parts[0] += f",{post}[a]"
        return ";".join(parts)
    if outside is not None and not duck_to_end:
        ramp = audio.duck_ramp_seconds
        env_in = duck_envelope(voice_start, voice_end, during=during, after=0.0, ramp=ramp)
        env_out = duck_envelope(voice_start, voice_end, during=0.0, after=after, ramp=ramp)
        return ";".join([
            f"{outside}{outside_stereo},volume='{env_out}':eval=frame[b0]",
            f"{orig}{orig_stereo}{pad},volume='{env_in}':eval=frame[b1]",
            "[b0][b1]amix=inputs=2:normalize=0:duration=first[a0]",
            f"{voice_label}{_MONO_TO_STEREO},volume={ffmpeg._num(voice_gain)}[a1]",
            f"[a0][a1]amix=inputs=2:normalize=0:duration=first,{post}[a]",
        ])
    env = duck_envelope(voice_start, voice_end, during=during, after=after, ramp=audio.duck_ramp_seconds)
    return ";".join([
        f"{orig}{orig_stereo}{pad},volume='{env}':eval=frame[a0]",
        f"{voice_label}{_MONO_TO_STEREO},volume={ffmpeg._num(voice_gain)}[a1]",
        f"[a0][a1]amix=inputs=2:normalize=0:duration=first,{post}[a]",
    ])


_MAX_VOICE_GAIN_DB = 30.0
_MAX_BED_BOOST_DB = 12.0  # never lift a quiet background by more than this
_MIN_BED_GAIN_DB = -60.0


def _db_to_lin(db: float) -> float:
    return 10 ** (db / 20.0)


def _lin_to_db(x: float) -> float:
    return 20 * math.log10(x) if x > 0 else -math.inf


def loudness_gains(audio: AudioSettings, *, voice_lufs: float | None, bed_during_lufs: float | None,
                   bed_lufs: float | None) -> MixGains:
    """Gains that put the voice at audio.voice_lufs (x voice_volume), the background
    audio.background_db_below_voice under it while the narration plays, and the background at
    voice_lufs x original_volume_after outside the narration. Unmeasurable (silent) inputs get unity gain;
    every gain is clamped (voice +-30 dB, background -60..+12 dB)."""
    def clamp(x: float, lo: float, hi: float) -> float:
        return min(hi, max(lo, x))

    vg = 0.0 if voice_lufs is None else clamp(audio.voice_lufs - voice_lufs, -_MAX_VOICE_GAIN_DB, _MAX_VOICE_GAIN_DB)
    voice = _db_to_lin(vg) * audio.voice_volume
    target_during = audio.voice_lufs - audio.background_db_below_voice
    during = 1.0 if bed_during_lufs is None else _db_to_lin(
        clamp(target_during - bed_during_lufs, _MIN_BED_GAIN_DB, _MAX_BED_BOOST_DB))
    if audio.original_volume_after <= 0:
        after = 0.0
    elif bed_lufs is None:
        after = 1.0
    else:
        target_after = audio.voice_lufs + _lin_to_db(audio.original_volume_after)
        after = _db_to_lin(clamp(target_after - bed_lufs, _MIN_BED_GAIN_DB, _MAX_BED_BOOST_DB))
    return MixGains(voice=voice, during=during, after=after)


MIN_TRIMMED_SECONDS = 3.0  # Facebook Reels' minimum


def short_voice_cut(audio: AudioSettings, voice_end: float, total: float) -> float | None:
    """New clip length for a short narration (audio.short_voice = trim_clip), or None to keep the clip."""
    if audio.short_voice != "trim_clip" or total <= 0 or voice_end <= 0:
        return None
    if voice_end >= total * audio.short_voice_below:
        return None
    new = max(voice_end + audio.short_voice_tail, MIN_TRIMMED_SECONDS)
    return round(new, 3) if new < total - 0.05 else None


def _r(x: float | None, nd: int = 1) -> float | None:
    return None if x is None else round(x, nd)


def render_final(*, cut: Path, voice: Path | None, ass: Path | None, dst: Path,
                 audio: AudioSettings, on_progress: ProgressFn | None = None,
                 words_json: Path | None = None, overlay: Path | None = None, bed: Path | None = None,
                 report: dict | None = None) -> Path:
    """Burn captions and mix: the background is ducked while the voice speaks and comes back outside it;
    then loudnorm + limiter.

    Background = ``bed`` (e.g. the Demucs no_vocals stem) or the cut's own audio. Levels: audio.mix
    "loudness" measures the voice and the background (loudness_gains); "gain" uses the fixed factors.
    A short narration may end the clip early (short_voice_cut).

    Captions come either from ``ass`` (libass burn-in) or from ``overlay`` (a transparent HyperFrames
    video composited on top); the two are mutually exclusive. The audio graph is the same either way.
    ``report`` (a dict) receives what was done: duration, trim, measured loudness, gains.
    """
    cut = Path(cut)
    if ass is not None and overlay is not None:
        raise RenderError("render_final: pass either ass or overlay captions, not both")
    if not cut.is_file():
        raise RenderError(f"cut clip not found: {cut} (run the cut stage first)")
    for label, p in (("voice", voice), ("captions", ass), ("caption overlay", overlay), ("background", bed)):
        if p is not None and not Path(p).is_file():
            raise RenderError(f"{label} file not found: {p}")
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    info = ffmpeg.probe(cut)
    if not info.has_video:
        raise RenderError(f"cannot render {cut.name}: it has no video stream")
    clip_total = info.duration
    total = clip_total
    rep: dict = {"mix": audio.mix if voice is not None else "original", "clip_duration": _r(clip_total, 3)}

    args = ["-i", str(cut)]
    inputs = 1
    voice_label = None
    ve = 0.0
    trimmed_to = None
    if voice is not None:
        args += ["-i", str(voice)]
        voice_label = f"[{inputs}:a]"
        inputs += 1
        ve = min(speech_end(Path(voice), words_json), total if total > 0 else math.inf)
        trimmed_to = short_voice_cut(audio, ve, total)
        if trimmed_to is not None:
            # words.json can end before the audio (a cut-short take captions only the words it could align):
            # never trim or fade over speech, so the trim follows the audio's own speech end when later
            vd = ffmpeg.duration(Path(voice))
            quiet = _detect_trailing_silence_start(Path(voice), vd)
            audio_end = min(quiet if quiet is not None and quiet > 0 else vd, total)
            if audio_end > ve + 0.05:
                ve = audio_end
                trimmed_to = short_voice_cut(audio, ve, total)
        if trimmed_to is not None:
            total = trimmed_to
        rep["voice_end"] = _r(ve, 2)
    bed_source: Path | None = None
    outside = outside_stereo = None  # with a stem: the clip's own audio, played outside the narration
    cut_stereo = _MONO_TO_STEREO if info.audio_channels == 1 else "aformat=channel_layouts=stereo"
    if bed is not None and voice is not None and ve > 0:
        args += ["-i", str(bed)]
        orig = f"[{inputs}:a]"
        orig_stereo = "aformat=channel_layouts=stereo"
        inputs += 1
        bed_source = Path(bed)
        if info.has_audio and trimmed_to is None:
            outside, outside_stereo = "[0:a]", cut_stereo
    elif info.has_audio:
        orig = "[0:a]"
        orig_stereo = _MONO_TO_STEREO if info.audio_channels == 1 else "aformat=channel_layouts=stereo"
        bed_source = cut
    else:
        args += ["-f", "lavfi", "-i", _SILENCE]
        orig = f"[{inputs}:a]"
        orig_stereo = "aformat=channel_layouts=stereo"
        inputs += 1
    rep["background"] = "no_vocals" if bed_source is not None and bed_source != cut else (
        "original" if bed_source is not None else "none")
    if outside:
        rep["background_outside"] = "original"  # the stem only under the voice, the clip's own audio around it
    overlay_index = None
    if overlay is not None:
        if Path(overlay).suffix.lower() == ".webm":
            args += ["-c:v", "libvpx-vp9"]  # input option: FFmpeg's native vp9 decoder drops the alpha
        args += ["-i", str(overlay)]
        overlay_index = inputs
        inputs += 1

    gains: MixGains | None = None
    if voice is not None and audio.mix == "loudness":
        # measured as they are mixed: the voice copied to both channels, the background as orig_stereo makes it
        lv = ffmpeg.measure_loudness(Path(voice), end=ve if ve > 0 else None, pre=_MONO_TO_STEREO)
        lb_during = ffmpeg.measure_loudness(bed_source, end=ve, pre=orig_stereo) \
            if bed_source is not None and ve > 0 else None
        # level outside the narration: of what plays there (the clip's own audio when a stem is used)
        lb = ffmpeg.measure_loudness(cut if outside else bed_source, end=total if total > 0 else None,
                                     pre=outside_stereo if outside else orig_stereo) if bed_source else None
        gains = loudness_gains(audio, voice_lufs=lv, bed_during_lufs=lb_during, bed_lufs=lb)
        rep.update({"voice_lufs": _r(lv), "background_lufs_under_voice": _r(lb_during), "background_lufs": _r(lb),
                    "voice_gain_db": _r(_lin_to_db(gains.voice)),
                    "background_gain_db_under_voice": _r(_lin_to_db(gains.during)),
                    "background_gain_db_outside": _r(_lin_to_db(gains.after))
                    if gains.after > 0 and trimmed_to is None else None})  # a trimmed clip has no "outside"
        stem = bed_source is not None and bed_source != cut
        if (lb_during is None and stem and ve > 0) or (
                lb_during is not None
                and (audio.voice_lufs - audio.background_db_below_voice) - lb_during > _MAX_BED_BOOST_DB):
            # silent (unmeasurable) or the +12 dB boost cap was hit: the background stays quieter than asked
            rep["background_note"] = ("nearly silent after vocal removal (the clip is mostly speech)"
                                      if bed_source is not None and bed_source != cut
                                      else "quieter than the target even after the +12 dB boost limit")
    fade = None
    if trimmed_to is not None:
        tail = max(0.0, trimmed_to - ve)
        fade = (max(0.0, trimmed_to - min(0.5, tail)), min(0.5, tail)) if tail > 0 else None
        rep.update({"trimmed_to": trimmed_to, "trim_reason": "short narration"})
    rep["duration"] = _r(total, 3)

    parts: list[str] = []
    if ass is not None:
        fonts = resolve_fonts_dir()
        parts.append(f"[0:v]{ffmpeg.ass_filter(Path(ass), fonts)}[v]")
        video_map = "[v]"
    elif overlay_index is not None:
        parts.append(f"[0:v][{overlay_index}:v]overlay=0:0:format=auto:eof_action=pass,format=yuv420p[v]")
        video_map = "[v]"
    else:
        video_map = "0:v:0"
    parts.append(build_audio_graph(orig=orig, orig_stereo=orig_stereo, voice_label=voice_label,
                                   audio=audio, voice_start=0.0, voice_end=ve, gains=gains,
                                   duck_to_end=trimmed_to is not None, fade_out=fade,
                                   bed_pad=bed_source is not None and bed_source != cut,
                                   outside=outside, outside_stereo=outside_stereo or ""))

    with _AtomicOutput(dst) as tmp:
        cmd = [*args, "-filter_complex", ";".join(parts), "-map", video_map, "-map", "[a]",
               *_X264, *_AAC]
        cmd += ["-t", ffmpeg._num(total)] if total > 0 else ["-shortest"]
        cmd += ["-movflags", "+faststart", str(tmp)]
        ffmpeg.run(cmd, desc=f"render {dst.name}", on_progress=on_progress, total=total)
    if report is not None:
        report.update(rep)
    return dst


def write_metadata(script: ClipScript, lang: str, dst: Path) -> Path:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    from .script import fix_title_quotes

    # titles of scripts written before the quote clean-up (a stray inner quote) are cleaned here too
    data = {"language": lang, "title": fix_title_quotes(script.title), "description": script.description,
            "hashtags": list(script.hashtags)}
    return write_text_atomic(dst, json.dumps(data, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------------------------- engines
RENDER_STAMP = "render.json"
_WARN_REASON_MAX = 200


@dataclass
class RenderOutcome:
    path: Path
    engine_used: Literal["ass", "hyperframes"]
    warning: str | None
    reason: str | None = None  # short fallback reason (for the stage message)
    hf_version: str | None = None
    fps: int | None = None
    audio: dict | None = None  # render_final's report (mix, loudness, trim, background)
    audio_warning: str | None = None  # e.g. vocal separation failed -> original background


def short_reason(e: BaseException) -> str:
    """One line, <= 200 chars, of an exception for the fallback warning."""
    text = " ".join(str(e).split()) or type(e).__name__
    return text if len(text) <= _WARN_REASON_MAX else text[: _WARN_REASON_MAX - 3] + "..."


def fallback_warning(reason: str) -> str:
    return f"HyperFrames captions failed ({reason}); used ASS captions"


def _sub_progress(on_progress: ProgressFn | None, lo: float, hi: float) -> ProgressFn | None:
    if on_progress is None:
        return None

    def f(fraction: float, message: str = "") -> None:
        on_progress(lo + (hi - lo) * min(1.0, max(0.0, float(fraction))), message)

    return f


def separation_warning(reason: str) -> str:
    return f"vocal separation failed ({reason}); used the original audio as the background"


def _background(cut: Path, settings: Settings, on_progress: ProgressFn | None, sep_runner=None, *,
                voice: Path | None = None, words_json: Path | None = None, cache: dict | None = None,
                cancel_check=None, info: dict | None = None) -> tuple[Path | None, str | None]:
    """(no_vocals stem or None, warning or None) for a voice-over render.

    No separation (and no Demucs run) when the voice is missing or silent: render_final then fails on
    its input check, or plays the original audio. A failure is remembered in ``cache`` (one pipeline run)
    per cut + separation settings, so the next language does not run the same failing Demucs again.
    ``info`` receives {"seconds": .., "cached": bool} of a successful separation.
    """
    if not settings.voiceover or settings.separation.background != "no_vocals":
        return None, None
    if voice is None or not Path(voice).is_file():
        return None, None
    try:
        if speech_end(Path(voice), words_json) <= 0:
            return None, None
    except Exception:  # noqa: BLE001 - an unreadable voice: render_final reports it
        return None, None
    from .. import separation

    s = settings.separation
    memo_key = ("separation-failed", str(Path(cut).resolve()), s.model, s.segment, s.python)
    if cache is not None and memo_key in cache:
        return None, separation_warning(f"{cache[memo_key]}; not retried for this language")
    try:
        msg = (lambda _f, m="": on_progress(0.0, m)) if on_progress is not None else None  # noqa: E731
        return separation.no_vocals_bed(cut, Path(cut).parent, s, runner=sep_runner, on_progress=msg,
                                        poll=cancel_check, info=info), None
    except Exception as e:  # noqa: BLE001 - SeparationError or anything unexpected: original background
        reason = short_reason(e)
        log.warning("vocal separation failed for %s (%s); using the original audio", cut, reason)
        if cache is not None:
            cache[memo_key] = reason
        return None, separation_warning(reason)


def render_clip(*, cut: Path, out_dir: Path, settings: Settings, on_progress: ProgressFn | None = None,
                hf_env_cache: dict | None = None, runner=None, sep_runner=None,
                cancel_check=None) -> RenderOutcome:
    """final.mp4 for one clip-language with the configured caption engine.

    engine "ass": exactly the step-2 render. engine "hyperframes": the HyperFrames overlay (progress
    0..0.7) composited by render_final (0.7..1). Any HyperFrames failure (or any unexpected Exception in
    it) falls back to the ASS render with a warning; KeyboardInterrupt / cancel propagate.
    ``hf_env_cache`` memoises probe_env per pipeline call (key "env" -> HFEnv or the exception).
    Voice-over with separation.background "no_vocals": the background is the Demucs stem (cached per
    clip); any separation failure falls back to the original audio with ``audio_warning`` (and is not
    retried for the other language in the same run: ``hf_env_cache`` remembers it).
    ``cancel_check()`` is called about twice a second while Demucs runs and may raise to cancel it.
    """
    out_dir = Path(out_dir)
    final = out_dir / "final.mp4"
    # Voice-over off: no voice input at all; the original audio plays at audio.original_volume_after.
    voice = out_dir / "voice.wav" if settings.voiceover else None
    words = out_dir / "words.json"
    ass = out_dir / "captions.ass"
    sep_info: dict = {}
    bed, audio_warning = _background(Path(cut), settings, on_progress, sep_runner, voice=voice, words_json=words,
                                     cache=hf_env_cache, cancel_check=cancel_check, info=sep_info)
    report: dict = {}
    common = dict(cut=Path(cut), voice=voice, dst=final, audio=settings.audio, words_json=words, bed=bed,
                  report=report)
    # captions.ass follows the current caption style, keywords and hook (G4/G5) on every render
    script = load_clip_script(out_dir)
    from .caption_style import hook_text
    from .captions import rebuild_ass

    try:
        clip_duration = ffmpeg.probe(Path(cut)).duration
    except ffmpeg.FFmpegError:
        clip_duration = None  # render_final reports the broken cut
    rebuild_ass(out_dir, lang=out_dir.name, settings=settings, script=script, duration=clip_duration)
    has_hook = bool(hook_text(script, settings))

    def done(outcome: RenderOutcome) -> RenderOutcome:
        if audio_warning:
            report["warning"] = audio_warning
        if bed is not None and sep_info:
            report["separation_seconds"] = sep_info.get("seconds")
            report["separation_cached"] = bool(sep_info.get("cached"))
        outcome.audio = report or None
        outcome.audio_warning = audio_warning
        return outcome

    if settings.captions.engine != "hyperframes":
        render_final(ass=ass, on_progress=on_progress, **common)
        return done(RenderOutcome(path=final, engine_used="ass", warning=None))

    if _no_words(words) and not has_hook:
        # Nothing is said in this clip (e.g. captions only over music) and there is no hook: there is
        # nothing to draw, so there is no overlay to make - not a HyperFrames failure.
        render_final(ass=None, on_progress=on_progress, **common)
        return done(RenderOutcome(path=final, engine_used="hyperframes", warning=None))

    from . import hyperframes as hf

    try:
        env = _cached_env(settings, hf_env_cache)
        overlay = hf.make_overlay(words_json=words, cut=Path(cut), lang=out_dir.name, settings=settings,
                                  out_dir=out_dir, on_progress=_sub_progress(on_progress, 0.0, 0.7),
                                  runner=runner, env=env)
    except Exception as e:  # noqa: BLE001 - HyperFramesError or anything unexpected: ASS fallback
        return done(_fallback(e, out_dir, on_progress, ass, common))
    try:
        render_final(ass=None, overlay=overlay, on_progress=_sub_progress(on_progress, 0.7, 1.0), **common)
    except ffmpeg.FFmpegError as e:  # the composite itself failed (e.g. an undecodable overlay)
        return done(_fallback(RuntimeError(f"overlay composite failed: {e}"), out_dir, on_progress, ass, common))
    fps = None
    try:
        fps = json.loads((out_dir / "hf" / "overlay.json").read_text(encoding="utf-8")).get("fps")
    except (OSError, ValueError, AttributeError):
        pass
    return done(RenderOutcome(path=final, engine_used="hyperframes", warning=None, hf_version=env.hf_version,
                              fps=fps))


def _no_words(words_json: Path) -> bool:
    """True only when words.json exists and is a valid, empty list (a missing/broken file is an error
    the HyperFrames path reports as usual)."""
    try:
        return json.loads(Path(words_json).read_text(encoding="utf-8")) == []
    except (OSError, ValueError):
        return False


def _fallback(e: BaseException, out_dir: Path, on_progress: ProgressFn | None, ass: Path,
              common: dict) -> RenderOutcome:
    reason = short_reason(e)
    log.warning("HyperFrames captions failed for %s (%s); using ASS captions", out_dir, reason)
    render_final(ass=ass, on_progress=_sub_progress(on_progress, 0.7, 1.0), **common)
    return RenderOutcome(path=Path(common["dst"]), engine_used="ass", warning=fallback_warning(reason),
                         reason=reason)


def _cached_env(settings: Settings, cache: dict | None):
    from . import hyperframes as hf

    if cache is None:
        return hf.probe_env(settings.hyperframes)
    key = ("env", settings.hyperframes.node_bin_dir, settings.hyperframes.package_dir)
    if key not in cache:
        try:
            cache[key] = hf.probe_env(settings.hyperframes)
        except hf.HyperFramesError as e:
            cache[key] = e
    value = cache[key]
    if isinstance(value, BaseException):
        raise value
    return value


AUDIO_KEY_VERSION = 2  # 2 = loudness mix + short-narration trim + vocal separation (this build)


def audio_key(settings: Settings) -> dict:
    """The audio settings a voice-over final.mp4 depends on (render.json "audio_key"). A voice-over render
    whose stamp has another (or no) audio_key is stale: it was made with an older mix or other audio
    settings, so a resume / the job's Mode button re-renders it."""
    a, sep = settings.audio, settings.separation
    key: dict = {"v": AUDIO_KEY_VERSION, "mix": a.mix, "after": a.original_volume_after,
                 "ramp": a.duck_ramp_seconds, "voice_volume": a.voice_volume, "loudnorm": a.loudnorm,
                 "target_lufs": a.target_lufs, "short_voice": a.short_voice, "background": sep.background}
    if a.mix == "loudness":
        key.update(voice_lufs=a.voice_lufs, below=a.background_db_below_voice)
    else:
        key["during"] = a.original_volume
    if a.short_voice == "trim_clip":
        key.update(short_below=a.short_voice_below, short_tail=a.short_voice_tail)
    if sep.background == "no_vocals":
        key["model"] = sep.model
    return key


def audio_settings_changed(stamp: dict, settings: Settings) -> bool:
    """A voice-over render made with an older mix or other audio settings (see audio_key)."""
    if not settings.voiceover or not rendered_voiceover(stamp):
        return False
    return stamp.get("audio_key") != audio_key(settings)


def render_key(settings: Settings) -> dict:
    """The settings a final.mp4 depends on beyond its inputs (CONTRACT_STEP3 5.1). "style" is the HyperFrames
    animation: captions.highlight_style when set (G5), else hyperframes.style."""
    if settings.captions.engine != "hyperframes":
        return {"caption_engine": "ass"}
    h = settings.hyperframes
    return {"caption_engine": "hyperframes", "style": settings.captions.highlight_style or h.style,
            "format": h.format, "fps": h.fps}


def load_clip_script(out_dir: Path) -> ClipScript | None:
    """out_dir/script.json, or None when it is missing or unreadable (no hook, no keywords then)."""
    try:
        return ClipScript.model_validate_json((Path(out_dir) / "script.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def style_changed(stamp: dict, out_dir: Path, settings: Settings) -> bool:
    """The caption look (style fields, hook, keywords; caption_style.style_key) differs from the one the
    final.mp4 was made with. A stamp from before G4/G5 (no style_key) matches only the step 1-4 look."""
    from .caption_style import legacy_look, style_key

    script = load_clip_script(out_dir)
    if "style_key" not in stamp:
        return not legacy_look(settings, script)
    return stamp.get("style_key") != style_key(settings, script, Path(out_dir).name)


def write_render_stamp(out_dir: Path, settings: Settings, outcome: RenderOutcome) -> Path:
    data = dict(render_key(settings))
    data.update({"engine_used": outcome.engine_used, "warning": outcome.warning})
    if not settings.voiceover:
        data["voiceover"] = False  # absent = rendered with the voice-over (every render before this key)
    if settings.voiceover:
        data["audio_key"] = audio_key(settings)  # the audio settings this render used (staleness)
    if outcome.audio:
        data["audio"] = outcome.audio  # informational (mix, loudness, trim)
    from .caption_style import style_key

    data["style_key"] = style_key(settings, load_clip_script(out_dir), Path(out_dir).name)  # the caption look (staleness, G4/G5)
    if outcome.engine_used == "hyperframes":
        data["hf_version"] = outcome.hf_version
        data["resolved_fps"] = outcome.fps
    return write_text_atomic(Path(out_dir) / RENDER_STAMP, json.dumps(data, indent=2, ensure_ascii=False))


def read_render_stamp(out_dir: Path) -> dict:
    """render.json, or the legacy default {"caption_engine": "ass", "engine_used": "ass"} when missing
    or unreadable (step-2 renders were ASS)."""
    try:
        data = json.loads((Path(out_dir) / RENDER_STAMP).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"caption_engine": "ass", "engine_used": "ass"}


def rendered_voiceover(stamp: dict) -> bool:
    """Whether a final.mp4 was rendered with the voice-over (stamps without the key: yes)."""
    return stamp.get("voiceover") is not False


def render_settings_changed(out_dir: Path, settings: Settings) -> bool:
    """True when the render settings (render_key) differ from the ones final.mp4 was made with. Unlike
    render_is_current this ignores an ASS fallback: the user changed nothing, so the UI shows the fallback
    warning instead of a "re-render to apply" hint (resume still retries the fallback)."""
    stamp = read_render_stamp(out_dir)
    if rendered_voiceover(stamp) != settings.voiceover:
        return True
    if audio_settings_changed(stamp, settings):
        return True
    if style_changed(stamp, out_dir, settings):
        return True
    return any(stamp.get(k) != v for k, v in render_key(settings).items())


def render_is_current(out_dir: Path, settings: Settings) -> bool:
    final = Path(out_dir) / "final.mp4"
    if not (final.is_file() and final.stat().st_size > 0):
        return False
    stamp = read_render_stamp(out_dir)
    if rendered_voiceover(stamp) != settings.voiceover:
        return False
    if audio_settings_changed(stamp, settings):
        return False
    if style_changed(stamp, out_dir, settings):
        return False
    key = render_key(settings)
    if any(stamp.get(k) != v for k, v in key.items()):
        return False
    return stamp.get("engine_used") == stamp.get("caption_engine")
