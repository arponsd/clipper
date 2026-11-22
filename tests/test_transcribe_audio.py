"""WhisperTranscriber audio loading, long-audio windows, CUDA->CPU fallback and atomic transcript writes.

No model download: faster_whisper.WhisperModel is replaced by fakes. Audio comes from make_tone (ffmpeg).
"""
from __future__ import annotations

import json
import logging
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from autoclipper.config import WhisperSettings
from autoclipper.stages import transcribe as tr
from fakes import FakeTranscriber, make_tone


class _RecordingModel:
    """Fake WhisperModel that insists on a float32 ndarray (never a path) and records what it got."""

    instances: list = []
    fail_on: dict = {}  # device -> ("load" | "transcribe" | "iterate", message)

    def __init__(self, name, device, compute_type):
        self.device, self.compute_type = device, compute_type
        self.received: list = []
        kind, msg = self.fail_on.get(device, (None, None))
        if kind == "load":
            raise RuntimeError(msg)
        _RecordingModel.instances.append(self)

    def transcribe(self, audio, **kw):
        assert isinstance(audio, np.ndarray), f"expected an ndarray, got {type(audio).__name__}"
        assert audio.dtype == np.float32 and audio.ndim == 1
        self.received.append((len(audio), kw))
        kind, msg = self.fail_on.get(self.device, (None, None))
        if kind == "transcribe":
            raise RuntimeError(msg)
        dur = len(audio) / tr.SAMPLE_RATE

        def segs():
            if kind == "iterate":  # faster-whisper is lazy: CUDA errors appear while iterating
                raise RuntimeError(msg)
            W = SimpleNamespace
            yield SimpleNamespace(start=0.5, end=dur - 0.5, text=" tone ",
                                  words=[W(start=0.5, end=dur / 2, word=" tone"),
                                         W(start=dur / 2, end=dur - 0.5, word=" end")])

        return segs(), SimpleNamespace(duration=dur, language=kw.get("language") or "en")


@pytest.fixture
def rec_model(monkeypatch):
    import faster_whisper

    _RecordingModel.instances = []
    _RecordingModel.fail_on = {}
    monkeypatch.setattr(faster_whisper, "WhisperModel", _RecordingModel)
    monkeypatch.setattr(tr, "_cuda_device_count", lambda: 0)
    return _RecordingModel


# ----------------------------------------------------------------------------- TESTS-1 / FAILURES-1


def test_load_audio_16k_mono_reads_pcm_directly(tmp_path, monkeypatch):
    wav = make_tone(tmp_path / "audio.wav", seconds=2.0, sample_rate=16000)

    def no_ffmpeg(*a, **k):
        raise AssertionError("a 16 kHz mono wav must not be converted")

    monkeypatch.setattr(tr.ffmpeg, "to_wav", no_ffmpeg)
    arr = tr._load_audio(wav)
    assert arr.dtype == np.float32 and arr.ndim == 1 and len(arr) == 32000
    with wave.open(str(wav), "rb") as w:
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    np.testing.assert_allclose(arr, raw.astype(np.float32) / 32768.0)
    assert 0.1 < float(np.abs(arr).max()) <= 1.0  # make_tone amplitude is 1/8


def test_load_audio_44k_is_resampled_to_16k(tmp_path):
    wav = make_tone(tmp_path / "voice.wav", seconds=2.0, sample_rate=44100)
    arr = tr._load_audio(wav)
    assert arr.dtype == np.float32 and arr.ndim == 1
    assert abs(len(arr) - 32000) <= 160  # resampler may pad/trim a few ms
    assert float(np.abs(arr).max()) > 0.1


def test_load_audio_undecodable_raises_transcribe_error(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio at all")
    with pytest.raises(tr.TranscribeError, match="bad.wav"):
        tr._load_audio(bad)


@pytest.mark.parametrize("rate", [16000, 44100])
def test_transcriber_passes_float32_ndarray_not_path(tmp_path, rec_model, rate):
    wav = make_tone(tmp_path / f"a{rate}.wav", seconds=3.0, sample_rate=rate)
    t = tr.WhisperTranscriber(WhisperSettings(model="tiny")).transcribe(wav, language="en")
    (model,) = rec_model.instances
    ((n, kw),) = model.received
    assert abs(n - 48000) <= 160
    assert kw["word_timestamps"] is True and kw["vad_filter"] is True and kw["language"] == "en"
    assert t.duration == pytest.approx(n / 16000)
    assert [w.text for w in t.segments[0].words] == ["tone", "end"]


def test_pyav_decode_audio_is_not_used(tmp_path, rec_model, monkeypatch):
    import faster_whisper
    import faster_whisper.audio

    def broken(*a, **k):
        raise TypeError("__init__() got an unexpected keyword argument 'metadata_errors'")

    monkeypatch.setattr(faster_whisper.audio, "decode_audio", broken)
    monkeypatch.setattr(faster_whisper, "decode_audio", broken, raising=False)
    wav = make_tone(tmp_path / "audio.wav", seconds=1.0, sample_rate=16000)
    t = tr.WhisperTranscriber(WhisperSettings()).transcribe(wav)
    assert t.segments and t.duration == pytest.approx(1.0)


# ----------------------------------------------------------------------------- FAILURES-2 windows


class _WindowModel:
    """Records each window's length; returns two segments per window in window-relative time."""

    def __init__(self):
        self.lengths: list[int] = []
        self.languages: list = []

    def transcribe(self, audio, **kw):
        assert isinstance(audio, np.ndarray)
        self.lengths.append(len(audio))
        self.languages.append(kw.get("language"))
        d = len(audio) / tr.SAMPLE_RATE
        W = SimpleNamespace
        segs = [SimpleNamespace(start=0.2, end=d / 2, text="a", words=[W(start=0.2, end=d / 2, word="a")]),
                # deliberately runs past the window end: must be clamped to stay monotonic
                SimpleNamespace(start=d / 2, end=d + 0.4, text="b", words=[W(start=d / 2, end=d + 0.4, word="b")])]
        return iter(segs), SimpleNamespace(duration=d, language="hi")


def _noise_with_dips(seconds: float, dips: list[float]) -> np.ndarray:
    rng = np.random.default_rng(0)
    arr = (rng.standard_normal(int(seconds * tr.SAMPLE_RATE)) * 0.3).astype(np.float32)
    for t in dips:  # 100 ms of silence
        a = int(t * tr.SAMPLE_RATE)
        arr[a:a + tr.SAMPLE_RATE // 10] = 0.0
    return arr


@pytest.fixture
def small_windows(monkeypatch):
    # scale 20 min / 15 min / 10 s down to 20 s / 15 s / 2 s
    monkeypatch.setattr(tr, "WINDOWING_THRESHOLD", 20.0)
    monkeypatch.setattr(tr, "WINDOW_SECONDS", 15.0)
    monkeypatch.setattr(tr, "SNAP_SECONDS", 2.0)


def _transcriber_with(model, monkeypatch, samples):
    wt = tr.WhisperTranscriber(WhisperSettings())
    wt._model = model
    monkeypatch.setattr(tr, "_load_audio", lambda p: samples)
    return wt


def test_short_audio_is_one_window(tmp_path, monkeypatch, small_windows):
    model = _WindowModel()
    samples = _noise_with_dips(19.0, [])
    (tmp_path / "a.wav").write_bytes(b"x")
    t = _transcriber_with(model, monkeypatch, samples).transcribe(tmp_path / "a.wav")
    assert model.lengths == [len(samples)]
    assert t.segments[-1].end == pytest.approx(19.0)  # clamped to the audio end


def test_long_audio_windows_offsets_and_quiet_cuts(tmp_path, monkeypatch, small_windows):
    model = _WindowModel()
    samples = _noise_with_dips(48.0, [16.3, 29.4])  # cuts should snap to these (15 +/- 2, then +15)
    (tmp_path / "a.wav").write_bytes(b"x")
    progress = []
    t = _transcriber_with(model, monkeypatch, samples).transcribe(
        tmp_path / "a.wav", on_progress=lambda f, m: progress.append((f, m)))
    assert len(model.lengths) == 3 and sum(model.lengths) == len(samples)
    cuts = np.cumsum(model.lengths)[:-1] / tr.SAMPLE_RATE
    assert cuts[0] == pytest.approx(16.35, abs=0.1)
    assert cuts[1] == pytest.approx(29.45, abs=0.1)
    # window offsets applied to segments and words
    starts = [s.start for s in t.segments]
    assert starts[0] == pytest.approx(0.2)
    assert starts[2] == pytest.approx(cuts[0] + 0.2)
    assert starts[4] == pytest.approx(cuts[1] + 0.2)
    assert t.segments[2].words[0].start == pytest.approx(cuts[0] + 0.2)
    # monotonic, inside the audio, no overlap across window borders
    flat = [(s.start, s.end) for s in t.segments]
    for (a0, a1), (b0, b1) in zip(flat, flat[1:]):
        assert a0 <= a1 <= b0 <= b1
    words = [w for s in t.segments for w in s.words]
    assert all(w1.start >= w0.end - 1e-9 for w0, w1 in zip(words, words[1:]))
    assert t.segments[-1].end == pytest.approx(48.0) and t.duration == pytest.approx(48.0)
    # progress spans all windows and never goes backwards
    fr = [f for f, _ in progress]
    assert fr == sorted(fr) and fr[-1] == 1.0 and any("2/3" in m for _, m in progress)
    # the language the first window detected is reused for the rest
    assert model.languages == [None, "hi", "hi"]
    assert t.language == "hi"


def test_split_points_real_scale_default_constants():
    # 25 min of audio at the real constants: two windows, cut within 15 min +/- 10 s
    n = int(25 * 60 * tr.SAMPLE_RATE)
    arr = np.full(n, 0.1, dtype=np.float32)
    quiet = int((15 * 60 + 7) * tr.SAMPLE_RATE)
    arr[quiet:quiet + 1600] = 0.0
    ranges = tr._split_points(arr)
    assert len(ranges) == 2 and ranges[0][0] == 0 and ranges[-1][1] == n
    assert ranges[0][1] == ranges[1][0]
    assert abs(ranges[0][1] / tr.SAMPLE_RATE - (15 * 60 + 7.05)) < 0.05
    assert tr._split_points(arr[: int(20 * 60 * tr.SAMPLE_RATE)]) == [(0, int(20 * 60 * tr.SAMPLE_RATE))]


# ----------------------------------------------------------------------------- WINDOWS-4 CUDA fallback

CUBLAS = "Library cublas64_12.dll is not found or cannot be loaded"


@pytest.mark.parametrize("stage", ["load", "transcribe", "iterate"])
def test_auto_cuda_library_error_falls_back_to_cpu(tmp_path, rec_model, monkeypatch, caplog, stage):
    monkeypatch.setattr(tr, "_cuda_device_count", lambda: 1)
    rec_model.fail_on = {"cuda": (stage, CUBLAS)}
    wav = make_tone(tmp_path / "audio.wav", seconds=1.0, sample_rate=16000)
    wt = tr.WhisperTranscriber(WhisperSettings(device="auto"))
    with caplog.at_level(logging.WARNING, logger=tr.__name__):
        t = wt.transcribe(wav)
    assert t.segments
    assert rec_model.instances[-1].device == "cpu" and rec_model.instances[-1].compute_type == "int8"
    assert "falling back to cpu" in caplog.text and "cublas64_12.dll" in caplog.text
    # later calls stay on CPU without reloading
    n = len(rec_model.instances)
    wt.transcribe(wav)
    assert len(rec_model.instances) == n


def test_explicit_cuda_does_not_fall_back(tmp_path, rec_model):
    rec_model.fail_on = {"cuda": ("load", CUBLAS)}
    wav = make_tone(tmp_path / "audio.wav", seconds=1.0, sample_rate=16000)
    with pytest.raises(tr.TranscribeError, match="cublas64_12.dll"):
        tr.WhisperTranscriber(WhisperSettings(device="cuda")).transcribe(wav)


def test_non_library_cuda_error_does_not_fall_back(tmp_path, rec_model, monkeypatch):
    monkeypatch.setattr(tr, "_cuda_device_count", lambda: 1)
    rec_model.fail_on = {"cuda": ("transcribe", "out of memory")}
    wav = make_tone(tmp_path / "audio.wav", seconds=1.0, sample_rate=16000)
    with pytest.raises(tr.TranscribeError, match="out of memory"):
        tr.WhisperTranscriber(WhisperSettings()).transcribe(wav)
    assert all(m.device == "cuda" for m in rec_model.instances)


def test_fallback_happens_only_once(tmp_path, rec_model, monkeypatch):
    monkeypatch.setattr(tr, "_cuda_device_count", lambda: 1)
    rec_model.fail_on = {"cuda": ("load", CUBLAS), "cpu": ("transcribe", "libcudnn_ops.so.9 missing")}
    wav = make_tone(tmp_path / "audio.wav", seconds=1.0, sample_rate=16000)
    with pytest.raises(tr.TranscribeError, match="libcudnn"):
        tr.WhisperTranscriber(WhisperSettings()).transcribe(wav)
    assert [m.device for m in rec_model.instances] == ["cpu"]


# ----------------------------------------------------------------------------- FAILURES-3 atomic writes


def test_save_transcript_leaves_old_file_when_serialization_raises(tmp_path, monkeypatch):
    out = tmp_path / "job" / "transcript.json"
    t = FakeTranscriber(duration=10).transcribe(tmp_path / "audio.wav")
    tr.save_transcript(t, out)
    before = out.read_text(encoding="utf-8")

    def boom(*a, **k):
        raise ValueError("cannot serialize")

    monkeypatch.setattr(tr.json, "dumps", boom)
    with pytest.raises(ValueError):
        tr.save_transcript(t, out)
    assert out.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in out.parent.iterdir()) == ["transcript.json"]


def test_save_transcript_no_partial_file_when_write_fails(tmp_path, monkeypatch):
    from autoclipper.stages import _atomic

    out = tmp_path / "job" / "transcript.json"
    t = FakeTranscriber(duration=10).transcribe(tmp_path / "audio.wav")

    def bad_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(_atomic.os, "replace", bad_replace)
    with pytest.raises(OSError):
        tr.save_transcript(t, out)
    assert list(out.parent.iterdir()) == []


def test_truncated_transcript_regenerated_end_to_end(tmp_path):
    fake = FakeTranscriber(duration=10)
    out = tmp_path / "transcript.json"
    good = tr.run_transcribe(tmp_path / "audio.wav", out, fake)
    out.write_text(out.read_text(encoding="utf-8")[:40], encoding="utf-8")  # crash mid-write
    again = tr.run_transcribe(tmp_path / "audio.wav", out, fake)
    assert again == good and len(fake.calls) == 2
    assert json.loads(out.read_text(encoding="utf-8"))["duration"] == 10
    assert (tmp_path / "transcript.json.corrupt").is_file()
