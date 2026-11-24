"""Voice-over mix quality: loudness mix, short-narration trim, lengthen timeout, Demucs vocal separation,
the job Mode endpoint and the title quote clean-up.

No real Anthropic / Voicebox / Demucs: fakes and synthetic ffmpeg media. The one real-Demucs test is marked
slow + demucs and skipped when ~/tools/demucs is missing.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from fakes import FakeLLM, FakeTranscriber, FakeVoicebox, make_tone
from test_product_fixes import volume_db
from test_web_support import (BASE, Stub, assert_error_shape, assert_keys, assert_summary, assert_task, make_client,
                              make_job_on_disk, read_json, wait_for)
from test_voicebox import _generating_server
from test_voicebox import make_client as make_vb_client

from autoclipper import cli, ffmpeg, separation
from autoclipper.config import AudioSettings, SeparationSettings, Settings, save_settings
from autoclipper.jobstore import JobStore, jobs_root
from autoclipper.pipeline import Deps, rerender, run_job
from autoclipper.stages import render as render_stage
from autoclipper.stages.render import (loudness_gains, read_render_stamp, render_clip, render_final,
                                       short_voice_cut)
from autoclipper.stages.script import _clean_title, normalize_script
from autoclipper.stages.voice import load_voice_result, make_voice
from autoclipper.config import VoiceProfile
from autoclipper.models import ClipScript
from autoclipper.voicebox import VoiceboxError, VoiceboxTimeout
from autoclipper.web.auth import TOKEN_HEADER
from autoclipper.web.worker import Worker

MIN_ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 568

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, \
Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, \
MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_cut(dst: Path, seconds: float = 8.0, gain: float = 1.0, freq: int = 440) -> Path:
    """Small 9:16 video whose audio is a sine at ``gain`` x the default amplitude (1/8)."""
    _ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=320x568:rate=25:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={seconds}",
            "-af", f"volume={gain}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-shortest", str(dst))
    return dst


def make_voice_wav(dst: Path, seconds: float = 3.0, gain: float = 0.3, freq: int = 660) -> Path:
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=44100:duration={seconds}",
            "-af", f"volume={gain}", "-ac", "1", "-c:a", "pcm_s16le", str(dst))
    return dst


def loud(**kw) -> AudioSettings:
    """The loudness mix with loudnorm off (so absolute levels can be compared)."""
    base = dict(loudnorm=False, mix="loudness", short_voice="keep", duck_ramp_seconds=0.2)
    base.update(kw)
    return AudioSettings(**base)


# ============================================================================ loudness mix: unit
def test_loudness_gains_put_voice_on_target_and_bed_below_it():
    a = AudioSettings(voice_lufs=-16, background_db_below_voice=18, original_volume_after=1.0)
    # the user's job: voice -20.6 LUFS, a gaming bed at -9.8 LUFS
    g = loudness_gains(a, voice_lufs=-20.6, bed_during_lufs=-9.8, bed_lufs=-9.8)
    assert render_stage._lin_to_db(g.voice) == pytest.approx(4.6, abs=0.01)
    assert render_stage._lin_to_db(g.during) == pytest.approx(-34 + 9.8, abs=0.01)  # bed at -34 LUFS
    assert render_stage._lin_to_db(g.after) == pytest.approx(-16 + 9.8, abs=0.01)  # as loud as the voice
    # the same voice over a quiet source: the bed is lifted, but by at most 12 dB
    q = loudness_gains(a, voice_lufs=-20.6, bed_during_lufs=-40, bed_lufs=-40)
    assert render_stage._lin_to_db(q.during) == pytest.approx(6.0, abs=0.01)
    assert render_stage._lin_to_db(q.after) == pytest.approx(12.0, abs=0.01)
    # original_volume_after scales the level outside the narration; voice_volume trims the voice
    h = loudness_gains(a.model_copy(update={"original_volume_after": 0.5, "voice_volume": 2.0}),
                       voice_lufs=-16, bed_during_lufs=-20, bed_lufs=-20)
    assert render_stage._lin_to_db(h.after) == pytest.approx(-16 - 6.02 + 20, abs=0.05)
    assert h.voice == pytest.approx(2.0)


def test_loudness_gains_silent_inputs_and_clamps():
    a = AudioSettings()
    g = loudness_gains(a, voice_lufs=None, bed_during_lufs=None, bed_lufs=None)
    assert (g.voice, g.during, g.after) == (1.0, 1.0, 1.0)
    assert loudness_gains(a.model_copy(update={"original_volume_after": 0.0}), voice_lufs=-20,
                          bed_during_lufs=-20, bed_lufs=-20).after == 0.0
    v = loudness_gains(a, voice_lufs=-80, bed_during_lufs=10, bed_lufs=10)
    assert render_stage._lin_to_db(v.voice) == pytest.approx(30.0)  # voice gain clamp
    assert render_stage._lin_to_db(v.during) == pytest.approx(-44.0)


@pytest.mark.parametrize("ve,total,mode,expected", [
    (10.0, 38.0, "trim_clip", 11.0),  # the user's hi clip: 10 s of voice in a 38 s clip
    (31.5, 38.0, "trim_clip", None),  # en: 83 % of the clip - not short
    (10.0, 38.0, "keep", None),
    (1.0, 20.0, "trim_clip", 3.0),  # never under 3 s
    (2.5, 3.0, "trim_clip", None),  # would not shorten anything
    (0.0, 20.0, "trim_clip", None),
])
def test_short_voice_cut(ve, total, mode, expected):
    assert short_voice_cut(AudioSettings(short_voice=mode), ve, total) == expected


def test_old_settings_file_loads_with_new_defaults(tmp_path):
    old = {"audio": {"original_volume": 0.15, "voice_volume": 1.0, "max_speedup": 1.25},
           "voicebox": {"url": "http://127.0.0.1:17493", "timeout_seconds": 600.0}}
    s = Settings.model_validate(old)
    assert s.audio.mix == "loudness" and s.audio.background_db_below_voice == 18.0
    assert s.audio.short_voice == "trim_clip" and s.audio.original_volume == 0.15
    assert s.separation.background == "original" and s.separation.model == "htdemucs"
    assert s.voicebox.lengthen_timeout_seconds == 240.0


# ============================================================================ loudness mix: real render
@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("mix")
    return {"dir": d, "loud": make_cut(d / "loud.mp4", gain=6.0), "quiet": make_cut(d / "quiet.mp4", gain=0.25),
            "voice": make_voice_wav(d / "voice.wav", seconds=3.0, gain=0.3)}


def test_loudness_mix_bed_sits_below_voice_whatever_the_source_level(media, tmp_path):
    during_bed = {}
    for name in ("loud", "quiet"):
        rep: dict = {}
        out = render_final(cut=media[name], voice=media["voice"], ass=None, dst=tmp_path / f"{name}.mp4",
                           audio=loud(), report=rep)
        voice_db = volume_db(out, 0.5, 2.0, band=660)[0]
        bed_db = volume_db(out, 0.5, 2.0, band=440)[0]
        after_db = volume_db(out, 4.0, 3.5, band=440)[0]
        assert voice_db - bed_db == pytest.approx(18.0, abs=1.5), name
        if name == "loud":
            assert after_db == pytest.approx(voice_db, abs=1.5)  # outside: as loud as the voice
        else:  # a very quiet source is lifted by at most 12 dB
            assert after_db == pytest.approx(volume_db(media[name], 4.0, 3.5, band=440)[0] + 12, abs=1.5)
        during_bed[name] = bed_db
        assert rep["mix"] == "loudness" and rep["background"] == "original" and "trimmed_to" not in rep
        assert rep["voice_lufs"] < -20 and rep["background_gain_db_under_voice"] < 0 or name == "quiet"
        assert ffmpeg.probe(out).duration == pytest.approx(8.0, abs=0.1)
    # the source differs by 27.6 dB, the bed under the voice does not
    assert during_bed["loud"] == pytest.approx(during_bed["quiet"], abs=1.5)


def test_loudness_mix_custom_gap(media, tmp_path):
    out = render_final(cut=media["loud"], voice=media["voice"], ass=None, dst=tmp_path / "o.mp4",
                       audio=loud(background_db_below_voice=10))
    assert volume_db(out, 0.5, 2.0, band=660)[0] - volume_db(out, 0.5, 2.0, band=440)[0] == \
        pytest.approx(10.0, abs=1.5)


def test_short_voice_trims_clip_keeps_bed_ducked_and_fades(media, tmp_path):
    rep: dict = {}
    out = render_final(cut=media["loud"], voice=media["voice"], ass=None, dst=tmp_path / "t.mp4",
                       audio=loud(short_voice="trim_clip", short_voice_tail=1.0), report=rep)
    assert ffmpeg.probe(out).duration == pytest.approx(4.0, abs=0.1)
    assert rep["trimmed_to"] == pytest.approx(4.0, abs=0.02) and rep["trim_reason"] == "short narration"
    assert rep["duration"] == pytest.approx(4.0, abs=0.02) and rep["clip_duration"] == pytest.approx(8.0, abs=0.1)
    during = volume_db(out, 0.5, 2.0, band=440)[0]
    tail = volume_db(out, 3.1, 0.35, band=440)[0]  # after the voice, before the fade
    assert tail == pytest.approx(during, abs=2.0)  # no loud original burst after the narration
    assert volume_db(out, 3.8, 0.2, band=440)[0] < during - 3  # faded out at the end


def test_gain_mix_is_unchanged_and_measures_nothing(media, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ffmpeg, "measure_loudness", lambda *a, **k: calls.append(a) or -20.0)
    rep: dict = {}
    render_final(cut=media["loud"], voice=media["voice"], ass=None, dst=tmp_path / "g.mp4",
                 audio=AudioSettings(mix="gain", short_voice="keep", loudnorm=False), report=rep)
    assert calls == [] and rep["mix"] == "gain" and "voice_lufs" not in rep


def test_measure_loudness(media, tmp_path):
    lv = ffmpeg.measure_loudness(media["voice"])
    assert lv is not None and -40 < lv < -20
    # the same mono signal on both channels is 3 dB louder
    assert ffmpeg.measure_loudness(media["voice"], pre="pan=stereo|c0=c0|c1=c0") == pytest.approx(lv + 3.0, abs=0.2)
    assert ffmpeg.measure_loudness(media["voice"], start=1.0, end=2.0) == pytest.approx(lv, abs=0.5)
    assert ffmpeg.measure_loudness(media["voice"], start=5.0) is None  # past the end
    silent = tmp_path / "s.wav"
    _ffmpeg("-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "2", str(silent))
    assert ffmpeg.measure_loudness(silent) is None
    assert ffmpeg.measure_loudness(tmp_path / "missing.wav") is None


# ============================================================================ Demucs separation (fake)
class FakeDemucs:
    """Stands in for `python -m demucs.separate`: writes <out>/<model>/no_vocals.wav (a 550 Hz tone)."""

    def __init__(self, rc: int = 0, write: bool = True, exc: BaseException | None = None, seconds: float = 8.0):
        self.calls: list[dict] = []
        self.rc, self.write, self.exc, self.seconds = rc, write, exc, seconds

    def __call__(self, cmd, *, env, timeout, cwd):
        self.calls.append({"cmd": list(cmd), "env": env, "timeout": timeout, "cwd": cwd})
        if self.exc is not None:
            raise self.exc
        if "-c" in cmd:  # the doctor's import check
            return subprocess.CompletedProcess(cmd, self.rc, "4.0.1 2.5.1+cpu\n", "boom" if self.rc else "")
        out = Path(cmd[cmd.index("-o") + 1]) / cmd[cmd.index("-n") + 1]
        assert Path(cmd[-1]).is_file()  # the extracted wav
        if self.write and self.rc == 0:
            out.mkdir(parents=True)
            _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=550:sample_rate=44100:duration={self.seconds}",
                    "-af", "volume=2", "-ac", "2", str(out / "no_vocals.wav"))
        return subprocess.CompletedProcess(cmd, self.rc, "", "Traceback ...\nRuntimeError: out of memory"
                                           if self.rc else "")


def sep_settings(tmp_path: Path, **kw) -> Settings:
    py = tmp_path / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("")
    s = Settings()
    s.audio = loud()
    s.separation = SeparationSettings(background="no_vocals", python=str(py), models_dir=str(tmp_path / "models"),
                                      **kw)
    return s


def clip_layout(tmp_path: Path, media) -> tuple[Path, Path]:
    clip = tmp_path / "clips" / "00"
    out = clip / "en"
    out.mkdir(parents=True)
    cut = clip / "cut.mp4"
    cut.write_bytes(Path(media["loud"]).read_bytes())
    (out / "voice.wav").write_bytes(Path(media["voice"]).read_bytes())
    (out / "captions.ass").write_text(MIN_ASS, encoding="utf-8")
    return cut, out


def test_demucs_command_and_env(tmp_path):
    s = SeparationSettings(model="htdemucs_ft", segment=5, threads=3, models_dir="~/m")
    cmd = separation.demucs_command(Path("/py"), Path("/in.wav"), Path("/out"), s)
    assert cmd[:3] == [str(Path("/py")), "-m", "demucs.separate"]  # "\\py" on Windows
    assert cmd[cmd.index("-n") + 1] == "htdemucs_ft" and cmd[cmd.index("--segment") + 1] == "5"
    assert cmd[cmd.index("--two-stems") + 1] == "vocals" and cmd[cmd.index("-d") + 1] == "cpu"
    env = separation._env(s)
    assert env["OMP_NUM_THREADS"] == "3" and Path(env["TORCH_HOME"]).name == "m" and "~" not in env["TORCH_HOME"]
    assert separation._env(SeparationSettings(threads=0)).get("OMP_NUM_THREADS") == os.environ.get("OMP_NUM_THREADS")


@pytest.mark.parametrize("bad", [{"background": "vocals"}, {"model": "a b"}, {"model": ""}, {"segment": 0},
                                 {"threads": -1}, {"timeout_seconds": 5}, {"python": "x\ny"}])
def test_separation_settings_validation(bad):
    with pytest.raises(ValueError):
        SeparationSettings(**bad)


def test_render_clip_uses_no_vocals_bed_once_per_clip(media, tmp_path):
    s = sep_settings(tmp_path)
    cut, out = clip_layout(tmp_path, media)
    fake = FakeDemucs()
    o1 = render_clip(cut=cut, out_dir=out, settings=s, sep_runner=fake)
    assert o1.audio_warning is None and o1.audio["background"] == "no_vocals"
    final = out / "final.mp4"
    # under the voice the bed is the stem (550 Hz), not the original (440 Hz); after the voice the original
    # (with its speech) comes back
    assert volume_db(final, 0.5, 2.0, band=550)[0] > volume_db(final, 0.5, 2.0, band=440)[0] + 20
    assert volume_db(final, 4.0, 3.5, band=440)[0] > volume_db(final, 4.0, 3.5, band=550)[0] + 20
    assert volume_db(final, 4.0, 3.5, band=440)[0] == pytest.approx(volume_db(final, 0.5, 2.0, band=660)[0], abs=1.5)
    assert o1.audio["background_outside"] == "original"
    assert volume_db(final, 0.5, 2.0, band=660)[0] - volume_db(final, 0.5, 2.0, band=550)[0] == \
        pytest.approx(18.0, abs=1.5)
    meta = read_json(cut.parent / separation.BED_JSON)
    assert meta["model"] == "htdemucs" and meta["cut_sha256"] == separation.file_sha256(cut)
    assert (cut.parent / separation.BED_WAV).is_file() and not list(cut.parent.glob("demucs-*"))
    # the other language (and a re-render) reuses the cached stem
    hi = cut.parent / "hi"
    hi.mkdir()
    for name in ("voice.wav", "captions.ass"):
        (hi / name).write_bytes((out / name).read_bytes())
    render_clip(cut=cut, out_dir=hi, settings=s, sep_runner=fake)
    render_clip(cut=cut, out_dir=out, settings=s, sep_runner=fake)
    assert len(fake.calls) == 1
    # another model, or a new cut, separates again
    s2 = s.model_copy(update={"separation": s.separation.model_copy(update={"model": "mdx"})})
    o2 = render_clip(cut=cut, out_dir=out, settings=s2, sep_runner=fake)
    assert len(fake.calls) == 2 and read_json(cut.parent / separation.BED_JSON)["model"] == "mdx"
    render_stage.write_render_stamp(out, s2, o2)
    stamp = read_render_stamp(out)
    assert stamp["audio"]["background"] == "no_vocals" and "warning" not in stamp["audio"]


def test_stem_trimmed_clip_has_no_original_after_voice(media, tmp_path):
    s = sep_settings(tmp_path)
    s.audio = loud(short_voice="trim_clip")
    cut, out = clip_layout(tmp_path, media)
    o = render_clip(cut=cut, out_dir=out, settings=s, sep_runner=FakeDemucs())
    assert o.audio["trimmed_to"] == pytest.approx(4.0, abs=0.02) and "background_outside" not in o.audio
    final = out / "final.mp4"
    assert volume_db(final, 3.1, 0.35, band=550)[0] > volume_db(final, 3.1, 0.35, band=440)[0] + 20


@pytest.mark.parametrize("fake,reason", [
    (FakeDemucs(rc=1), "Demucs failed (exit 1)"),
    (FakeDemucs(write=False), "no no_vocals stem"),
    (FakeDemucs(exc=subprocess.TimeoutExpired("demucs", 900)), "did not finish within 900s"),
    (FakeDemucs(exc=OSError("exec format error")), "could not start"),
])
def test_separation_failure_falls_back_to_original_with_warning(media, tmp_path, fake, reason):
    s = sep_settings(tmp_path)
    cut, out = clip_layout(tmp_path, media)
    o = render_clip(cut=cut, out_dir=out, settings=s, sep_runner=fake)
    assert o.audio_warning and reason in o.audio_warning and "used the original audio" in o.audio_warning
    assert o.audio["background"] == "original" and o.audio["warning"] == o.audio_warning
    final = out / "final.mp4"
    assert volume_db(final, 4.0, 3.5, band=440)[0] > volume_db(final, 4.0, 3.5, band=550)[0] + 25
    assert not (cut.parent / separation.BED_WAV).exists() and not list(cut.parent.glob("demucs-*"))


def test_separation_missing_python_and_not_used_when_off(media, tmp_path):
    s = sep_settings(tmp_path)
    s.separation.python = str(tmp_path / "nope" / "python")
    cut, out = clip_layout(tmp_path, media)
    fake = FakeDemucs()
    o = render_clip(cut=cut, out_dir=out, settings=s, sep_runner=fake)
    assert "Demucs Python not found" in o.audio_warning and fake.calls == []
    # voice-over off: no separation, no voice, original audio
    s.voiceover = False
    s.separation.python = sep_settings(tmp_path).separation.python
    o = render_clip(cut=cut, out_dir=out, settings=s, sep_runner=fake)
    assert fake.calls == [] and o.audio_warning is None and o.audio["mix"] == "original"


def test_doctor_demucs_row(tmp_path):
    s = sep_settings(tmp_path)
    ok = cli.check_demucs(s, runner=FakeDemucs())
    assert ok.level == "ok" and "demucs 4.0.1, torch 2.5.1+cpu" in ok.detail
    bad = cli.check_demucs(s, runner=FakeDemucs(rc=1))
    assert bad.level == "warn" and "not importable" in bad.detail and "fall back" in bad.detail
    s.separation.python = str(tmp_path / "missing")
    assert cli.check_demucs(s).level == "warn"
    s.separation.background = "original"
    row = cli.check_demucs(s)
    assert row.level == "ok" and "not used" in row.detail and "not installed" in row.detail
    assert any(c.name == "demucs" for c in cli.run_doctor(Settings()))


# ============================================================================ pipeline + voice
def pipe_settings(**audio) -> Settings:
    s = Settings()
    s.voicebox.english.profile_id = "fake-en"
    s.voicebox.hindi.profile_id = "fake-hi"
    s.clips.count, s.clips.min_seconds, s.clips.max_seconds = 1, 10, 12
    s.audio = s.audio.model_copy(update=audio)
    return s


def test_pipeline_short_narration_trims_and_reports(tmp_path, monkeypatch):
    from fakes import make_video

    video = make_video(tmp_path / "src.mp4", seconds=30)
    s = pipe_settings(short_voice="trim_clip", short_voice_tail=1.0)
    save_settings(s)
    # ~4 s of voice in a 10-12 s clip (a realistic 5 words/s: a faster take would count as cut short)
    vb = FakeVoicebox(seconds_per_word=0.2)

    class NoLengthenLLM(FakeLLM):
        def lengthen(self, *, lang, narration, target_words):
            return narration  # "no longer text": the short take is kept, with a warning

    deps = Deps(llm=NoLengthenLLM(narration_words=20), voicebox=vb, transcriber=FakeTranscriber(duration=30))
    logs: list[str] = []
    store = run_job(str(video), settings=s, deps=deps, langs=["en"], log=logs.append)
    out = store.clip_dir(0, "en")
    stamp = read_render_stamp(out)
    a = stamp["audio"]
    assert a["mix"] == "loudness" and a["trimmed_to"] is not None and a["trimmed_to"] < a["clip_duration"] - 3
    assert ffmpeg.probe(out / "final.mp4").duration == pytest.approx(a["trimmed_to"], abs=0.1)
    words = json.loads((out / "words.json").read_text(encoding="utf-8"))
    assert max(w["end"] for w in words) <= a["trimmed_to"]  # captions fit the shorter clip
    assert any("trimmed to" in line for line in logs)
    st = store.job.clips[0].langs["en"]
    assert "could not be lengthened" in (st.warning or "") and st.error is None
    # web detail shows it
    client, _, _ = make_client(start=False)
    d = client.get(f"/api/jobs/{store.job.id}").json()["clips"][0]["langs"]["en"]
    assert d["audio"]["trimmed_to"] == a["trimmed_to"] and d["audio"]["trim_reason"] == "short narration"
    assert d["audio"]["background"] == "original" and isinstance(d["audio"]["voice_lufs"], float)


def test_pipeline_separation_failure_sets_clip_warning(tmp_path, monkeypatch):
    from fakes import make_video

    video = make_video(tmp_path / "src.mp4", seconds=30)
    s = pipe_settings(short_voice="keep")
    s.separation = sep_settings(tmp_path).separation
    monkeypatch.setattr(separation, "_default_runner", FakeDemucs(rc=1))
    save_settings(s)
    deps = Deps(llm=FakeLLM(), voicebox=FakeVoicebox(), transcriber=FakeTranscriber(duration=30))
    store = run_job(str(video), settings=s, deps=deps, langs=["en"], log=lambda _l: None)
    st = store.job.clips[0].langs["en"]
    assert st.error is None and st.final and "vocal separation failed" in (st.warning or "")
    # fixing Demucs: a re-render separates and the warning goes away
    monkeypatch.setattr(separation, "_default_runner", FakeDemucs(seconds=12))
    rerender(store.job.id, 0, "en", settings=s, deps=deps, log=lambda _l: None)
    st = JobStore.load(store.job.id).job.clips[0].langs["en"]
    assert st.warning is None and read_render_stamp(store.clip_dir(0, "en"))["audio"]["background"] == "no_vocals"


class TimeoutVoicebox(FakeVoicebox):
    """Accepts timeout=; the second generation (the lengthen retry) times out."""

    def synthesize(self, text, *, profile_id, language, engine, dst, on_progress=None, timeout=None):
        self.calls.append(dict(text=text, timeout=timeout))
        if len(self.calls) > 1:
            raise VoiceboxTimeout(f"Voicebox generation g2 did not finish within {timeout:.0f}s")
        return make_tone(Path(dst), seconds=max(0.5, len(text.split()) * self.seconds_per_word))


def test_lengthen_retry_uses_its_own_timeout_and_warns(tmp_path):
    vb = TimeoutVoicebox(seconds_per_word=0.3)
    llm = FakeLLM()
    r = make_voice(client=vb, narration="one two three four five", lang="hi", voice=VoiceProfile(profile_id="p"),
                   clip_duration=20.0, out_dir=tmp_path, max_speedup=1.25,
                   lengthen=lambda t, n: llm.lengthen(lang="hi", narration=t, target_words=n),
                   lengthen_timeout=120)
    assert [c["timeout"] for c in vb.calls] == [None, 120]
    assert not r.lengthened and r.narration == "one two three four five"
    (w,) = r.warnings
    assert "longer than 120s" in w and "voicebox.lengthen_timeout_seconds" in w and "kept the original" in w
    assert load_voice_result(tmp_path).warnings == r.warnings
    assert not list(tmp_path.glob("*.orig.wav"))


def test_lengthen_timeout_not_passed_to_clients_without_it(tmp_path):
    vb = FakeVoicebox(seconds_per_word=0.3)  # synthesize() has no timeout parameter
    r = make_voice(client=vb, narration="one two three", lang="en", voice=VoiceProfile(profile_id="p"),
                   clip_duration=20.0, out_dir=tmp_path, max_speedup=1.25,
                   lengthen=lambda t, n: " ".join(["word"] * n), lengthen_timeout=60)
    assert len(vb.calls) == 2 and r.warnings == [] and r.lengthened


def test_pipeline_passes_lengthen_timeout_and_shows_warning(tmp_path):
    from fakes import make_video

    video = make_video(tmp_path / "src.mp4", seconds=30)
    s = pipe_settings(short_voice="keep")
    s.voicebox.lengthen_timeout_seconds = 45
    save_settings(s)
    vb = TimeoutVoicebox(seconds_per_word=0.25)  # 2.5 s: short, but not cut short (10 words need ~3.3 s)
    deps = Deps(llm=FakeLLM(narration_words=10), voicebox=vb, transcriber=FakeTranscriber(duration=30))
    store = run_job(str(video), settings=s, deps=deps, langs=["en"], log=lambda _l: None)
    assert vb.calls[1]["timeout"] == 45
    st = store.job.clips[0].langs["en"]
    assert st.final and "longer than 45s" in (st.warning or "")
    client, _, _ = make_client(start=False)
    v = client.get(f"/api/jobs/{store.job.id}").json()["clips"][0]["langs"]["en"]["voice"]
    assert len(v["warnings"]) == 1 and "longer than 45s" in v["warnings"][0]


def test_voicebox_wait_per_call_timeout_is_capped():
    handler, _ = _generating_server()
    client, _ = make_vb_client(handler, timeout=600.0, poll=1.0)
    with pytest.raises(VoiceboxTimeout, match="within 5s"):
        client.wait("g1", timeout=5)
    client2, _ = make_vb_client(handler, timeout=4.0, poll=1.0)
    with pytest.raises(VoiceboxError, match="within 4s"):
        client2.wait("g1", timeout=500)  # never longer than the client's own timeout


# ============================================================================ web: settings + Mode
def test_web_settings_new_fields_round_trip_and_422():
    client, _, _ = make_client(start=False)
    s = client.get("/api/settings").json()
    assert s["audio"]["mix"] == "loudness" and s["separation"]["background"] == "original"
    s["audio"].update(mix="gain", background_db_below_voice=20.0, short_voice="keep", short_voice_tail=0.5)
    s["separation"].update(background="no_vocals", segment=5, model="htdemucs_ft")
    s["voicebox"]["lengthen_timeout_seconds"] = 90.0
    r = client.put("/api/settings", json=s)
    assert r.status_code == 200, r.text
    saved = read_json(Path(os.environ["AUTOCLIPPER_DATA_DIR"]) / "settings.json")
    assert saved["separation"]["model"] == "htdemucs_ft" and saved["audio"]["short_voice"] == "keep"
    for path, value in [("audio.background_db_below_voice", -1.0), ("audio.short_voice", "cut"),
                        ("audio.short_voice_below", 2.0), ("audio.mix", "loud"), ("separation.segment", 0),
                        ("separation.background", "vocals"), ("separation.model", "a/b"),
                        ("separation.timeout_seconds", "900"), ("voicebox.lengthen_timeout_seconds", 0.0),
                        ("separation.python", "x" * 501)]:
        bad = json.loads(json.dumps(saved))
        sec, key = path.split(".")
        bad[sec][key] = value
        body = assert_error_shape(client.put("/api/settings", json=bad), 422)
        assert path in body["errors"], (path, body)
    unknown = json.loads(json.dumps(saved))
    unknown["separation"]["gpu"] = True
    assert "separation.gpu" in assert_error_shape(client.put("/api/settings", json=unknown), 422)["errors"]


class ModeStub(Stub):
    """run_job stub that records the voiceover switch and writes it to job.json like the pipeline does."""

    def run_job(self, source, *, settings, deps, langs, count, min_seconds, max_seconds, job_id, log,
                voiceover=None):
        with self.lock:
            self.calls.append(("run_job", job_id, {"voiceover": voiceover}))
        if voiceover is not None:
            store = JobStore.load(job_id)
            store.job.voiceover = voiceover
            store.save()
        self._body(log)


@pytest.fixture
def mode_api():
    stub = ModeStub()
    client, worker, _ = make_client(stub=stub, worker=Worker(run_job=stub.run_job, rerender=stub.rerender))
    with client:
        yield client, worker, stub
        stub.release.set()


MODE_KEYS = {"job": dict, "voiceover": bool, "changed": bool, "task": (dict, type(None))}


def test_mode_switch_queues_run_and_updates_job(mode_api):
    client, worker, stub = mode_api
    store = make_job_on_disk(clips=1)
    r = client.put(f"/api/jobs/{store.job.id}/mode", json={"voiceover": False})
    assert r.status_code == 202, r.text
    body = r.json()
    assert_keys(body, MODE_KEYS)
    assert_summary(body["job"])
    assert_task(body["task"])
    assert body["task"]["kind"] == "run_job" and body["voiceover"] is False and body["changed"] is True
    assert wait_for(lambda: stub.calls and worker.wait_idle(5))
    assert stub.calls[-1] == ("run_job", store.job.id, {"voiceover": False})
    assert read_json(jobs_root() / store.job.id / "job.json")["voiceover"] is False  # side effect
    d = client.get(f"/api/jobs/{store.job.id}").json()
    assert d["voiceover"] is False and d["clips"][0]["langs"]["en"]["render_stale"] is True


def test_mode_same_and_all_rendered_is_a_no_op(mode_api):
    client, _, stub = mode_api
    store = make_job_on_disk(clips=1)  # legacy job (no stored mode) = voice-over on, finals rendered with it
    for lang in ("en", "hi"):  # ... and with the current mix (else they are stale: see test_mix_lead)
        (store.clip_dir(0, lang) / "render.json").write_text(json.dumps(
            {"caption_engine": "ass", "engine_used": "ass", "audio_key": render_stage.audio_key(Settings())}))
    r = client.put(f"/api/jobs/{store.job.id}/mode", json={"voiceover": True})
    assert r.status_code == 200, r.text
    assert_keys(r.json(), MODE_KEYS)
    assert r.json()["task"] is None and r.json()["changed"] is False and stub.calls == []


def test_mode_same_but_some_clip_in_other_mode_rerenders(mode_api):
    client, worker, stub = mode_api
    store = make_job_on_disk(clips=1)
    (store.clip_dir(0, "hi") / "render.json").write_text(json.dumps({"caption_engine": "ass",
                                                                     "engine_used": "ass", "voiceover": False}))
    r = client.put(f"/api/jobs/{store.job.id}/mode", json={"voiceover": True})
    assert r.status_code == 202 and r.json()["changed"] is False
    assert wait_for(lambda: stub.calls and worker.wait_idle(5))


@pytest.mark.parametrize("body", [{}, {"voiceover": "false"}, {"voiceover": 0}, {"voiceover": None},
                                  {"voiceover": True, "rerender": True}, [], "x"])
def test_mode_422(mode_api, body):
    client, _, stub = mode_api
    store = make_job_on_disk(clips=1)
    r = client.put(f"/api/jobs/{store.job.id}/mode", json=body)
    assert_error_shape(r, 422)
    assert stub.calls == [] and read_json(jobs_root() / store.job.id / "job.json").get("voiceover") is None


def test_mode_404_401_403(mode_api):
    client, _, stub = mode_api
    assert_error_shape(client.put("/api/jobs/nojob/mode", json={"voiceover": False}), 404)
    assert_error_shape(client.put("/api/jobs/bad..id/mode", json={"voiceover": False}), 422)
    store = make_job_on_disk(clips=1)
    from fastapi.testclient import TestClient

    bare = TestClient(client.app, base_url=BASE)
    assert_error_shape(bare.put(f"/api/jobs/{store.job.id}/mode", json={"voiceover": False}), 401)
    assert_error_shape(client.put(f"/api/jobs/{store.job.id}/mode", json={"voiceover": False},
                                  headers={TOKEN_HEADER: "x" * 43}), 403)
    assert_error_shape(client.put(f"/api/jobs/{store.job.id}/mode", json={"voiceover": False},
                                  headers={"Origin": "http://evil.com"}), 403)
    assert stub.calls == []


def test_mode_conflict_while_job_runs_in_other_mode(tmp_path):
    stub = ModeStub(block=True)
    client, worker, _ = make_client(stub=stub, worker=Worker(run_job=stub.run_job, rerender=stub.rerender))
    with client:
        store = make_job_on_disk(clips=1)
        assert client.put(f"/api/jobs/{store.job.id}/mode", json={"voiceover": False}).status_code == 202
        assert stub.started.wait(5)
        # same mode again: the running task is returned (200), the other mode is a 409
        again = client.put(f"/api/jobs/{store.job.id}/mode", json={"voiceover": False})
        assert again.status_code == 200 and again.json()["task"]["state"] == "running"
        assert_error_shape(client.put(f"/api/jobs/{store.job.id}/mode", json={"voiceover": True}), 409)
        # a plain resume while it runs still just returns it
        assert client.post(f"/api/jobs/{store.job.id}/resume").status_code == 200
        stub.release.set()
        assert worker.wait_idle(10)
    assert len(stub.calls) == 1


# ============================================================================ title clean-up
@pytest.mark.parametrize("raw,expected", [
    ('"Slipped the Surly Bonds of Earth" – Reagan\'s Goodbye', '"Slipped the Surly Bonds of Earth" – Reagan\'s Goodbye'),
    ('Slipped the Surly Bonds of Earth" – Reagan\'s Goodbye', "Slipped the Surly Bonds of Earth – Reagan's Goodbye"),
    ('"Whole Title In Quotes"', "Whole Title In Quotes"),
    ("“Curly wrapped”", "Curly wrapped"),
    ("Stray curly” end", "Stray curly end"),
    ("Friend Roasts Him Over a 'Garbage' Red SUV", "Friend Roasts Him Over a 'Garbage' Red SUV"),
    ('He said "go" and "stop', 'He said "go" and stop'),
    ('Odd quote " , here', "Odd quote, here"),
    ("जीटीए छह में जो गाड़ी चुराई\"", "जीटीए छह में जो गाड़ी चुराई"),
])
def test_title_quote_cleanup(raw, expected):
    assert _clean_title(raw) == expected
    s = normalize_script(ClipScript(narration="n", title=raw, description="d", hashtags=[]))
    assert s.title == expected


# ============================================================================ real Demucs (slow)
@pytest.mark.slow
@pytest.mark.demucs
def test_real_demucs_separation(tmp_path):
    import pwd

    real_py = Path(pwd.getpwuid(os.getuid()).pw_dir) / "tools" / "demucs" / "bin" / "python"
    py = Path(os.environ.get("AUTOCLIPPER_TEST_DEMUCS_PYTHON", str(real_py)))
    if not py.is_file():
        pytest.skip(f"Demucs is not installed at {py}")
    s = SeparationSettings(background="no_vocals", python=str(py), segment=7, threads=4,
                           models_dir=str(py.parent.parent / "models"))
    clip = tmp_path / "clip"
    clip.mkdir()
    cut = make_cut(clip / "cut.mp4", seconds=6.0)
    bed = separation.no_vocals_bed(cut, clip, s)
    assert bed.is_file() and ffmpeg.duration(bed) == pytest.approx(6.0, abs=0.2)
    assert separation.cached_bed(clip, separation.cache_key(cut, s)) == bed
