"""Cheap local audio/video signals for moment picking (feature "moment picking for low-speech videos").

Computed once per job at the ``signals`` stage and cached in ``signals.json`` (recomputed only when the
source, audio.wav, the analysis parameters or the format version change):

- audio, from ``audio.wav`` (16 kHz mono) with numpy: per-second loudness (RMS dBFS) and the biggest
  sudden jump per second (a 50 ms frame's level above the median of the 3 s before it: gunshots,
  crashes, sirens, music hits);
- video, from ONE ffmpeg decode at low resolution and a low frame rate (default 96x54 grey at 4 fps):
  per-second motion (share of pixels that changed between samples), scene cuts (a large histogram change
  that stands out from its neighbours) and static spans (very little movement for 2 s or more: menus,
  maps, pause or loading screens).

``window_summary`` turns this into a compact per-window table for the LLM; ``MomentSignals`` gives the
moments stage the per-second data for boundary fallback (scene cuts, quiet points) and the static post-filter.
No ML, no network.
"""
from __future__ import annotations

import json
import math
import subprocess
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .. import ffmpeg
from ..interfaces import ProgressFn
from ..models import Transcript
from ._atomic import write_text_atomic

SIGNALS_FILE = "signals.json"
SIGNALS_VERSION = 2  # 2: sudden jumps measured against a floor (a rise out of silence is not a hit)
SAMPLE_RATE = 16000
FRAME_SECONDS = 0.05  # audio analysis frame (50 ms)
ONSET_CONTEXT_SECONDS = 3.0
# A jump is measured from at least this far below the video's median loudness: in games the map / menu
# mutes the sound, and the sound coming back after it would otherwise look like the biggest "hit".
ONSET_FLOOR_BELOW_MEDIAN = 10.0
# Near-silence without speech for MIN_STATIC_RUN s or more is a span to avoid (in games: map, menu, loading).
SILENCE_DB = -50.0              # a second is silent below min(SILENCE_DB, median - SILENCE_BELOW_MEDIAN)
SILENCE_BELOW_MEDIAN = 20.0
SILENCE_BRIDGE_SECONDS = 3      # short gaps between silent runs (UI clicks on a map) are bridged ...
SILENCE_BRIDGE_BELOW_MEDIAN = 6.0  # ... when they stay this far below the median loudness
SILENCE_PAD_SECONDS = 1         # the second before and after a silent run (opening / closing the map)
MAX_FRAME_BUFFER = 64           # frames read from ffmpeg at once (features are computed per chunk)
FLOOR_DB = -90.0
PIXEL_CHANGE = 12  # grey levels (0-255): a pixel "changed" between two samples
HIST_BINS = 16
MIN_STATIC_RUN = 2  # seconds: shorter low-motion runs (a held frame) are not "static"
MAX_WINDOWS = 360  # the LLM table never gets more lines than this (the window grows instead)


class SignalsError(RuntimeError):
    """The signals could not be computed (the pipeline then picks moments from the transcript only)."""


# --------------------------------------------------------------------------------------------- data
@dataclass
class Signals:
    duration: float
    loudness_db: list[float]            # per second, RMS dBFS
    onset_db: list[float]               # per second, biggest sudden jump in dB (>= 0)
    motion: list[float] | None = None   # per second, share of pixels changed between samples (0..1)
    static: list[bool] | None = None    # per second, part of a low-motion run of >= MIN_STATIC_RUN s
    flatness: list[float] | None = None # per second, share of flat (texture-less) pixels (0..1)
    menu: list[bool] | None = None      # per second, part of a very flat run of >= MIN_STATIC_RUN s (map,
    #                                     menu, pause blur, fade): game UI rather than gameplay
    cuts: list[float] = field(default_factory=list)  # scene-cut times in seconds
    params: dict = field(default_factory=dict)
    compute_seconds: float = 0.0

    @property
    def has_video(self) -> bool:
        return self.motion is not None

    def to_dict(self) -> dict:
        return {"version": SIGNALS_VERSION, "duration": self.duration, "params": self.params,
                "compute_seconds": round(self.compute_seconds, 2),
                "loudness_db": [round(x, 1) for x in self.loudness_db],
                "onset_db": [round(x, 1) for x in self.onset_db],
                "motion": [round(x, 4) for x in self.motion] if self.motion is not None else None,
                "static": [int(x) for x in self.static] if self.static is not None else None,
                "flatness": [round(x, 3) for x in self.flatness] if self.flatness is not None else None,
                "menu": [int(x) for x in self.menu] if self.menu is not None else None,
                "cuts": [round(x, 2) for x in self.cuts]}

    @classmethod
    def from_dict(cls, d: dict) -> "Signals":
        motion = d.get("motion")
        static = d.get("static")
        flat = d.get("flatness")
        menu = d.get("menu")
        return cls(duration=float(d["duration"]), loudness_db=[float(x) for x in d["loudness_db"]],
                   onset_db=[float(x) for x in d["onset_db"]],
                   motion=[float(x) for x in motion] if motion is not None else None,
                   static=[bool(x) for x in static] if static is not None else None,
                   flatness=[float(x) for x in flat] if flat is not None else None,
                   menu=[bool(x) for x in menu] if menu is not None else None,
                   cuts=[float(x) for x in d.get("cuts") or []], params=dict(d.get("params") or {}),
                   compute_seconds=float(d.get("compute_seconds") or 0.0))


# --------------------------------------------------------------------------------------------- audio
def _read_wav(path: Path) -> np.ndarray | None:
    """16 kHz mono 16-bit wav -> float32 samples in -1..1; None when it is another format."""
    try:
        with wave.open(str(path), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != SAMPLE_RATE:
                return None
            raw = w.readframes(w.getnframes())
    except (OSError, wave.Error, EOFError):
        return None
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _decode_audio(source: Path, timeout: float) -> np.ndarray:
    """Any media -> 16 kHz mono float samples (empty when the file has no audio)."""
    info = ffmpeg.probe(source)
    if not info.has_audio:
        return np.zeros(0, dtype=np.float32)
    cmd = [ffmpeg.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(source), "-map", "0:a:0",
           "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise SignalsError(f"audio analysis took longer than {timeout:.0f} s") from e
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise SignalsError("audio decode failed: " + " | ".join(err))
    return np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0


def audio_features(samples: np.ndarray, duration: float) -> tuple[list[float], list[float]]:
    """Per-second (loudness dBFS, biggest sudden jump dB) for ``ceil(duration)`` seconds."""
    n_sec = max(1, int(math.ceil(duration - 1e-6)))
    hop = int(SAMPLE_RATE * FRAME_SECONDS)
    per_sec = int(round(1.0 / FRAME_SECONDS))
    n_frames = n_sec * per_sec
    x = np.zeros(n_frames * hop, dtype=np.float32)
    m = min(len(samples), len(x))
    x[:m] = samples[:m]
    power = np.mean(x.reshape(n_frames, hop) ** 2, axis=1)
    frame_db = np.maximum(10.0 * np.log10(power + 1e-12), FLOOR_DB)
    sec_power = power.reshape(n_sec, per_sec).mean(axis=1)
    loud = np.maximum(10.0 * np.log10(sec_power + 1e-12), FLOOR_DB)
    ctx = int(ONSET_CONTEXT_SECONDS / FRAME_SECONDS)
    padded = np.concatenate([np.full(ctx, frame_db[0] if n_frames else FLOOR_DB), frame_db])
    windows = np.lib.stride_tricks.sliding_window_view(padded[:-1], ctx)  # the ctx frames BEFORE each frame
    base = np.median(windows, axis=1)
    if n_sec:
        base = np.maximum(base, float(np.median(loud)) - ONSET_FLOOR_BELOW_MEDIAN)
    jump = np.maximum(frame_db - base, 0.0)
    jump[frame_db < -60.0] = 0.0  # a "jump" into near-silence is noise, not an event
    onset = jump.reshape(n_sec, per_sec).max(axis=1)
    return [float(v) for v in loud], [float(v) for v in onset]


# --------------------------------------------------------------------------------------------- video
class _FrameStats:
    """Per-frame video features computed chunk by chunk while ffmpeg streams the frames, so memory stays
    small whatever the video length or the analysis size: change share and histogram distance between
    consecutive frames, and the flat share of each frame."""

    def __init__(self) -> None:
        self.changed: list[np.ndarray] = []
        self.hdist: list[np.ndarray] = []
        self.flat: list[np.ndarray] = []
        self._prev: np.ndarray | None = None
        self._prev_hist: np.ndarray | None = None
        self.n = 0

    def add(self, chunk: np.ndarray) -> None:
        if len(chunk) == 0:
            return
        hists = _hists(chunk)
        if self._prev is not None:
            frames = np.concatenate([self._prev[None], chunk])
            hists_all = np.concatenate([self._prev_hist[None], hists])
        else:
            frames, hists_all = chunk, hists
        if len(frames) >= 2:
            self.changed.append(_changed_share(frames))
            self.hdist.append(0.5 * np.abs(hists_all[1:] - hists_all[:-1]).sum(axis=1))
        self.flat.append(flat_share(chunk))
        self._prev = chunk[-1].copy()
        self._prev_hist = hists[-1]
        self.n += len(chunk)

    @staticmethod
    def _cat(parts: list[np.ndarray]) -> np.ndarray:
        return np.concatenate(parts) if parts else np.zeros(0)

    def arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return self._cat(self.changed), self._cat(self.hdist), self._cat(self.flat)


def _hists(frames: np.ndarray) -> np.ndarray:
    """Per frame: normalised 16-bin grey histogram."""
    h = np.stack([np.bincount((fr >> 4).ravel(), minlength=HIST_BINS) for fr in frames]).astype(float)
    return h / np.maximum(h.sum(axis=1, keepdims=True), 1.0)


def _scan_frames(source: Path, *, fps: float, width: int, height: int, duration: float, timeout: float,
                 on_progress: ProgressFn | None) -> _FrameStats:
    """One low-resolution grey ffmpeg decode; the features are computed per chunk as the frames arrive
    (never all frames in memory)."""
    vf = f"fps={fps:g},scale={width}:{height}:flags=area,format=gray"
    cmd = [ffmpeg.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "0",
           "-i", str(source), "-map", "0:v:0", "-an", "-sn", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    frame_bytes = width * height
    expected = max(1, int(duration * fps))
    stats = _FrameStats()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timer = threading.Timer(timeout, proc.kill)
    timer.daemon = True
    timer.start()
    err_chunks: list[bytes] = []
    reader = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read() if proc.stderr else b""),
                              daemon=True)
    reader.start()
    pending = bytearray()
    last = 0.0
    started = time.monotonic()
    try:
        assert proc.stdout is not None
        while True:
            buf = proc.stdout.read(frame_bytes * MAX_FRAME_BUFFER)
            if not buf:
                break
            pending += buf
            k = len(pending) // frame_bytes
            if k:
                chunk = np.frombuffer(bytes(pending[:k * frame_bytes]), dtype=np.uint8).reshape(k, height, width)
                del pending[:k * frame_bytes]
                stats.add(chunk)
            if on_progress is not None and time.monotonic() - last > 1.0:
                last = time.monotonic()
                on_progress(min(0.99, stats.n / expected), "scanning video")
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        reader.join(timeout=5)
    if time.monotonic() - started >= timeout - 0.5 and proc.returncode != 0:
        raise SignalsError(f"video analysis took longer than {timeout:.0f} s (signals.timeout_seconds)")
    if proc.returncode != 0:
        err = b"".join(err_chunks).decode("utf-8", "replace").strip().splitlines()[-3:]
        raise SignalsError("video decode failed: " + " | ".join(err))
    return stats


def _runs(flags: np.ndarray, min_run: int = MIN_STATIC_RUN) -> np.ndarray:
    """Keep only runs of True that last at least ``min_run`` seconds."""
    out = np.zeros(len(flags), dtype=bool)
    i, n = 0, len(flags)
    while i < n:
        if flags[i]:
            j = i
            while j < n and flags[j]:
                j += 1
            if j - i >= min_run:
                out[i:j] = True
            i = j
        else:
            i += 1
    return out


def _per_second(values: np.ndarray, times: np.ndarray, n_sec: int) -> np.ndarray:
    """Mean of sample values per second; seconds without a sample take the nearest sampled second."""
    acc = np.zeros(n_sec)
    counts = np.zeros(n_sec)
    if len(values):
        idx = np.clip(times.astype(int), 0, n_sec - 1)
        np.add.at(acc, idx, values)
        np.add.at(counts, idx, 1)
    out = np.where(counts > 0, acc / np.maximum(counts, 1), 0.0)
    if (counts == 0).any() and (counts > 0).any():
        have = np.flatnonzero(counts > 0)
        for i in np.flatnonzero(counts == 0):
            out[i] = out[have[np.argmin(np.abs(have - i))]]
    return out


CHUNK_FRAMES = 1024  # frames processed at once (keeps the int16 copies small for long videos)


def _changed_share(frames: np.ndarray) -> np.ndarray:
    """Per consecutive pair: share of pixels whose grey level changed by more than PIXEL_CHANGE."""
    out = np.zeros(max(0, len(frames) - 1))
    for a in range(0, len(frames) - 1, CHUNK_FRAMES):
        f = frames[a:a + CHUNK_FRAMES + 1].astype(np.int16)
        out[a:a + len(f) - 1] = (np.abs(f[1:] - f[:-1]) > PIXEL_CHANGE).reshape(len(f) - 1, -1).mean(axis=1)
    return out


def flat_share(frames: np.ndarray) -> np.ndarray:
    """Per frame: share of pixels with almost no local contrast. Maps, menus, pause blurs and fades are far
    flatter (about 0.65 at 96x54) than gameplay or camera footage (0.2-0.4)."""
    out = np.zeros(len(frames))
    for a in range(0, len(frames), CHUNK_FRAMES):
        f = frames[a:a + CHUNK_FRAMES].astype(np.int16)
        gx = np.abs(np.diff(f, axis=2))[:, :-1, :]
        gy = np.abs(np.diff(f, axis=1))[:, :, :-1]
        out[a:a + len(f)] = ((gx + gy) < 4).reshape(len(f), -1).mean(axis=1)
    return out


@dataclass
class VideoFeatures:
    motion: list[float]
    static: list[bool]
    flatness: list[float]
    menu: list[bool]
    cuts: list[float]


def video_features(frames: np.ndarray, fps: float, duration: float, *, static_motion: float,
                   cut_threshold: float, menu_flatness: float = 0.6) -> VideoFeatures:
    """Per-second motion / static / flatness / menu flags and scene-cut times from sampled grey frames."""
    stats = _FrameStats()
    for a in range(0, len(frames), CHUNK_FRAMES):
        stats.add(frames[a:a + CHUNK_FRAMES])
    return features_from_stats(stats, fps, duration, static_motion=static_motion, cut_threshold=cut_threshold,
                               menu_flatness=menu_flatness)


def features_from_stats(stats: _FrameStats, fps: float, duration: float, *, static_motion: float,
                        cut_threshold: float, menu_flatness: float = 0.6) -> VideoFeatures:
    n_sec = max(1, int(math.ceil(duration - 1e-6)))
    n = stats.n
    changed, hdist, flat_frames = stats.arrays()
    cuts: list[float] = []
    pair_times = np.zeros(0)
    if n >= 2:
        # a cut: the picture changes a lot AND clearly more than in the samples around it (fast camera
        # motion changes the pixels in many samples in a row, a cut only in one)
        score = 0.5 * hdist + 0.5 * changed
        k = 4
        pad = np.pad(score, (k, k), mode="edge")
        neigh = np.stack([pad[i:i + len(score)] for i in range(2 * k + 1) if i != k])
        local = np.median(neigh, axis=0)
        pair_times = np.arange(1, n) / fps  # time of the later sample of each pair
        is_cut = (score >= cut_threshold) & (score >= 2.0 * local + 0.05) & (changed >= 0.3)
        last_cut = -10.0
        for t, c in zip(pair_times, is_cut):
            if c and t - last_cut >= 0.75:
                cuts.append(float(round(t, 2)))
                last_cut = t
    motion = _per_second(changed, pair_times - 0.5 / fps, n_sec)
    flat = _per_second(flat_frames, np.arange(n) / fps, n_sec)
    static = _runs(motion < static_motion) if n >= 2 else np.zeros(n_sec, dtype=bool)
    menu = _runs(flat >= menu_flatness) if n >= 1 else np.zeros(n_sec, dtype=bool)
    return VideoFeatures(motion=[float(v) for v in motion], static=[bool(v) for v in static],
                         flatness=[float(v) for v in flat], menu=[bool(v) for v in menu], cuts=cuts)


# --------------------------------------------------------------------------------------------- stage
def _params(settings: Any) -> dict:
    return {"video": bool(settings.video), "fps": float(settings.sample_fps), "width": int(settings.sample_width),
            "static_motion": float(settings.static_motion), "cut_threshold": float(settings.cut_threshold),
            "menu_flatness": float(settings.menu_flatness)}


def _stamp(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return {"name": Path(path).name, "size": st.st_size, "mtime": int(st.st_mtime)}


def load_signals(path: Path) -> Signals | None:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(d, dict) or d.get("version") != SIGNALS_VERSION:
            return None
        return Signals.from_dict(d)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def signals_current(path: Path, source: Path, audio: Path | None, settings: Any) -> Signals | None:
    """The cached signals when they still match the source, audio.wav and the analysis settings."""
    sig = load_signals(path)
    if sig is None:
        return None
    p = sig.params
    if p.get("analysis") != _params(settings):
        return None
    if p.get("source") != _stamp(source) or p.get("audio") != _stamp(audio):
        return None
    return sig


def compute_signals(source: Path, out_json: Path, settings: Any, *, audio: Path | None = None,
                    on_progress: ProgressFn | None = None) -> Signals:
    """Compute (or reuse, when current) the job's signals and write ``out_json``.

    ``settings`` is config.SignalSettings. ``audio`` is the job's audio.wav (16 kHz mono); without it the
    audio is decoded from the source.
    """
    source = Path(source)
    out_json = Path(out_json)
    if audio is not None and not Path(audio).is_file():
        audio = None
    cached = signals_current(out_json, source, audio, settings)
    if cached is not None:
        return cached
    t0 = time.monotonic()
    try:
        info = ffmpeg.probe(source)
    except ffmpeg.FFmpegError as e:
        raise SignalsError(f"cannot read {source.name}: {e}") from e
    duration = float(info.duration or 0.0)
    if duration <= 0:
        raise SignalsError(f"{source.name} has no duration")
    timeout = float(settings.timeout_seconds)
    samples = _read_wav(audio) if audio is not None else None
    if samples is None:
        samples = _decode_audio(source, timeout)
    loud, onset = audio_features(samples, duration)
    if on_progress is not None:
        on_progress(0.05, "audio analysed")
    vf: VideoFeatures | None = None
    if settings.video and info.has_video:
        width = int(settings.sample_width)
        height = max(8, int(round(width * 9 / 16 / 2)) * 2)
        stats = _scan_frames(source, fps=float(settings.sample_fps), width=width, height=height,
                             duration=duration, timeout=max(1.0, timeout - (time.monotonic() - t0)),
                             on_progress=(lambda f, m: on_progress(0.05 + 0.9 * f, m)) if on_progress else None)
        vf = features_from_stats(stats, float(settings.sample_fps), duration,
                                 static_motion=float(settings.static_motion),
                                 cut_threshold=float(settings.cut_threshold),
                                 menu_flatness=float(settings.menu_flatness))
    sig = Signals(duration=duration, loudness_db=loud, onset_db=onset,
                  motion=vf.motion if vf else None, static=vf.static if vf else None,
                  flatness=vf.flatness if vf else None, menu=vf.menu if vf else None,
                  cuts=vf.cuts if vf else [],
                  params={"analysis": _params(settings), "source": _stamp(source), "audio": _stamp(audio)},
                  compute_seconds=time.monotonic() - t0)
    write_text_atomic(out_json, json.dumps(sig.to_dict(), ensure_ascii=False))
    if on_progress is not None:
        on_progress(1.0, "signals ready")
    return sig


# --------------------------------------------------------------------------------------------- speech
WORD_MAX_SECONDS = 1.0  # Whisper sometimes stretches the last word of a segment over a long silence


def _speech_spans(t: Transcript) -> list[tuple[float, float]]:
    """Where someone speaks: each word's span (at most WORD_MAX_SECONDS long), or the segment's span when
    it has no word timings."""
    spans: list[tuple[float, float]] = []
    for s in t.segments:
        if not s.text.strip():
            continue
        if s.words:
            for w in s.words:
                a = float(w.start)
                spans.append((a, max(a, min(float(w.end), a + WORD_MAX_SECONDS))))
        else:
            spans.append((float(s.start), float(s.end)))
    return spans


def speech_seconds(t: Transcript, n_sec: int) -> np.ndarray:
    """Per second: the share of that second with speech (0..1)."""
    n_sec = max(1, n_sec)
    cover = np.zeros(n_sec)
    for a, b in _speech_spans(t):
        a, b = max(0.0, a), min(float(n_sec), b)
        if b <= a:
            continue
        for i in range(int(math.floor(a)), min(int(math.ceil(b)), n_sec)):
            cover[i] += max(0.0, min(b, i + 1) - max(a, i))
    return np.minimum(cover, 1.0)


def speech_coverage(t: Transcript) -> float:
    """Share of the video with speech (union of the word spans)."""
    dur = float(t.duration or 0.0)
    if dur <= 0:
        return 0.0
    total, cur_a, cur_b = 0.0, None, None
    for a, b in sorted((max(0.0, a), min(dur, b)) for a, b in _speech_spans(t)):
        if b <= a:
            continue
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a
    return min(1.0, total / dur)


# --------------------------------------------------------------------------------------------- silence
def silent_seconds(loudness_db: Sequence[float], speech: np.ndarray | None = None) -> np.ndarray:
    """Per second: part of a near-silent stretch without speech (>= MIN_STATIC_RUN s). In games the map,
    menu and loading screens mute the game sound, while the picture may still pan or show clicks; the
    picture alone does not catch them reliably.

    Silent = below min(SILENCE_DB, median - SILENCE_BELOW_MEDIAN). Gaps of up to SILENCE_BRIDGE_SECONDS
    that stay well below the median (UI clicks) are bridged, and one second is added on each side (opening
    and closing the screen). Seconds with speech are never silent."""
    loud = np.asarray(loudness_db, dtype=float)
    n = len(loud)
    if n == 0:
        return np.zeros(0, dtype=bool)
    med = float(np.median(loud))
    quiet = loud < min(SILENCE_DB, med - SILENCE_BELOW_MEDIAN)
    if speech is not None:
        quiet &= np.asarray(speech[:n]) < 0.2
    runs = _runs(quiet)
    # bridge short, still-quiet gaps between two silent runs
    idx = np.flatnonzero(runs)
    out = runs.copy()
    for a, b in zip(idx[:-1], idx[1:]):
        gap = slice(a + 1, b)
        if 1 < b - a <= SILENCE_BRIDGE_SECONDS + 1 and (loud[gap] < med - SILENCE_BRIDGE_BELOW_MEDIAN).all():
            if speech is None or (np.asarray(speech[gap]) < 0.2).all():
                out[gap] = True
    if SILENCE_PAD_SECONDS:
        padded = out.copy()
        for i in np.flatnonzero(out):
            lo, hi = max(0, i - SILENCE_PAD_SECONDS), min(n, i + SILENCE_PAD_SECONDS + 1)
            padded[lo:hi] = True
        if speech is not None:
            padded &= (np.asarray(speech[:n]) < 0.2) | out
        out = padded
    return out


# --------------------------------------------------------------------------------------------- LLM table
def _ranks(values: np.ndarray) -> np.ndarray:
    """0..9 rank (decile) of each value among all values."""
    if len(values) == 0:
        return np.zeros(0, dtype=int)
    order = np.argsort(np.argsort(values, kind="stable"), kind="stable")
    return np.minimum(9, (order * 10) // max(1, len(values))).astype(int)


def window_size(duration: float, base: float) -> float:
    w = float(base)
    while duration / w > MAX_WINDOWS:
        w *= 2
    return w


def _mean(arr: np.ndarray | None, sl: slice) -> float | None:
    if arr is None:
        return None
    part = arr[sl]
    return float(part.mean()) if part.size else 0.0


def window_summary(sig: Signals, t: Transcript, *, window: float) -> str:
    """Compact per-window table for the moment picker: one line per window, e.g.
    ``[120-130] loud 8 jump 14 cuts 3 motion 6 speech 0%`` plus `` STATIC`` / `` MENU`` for spans to avoid."""
    dur = float(sig.duration)
    w = window_size(dur, window)
    n_sec = len(sig.loudness_db)
    loud = np.array(sig.loudness_db)
    onset = np.array(sig.onset_db)
    motion = np.array(sig.motion) if sig.motion is not None else None
    static = np.array(sig.static, dtype=float) if sig.static is not None else None
    menu = np.array(sig.menu, dtype=float) if sig.menu is not None else None
    speech = speech_seconds(t, n_sec)
    silent = silent_seconds(sig.loudness_db, speech).astype(float)
    cuts = np.array(sig.cuts)
    rows = []
    for a in np.arange(0.0, dur, w):
        b = min(dur, a + w)
        sl = slice(int(a), min(max(int(a) + 1, int(math.ceil(b))), n_sec))
        lin = float(np.mean(10 ** (loud[sl] / 10.0))) if loud[sl].size else 1e-9
        rows.append({
            "a": a, "b": b, "loud": 10 * math.log10(lin + 1e-12),
            "jump": float(onset[sl].max()) if onset[sl].size else 0.0,
            "cuts": int(((cuts >= a) & (cuts < b)).sum()) if cuts.size else 0,
            "motion": _mean(motion, sl), "static": _mean(static, sl) or 0.0, "menu": _mean(menu, sl) or 0.0,
            "silent": _mean(silent, sl) or 0.0, "speech": _mean(speech, sl) or 0.0,
        })
    if not rows:
        return ""
    loud_rank = _ranks(np.array([r["loud"] for r in rows]))
    motion_rank = _ranks(np.array([r["motion"] for r in rows])) if motion is not None else None
    lines = []
    for k, r in enumerate(rows):
        parts = [f"[{r['a']:.0f}-{r['b']:.0f}]", f"loud {loud_rank[k]}", f"jump {r['jump']:.0f}"]
        if motion_rank is not None:
            parts += [f"cuts {r['cuts']}", f"motion {motion_rank[k]}"]
        parts.append(f"speech {round(r['speech'] * 100):d}%")
        if r["static"] >= 0.5:
            parts.append("STATIC")
        elif r["menu"] >= 0.5:
            parts.append("MENU")
        elif r["silent"] >= 0.5:
            parts.append("SILENT")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def legend(window: float, has_video: bool) -> str:
    text = (f"One line per {window:g} s window: loud = loudness rank 0-9 within this video (9 = loudest); "
            "jump = biggest sudden loudness jump in dB above the video's usual level (gunshots, crashes, explosions, sirens, music hits; "
            "above about 15 is a strong hit)")
    if has_video:
        text += ("; cuts = scene cuts; motion = on-screen movement rank 0-9; STATIC = the picture barely "
                 "moves (frozen frame, pause, loading or end screen); MENU = a flat, texture-less picture "
                 "(map, menu, inventory, pause blur or fade), not gameplay")
    text += ("; SILENT = near silence without speech for 2 s or more (in games usually a map, menu or loading "
             "screen that mutes the sound)")
    return text + "; speech = share of the window with speech."


# --------------------------------------------------------------------------------------------- moments
@dataclass
class MomentSignals:
    """What the moments stage needs from the signals: boundary fallbacks and the static/menu post-filter."""
    avoid: np.ndarray | None           # per second: static, menu-like or silent without speech (bool)
    speech: np.ndarray                 # per second 0..1
    cuts: list[float]
    quiet: list[float]                 # second centres clearly quieter than the seconds around them
    coverage: float                    # speech share of the whole video
    low_speech: bool                   # coverage below signals.speech_rich_coverage
    reject_static_fraction: float
    static_speech_max: float = 0.5     # a static span WITH this much speech or more is kept (talking head)

    @staticmethod
    def _fraction(arr: np.ndarray | None, start: float, end: float) -> float:
        if arr is None or len(arr) == 0:
            return 0.0
        i0 = max(0, int(math.floor(start)))
        i1 = min(len(arr), max(i0 + 1, int(math.ceil(end))))
        part = arr[i0:i1]
        return float(np.mean(part)) if part.size else 0.0

    def static_fraction(self, start: float, end: float) -> float:
        return self._fraction(self.avoid.astype(float) if self.avoid is not None else None, start, end)

    def speech_fraction(self, start: float, end: float) -> float:
        return self._fraction(self.speech, start, end)

    def boundaries(self) -> list[float]:
        return sorted(set(self.cuts) | set(self.quiet))


def quiet_points(loudness_db: Sequence[float], *, drop_db: float = 10.0) -> list[float]:
    """Seconds clearly quieter than the 5 s around them (pauses between actions), as boundary candidates."""
    loud = np.array(loudness_db, dtype=float)
    n = len(loud)
    out: list[float] = []
    for i in range(n):
        a, b = max(0, i - 2), min(n, i + 3)
        around = np.concatenate([loud[a:i], loud[i + 1:b]])
        if around.size and loud[i] <= np.median(around) - drop_db and loud[i] <= loud[a:b].min() + 1e-9:
            out.append(i + 0.5)
    return out


def moment_signals(sig: Signals | None, t: Transcript, *, speech_rich_coverage: float,
                   reject_static_fraction: float) -> MomentSignals:
    n_sec = len(sig.loudness_db) if sig is not None else max(1, int(math.ceil(t.duration)))
    speech = speech_seconds(t, n_sec)
    avoid = None
    if sig is not None:
        avoid = silent_seconds(sig.loudness_db, speech)
        for flags in (sig.static, sig.menu):
            if flags is not None:
                f = np.array(flags[:n_sec], dtype=bool)
                avoid[:len(f)] |= f
    cov = speech_coverage(t)
    return MomentSignals(
        avoid=avoid, speech=speech,
        cuts=list(sig.cuts) if sig is not None else [],
        quiet=quiet_points(sig.loudness_db) if sig is not None else [],
        coverage=cov, low_speech=cov < speech_rich_coverage,
        reject_static_fraction=reject_static_fraction)
