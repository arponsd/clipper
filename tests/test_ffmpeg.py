"""Tests for autoclipper.ffmpeg using synthetic lavfi media (no network)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from fakes import make_tone, make_video
from autoclipper import ffmpeg
from autoclipper.ffmpeg import FFmpegError


def streams(path: Path) -> list[dict]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)["streams"]


def stream(path: Path, kind: str) -> dict:
    return next(s for s in streams(path) if s["codec_type"] == kind)


def frame_md5(path: Path, at: float) -> str:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True,
    ).stdout
    assert raw
    return hashlib.md5(raw).hexdigest()


@pytest.fixture(scope="module")
def video(tmp_path_factory) -> Path:
    return make_video(tmp_path_factory.mktemp("media") / "src.mp4", seconds=4, width=640, height=360)


@pytest.fixture(scope="module")
def silent_video(tmp_path_factory) -> Path:
    return make_video(tmp_path_factory.mktemp("media") / "silent.mp4", seconds=3, width=640, height=360,
                      audio=False)


# --------------------------------------------------------------------------- binaries / run / probe


def test_missing_binary_has_install_hint(monkeypatch):
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: None)
    with pytest.raises(FFmpegError, match="ffmpeg not found on PATH.*install"):
        ffmpeg.ffmpeg_bin()
    with pytest.raises(FFmpegError, match="ffprobe not found on PATH"):
        ffmpeg.ffprobe_bin()


def test_run_error_includes_command_and_stderr(tmp_path):
    missing = tmp_path / "nope.mp4"
    with pytest.raises(FFmpegError) as ei:
        ffmpeg.run(["-i", str(missing), str(tmp_path / "out.wav")], desc="unit test")
    msg = str(ei.value)
    assert msg.startswith("unit test failed (exit code")
    assert "command:" in msg and "nope.mp4" in msg
    assert "stderr:" in msg and "No such file" in msg


def test_run_prepends_binary_and_flags(tmp_path):
    proc = ffmpeg.run(["-f", "lavfi", "-i", "anullsrc", "-t", "0.1", str(tmp_path / "s.wav")])
    assert proc.args[1:6] == ["-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    assert (tmp_path / "s.wav").stat().st_size > 0


def test_probe_video_and_audio(video, silent_video, tmp_path):
    info = ffmpeg.probe(video)
    assert (info.width, info.height, info.has_video, info.has_audio) == (640, 360, True, True)
    assert info.duration == pytest.approx(4.0, abs=0.1)
    assert ffmpeg.duration(video) == pytest.approx(4.0, abs=0.1)

    assert ffmpeg.probe(silent_video).has_audio is False

    tone = ffmpeg.probe(make_tone(tmp_path / "t.wav", seconds=1.5))
    assert (tone.has_video, tone.has_audio, tone.width, tone.height) == (False, True, None, None)
    assert tone.duration == pytest.approx(1.5, abs=0.02)


def test_probe_errors(tmp_path):
    with pytest.raises(FFmpegError, match="does not exist"):
        ffmpeg.probe(tmp_path / "missing.mp4")
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video at all")
    with pytest.raises(FFmpegError, match="ffprobe junk.mp4 failed"):
        ffmpeg.probe(junk)


# --------------------------------------------------------------------------- audio


def test_to_wav_16k_mono(video, tmp_path):
    out = ffmpeg.to_wav(video, tmp_path / "sub dir" / "a.wav")
    a = stream(out, "audio")
    assert (a["codec_name"], a["sample_rate"], a["channels"]) == ("pcm_s16le", "16000", 1)
    assert not list(out.parent.glob("*.part*")), "temp file must be renamed away"


def test_to_wav_custom_rate(video, tmp_path):
    a = stream(ffmpeg.to_wav(video, tmp_path / "a.wav", sample_rate=44100, channels=2), "audio")
    assert (a["sample_rate"], a["channels"]) == ("44100", 2)


def test_to_wav_failure_leaves_no_output(silent_video, tmp_path):
    dst = tmp_path / "a.wav"
    with pytest.raises(FFmpegError):
        ffmpeg.to_wav(silent_video, dst)
    assert not dst.exists() and not list(tmp_path.glob("*.part*"))


@pytest.mark.parametrize(
    "factor, expected",
    [
        (1.0, "anull"),
        (1.1, "atempo=1.1"),
        (2.0, "atempo=2.0"),
        (0.5, "atempo=0.5"),
        (2.5, "atempo=2.0,atempo=1.25"),
        (4.0, "atempo=2.0,atempo=2.0"),
        (0.25, "atempo=0.5,atempo=0.5"),
        (0.3, "atempo=0.5,atempo=0.6"),
    ],
)
def test_atempo_chain_exact(factor, expected):
    assert ffmpeg.atempo_chain(factor) == expected


@pytest.mark.parametrize("factor", [0.01, 0.07, 0.49, 0.75, 1.0001, 1.25, 1.999, 3.3, 7.0, 100.0])
def test_atempo_chain_links_in_range_and_multiply_to_factor(factor):
    chain = ffmpeg.atempo_chain(factor)
    links = [float(part.split("=")[1]) for part in chain.split(",")]
    assert all(0.5 <= x <= 2.0 for x in links)
    assert math.prod(links) == pytest.approx(factor, rel=1e-5)


@pytest.mark.parametrize("bad", [0, -1.5, float("nan"), float("inf")])
def test_atempo_chain_rejects_bad_factor(bad):
    with pytest.raises(ValueError):
        ffmpeg.atempo_chain(bad)


def test_change_tempo_speeds_up(tmp_path):
    src = make_tone(tmp_path / "t.wav", seconds=3.0)
    out = ffmpeg.change_tempo(src, tmp_path / "fast.wav", 1.5)
    assert ffmpeg.duration(out) == pytest.approx(2.0, abs=0.05)
    slow = ffmpeg.change_tempo(src, tmp_path / "slow.wav", 0.4)  # needs a 2-link chain
    assert ffmpeg.duration(slow) == pytest.approx(7.5, abs=0.1)


def test_trim_with_fade(tmp_path):
    src = make_tone(tmp_path / "t.wav", seconds=3.0)
    out = ffmpeg.trim_with_fade(src, tmp_path / "trim.wav", 2.0, fade=0.3)
    assert ffmpeg.duration(out) == pytest.approx(2.0, abs=0.02)
    # fade longer than the clip is clamped, not an error
    out2 = ffmpeg.trim_with_fade(src, tmp_path / "trim2.wav", 0.2, fade=1.0)
    assert ffmpeg.duration(out2) == pytest.approx(0.2, abs=0.02)
    with pytest.raises(ValueError):
        ffmpeg.trim_with_fade(src, tmp_path / "x.wav", 0)


# --------------------------------------------------------------------------- reframe / cut


def test_reframe_filter_text():
    crop = ffmpeg.reframe_filter("crop", 1080, 1920)
    assert crop.startswith("[0:v]") and crop.endswith("setsar=1,format=yuv420p[v]")
    assert "force_original_aspect_ratio=increase" in crop and "crop=1080:1920" in crop
    blur = ffmpeg.reframe_filter("blur", 1080, 1920)
    assert blur.startswith("[0:v]split=2") and blur.endswith("setsar=1,format=yuv420p[v]")
    assert "boxblur" in blur and "overlay=(W-w)/2:(H-h)/2" in blur
    assert "force_original_aspect_ratio=decrease" in blur
    with pytest.raises(ValueError, match="unknown reframe mode"):
        ffmpeg.reframe_filter("zoom", 1080, 1920)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="even"):
        ffmpeg.reframe_filter("crop", 1081, 1920)


def test_cut_vertical_full_size_crop(video, tmp_path):
    seen: list[float] = []
    out = ffmpeg.cut_vertical(video, tmp_path / "clips" / "00" / "cut.mp4", 1.0, 2.5,
                              on_progress=lambda f, m: seen.append(f))
    v, a = stream(out, "video"), stream(out, "audio")
    assert (v["width"], v["height"], v["codec_name"], v["pix_fmt"]) == (1080, 1920, "h264", "yuv420p")
    assert (a["codec_name"], a["sample_rate"], a["channels"]) == ("aac", "48000", 2)
    assert ffmpeg.duration(out) == pytest.approx(1.5, abs=0.1)
    assert seen and seen[-1] == 1.0 and all(0 <= f <= 1 for f in seen)
    assert seen == sorted(seen)


def test_cut_vertical_faststart(video, tmp_path):
    out = ffmpeg.cut_vertical(video, tmp_path / "c.mp4", 0, 1, width=270, height=480)
    data = out.read_bytes()
    assert 0 < data.find(b"moov") < data.find(b"mdat"), "moov atom must precede mdat (+faststart)"


def test_cut_vertical_blur(video, tmp_path):
    out = ffmpeg.cut_vertical(video, tmp_path / "b.mp4", 0.5, 1.5, width=540, height=960, mode="blur")
    v = stream(out, "video")
    assert (v["width"], v["height"]) == (540, 960)
    assert ffmpeg.probe(out).has_audio


def test_cut_vertical_adds_silent_audio(silent_video, tmp_path):
    out = ffmpeg.cut_vertical(silent_video, tmp_path / "s.mp4", 0.5, 2.0, width=270, height=480)
    info = ffmpeg.probe(out)
    assert info.has_audio and info.has_video
    assert info.duration == pytest.approx(1.5, abs=0.1)
    a = stream(out, "audio")
    assert a["sample_rate"] == "48000" and float(a["duration"]) == pytest.approx(1.5, abs=0.1)


def test_cut_vertical_clamps_end_and_rejects_empty(video, tmp_path):
    out = ffmpeg.cut_vertical(video, tmp_path / "end.mp4", 3.0, 99.0, width=270, height=480)
    assert ffmpeg.duration(out) == pytest.approx(1.0, abs=0.1)
    with pytest.raises(FFmpegError, match="empty range"):
        ffmpeg.cut_vertical(video, tmp_path / "x.mp4", 5.0, 6.0, width=270, height=480)
    with pytest.raises(FFmpegError, match="empty range"):
        ffmpeg.cut_vertical(video, tmp_path / "x.mp4", 2.0, 2.0, width=270, height=480)
    assert not (tmp_path / "x.mp4").exists()


# --------------------------------------------------------------------------- subtitles / mix


def test_escape_windows_path_with_space_and_quote():
    p = PureWindowsPath(r"C:\Users\My Folder\it's\captions.ass")
    assert ffmpeg.escape_filter_path(p) == r"'C\:/Users/My Folder/it\'\''s/captions.ass'"


def test_ass_filter_windows_paths():
    f = ffmpeg.ass_filter(PureWindowsPath(r"D:\jobs\a b\captions.ass"), PureWindowsPath(r"D:\app\fonts"))
    assert f == r"ass=filename='D\:/jobs/a b/captions.ass':shaping=complex:fontsdir='D\:/app/fonts'"


def test_ass_filter_posix_and_no_fontsdir():
    f = ffmpeg.ass_filter(PurePosixPath("/data/my job/x:y.ass"), None)
    assert f == r"ass=filename='/data/my job/x\:y.ass':shaping=complex"
    assert "fontsdir" not in f


ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 270
PlayResY: 480
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans,60,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,4,0,5,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,HELLO
"""


@pytest.fixture
def vertical(video, tmp_path) -> Path:
    return ffmpeg.cut_vertical(video, tmp_path / "cut.mp4", 0, 2, width=270, height=480)


def test_mix_burn_real_path_with_space_and_quote(vertical, tmp_path):
    d = tmp_path / "my clip's dir"
    d.mkdir()
    ass = d / "captions.ass"
    ass.write_text(ASS, encoding="utf-8")
    voice = make_tone(d / "voice.wav", seconds=1.0, sample_rate=44100)

    burned = ffmpeg.mix_burn(vertical, d / "final.mp4", voice=voice, ass=ass, original_volume=0.15,
                             voice_volume=1.0, fonts_dir=None, duration=ffmpeg.duration(vertical))
    plain = ffmpeg.mix_burn(vertical, d / "plain.mp4", voice=voice, ass=None, original_volume=0.15,
                            voice_volume=1.0, fonts_dir=None)
    info = ffmpeg.probe(burned)
    assert info.has_audio and (info.width, info.height) == (270, 480)
    assert info.duration == pytest.approx(ffmpeg.duration(vertical), abs=0.1)
    assert ffmpeg.duration(plain) == pytest.approx(ffmpeg.duration(vertical), abs=0.1)
    assert frame_md5(burned, 1.0) != frame_md5(plain, 1.0), "subtitles were not burned in"


def test_mix_burn_voice_longer_than_video_is_cut(vertical, tmp_path):
    voice = make_tone(tmp_path / "long.wav", seconds=5.0)
    out = ffmpeg.mix_burn(vertical, tmp_path / "o.mp4", voice=voice, ass=None, original_volume=0.2,
                          voice_volume=1.0, fonts_dir=None, duration=ffmpeg.duration(vertical))
    assert ffmpeg.duration(out) == pytest.approx(2.0, abs=0.1)


def test_mix_burn_without_voice(vertical, tmp_path):
    out = ffmpeg.mix_burn(vertical, tmp_path / "o.mp4", voice=None, ass=None, original_volume=0.5,
                          voice_volume=1.0, fonts_dir=None)
    a = stream(out, "audio")
    assert (a["codec_name"], a["sample_rate"]) == ("aac", "48000")


def test_mix_burn_video_without_audio(silent_video, tmp_path):
    out = ffmpeg.mix_burn(silent_video, tmp_path / "o.mp4", voice=make_tone(tmp_path / "v.wav", 1.0),
                          ass=None, original_volume=0.15, voice_volume=1.0, fonts_dir=None)
    info = ffmpeg.probe(out)
    assert info.has_audio and info.duration == pytest.approx(3.0, abs=0.15)


def test_mix_burn_missing_inputs(vertical, tmp_path):
    with pytest.raises(FFmpegError, match="voice file not found"):
        ffmpeg.mix_burn(vertical, tmp_path / "o.mp4", voice=tmp_path / "nope.wav", ass=None,
                        original_volume=0.1, voice_volume=1, fonts_dir=None)
    with pytest.raises(FFmpegError, match="subtitle file not found"):
        ffmpeg.mix_burn(vertical, tmp_path / "o.mp4", voice=None, ass=tmp_path / "nope.ass",
                        original_volume=0.1, voice_volume=1, fonts_dir=None)


def _captured_graph(monkeypatch, vertical, tmp_path, fonts_dir, voice=True) -> str:
    captured: dict = {}

    def fake_run(args, **kw):
        captured["args"] = args
        Path(args[-1]).write_bytes(b"x")  # pretend ffmpeg wrote the temp output

    monkeypatch.setattr(ffmpeg, "run", fake_run)
    ass = tmp_path / "c.ass"
    ass.write_text(ASS, encoding="utf-8")
    v = make_tone(tmp_path / "v.wav", 0.5) if voice else None
    ffmpeg.mix_burn(vertical, tmp_path / "o.mp4", voice=v, ass=ass, original_volume=0.15,
                    voice_volume=1.0, fonts_dir=fonts_dir, duration=2.0)
    args = captured["args"]
    assert args[args.index("-t") + 1] == "2.0"
    assert "+faststart" in args and "libx264" in args
    return args[args.index("-filter_complex") + 1]


def test_mix_burn_graph_and_fontsdir_only_with_fonts(monkeypatch, vertical, tmp_path):
    empty = tmp_path / "fonts_empty"
    empty.mkdir()
    graph = _captured_graph(monkeypatch, vertical, tmp_path, empty)
    assert "fontsdir" not in graph
    # HINDI_CAPTIONS-3: explicit unity-gain upmix of the mono voice, limiter after the mix
    assert "[0:a]aformat=channel_layouts=stereo,volume=0.15[a0]" in graph
    assert "[1:a]aformat=channel_layouts=mono,pan=stereo|c0=c0|c1=c0,volume=1.0[a1]" in graph
    assert "[a0][a1]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.97:level=0[a]" in graph
    assert "apad" not in graph

    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "NotoSans-Regular.ttf").write_bytes(b"\0")
    assert "fontsdir=" in _captured_graph(monkeypatch, vertical, tmp_path, fonts)


def test_mix_burn_graph_without_voice(monkeypatch, vertical, tmp_path):
    graph = _captured_graph(monkeypatch, vertical, tmp_path, None, voice=False)
    assert "amix" not in graph
    assert "[0:a]aformat=channel_layouts=stereo,volume=0.15,alimiter=limit=0.97:level=0[a]" in graph


def test_synth_video(tmp_path):
    out = ffmpeg.synth_video(tmp_path / "s.mp4", 1.0, width=320, height=240)
    info = ffmpeg.probe(out)
    assert (info.width, info.height, info.has_audio) == (320, 240, True)


# --------------------------------------------------------------------------- HINDI_CAPTIONS-3 levels


def _peak_db(path: Path) -> float:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:a:0",
                           "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", proc.stderr)
    assert m, proc.stderr[-500:]
    return float(m.group(1))


def _loud_tone(dst: Path, seconds: float, *, channels: int = 1, volume_db: float = -1.0) -> Path:
    amp = 10 ** (volume_db / 20)  # lavfi `sine` is fixed at 1/8 amplitude, so build the tone with aevalsrc
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"aevalsrc={amp:.4f}*sin(2*PI*330*t):s=48000:d={seconds}",
                    "-ac", str(channels), "-c:a", "pcm_s16le", str(dst)], check=True)
    return dst


@pytest.fixture
def loud_vertical(tmp_path) -> Path:
    """Vertical clip whose stereo original audio peaks near 0 dBFS."""
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=270x480:rate=30:duration=2",
                    "-f", "lavfi", "-i", "aevalsrc=0.944*sin(2*PI*440*t)|0.944*sin(2*PI*440*t):s=48000:d=2",  # stereo, -0.5 dBFS
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "pcm_s16le", "-shortest", str(tmp_path / "loud_src.mov")], check=True)
    return tmp_path / "loud_src.mov"


def test_mix_burn_voice_volume_2_never_clips(loud_vertical, tmp_path):
    voice = _loud_tone(tmp_path / "voice.wav", 1.5, volume_db=-1.0)
    assert _peak_db(voice) == pytest.approx(-1.0, abs=0.2)
    assert _peak_db(loud_vertical) == pytest.approx(-0.5, abs=0.2)
    out = ffmpeg.mix_burn(loud_vertical, tmp_path / "o.mp4", voice=voice, ass=None, original_volume=0.15,
                          voice_volume=2.0, fonts_dir=None, duration=2.0)
    # volumedetect clamps at 0.0 dB, so an unlimited (clipping) render reads exactly 0.0
    assert _peak_db(out) < 0.0


def test_mix_burn_voice_volume_1_is_unity(vertical, tmp_path):
    # a mono -6 dB voice over a silenced original must come out at about -6 dB on each channel
    voice = _loud_tone(tmp_path / "voice.wav", 1.5, volume_db=-6.0)
    out = ffmpeg.mix_burn(vertical, tmp_path / "o.mp4", voice=voice, ass=None, original_volume=0.0,
                          voice_volume=1.0, fonts_dir=None, duration=2.0)
    assert _peak_db(voice) == pytest.approx(-6.0, abs=0.2)
    assert _peak_db(out) == pytest.approx(-6.0, abs=0.6)


def test_mix_burn_without_voice_is_limited(loud_vertical, tmp_path):
    out = ffmpeg.mix_burn(loud_vertical, tmp_path / "o.mp4", voice=None, ass=None, original_volume=2.0,
                          voice_volume=1.0, fonts_dir=None)
    assert _peak_db(out) < 0.0


# --------------------------------------------------------------------------- WINDOWS-3 locked output


def test_atomic_output_locked_destination_raises_clear_error(tmp_path, monkeypatch):
    dst = tmp_path / "final.mp4"
    dst.write_bytes(b"old")
    calls = []

    def locked(src, target):
        calls.append(target)
        raise PermissionError(13, "The process cannot access the file", str(target))

    monkeypatch.setattr(ffmpeg, "_REPLACE_RETRY_SECONDS", 0.3)
    monkeypatch.setattr(ffmpeg.os, "replace", locked)
    with pytest.raises(FFmpegError, match=r"could not replace final\.mp4: is it open in another program "
                                          r"\(e\.g\. a video player\)\? Close it and retry"):
        with ffmpeg._AtomicOutput(dst) as tmp:
            tmp.write_bytes(b"new")
    assert len(calls) >= 2  # retried before giving up
    assert not (tmp_path / "final.part.mp4").exists()
    assert dst.read_bytes() == b"old"


def test_atomic_output_retry_succeeds_when_lock_released(tmp_path, monkeypatch):
    dst = tmp_path / "final.mp4"
    real = os.replace
    attempts = []

    def flaky(src, target):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError(13, "locked")
        real(src, target)

    monkeypatch.setattr(ffmpeg.os, "replace", flaky)
    with ffmpeg._AtomicOutput(dst) as tmp:
        tmp.write_bytes(b"new")
    assert dst.read_bytes() == b"new" and len(attempts) == 3


def test_atomic_output_other_oserror_fails_fast(tmp_path, monkeypatch):
    (tmp_path / "out").mkdir()
    dst = tmp_path / "out" / "final.mp4"

    def bad(src, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ffmpeg.os, "replace", bad)
    with pytest.raises(FFmpegError, match="could not replace final.mp4"):
        with ffmpeg._AtomicOutput(dst) as tmp:
            tmp.write_bytes(b"new")
    assert list(dst.parent.iterdir()) == []
