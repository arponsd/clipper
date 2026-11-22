"""Render stage: cut.mp4 + voice + burned captions -> final.mp4, plus per-language metadata.json."""
from __future__ import annotations

import json
import logging
import math
import re
import subprocess
from pathlib import Path

from .. import config, ffmpeg
from ..ffmpeg import _AAC, _LIMITER, _MONO_TO_STEREO, _SILENCE, _X264, _AtomicOutput
from ..config import AudioSettings
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
                 words_json: Path | None = None) -> Path:
    """Burn captions and mix: the original is ducked to audio.original_volume only while the voice
    speaks and ramps back to audio.original_volume_after outside it; then loudnorm + limiter."""
    cut = Path(cut)
    if not cut.is_file():
        raise RenderError(f"cut clip not found: {cut} (run the cut stage first)")
    for label, p in (("voice", voice), ("captions", ass)):
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

    parts: list[str] = []
    if ass is not None:
        fonts = resolve_fonts_dir()
        parts.append(f"[0:v]{ffmpeg.ass_filter(Path(ass), fonts)}[v]")
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
