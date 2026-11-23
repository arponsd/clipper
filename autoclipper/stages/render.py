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


def _post_chain(audio: AudioSettings) -> str:
    chain = []
    if audio.loudnorm:
        chain.append(f"loudnorm=I={ffmpeg._num(audio.target_lufs)}:TP=-1.5:LRA=11")
        chain.append("aresample=48000")
    chain.append(_LIMITER)
    return ",".join(chain)


def build_audio_graph(*, orig: str, orig_stereo: str, voice_label: str | None, audio: AudioSettings,
                      voice_start: float = 0.0, voice_end: float = 0.0) -> str:
    """filter_complex audio part producing ``[a]`` (see CONTRACT_WEB section 9.1)."""
    post = _post_chain(audio)
    if voice_label is None or voice_end <= voice_start:
        parts = [f"{orig}{orig_stereo},volume={ffmpeg._num(audio.original_volume_after)}"]
        if voice_label is not None:  # an empty voice still has to be consumed by the graph
            parts[0] += "[a0]"
            parts.append(f"{voice_label}{_MONO_TO_STEREO},volume={ffmpeg._num(audio.voice_volume)}[a1]")
            parts.append(f"[a0][a1]amix=inputs=2:normalize=0:duration=first,{post}[a]")
        else:
            parts[0] += f",{post}[a]"
        return ";".join(parts)
    env = duck_envelope(voice_start, voice_end, during=audio.original_volume,
                        after=audio.original_volume_after, ramp=audio.duck_ramp_seconds)
    return ";".join([
        f"{orig}{orig_stereo},volume='{env}':eval=frame[a0]",
        f"{voice_label}{_MONO_TO_STEREO},volume={ffmpeg._num(audio.voice_volume)}[a1]",
        f"[a0][a1]amix=inputs=2:normalize=0:duration=first,{post}[a]",
    ])


def render_final(*, cut: Path, voice: Path | None, ass: Path | None, dst: Path,
                 audio: AudioSettings, on_progress: ProgressFn | None = None,
                 words_json: Path | None = None, overlay: Path | None = None) -> Path:
    """Burn captions and mix: the original is ducked to audio.original_volume only while the voice
    speaks and ramps back to audio.original_volume_after outside it; then loudnorm + limiter.

    Captions come either from ``ass`` (libass burn-in) or from ``overlay`` (a transparent HyperFrames
    video composited on top); the two are mutually exclusive. The audio graph is the same either way.
    """
    cut = Path(cut)
    if ass is not None and overlay is not None:
        raise RenderError("render_final: pass either ass or overlay captions, not both")
    if not cut.is_file():
        raise RenderError(f"cut clip not found: {cut} (run the cut stage first)")
    for label, p in (("voice", voice), ("captions", ass), ("caption overlay", overlay)):
        if p is not None and not Path(p).is_file():
            raise RenderError(f"{label} file not found: {p}")
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    info = ffmpeg.probe(cut)
    if not info.has_video:
        raise RenderError(f"cannot render {cut.name}: it has no video stream")
    total = info.duration

    args = ["-i", str(cut)]
    inputs = 1
    voice_label = None
    ve = 0.0
    if voice is not None:
        args += ["-i", str(voice)]
        voice_label = f"[{inputs}:a]"
        inputs += 1
        ve = min(speech_end(Path(voice), words_json), total if total > 0 else math.inf)
    if info.has_audio:
        orig = "[0:a]"
        orig_stereo = _MONO_TO_STEREO if info.audio_channels == 1 else "aformat=channel_layouts=stereo"
    else:
        args += ["-f", "lavfi", "-i", _SILENCE]
        orig = f"[{inputs}:a]"
        orig_stereo = "aformat=channel_layouts=stereo"
        inputs += 1
    overlay_index = None
    if overlay is not None:
        if Path(overlay).suffix.lower() == ".webm":
            args += ["-c:v", "libvpx-vp9"]  # input option: FFmpeg's native vp9 decoder drops the alpha
        args += ["-i", str(overlay)]
        overlay_index = inputs
        inputs += 1

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
                                   audio=audio, voice_start=0.0, voice_end=ve))

    with _AtomicOutput(dst) as tmp:
        cmd = [*args, "-filter_complex", ";".join(parts), "-map", video_map, "-map", "[a]",
               *_X264, *_AAC]
        cmd += ["-t", ffmpeg._num(total)] if total > 0 else ["-shortest"]
        cmd += ["-movflags", "+faststart", str(tmp)]
        ffmpeg.run(cmd, desc=f"render {dst.name}", on_progress=on_progress, total=total)
    return dst


def write_metadata(script: ClipScript, lang: str, dst: Path) -> Path:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = {"language": lang, "title": script.title, "description": script.description,
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


def render_clip(*, cut: Path, out_dir: Path, settings: Settings, on_progress: ProgressFn | None = None,
                hf_env_cache: dict | None = None, runner=None) -> RenderOutcome:
    """final.mp4 for one clip-language with the configured caption engine.

    engine "ass": exactly the step-2 render. engine "hyperframes": the HyperFrames overlay (progress
    0..0.7) composited by render_final (0.7..1). Any HyperFrames failure (or any unexpected Exception in
    it) falls back to the ASS render with a warning; KeyboardInterrupt / cancel propagate.
    ``hf_env_cache`` memoises probe_env per pipeline call (key "env" -> HFEnv or the exception).
    """
    out_dir = Path(out_dir)
    final = out_dir / "final.mp4"
    voice = out_dir / "voice.wav"
    words = out_dir / "words.json"
    ass = out_dir / "captions.ass"
    common = dict(cut=Path(cut), voice=voice, dst=final, audio=settings.audio, words_json=words)
    if settings.captions.engine != "hyperframes":
        render_final(ass=ass, on_progress=on_progress, **common)
        return RenderOutcome(path=final, engine_used="ass", warning=None)

    from . import hyperframes as hf

    try:
        env = _cached_env(settings, hf_env_cache)
        overlay = hf.make_overlay(words_json=words, cut=Path(cut), lang=out_dir.name, settings=settings,
                                  out_dir=out_dir, on_progress=_sub_progress(on_progress, 0.0, 0.7),
                                  runner=runner, env=env)
    except Exception as e:  # noqa: BLE001 - HyperFramesError or anything unexpected: ASS fallback
        return _fallback(e, out_dir, on_progress, ass, common)
    try:
        render_final(ass=None, overlay=overlay, on_progress=_sub_progress(on_progress, 0.7, 1.0), **common)
    except ffmpeg.FFmpegError as e:  # the composite itself failed (e.g. an undecodable overlay)
        return _fallback(RuntimeError(f"overlay composite failed: {e}"), out_dir, on_progress, ass, common)
    fps = None
    try:
        fps = json.loads((out_dir / "hf" / "overlay.json").read_text(encoding="utf-8")).get("fps")
    except (OSError, ValueError, AttributeError):
        pass
    return RenderOutcome(path=final, engine_used="hyperframes", warning=None, hf_version=env.hf_version, fps=fps)


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


def render_key(settings: Settings) -> dict:
    """The settings a final.mp4 depends on beyond its inputs (CONTRACT_STEP3 5.1)."""
    if settings.captions.engine != "hyperframes":
        return {"caption_engine": "ass"}
    h = settings.hyperframes
    return {"caption_engine": "hyperframes", "style": h.style, "format": h.format, "fps": h.fps}


def write_render_stamp(out_dir: Path, settings: Settings, outcome: RenderOutcome) -> Path:
    data = dict(render_key(settings))
    data.update({"engine_used": outcome.engine_used, "warning": outcome.warning})
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


def render_settings_changed(out_dir: Path, settings: Settings) -> bool:
    """True when the render settings (render_key) differ from the ones final.mp4 was made with. Unlike
    render_is_current this ignores an ASS fallback: the user changed nothing, so the UI shows the fallback
    warning instead of a "re-render to apply" hint (resume still retries the fallback)."""
    stamp = read_render_stamp(out_dir)
    return any(stamp.get(k) != v for k, v in render_key(settings).items())


def render_is_current(out_dir: Path, settings: Settings) -> bool:
    final = Path(out_dir) / "final.mp4"
    if not (final.is_file() and final.stat().st_size > 0):
        return False
    stamp = read_render_stamp(out_dir)
    key = render_key(settings)
    if any(stamp.get(k) != v for k, v in key.items()):
        return False
    return stamp.get("engine_used") == stamp.get("caption_engine")
