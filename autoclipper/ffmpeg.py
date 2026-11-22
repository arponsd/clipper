"""Thin, typed wrappers around the ffmpeg / ffprobe binaries.

Design rules:
- Commands are built as argument lists and run without a shell, so nothing is quoted manually.
- The only hand-escaping is for values embedded inside a filtergraph (subtitle paths), see
  :func:`escape_filter_path`.
- Outputs are written to a temporary sibling file first and renamed into place, so an interrupted
  run never leaves a half-written file that an idempotency check would mistake for a finished one.
"""
from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath
from typing import Literal

from .interfaces import ProgressFn

__all__ = [
    "FFmpegError",
    "MediaInfo",
    "ffmpeg_bin",
    "ffprobe_bin",
    "run",
    "probe",
    "duration",
    "to_wav",
    "atempo_chain",
    "change_tempo",
    "trim_with_fade",
    "reframe_filter",
    "cut_vertical",
    "escape_filter_path",
    "ass_filter",
    "mix_burn",
    "synth_video",
]

STDERR_TAIL_LINES = 20
_REPLACE_RETRY_SECONDS = 1.0
FONT_SUFFIXES = {".ttf", ".otf", ".ttc", ".otc"}
_INSTALL_HINT = (
    "install FFmpeg and make sure it is on PATH "
    "(Linux: `sudo apt install ffmpeg`; Windows: `winget install Gyan.FFmpeg`, then reopen the terminal)"
)


class FFmpegError(RuntimeError):
    """An ffmpeg/ffprobe invocation failed. The message holds the command and the stderr tail."""


@dataclass
class MediaInfo:
    duration: float
    width: int | None
    height: int | None
    has_video: bool
    has_audio: bool
    audio_channels: int = 0  # channels of the first audio stream (0 = none/unknown)


# --------------------------------------------------------------------------- binaries / running


def _which(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise FFmpegError(f"{name} not found on PATH - {_INSTALL_HINT}")
    return found


def ffmpeg_bin() -> str:
    return _which("ffmpeg")


def ffprobe_bin() -> str:
    return _which("ffprobe")


def _summarize(cmd: list[str], limit: int = 600) -> str:
    text = shlex.join(cmd)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _error(desc: str, cmd: list[str], stderr: str, returncode: int | None) -> FFmpegError:
    tail = [ln for ln in stderr.strip().splitlines() if ln.strip()][-STDERR_TAIL_LINES:]
    rc = f" (exit code {returncode})" if returncode is not None else ""
    msg = f"{desc} failed{rc}\ncommand: {_summarize(cmd)}"
    if tail:
        msg += "\nstderr:\n" + "\n".join(tail)
    return FFmpegError(msg)


def _parse_out_time(line: str) -> float | None:
    """Parse one `-progress` line; returns seconds for out_time_us / out_time_ms keys."""
    key, _, value = line.partition("=")
    if key not in ("out_time_us", "out_time_ms"):
        return None
    try:
        return int(value) / 1_000_000  # both keys are microseconds in ffmpeg >= 4
    except ValueError:
        return None


def run(
    args: list[str],
    *,
    desc: str = "ffmpeg",
    on_progress: ProgressFn | None = None,
    total: float | None = None,
) -> subprocess.CompletedProcess:
    """Run ffmpeg with ``args`` (WITHOUT the binary name).

    Prepends ``ffmpeg -hide_banner -nostdin -y -loglevel error``. When ``on_progress`` and a positive
    ``total`` (seconds of output) are given, progress is reported from ffmpeg's ``-progress`` stream.
    Raises :class:`FFmpegError` on a non-zero exit.
    """
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    track = on_progress is not None and total is not None and total > 0
    if track:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [str(a) for a in args]

    if not track:
        try:
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
        except OSError as e:
            raise FFmpegError(f"{desc} could not start: {e}") from e
        if proc.returncode != 0:
            raise _error(desc, cmd, proc.stderr, proc.returncode)
        return proc

    assert on_progress is not None and total is not None
    try:
        popen = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as e:
        raise FFmpegError(f"{desc} could not start: {e}") from e

    # Drain stderr on a thread so a chatty ffmpeg can never block on a full pipe.
    err_lines: deque[str] = deque(maxlen=200)
    assert popen.stderr is not None and popen.stdout is not None
    reader = threading.Thread(target=lambda: err_lines.extend(popen.stderr), daemon=True)  # type: ignore[arg-type]
    reader.start()
    out_lines: list[str] = []
    last = -1.0
    try:
        for raw in popen.stdout:
            line = raw.strip()
            out_lines.append(line)
            seconds = _parse_out_time(line)
            if seconds is not None:
                frac = min(max(seconds / total, 0.0), 0.99)
                if frac - last >= 0.01:
                    last = frac
                    on_progress(frac, desc)
        returncode = popen.wait()
    except BaseException:
        # on_progress raised (job.json write error, Ctrl-C, UI cancel): never orphan the ffmpeg child.
        _kill(popen)
        reader.join(timeout=5)  # stderr hits EOF once the child is gone
        for stream in (popen.stdout, popen.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        raise
    reader.join(timeout=5)
    stderr = "".join(err_lines)
    if returncode != 0:
        raise _error(desc, cmd, stderr, returncode)
    on_progress(1.0, desc)
    return subprocess.CompletedProcess(cmd, returncode, "\n".join(out_lines), stderr)


def _kill(popen: subprocess.Popen) -> None:
    try:
        popen.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        popen.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - kill() is SIGKILL
        pass


class _AtomicOutput:
    """Context manager yielding a temp path next to ``dst`` (same extension, so ffmpeg picks the
    right muxer); renamed onto ``dst`` only on success, deleted on failure."""

    def __init__(self, dst: Path):
        self.dst = Path(dst)
        self.tmp = self.dst.with_name(f"{self.dst.stem}.part{self.dst.suffix}")

    def __enter__(self) -> Path:
        self.dst.parent.mkdir(parents=True, exist_ok=True)
        self.tmp.unlink(missing_ok=True)
        return self.tmp

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.tmp.unlink(missing_ok=True)
            return
        # On Windows os.replace fails while another program (a video player) has dst open.
        deadline = time.monotonic() + _REPLACE_RETRY_SECONDS
        while True:
            try:
                os.replace(self.tmp, self.dst)
                return
            except PermissionError as e:
                if time.monotonic() < deadline:
                    time.sleep(0.1)
                    continue
                err: OSError = e
            except OSError as e:
                err = e
            break
        self.tmp.unlink(missing_ok=True)
        raise FFmpegError(f"could not replace {self.dst.name}: is it open in another program "
                          f"(e.g. a video player)? Close it and retry ({err})") from err


# --------------------------------------------------------------------------- probing


def _float(value: object) -> float | None:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def probe(path: Path) -> MediaInfo:
    """Inspect a media file with ffprobe."""
    path = Path(path)
    if not path.is_file():
        raise FFmpegError(f"cannot probe {path}: file does not exist")
    cmd = [ffprobe_bin(), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except OSError as e:
        raise FFmpegError(f"ffprobe could not start: {e}") from e
    if proc.returncode != 0:
        raise _error(f"ffprobe {path.name}", cmd, proc.stderr, proc.returncode)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as e:
        raise FFmpegError(f"ffprobe returned invalid JSON for {path}: {e}") from e

    streams = data.get("streams") or []
    video = [
        s for s in streams
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    dur = _float((data.get("format") or {}).get("duration"))
    if dur is None:
        candidates = [d for d in (_float(s.get("duration")) for s in streams) if d is not None]
        dur = max(candidates, default=0.0)
    width = height = None
    if video:
        width = int(video[0]["width"]) if video[0].get("width") else None
        height = int(video[0]["height"]) if video[0].get("height") else None
    channels = 0
    if audio:
        try:
            channels = int(audio[0].get("channels") or 0)
        except (TypeError, ValueError):
            channels = 0
    return MediaInfo(duration=dur, width=width, height=height, has_video=bool(video), has_audio=bool(audio),
                     audio_channels=channels)


def duration(path: Path) -> float:
    return probe(path).duration


# --------------------------------------------------------------------------- audio helpers


def _num(x: float) -> str:
    """Compact decimal for filter args: 2 -> '2.0', 1.25 -> '1.25' (no exponent notation)."""
    text = f"{x:.6f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def to_wav(src: Path, dst: Path, *, sample_rate: int = 16000, channels: int = 1) -> Path:
    """Decode the first audio stream of ``src`` to PCM s16le WAV."""
    dst = Path(dst)
    with _AtomicOutput(dst) as tmp:
        run(
            ["-i", str(src), "-map", "0:a:0", "-vn", "-ac", str(channels), "-ar", str(sample_rate),
             "-c:a", "pcm_s16le", str(tmp)],
            desc=f"extract wav from {Path(src).name}",
        )
    return dst


def atempo_chain(factor: float) -> str:
    """atempo filter chain for any factor > 0, each link within atempo's [0.5, 2.0] range."""
    if not (isinstance(factor, (int, float)) and math.isfinite(factor) and factor > 0):
        raise ValueError(f"tempo factor must be a finite number > 0, got {factor!r}")
    if math.isclose(factor, 1.0, rel_tol=0, abs_tol=1e-9):
        return "anull"
    links: list[float] = []
    f = float(factor)
    while f > 2.0:
        links.append(2.0)
        f /= 2.0
    while f < 0.5:
        links.append(0.5)
        f /= 0.5
    if not math.isclose(f, 1.0, rel_tol=0, abs_tol=1e-9) or not links:
        links.append(f)
    return ",".join(f"atempo={_num(x)}" for x in links)


def change_tempo(src: Path, dst: Path, factor: float) -> Path:
    """Speed audio up (factor > 1) or down without changing pitch."""
    dst = Path(dst)
    with _AtomicOutput(dst) as tmp:
        run(["-i", str(src), "-vn", "-filter:a", atempo_chain(factor), str(tmp)],
            desc=f"change tempo x{_num(factor)}")
    return dst


def trim_with_fade(src: Path, dst: Path, duration: float, fade: float = 0.3) -> Path:
    """Cut audio to ``duration`` seconds with a fade-out over the last ``fade`` seconds."""
    if duration <= 0:
        raise ValueError(f"duration must be > 0, got {duration}")
    fade = min(max(fade, 0.0), duration)
    dst = Path(dst)
    af = f"afade=t=out:st={_num(duration - fade)}:d={_num(fade)}" if fade > 0 else "anull"
    with _AtomicOutput(dst) as tmp:
        run(["-i", str(src), "-vn", "-af", af, "-t", _num(duration), str(tmp)],
            desc=f"trim audio to {_num(duration)}s")
    return dst


# --------------------------------------------------------------------------- video helpers


def _check_even(width: int, height: int) -> None:
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise ValueError(f"output size must be positive and even (yuv420p), got {width}x{height}")


def reframe_filter(mode: Literal["crop", "blur"], width: int, height: int) -> str:
    """filter_complex text reading ``[0:v]`` and producing ``[v]`` at ``width``x``height``.

    crop: scale to cover the frame, then center crop.
    blur: blurred cover-scaled background with the whole source fitted and centered on top.
    """
    _check_even(width, height)
    w, h = width, height
    cover = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    tail = "setsar=1,format=yuv420p"
    if mode == "crop":
        return f"[0:v]{cover},{tail}[v]"
    if mode == "blur":
        # Blur at quarter resolution (much faster, same look), then scale back up.
        bw, bh = max(2, (w // 4) // 2 * 2), max(2, (h // 4) // 2 * 2)
        return (
            f"[0:v]split=2[bg][fg];"
            f"[bg]{cover},scale={bw}:{bh},boxblur=10:2,scale={w}:{h}[bgb];"
            f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,{tail}[v]"
        )
    raise ValueError(f"unknown reframe mode {mode!r} (expected 'crop' or 'blur')")


_X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
_AAC = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
_SILENCE = "anullsrc=channel_layout=stereo:sample_rate=48000"
# Mono -> stereo at unity gain on both channels (ffmpeg's implicit upmix puts mono at -3 dB per side).
_MONO_TO_STEREO = "aformat=channel_layouts=mono,pan=stereo|c0=c0|c1=c0"
# Final safety limiter: peaks stay below 0 dBFS. level=0 turns off auto-level (no make-up gain).
_LIMITER = "alimiter=limit=0.97:level=0"


def cut_vertical(
    src: Path,
    dst: Path,
    start: float,
    end: float,
    *,
    width: int = 1080,
    height: int = 1920,
    mode: str = "crop",
    on_progress: ProgressFn | None = None,
) -> Path:
    """Cut [start, end) of ``src`` and reframe it to a vertical ``width``x``height`` mp4.

    Re-encodes (frame-accurate), h264 crf 20 + aac 192k 48 kHz stereo, +faststart. The output always
    has an audio stream: a silent track is generated when the source has none.
    """
    src, dst = Path(src), Path(dst)
    info = probe(src)
    if not info.has_video:
        raise FFmpegError(f"cannot cut {src.name}: it has no video stream")
    start = max(0.0, float(start))
    end = float(end)
    if info.duration > 0:
        end = min(end, info.duration)
    if not end > start:
        raise FFmpegError(
            f"cannot cut {src.name}: empty range {start:.3f}-{end:.3f}s (source is {info.duration:.3f}s long)"
        )
    length = end - start
    graph = reframe_filter(mode, width, height)  # type: ignore[arg-type]

    args = ["-ss", _num(start), "-t", _num(length), "-i", str(src)]
    if info.has_audio:
        audio_map = "0:a:0"
    else:
        args += ["-f", "lavfi", "-t", _num(length), "-i", _SILENCE]
        audio_map = "1:a:0"
    with _AtomicOutput(dst) as tmp:
        args += [
            "-filter_complex", graph, "-map", "[v]", "-map", audio_map,
            *_X264, *_AAC, "-t", _num(length), "-movflags", "+faststart", "-map_metadata", "-1",
            str(tmp),
        ]
        run(args, desc=f"cut {src.name} {start:.2f}-{end:.2f}s", on_progress=on_progress, total=length)
    return dst


def escape_filter_path(path: PurePath | str) -> str:
    """Escape a file path for use as a quoted option value inside an ffmpeg filtergraph.

    Two escaping levels apply: the filter option parser (``\\``, ``'`` and ``:`` are special) and the
    filtergraph parser (handled by single-quoting, where only ``'`` is special). Windows paths are
    converted to forward slashes first, so ``C:\\x y\\a.ass`` becomes ``'C\\:/x y/a.ass'``.
    Returns the value INCLUDING its surrounding single quotes.
    """
    if isinstance(path, PureWindowsPath) or (isinstance(path, str) and os.name == "nt"):
        text = PureWindowsPath(path).as_posix()
    else:
        text = str(path)
    # Level 1: option value escaping.
    level1 = text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    # Level 2: quote for the graph parser; a literal ' must close the quote, be escaped, reopen.
    return "'" + level1.replace("'", "'\\''") + "'"


def _abs(path: PurePath) -> PurePath:
    return Path(path).resolve() if isinstance(path, Path) else path


def ass_filter(ass_path: PurePath, fonts_dir: PurePath | None) -> str:
    """``ass=filename='...':shaping=complex[:fontsdir='...']`` with paths escaped for the filtergraph.

    The ``ass`` filter is used instead of ``subtitles`` because only it exposes ``shaping``. With
    the default shaper some libass/FFmpeg builds (Ubuntu FFmpeg 7.1 + libass 0.17) shape simply,
    which breaks Devanagari: the i-matra is not reordered and conjuncts show a visible virama
    (भविष्य -> "भवष्ि्य", खत्म -> "खत्‌म"). Complex (HarfBuzz) shaping fixes it and is a no-op
    for Latin text.
    """
    text = f"ass=filename={escape_filter_path(_abs(ass_path))}:shaping=complex"
    if fonts_dir is not None:
        text += f":fontsdir={escape_filter_path(_abs(fonts_dir))}"
    return text


def _has_fonts(fonts_dir: Path | None) -> bool:
    if fonts_dir is None:
        return False
    d = Path(fonts_dir)
    return d.is_dir() and any(p.suffix.lower() in FONT_SUFFIXES for p in d.rglob("*") if p.is_file())


def mix_burn(
    video: Path,
    dst: Path,
    *,
    voice: Path | None,
    ass: Path | None,
    original_volume: float,
    voice_volume: float,
    fonts_dir: Path | None,
    duration: float | None = None,
    on_progress: ProgressFn | None = None,
) -> Path:
    """Mix the voice-over over the (quieted) original audio and burn ASS subtitles.

    Output length follows the video (``-t duration`` when given). With ``voice=None`` the output
    carries the original audio only, at ``original_volume``. ``fonts_dir`` is passed to libass only
    when it actually contains font files; otherwise system fonts (fontconfig) are used.
    """
    video, dst = Path(video), Path(dst)
    info = probe(video)
    if not info.has_video:
        raise FFmpegError(f"cannot render {video.name}: it has no video stream")
    if voice is not None and not Path(voice).is_file():
        raise FFmpegError(f"voice file not found: {voice}")
    if ass is not None and not Path(ass).is_file():
        raise FFmpegError(f"subtitle file not found: {ass}")

    args = ["-i", str(video)]
    inputs = 1
    voice_idx = None
    if voice is not None:
        args += ["-i", str(voice)]
        voice_idx = inputs
        inputs += 1
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
        parts.append(f"[0:v]{ass_filter(Path(ass), fonts_dir if _has_fonts(fonts_dir) else None)}[v]")
        video_map = "[v]"
    else:
        video_map = "0:v:0"
    if voice_idx is not None:
        # The voice is downmixed to mono and copied to both channels, so voice_volume=1.0 is unity.
        # No apad: amix duration=first already keeps the original running after the voice ends, and
        # an endless apad input with -t intermittently deadlocks ffmpeg 7.1 under load.
        parts.append(f"{orig}{orig_stereo},volume={_num(original_volume)}[a0]")
        parts.append(f"[{voice_idx}:a]{_MONO_TO_STEREO},volume={_num(voice_volume)}[a1]")
        parts.append(f"[a0][a1]amix=inputs=2:normalize=0:duration=first,{_LIMITER}[a]")
    else:
        parts.append(f"{orig}{orig_stereo},volume={_num(original_volume)},{_LIMITER}[a]")

    total = duration if duration is not None else info.duration
    with _AtomicOutput(dst) as tmp:
        args += ["-filter_complex", ";".join(parts), "-map", video_map, "-map", "[a]", *_X264, *_AAC]
        if duration is not None:
            args += ["-t", _num(duration)]
        elif not info.has_audio:
            args += ["-shortest"]  # the generated silence is endless
        args += ["-movflags", "+faststart", str(tmp)]
        run(args, desc=f"render {dst.name}", on_progress=on_progress, total=total)
    return dst


def synth_video(dst: Path, seconds: float, width: int = 1280, height: int = 720, audio: bool = True) -> Path:
    """Synthetic testsrc2 (+ 440 Hz sine) clip, handy for demos and `doctor`-style self checks."""
    dst = Path(dst)
    args = ["-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=25:duration={_num(seconds)}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={_num(seconds)}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    with _AtomicOutput(dst) as tmp:
        run([*args, str(tmp)], desc="synthesize test video")
    return dst
