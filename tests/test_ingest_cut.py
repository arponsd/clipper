"""Tests for the ingest and cut stages (yt-dlp is faked; nothing is downloaded)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yt_dlp

from fakes import make_video
from autoclipper import ffmpeg
from autoclipper.config import ClipSettings
from autoclipper.models import Moment
from autoclipper.stages import ingest as ingest_mod
from autoclipper.stages.cut import cut_clip
from autoclipper.stages.ingest import IngestError, extract_audio_for_asr, find_source, ingest, is_url


@pytest.fixture(scope="module")
def video(tmp_path_factory) -> Path:
    return make_video(tmp_path_factory.mktemp("media") / "Input Video.MP4", seconds=4, width=640, height=360)


def audio_stream(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return next(s for s in json.loads(out)["streams"] if s["codec_type"] == "audio")


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize("s, expected", [
    ("https://youtu.be/abc", True), ("http://x.org/v.mp4", True), ("  HTTPS://X.ORG ", True),
    ("/home/me/v.mp4", False), (r"C:\videos\v.mp4", False), ("ftp://x/v.mp4", False), ("", False),
])
def test_is_url(s, expected):
    assert is_url(s) is expected


def test_find_source_ignores_partials(tmp_path):
    assert find_source(tmp_path / "missing") is None
    for name in ["source.mp4.part", "source.f137.mp4", "source.temp.mp4", "source.part.mp4",
                 "source.webm.ytdl", "source.tmp", "other.mp4"]:
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "source.mkv").write_bytes(b"")  # empty -> not finished
    assert find_source(tmp_path) is None
    (tmp_path / "source.mp4").write_bytes(b"data")
    assert find_source(tmp_path) == tmp_path / "source.mp4"


# --------------------------------------------------------------------------- local ingest


def test_ingest_local_copy_and_idempotent(video, tmp_path):
    job = tmp_path / "job"
    seen: list[float] = []
    out = ingest(str(video), job, on_progress=lambda f, m: seen.append(f))
    assert out == job / "source.mp4"  # suffix lower-cased
    assert out.read_bytes() == video.read_bytes()
    assert seen[-1] == 1.0
    assert not list(job.glob("source.part*"))

    mtime = out.stat().st_mtime_ns
    # Second call returns the existing file even though the original source is gone.
    assert ingest(str(tmp_path / "gone.mp4"), job) == out
    assert out.stat().st_mtime_ns == mtime


def test_ingest_local_errors(tmp_path):
    with pytest.raises(IngestError, match="source file not found"):
        ingest(str(tmp_path / "nope.mp4"), tmp_path / "job")
    junk = tmp_path / "notes.txt"
    junk.write_text("hello", encoding="utf-8")
    with pytest.raises(IngestError, match="not a readable media file"):
        ingest(str(junk), tmp_path / "job")
    with pytest.raises(IngestError, match="no source given"):
        ingest("  ", tmp_path / "job")


def test_ingest_rejects_audio_only(tmp_path):
    from fakes import make_tone
    with pytest.raises(IngestError, match="no video stream"):
        ingest(str(make_tone(tmp_path / "a.wav", 1.0)), tmp_path / "job")


# --------------------------------------------------------------------------- URL ingest (fake yt-dlp)


class FakeYDL:
    instances: list["FakeYDL"] = []
    sample: Path
    fail: bool = False

    def __init__(self, opts: dict):
        self.opts = opts
        self.urls: list[str] = []
        FakeYDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls: list[str]) -> int:
        self.urls = list(urls)
        if self.fail:
            raise yt_dlp.utils.DownloadError("ERROR: Video unavailable")
        target = Path(self.opts["outtmpl"].replace("%(ext)s", "mp4"))
        hooks = self.opts["progress_hooks"]
        for done in (0, 500, 1000):
            for h in hooks:
                h({"status": "downloading", "downloaded_bytes": done, "total_bytes": 1000})
        shutil.copy(self.sample, target)
        for h in hooks:
            h({"status": "finished", "filename": str(target)})
        return 0


@pytest.fixture
def fake_ydl(monkeypatch, video):
    FakeYDL.instances = []
    FakeYDL.sample = video
    FakeYDL.fail = False
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


def test_ingest_url_uses_ytdlp(fake_ydl, tmp_path):
    job = tmp_path / "job"
    seen: list[float] = []
    out = ingest("https://example.com/watch?v=1", job, on_progress=lambda f, m: seen.append(f))
    assert out == job / "source.mp4" and out.stat().st_size > 0
    (ydl,) = fake_ydl.instances
    assert ydl.urls == ["https://example.com/watch?v=1"]
    o = ydl.opts
    assert o["outtmpl"] == str(job / "source.%(ext)s")
    assert o["merge_output_format"] == "mp4"
    assert o["format"] == "bv*[height<=1080]+ba/b[height<=1080]/b"
    assert o["noplaylist"] is True and o["quiet"] is True
    assert seen == sorted(seen) and seen[-1] == 1.0 and all(0 <= f <= 1 for f in seen)
    assert any(0 < f < 1 for f in seen)

    # idempotent: no second download
    assert ingest("https://example.com/watch?v=1", job) == out
    assert len(fake_ydl.instances) == 1


def test_ingest_url_download_error(fake_ydl, tmp_path):
    fake_ydl.fail = True
    with pytest.raises(IngestError, match="download failed.*Video unavailable"):
        ingest("https://example.com/x", tmp_path / "job")


def test_ingest_url_nothing_written(monkeypatch, tmp_path):
    class Silent(FakeYDL):
        def download(self, urls):
            return 0

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Silent)
    with pytest.raises(IngestError, match="no source file was written"):
        ingest("https://example.com/x", tmp_path / "job")


# --------------------------------------------------------------------------- audio for ASR


def test_extract_audio_for_asr(video, tmp_path):
    out = extract_audio_for_asr(video, tmp_path)
    assert out == tmp_path / "audio.wav"
    a = audio_stream(out)
    assert (a["codec_name"], a["sample_rate"], a["channels"]) == ("pcm_s16le", "16000", 1)
    assert ffmpeg.duration(out) == pytest.approx(4.0, abs=0.1)
    mtime = out.stat().st_mtime_ns
    assert extract_audio_for_asr(video, tmp_path) == out
    assert out.stat().st_mtime_ns == mtime


def test_extract_audio_for_asr_no_audio(tmp_path):
    silent = make_video(tmp_path / "silent.mp4", seconds=1, width=320, height=240, audio=False)
    with pytest.raises(IngestError, match="no audio track"):
        extract_audio_for_asr(silent, tmp_path / "job")


# --------------------------------------------------------------------------- cut stage


@pytest.mark.parametrize("mode", ["crop", "blur"])
def test_cut_clip_writes_vertical_with_audio(video, tmp_path, mode):
    dst = tmp_path / "clips" / "00" / "cut.mp4"
    seen: list[float] = []
    out = cut_clip(video, Moment(start=0.5, end=2.0, score=9), dst, ClipSettings(reframe=mode),
                   on_progress=lambda f, m: seen.append(f))
    assert out == dst
    info = ffmpeg.probe(out)
    assert (info.width, info.height, info.has_audio) == (1080, 1920, True)
    assert info.duration == pytest.approx(1.5, abs=0.1)
    assert audio_stream(out)["sample_rate"] == "48000"
    assert seen and seen[-1] == 1.0


def test_cut_clip_source_without_audio(tmp_path):
    silent = make_video(tmp_path / "silent.mp4", seconds=2, width=320, height=240, audio=False)
    out = cut_clip(silent, Moment(start=0, end=1), tmp_path / "cut.mp4", ClipSettings(width=270, height=480))
    assert ffmpeg.probe(out).has_audio


def test_cut_clip_bad_range(video, tmp_path):
    with pytest.raises(ffmpeg.FFmpegError, match="empty range"):
        cut_clip(video, Moment(start=10, end=12), tmp_path / "cut.mp4", ClipSettings())


def test_ingest_module_format_constant():
    assert ingest_mod.YTDLP_FORMAT == "bv*[height<=1080]+ba/b[height<=1080]/b"
