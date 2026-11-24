"""Ingest stage: bring the source video into the job dir and extract 16 kHz mono audio for ASR."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable

from .. import ffmpeg
from ..interfaces import ProgressFn

__all__ = ["IngestError", "is_url", "find_source", "ingest", "extract_audio_for_asr", "YTDLP_FORMAT"]

YTDLP_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/b"
# Leftovers of interrupted copies/downloads; never treat them as the source.
_IGNORED_SUFFIXES = {".part", ".ytdl", ".tmp", ".temp"}


class IngestError(RuntimeError):
    """The source could not be fetched, copied or decoded."""


def is_url(source: str) -> bool:
    s = source.strip().lower()
    return s.startswith("http://") or s.startswith("https://")


def _done(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def find_source(job_dir: Path) -> Path | None:
    """Return the finished ``source.<ext>`` in ``job_dir``, or None.

    Only names whose stem is exactly ``source`` count, so yt-dlp intermediates such as
    ``source.f137.mp4``, ``source.mp4.part`` or ``source.temp.mp4`` and our own
    ``source.part.mp4`` are ignored.
    """
    job_dir = Path(job_dir)
    if not job_dir.is_dir():
        return None
    for p in sorted(job_dir.glob("source.*")):
        if p.stem == "source" and p.suffix.lower() not in _IGNORED_SUFFIXES and _done(p):
            return p
    return None


def _report(on_progress: ProgressFn | None, frac: float, msg: str) -> None:
    if on_progress is not None:
        on_progress(min(max(frac, 0.0), 1.0), msg)


def _check_has_video(path: Path) -> None:
    try:
        info = ffmpeg.probe(path)
    except ffmpeg.FFmpegError as e:
        raise IngestError(f"{path.name} is not a readable media file: {e}") from e
    if not info.has_video:
        raise IngestError(f"{path.name} has no video stream - Auto Clipper needs a video source")


def _copy_local(source: str, job_dir: Path, on_progress: ProgressFn | None) -> Path:
    src = Path(source).expanduser()
    if not src.is_file():
        raise IngestError(f"source file not found: {src}")
    _check_has_video(src)
    suffix = src.suffix.lower() or ".bin"
    dst = job_dir / f"source{suffix}"
    tmp = job_dir / f"source.part{suffix}"
    _report(on_progress, 0.0, f"copying {src.name}")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise IngestError(f"could not copy {src} into {job_dir}: {e}") from e
    _report(on_progress, 1.0, "copied")
    return dst


def _reject(path: Path) -> Path | None:
    """Move an unusable download aside as ``source.rejected.<ext>`` so find_source skips it."""
    dst = path.with_name(f"source.rejected{path.suffix}")
    try:
        os.replace(path, dst)
        return dst
    except OSError:
        path.unlink(missing_ok=True)
        return None


InfoFn = Callable[[dict], None]


def _download(url: str, job_dir: Path, on_progress: ProgressFn | None, on_info: InfoFn | None = None) -> Path:
    import yt_dlp  # imported lazily: heavy, and tests monkeypatch yt_dlp.YoutubeDL

    best = [0.0]  # video+audio formats download as separate files; never report going backwards

    def hook(d: dict[str, Any]) -> None:
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            if total:
                best[0] = max(best[0], min(0.98 * done / total, 0.98))
                _report(on_progress, best[0], "downloading")
        elif status == "finished":
            _report(on_progress, max(best[0], 0.5), "download finished, merging")

    opts: dict[str, Any] = {
        "outtmpl": str(job_dir / "source.%(ext)s"),
        "format": YTDLP_FORMAT,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
    }
    _report(on_progress, 0.0, f"downloading {url}")
    info: Any = None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            if on_info is not None and hasattr(ydl, "extract_info"):
                # same download, but keep the page info (title, uploader, description, tags) for the context
                info = ydl.extract_info(url, download=True)
            else:
                ydl.download([url])
    except Exception as e:  # yt_dlp.utils.DownloadError and friends
        raise IngestError(f"download failed for {url}: {e}") from e
    path = find_source(job_dir)
    if path is None:
        raise IngestError(f"yt-dlp finished but no source file was written to {job_dir} for {url}")
    try:
        _check_has_video(path)
    except IngestError:
        _reject(path)
        raise
    if on_info is not None and isinstance(info, dict):
        try:
            on_info(info)
        except Exception:  # noqa: BLE001 - the context is optional; never fail a finished download for it
            pass
    _report(on_progress, 1.0, "downloaded")
    return path


def ingest(source: str, job_dir: Path, *, on_progress: ProgressFn | None = None,
           on_info: InfoFn | None = None) -> Path:
    """Copy a local file or download a URL to ``job_dir/source.<ext>``. Idempotent.

    ``on_info`` receives yt-dlp's info dict for a URL download (the source context); never for a file."""
    job_dir = Path(job_dir)
    existing = find_source(job_dir)
    if existing is not None:
        try:
            _check_has_video(existing)
        except IngestError as e:
            raise IngestError(
                f"{existing.name} in {job_dir} has no video stream; delete it or start a new job ({e})"
            ) from e
        _report(on_progress, 1.0, "source already present")
        return existing
    if not source or not source.strip():
        raise IngestError("no source given - pass a video file path or an http(s) URL")
    job_dir.mkdir(parents=True, exist_ok=True)
    if is_url(source):
        return _download(source.strip(), job_dir, on_progress, on_info)
    return _copy_local(source, job_dir, on_progress)


def extract_audio_for_asr(source: Path, job_dir: Path) -> Path:
    """Write ``job_dir/audio.wav`` (16 kHz mono pcm_s16le) for transcription. Idempotent."""
    dst = Path(job_dir) / "audio.wav"
    if _done(dst):
        return dst
    source = Path(source)
    try:
        info = ffmpeg.probe(source)
    except ffmpeg.FFmpegError as e:
        raise IngestError(f"cannot read {source}: {e}") from e
    if not info.has_audio:
        raise IngestError(f"{source.name} has no audio track, so there is nothing to transcribe")
    try:
        return ffmpeg.to_wav(source, dst, sample_rate=16000, channels=1)
    except ffmpeg.FFmpegError as e:
        raise IngestError(f"audio extraction failed: {e}") from e
