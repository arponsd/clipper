"""Step 3 integration: overlay composite, render_clip fallback, pipeline stamps/staleness, doctor, settings."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from fakes import FakeLLM, FakeTranscriber, FakeVoicebox, make_alpha_overlay, make_tone, make_video

from autoclipper import cli, ffmpeg, pipeline
from autoclipper.config import Settings, load_settings, save_settings, settings_path
from autoclipper.jobstore import JobStore
from autoclipper.pipeline import Deps, rerender, run_job
from autoclipper.stages import hyperframes as hf
from autoclipper.stages import render
from autoclipper.stages.render import (RenderError, render_clip, render_final, render_is_current, render_key,
                                       write_render_stamp)

W, H = 270, 480


# --------------------------------------------------------------------------------------------- helpers
def _frame(path: Path, t: float, w: int = W, h: int = H) -> bytes:
    return subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout


def _px(frame: bytes, x: int, y: int, w: int = W) -> tuple[int, int, int]:
    i = (y * w + x) * 3
    return frame[i], frame[i + 1], frame[i + 2]


def _lufs(path: Path) -> float:
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return float(re.findall(r"I:\s+(-?[0-9.]+) LUFS", err)[-1])


@pytest.fixture
def media(tmp_path: Path) -> dict:
    out = tmp_path / "clips" / "00" / "en"
    out.mkdir(parents=True)
    cut = make_video(tmp_path / "clips" / "00" / "cut.mp4", seconds=2.0, width=W, height=H)
    voice = make_tone(out / "voice.wav", seconds=1.2)
    (out / "words.json").write_text(json.dumps([{"start": 0.1, "end": 0.6, "text": "hello"},
                                                {"start": 0.6, "end": 1.1, "text": "world"}]), encoding="utf-8")
    (out / "captions.ass").write_text(_ass(), encoding="utf-8")
    overlay = make_alpha_overlay(tmp_path / "ov.webm", 2.0, W, H)
    return {"cut": cut, "voice": voice, "out": out, "overlay": overlay}


def _ass() -> str:
    from autoclipper.models import Word
    from autoclipper.stages.captions import build_ass

    s = Settings()
    return build_ass([Word(start=0.1, end=0.6, text="hello"), Word(start=0.6, end=1.1, text="world")], lang="en",
                     settings=s.captions, width=W, height=H)


def _capture_runs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = ffmpeg.run

    def spy(args, **kw):
        calls.append([str(a) for a in args])
        return real(args, **kw)

    monkeypatch.setattr(ffmpeg, "run", spy)
    return calls


def _fc(args: list[str]) -> str:
    return args[args.index("-filter_complex") + 1]


# --------------------------------------------------------------------------------------------- render_final
def test_render_final_with_overlay_composites_and_keeps_audio(media: dict, tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_runs(monkeypatch)
    s = Settings()
    a = render_final(cut=media["cut"], voice=media["voice"], ass=None, overlay=media["overlay"],
                     dst=tmp_path / "with.mp4", audio=s.audio, words_json=media["out"] / "words.json")
    b = render_final(cut=media["cut"], voice=media["voice"], ass=None, overlay=None,
                     dst=tmp_path / "without.mp4", audio=s.audio, words_json=media["out"] / "words.json")
    ia, ib, ic = ffmpeg.probe(a), ffmpeg.probe(b), ffmpeg.probe(media["cut"])
    assert (ia.width, ia.height) == (ic.width, ic.height) and abs(ia.duration - ic.duration) < 0.1
    fa, fb = _frame(a, 1.0), _frame(b, 1.0)
    box = [(x, y) for x in range(W // 3 + 4, 2 * W // 3 - 4, 6) for y in range(H // 2 - H // 16 + 3, H // 2 + H // 16 - 3, 4)]
    yellow = lambda p: p[0] > 200 and p[1] > 200 and p[2] < 70  # noqa: E731
    assert all(yellow(_px(fa, x, y)) for x, y in box)  # the overlay's opaque box is composited
    assert sum(not yellow(_px(fb, x, y)) for x, y in box) > len(box) // 4  # ... and is not in the plain render
    for x, y in ((5, 5), (W - 6, 5), (5, H - 6), (W - 6, H - 6)):  # outside the box: unchanged (no black box)
        assert sum(abs(p - q) for p, q in zip(_px(fa, x, y), _px(fb, x, y))) < 40
    graph_a, graph_b = _fc(calls[0]), _fc(calls[1])
    assert graph_a.split(";", 1)[1] == graph_b  # the audio graph is byte-identical
    assert graph_a.startswith("[0:v][2:v]overlay=0:0:format=auto:eof_action=pass,format=yuv420p[v];")
    assert abs(_lufs(a) - _lufs(b)) < 0.1
    # the libvpx decoder is an input option right before the webm input only
    args = calls[0]
    i = args.index(str(media["overlay"]))
    assert args[i - 3:i] == ["-c:v", "libvpx-vp9", "-i"]
    assert args.count("libvpx-vp9") == 1 and "libvpx-vp9" not in calls[1]
    assert args[args.index("-map") + 1] == "[v]"


def test_render_final_mov_overlay_has_no_decoder_flag(media: dict, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_runs(monkeypatch)
    mov = make_alpha_overlay(tmp_path / "ov.mov", 2.0, W, H, fmt="mov")
    render_final(cut=media["cut"], voice=None, ass=None, overlay=mov, dst=tmp_path / "m.mp4", audio=Settings().audio)
    assert "libvpx-vp9" not in calls[0]
    f = _frame(tmp_path / "m.mp4", 1.0)
    assert _px(f, W // 2, H // 2)[0] > 180 and _px(f, W // 2, H // 2)[2] < 80


def test_render_final_ass_and_overlay_are_exclusive(media: dict, tmp_path: Path) -> None:
    with pytest.raises(RenderError, match="not both"):
        render_final(cut=media["cut"], voice=None, ass=media["out"] / "captions.ass", overlay=media["overlay"],
                     dst=tmp_path / "x.mp4", audio=Settings().audio)
    with pytest.raises(RenderError, match="caption overlay file not found"):
        render_final(cut=media["cut"], voice=None, ass=None, overlay=tmp_path / "missing.webm",
                     dst=tmp_path / "x.mp4", audio=Settings().audio)


# --------------------------------------------------------------------------------------------- render_clip
def _hf_settings() -> Settings:
    s = Settings()
    s.captions.engine = "hyperframes"
    return s


def _fake_env() -> hf.HFEnv:
    return hf.HFEnv(node=Path("/x/node"), cli=Path("/x/hf.mjs"), node_version="v22.23.5", hf_version="0.9.52",
                    gsap=Path("/x/gsap.min.js"))


def _good_make_overlay(calls: list | None = None):
    def fake(*, words_json, cut, lang, settings, out_dir, on_progress=None, runner=None, env=None):
        if calls is not None:
            calls.append(dict(lang=lang, out_dir=Path(out_dir), env=env))
        info = ffmpeg.probe(Path(cut))
        dst = Path(out_dir) / f"captions_overlay.{settings.hyperframes.format}"
        make_alpha_overlay(dst, info.duration, info.width, info.height, settings.hyperframes.format)
        (Path(out_dir) / "hf").mkdir(exist_ok=True)
        (Path(out_dir) / "hf" / "overlay.json").write_text(json.dumps({"fps": 25}), encoding="utf-8")
        if on_progress:
            on_progress(0.5, "hyperframes capture")
        return dst
    return fake


@pytest.fixture
def probe_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    def fake_probe(_s):
        calls.append(1)
        return _fake_env()

    monkeypatch.setattr(hf, "probe_env", fake_probe)
    return calls


def test_render_clip_ass_engine_never_calls_hyperframes(media: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kw):
        raise AssertionError("make_overlay must not run for engine ass")

    monkeypatch.setattr(hf, "make_overlay", boom)
    monkeypatch.setattr(hf, "probe_env", boom)
    o = render_clip(cut=media["cut"], out_dir=media["out"], settings=Settings())
    assert o.engine_used == "ass" and o.warning is None and o.path.is_file()


def test_render_clip_hyperframes_ok(media: dict, monkeypatch: pytest.MonkeyPatch, probe_calls: list[int]) -> None:
    monkeypatch.setattr(hf, "make_overlay", _good_make_overlay())
    seen: list[float] = []
    o = render_clip(cut=media["cut"], out_dir=media["out"], settings=_hf_settings(),
                    on_progress=lambda f, m="": seen.append(f))
    assert o.engine_used == "hyperframes" and o.warning is None and o.hf_version == "0.9.52" and o.fps == 25
    assert seen and seen == sorted(seen) and 0.34 < seen[0] < 0.36 and max(seen) <= 1.0  # 0.5 of 0..0.7
    f = _frame(o.path, 1.0)
    assert _px(f, W // 2, H // 2)[0] > 180 and _px(f, W // 2, H // 2)[2] < 80


@pytest.mark.parametrize("exc", [hf.HyperFramesUnavailable("node v16.20.2 is too old"),
                                 hf.HyperFramesError("hyperframes render failed (rc 1): boom"),
                                 hf.HyperFramesTimeout("timed out after 300 s"),
                                 ValueError("unexpected")])
def test_render_clip_falls_back_to_ass(media: dict, monkeypatch: pytest.MonkeyPatch, probe_calls: list[int],
                                       exc: Exception) -> None:
    def fail(**kw):
        raise exc

    monkeypatch.setattr(hf, "make_overlay", fail)
    o = render_clip(cut=media["cut"], out_dir=media["out"], settings=_hf_settings())
    assert o.engine_used == "ass" and o.path.is_file()
    assert o.warning == f"HyperFrames captions failed ({' '.join(str(exc).split())}); used ASS captions"
    assert len(o.reason) <= 200


def test_render_clip_fallback_reason_is_short(media: dict, monkeypatch: pytest.MonkeyPatch,
                                              probe_calls: list[int]) -> None:
    def fail(**kw):
        raise hf.HyperFramesError("x" * 1000 + "\nmore\nlines")

    monkeypatch.setattr(hf, "make_overlay", fail)
    o = render_clip(cut=media["cut"], out_dir=media["out"], settings=_hf_settings())
    assert len(o.reason) == 200 and o.reason.endswith("...") and "\n" not in o.warning


def test_render_clip_probe_unavailable_falls_back(media: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_s):
        raise hf.HyperFramesUnavailable("node not found in ~/tools/node22/bin")

    monkeypatch.setattr(hf, "probe_env", unavailable)
    o = render_clip(cut=media["cut"], out_dir=media["out"], settings=_hf_settings(), hf_env_cache={})
    assert o.engine_used == "ass" and "node not found" in o.warning


def test_render_clip_keyboard_interrupt_propagates(media: dict, monkeypatch: pytest.MonkeyPatch,
                                                   probe_calls: list[int]) -> None:
    def interrupt(**kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(hf, "make_overlay", interrupt)
    with pytest.raises(KeyboardInterrupt):
        render_clip(cut=media["cut"], out_dir=media["out"], settings=_hf_settings())
    assert not (media["out"] / "final.mp4").exists()


def test_render_clip_composite_failure_falls_back(media: dict, monkeypatch: pytest.MonkeyPatch,
                                                  probe_calls: list[int]) -> None:
    def broken(**kw):
        dst = Path(kw["out_dir"]) / "captions_overlay.webm"
        dst.write_bytes(b"not a video")
        return dst

    monkeypatch.setattr(hf, "make_overlay", broken)
    o = render_clip(cut=media["cut"], out_dir=media["out"], settings=_hf_settings())
    assert o.engine_used == "ass" and "overlay composite failed" in o.warning


# --------------------------------------------------------------------------------------------- staleness
def test_render_key_stamp_and_is_current(media: dict) -> None:
    out = media["out"]
    ass, hfs = Settings(), _hf_settings()
    assert render_key(ass) == {"caption_engine": "ass"}
    assert render_key(hfs) == {"caption_engine": "hyperframes", "style": "pop", "format": "webm", "fps": None}
    assert not render_is_current(out, ass)  # no final.mp4
    (out / "final.mp4").write_bytes(b"x")
    # legacy final without render.json = ASS, made with the pre-loudness voice-over mix: stale since the
    # loudness mix (render.json audio_key, lead triage of the mix review)
    assert not render_is_current(out, ass)
    assert not render_is_current(out, hfs)
    write_render_stamp(out, hfs, render.RenderOutcome(path=out / "final.mp4", engine_used="hyperframes",
                                                      warning=None, hf_version="0.9.52", fps=25))
    stamp = json.loads((out / "render.json").read_text(encoding="utf-8"))
    assert stamp == {"caption_engine": "hyperframes", "style": "pop", "format": "webm", "fps": None,
                     "engine_used": "hyperframes", "warning": None, "hf_version": "0.9.52", "resolved_fps": 25,
                     "audio_key": render.audio_key(hfs)}
    assert render_is_current(out, hfs) and not render_is_current(out, ass)
    hfs.hyperframes.style = "karaoke"
    assert not render_is_current(out, hfs)
    hfs.hyperframes.style = "pop"
    hfs.hyperframes.fps = 30
    assert not render_is_current(out, hfs)
    hfs.hyperframes.fps = None
    hfs.hyperframes.format = "mov"
    assert not render_is_current(out, hfs)
    hfs.hyperframes.format = "webm"
    hfs.hyperframes.workers = 4  # not part of the key
    assert render_is_current(out, hfs)
    # a fallback render is stale for hyperframes (a resume retries it)
    write_render_stamp(out, hfs, render.RenderOutcome(path=out / "final.mp4", engine_used="ass", warning="w"))
    assert not render_is_current(out, hfs)
    (out / "render.json").write_text("{corrupt", encoding="utf-8")
    assert not render_is_current(out, ass)  # unreadable stamp = legacy ASS render with the old mix: stale


# --------------------------------------------------------------------------------------------- pipeline
def _settings(engine: str = "ass") -> Settings:
    s = Settings()
    s.voicebox.english.profile_id = "fake-en"
    s.voicebox.hindi.profile_id = "fake-hi"
    s.clips.count = 2
    s.clips.min_seconds = 6
    s.clips.max_seconds = 10
    s.clips.width, s.clips.height = W, H
    s.captions.engine = engine  # type: ignore[assignment]
    return s


class Counting:
    def __init__(self) -> None:
        self.voicebox = FakeVoicebox(seconds_per_word=0.2)
        self.llm = FakeLLM(narration_words=8)
        self.transcriber = FakeTranscriber(duration=24)

    def deps(self) -> Deps:
        return Deps(llm=self.llm, voicebox=self.voicebox, transcriber=self.transcriber)

    def voice_transcribes(self) -> int:
        return sum(1 for a, _ in self.transcriber.calls if Path(a).name != "audio.wav")


@pytest.fixture
def video(tmp_path: Path) -> Path:
    return make_video(tmp_path / "src.mp4", seconds=24, width=320, height=180)


def _states(store: JobStore):
    return [(c.index, lang, st) for c in store.job.clips for lang, st in c.langs.items()]


def test_run_job_hyperframes_ok_and_resume_rules(video: Path, monkeypatch: pytest.MonkeyPatch,
                                                 probe_calls: list[int]) -> None:
    mk: list[dict] = []
    monkeypatch.setattr(hf, "make_overlay", _good_make_overlay(mk))
    c = Counting()
    logs: list[str] = []
    store = run_job(str(video), settings=_settings("hyperframes"), deps=c.deps(), langs=["en", "hi"], log=logs.append)
    assert store.job.status == "done"
    states = _states(store)
    assert len(states) == 4 and len(mk) == 4
    assert len(probe_calls) == 1  # probe_env memoised per run_job (2 clips x 2 langs)
    for idx, lang, st in states:
        out = store.clip_dir(idx, lang)
        stamp = json.loads((out / "render.json").read_text(encoding="utf-8"))
        assert stamp["engine_used"] == "hyperframes" and stamp["caption_engine"] == "hyperframes"
        assert st.warning is None and st.error is None
        assert "HyperFrames pop captions" in st.stages["render"].message
        assert (out / "captions.ass").is_file() and (out / "words.json").is_file()  # ASS fallback still built

    # unchanged engine: resume skips render (and everything else)
    n_voice, n_tr = len(c.voicebox.calls), c.voice_transcribes()
    mk.clear()
    run_job("", settings=_settings("hyperframes"), deps=c.deps(), job_id=store.job.id, log=logs.append)
    assert mk == [] and len(c.voicebox.calls) == n_voice and c.voice_transcribes() == n_tr

    # switch engine to ass: only the render stage reruns, without HyperFrames
    store = run_job("", settings=_settings("ass"), deps=c.deps(), job_id=store.job.id, log=logs.append)
    assert mk == [] and len(c.voicebox.calls) == n_voice and c.voice_transcribes() == n_tr
    for idx, lang, st in _states(store):
        assert json.loads((store.clip_dir(idx, lang) / "render.json").read_text())["engine_used"] == "ass"
        assert st.stages["render"].status == "done"

    # style change with hyperframes: render reruns for every clip-lang, still no voice/captions work
    s = _settings("hyperframes")
    s.hyperframes.style = "karaoke"
    run_job("", settings=s, deps=c.deps(), job_id=store.job.id, log=logs.append)
    assert len(mk) == 4 and len(c.voicebox.calls) == n_voice and c.voice_transcribes() == n_tr


def test_run_job_hyperframes_failure_falls_back_and_resume_retries(video: Path, monkeypatch: pytest.MonkeyPatch,
                                                                   probe_calls: list[int]) -> None:
    def fail(**kw):
        raise hf.HyperFramesError("hyperframes render failed (rc 1): chrome crashed")

    monkeypatch.setattr(hf, "make_overlay", fail)
    c = Counting()
    logs: list[str] = []
    store = run_job(str(video), settings=_settings("hyperframes"), deps=c.deps(), langs=["en"], log=logs.append)
    assert store.job.status == "done" and store.job.error is None
    for idx, lang, st in _states(store):
        assert st.error is None and st.final
        assert st.warning == ("HyperFrames captions failed (hyperframes render failed (rc 1): chrome crashed); "
                              "used ASS captions")
        assert st.stages["render"].status == "done"
        assert st.stages["render"].message.startswith("final.mp4 (ASS fallback: hyperframes render failed")
        out = store.clip_dir(idx, lang)
        assert (out / "final.mp4").stat().st_size > 0
        assert json.loads((out / "render.json").read_text())["engine_used"] == "ass"
        assert not render_is_current(out, _settings("hyperframes"))
    assert any("WARNING HyperFrames captions failed" in ln and " render " in ln for ln in logs)
    job = json.loads((store.dir / "job.json").read_text(encoding="utf-8"))
    assert job["clips"][0]["langs"]["en"]["warning"].startswith("HyperFrames captions failed")

    # the user installs HyperFrames: resume retries exactly the fallback renders, nothing else
    mk: list[dict] = []
    monkeypatch.setattr(hf, "make_overlay", _good_make_overlay(mk))
    n_voice, n_tr = len(c.voicebox.calls), c.voice_transcribes()
    store = run_job("", settings=_settings("hyperframes"), deps=c.deps(), job_id=store.job.id, langs=["en"],
                    log=logs.append)
    assert len(mk) == 2 and len(c.voicebox.calls) == n_voice and c.voice_transcribes() == n_tr
    for _, _, st in _states(store):
        assert st.warning is None  # reset by the new render


@pytest.mark.parametrize("exc", [RenderError("ffmpeg failed (rc 1)"), KeyboardInterrupt()])
def test_failed_or_cancelled_render_does_not_leave_a_stampless_final(video: Path, monkeypatch: pytest.MonkeyPatch,
                                                                     probe_calls: list[int],
                                                                     exc: BaseException) -> None:
    """Triage R1 (correctness): an HF final whose ASS re-render fails or is cancelled must not count as a
    current legacy ASS render on the next resume."""
    monkeypatch.setattr(hf, "make_overlay", _good_make_overlay())
    c = Counting()
    store = run_job(str(video), settings=_settings("hyperframes"), deps=c.deps(), langs=["en"], log=lambda _m: None)
    jid = store.job.id
    outs = [store.clip_dir(idx, lang) for idx, lang, _ in _states(store)]
    assert all((o / "final.mp4").is_file() for o in outs)

    def boom(**kw):
        raise exc

    real = render.render_clip
    monkeypatch.setattr(render, "render_clip", boom)
    if isinstance(exc, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            run_job("", settings=_settings("ass"), deps=c.deps(), job_id=jid, langs=["en"], log=lambda _m: None)
    else:
        run_job("", settings=_settings("ass"), deps=c.deps(), job_id=jid, langs=["en"], log=lambda _m: None)
    first = outs[0]
    assert not (first / "final.mp4").exists() and not (first / "render.json").exists()
    assert not render_is_current(first, _settings("ass"))
    st = JobStore.load(jid).job.clips[0].langs["en"]
    assert st.final is None and st.error

    calls: list[int] = []
    monkeypatch.setattr(render, "render_clip", lambda **kw: calls.append(1) or real(**kw))
    store = run_job("", settings=_settings("ass"), deps=c.deps(), job_id=jid, langs=["en"], log=lambda _m: None)
    assert calls  # the clip is rendered again instead of being skipped
    for o in outs:
        assert json.loads((o / "render.json").read_text())["engine_used"] == "ass"
        assert render_is_current(o, _settings("ass"))


def test_legacy_final_without_stamp_is_rerendered_once(video: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A step-2 final (no render.json) was made with the old voice-over mix: a resume re-renders it once
    (render.json audio_key), then it is current."""
    c = Counting()
    store = run_job(str(video), settings=_settings("ass"), deps=c.deps(), langs=["en"], log=lambda _m: None)
    for idx, lang, _ in _states(store):
        (store.clip_dir(idx, lang) / "render.json").unlink()  # a step-2 job had no stamp
    calls = []
    real = render.render_clip
    monkeypatch.setattr(render, "render_clip", lambda **kw: calls.append(1) or real(**kw))
    run_job("", settings=_settings("ass"), deps=c.deps(), job_id=store.job.id, langs=["en"], log=lambda _m: None)
    assert calls and len(calls) == len(list(_states(store)))
    calls.clear()
    run_job("", settings=_settings("ass"), deps=c.deps(), job_id=store.job.id, langs=["en"], log=lambda _m: None)
    assert calls == []


def test_rerender_works_with_both_engines(video: Path, monkeypatch: pytest.MonkeyPatch,
                                          probe_calls: list[int]) -> None:
    c = Counting()
    store = run_job(str(video), settings=_settings("ass"), deps=c.deps(), langs=["en"], log=lambda _m: None)
    jid = store.job.id
    mk: list[dict] = []
    monkeypatch.setattr(hf, "make_overlay", _good_make_overlay(mk))
    final = rerender(jid, 0, "en", settings=_settings("hyperframes"), deps=c.deps(), log=lambda _m: None)
    out = final.parent
    assert len(mk) == 1 and json.loads((out / "render.json").read_text())["engine_used"] == "hyperframes"
    final = rerender(jid, 0, "en", settings=_settings("ass"), deps=c.deps(), log=lambda _m: None)
    assert len(mk) == 1 and json.loads((out / "render.json").read_text())["engine_used"] == "ass"

    def fail(**kw):
        raise hf.HyperFramesUnavailable("HyperFrames not installed at /bogus")

    monkeypatch.setattr(hf, "make_overlay", fail)
    final = rerender(jid, 0, "en", settings=_settings("hyperframes"), deps=c.deps(), log=lambda _m: None)
    st = JobStore.load(jid).job.clips[0].langs["en"]
    assert final.is_file() and st.error is None and "not installed" in st.warning


def test_recut_deletes_overlay_and_stamp() -> None:
    for name in ("render.json", "captions_overlay.webm", "captions_overlay.mov", "final.mp4"):
        assert name in pipeline._CUT_DEPENDENT_OUTPUTS


# --------------------------------------------------------------------------------------------- doctor
def _cp(stdout: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, stdout, "")


def _doctor_fs(tmp_path: Path, *, node: bool = True, pkg: bool = True) -> Settings:
    s = Settings()
    s.hyperframes.node_bin_dir = str(tmp_path / "n" / "bin")
    s.hyperframes.package_dir = str(tmp_path / "p")
    if node:
        (tmp_path / "n" / "bin").mkdir(parents=True)
        (tmp_path / "n" / "bin" / "node").write_text("", encoding="utf-8")
    if pkg:
        cli_file = tmp_path / "p" / "node_modules" / "hyperframes" / "bin" / "hyperframes.mjs"
        cli_file.parent.mkdir(parents=True)
        cli_file.write_text("", encoding="utf-8")
        g = cli_file.parent.parent / "dist" / "vendor" / "npm" / "gsap@3.15.0" / "dist" / "gsap.min.js"
        g.parent.mkdir(parents=True)
        g.write_text("", encoding="utf-8")
    return s


def _fake_run(node_v: str = "v22.23.5", hf_v: str = "0.9.52", chrome: str = "/c/chrome", vp9: bool = True):
    def run(args):
        if args[-1] == "--version" and len(args) == 2:
            return _cp(node_v + "\n")
        if args[-1] == "--version":
            return _cp(hf_v + "\n")
        if args[-2:] == ["browser", "path"]:
            return _cp(chrome + "\n" if chrome else "", 0 if chrome else 1)
        if "-decoders" in args:
            return _cp(" V..... libvpx-vp9           libvpx VP9 (codec vp9)\n" if vp9 else " V..... vp9  Google VP9\n")
        raise AssertionError(args)
    return run


def _rows(checks) -> dict[str, tuple[str, str]]:
    return {c.name: (c.level, c.detail) for c in checks}


def test_check_hyperframes_all_ok(tmp_path: Path) -> None:
    s = _doctor_fs(tmp_path)
    which = lambda n: "/usr/bin/ffmpeg" if n == "ffmpeg" else None  # noqa: E731
    rows = cli.check_hyperframes(s, run=_fake_run(), which=which)
    assert [c.name for c in rows] == ["node", "hyperframes", "hyperframes chrome", "ffmpeg vp9 alpha", "caption engine"]
    r = _rows(rows)
    assert r["node"] == ("ok", f"v22.23.5 ({tmp_path / 'n' / 'bin' / 'node'})")
    assert r["hyperframes"] == ("ok", f"0.9.52 ({s.hyperframes.package_dir})")
    assert r["hyperframes chrome"] == ("ok", "/c/chrome")
    assert r["ffmpeg vp9 alpha"][0] == "ok" and r["caption engine"][0] == "ok"
    s.captions.engine = "hyperframes"
    r = _rows(cli.check_hyperframes(s, run=_fake_run(), which=which))
    assert r["caption engine"] == ("ok", "hyperframes (pop, webm)")
    assert all(c.level != "fail" for c in cli.check_hyperframes(s, run=_fake_run(), which=which))


def test_check_hyperframes_node16_and_missing(tmp_path: Path) -> None:
    s = _doctor_fs(tmp_path)
    s.captions.engine = "hyperframes"
    which = lambda n: "/usr/bin/ffmpeg" if n == "ffmpeg" else None  # noqa: E731
    r = _rows(cli.check_hyperframes(s, run=_fake_run(node_v="v16.20.2"), which=which))
    assert r["node"][0] == "warn" and "v16.20.2" in r["node"][1] and "older than 22" in r["node"][1]
    assert r["hyperframes"][0] == "warn" and r["hyperframes chrome"][0] == "warn"
    assert r["caption engine"] == ("warn", "hyperframes, but a prerequisite is missing - clips will fall back to "
                                           "ASS captions")
    s2 = _doctor_fs(tmp_path / "b", node=True, pkg=False)
    s2.captions.engine = "hyperframes"
    r = _rows(cli.check_hyperframes(s2, run=_fake_run(), which=which))
    assert r["node"][0] == "ok"
    assert r["hyperframes"][0] == "warn" and "not installed" in r["hyperframes"][1]
    assert "npm --prefix" in r["hyperframes"][1] and "hyperframes@0.9.52" in r["hyperframes"][1]
    s3 = _doctor_fs(tmp_path / "c", node=False, pkg=False)
    r = _rows(cli.check_hyperframes(s3, run=_fake_run(), which=which))
    assert r["node"][0] == "warn" and "not found" in r["node"][1]
    assert r["caption engine"][0] == "ok"  # engine ass: nothing falls back


def test_check_hyperframes_version_chrome_and_vp9(tmp_path: Path) -> None:
    s = _doctor_fs(tmp_path)
    s.captions.engine = "hyperframes"
    which = lambda n: "/usr/bin/ffmpeg" if n == "ffmpeg" else None  # noqa: E731
    r = _rows(cli.check_hyperframes(s, run=_fake_run(hf_v="0.9.60"), which=which))
    assert r["hyperframes"][0] == "warn" and "0.9.60" in r["hyperframes"][1] and "still usable" in r["hyperframes"][1]
    r = _rows(cli.check_hyperframes(s, run=_fake_run(chrome=""), which=which))
    assert r["hyperframes chrome"][0] == "warn" and "browser ensure" in r["hyperframes chrome"][1]
    assert r["caption engine"][0] == "warn"
    r = _rows(cli.check_hyperframes(s, run=_fake_run(vp9=False), which=which))
    assert r["ffmpeg vp9 alpha"][0] == "warn" and "hyperframes.format=mov" in r["ffmpeg vp9 alpha"][1]
    assert r["caption engine"][0] == "warn"
    s.hyperframes.format = "mov"
    rows = cli.check_hyperframes(s, run=_fake_run(vp9=False), which=which)
    assert "ffmpeg vp9 alpha" not in _rows(rows) and _rows(rows)["caption engine"][0] == "ok"


def test_doctor_exit_code_unaffected_by_hyperframes_warnings(monkeypatch: pytest.MonkeyPatch,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    ok = lambda name: cli.Check(name, "ok", "fake")  # noqa: E731
    monkeypatch.setattr(cli, "check_binaries", lambda *a, **k: [ok("ffmpeg"), ok("ffprobe")])
    monkeypatch.setattr(cli, "check_filters", lambda *a, **k: ok("ffmpeg filters"))
    monkeypatch.setattr(cli, "check_fonts", lambda *a, **k: ok("fonts"))
    monkeypatch.setattr(cli, "check_voicebox", lambda *a, **k: [ok("voicebox")])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    # real check_hyperframes against an empty HOME: node/hyperframes missing -> warnings only
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "[WARN] node" in out and "[WARN] hyperframes" in out and "0 problem(s)" in out


# --------------------------------------------------------------------------------------------- settings
def test_settings_defaults_and_old_file_loads() -> None:
    s = Settings()
    assert s.captions.engine == "ass"
    h = s.hyperframes
    assert (h.node_bin_dir, h.package_dir, h.style, h.fps, h.format, h.workers, h.timeout_seconds) == (
        "~/tools/node22/bin", "~/tools/hyperframes", "pop", None, "webm", 0, 300.0)
    old = Settings().model_dump()
    del old["hyperframes"]
    del old["captions"]["engine"]
    settings_path().write_text(json.dumps(old), encoding="utf-8")
    loaded = load_settings()
    assert loaded.captions.engine == "ass" and loaded.hyperframes.style == "pop"


def test_cli_settings_set_engine_and_fps_null(capsys: pytest.CaptureFixture[str]) -> None:
    save_settings(Settings())
    assert cli.main(["settings", "set", "captions.engine=hyperframes", "hyperframes.style=karaoke",
                     "hyperframes.fps=30"]) == 0
    s = load_settings()
    assert s.captions.engine == "hyperframes" and s.hyperframes.style == "karaoke" and s.hyperframes.fps == 30
    assert cli.main(["settings", "set", "hyperframes.fps=null", "hyperframes.node_bin_dir=~/x/bin"]) == 0
    s = load_settings()
    assert s.hyperframes.fps is None and s.hyperframes.node_bin_dir == "~/x/bin"  # stored as typed
    capsys.readouterr()


@pytest.mark.parametrize("assignment", ["captions.engine=libass", "hyperframes.style=x", "hyperframes.fps=5",
                                        "hyperframes.fps=61", "hyperframes.workers=99", "hyperframes.workers=-1",
                                        "hyperframes.timeout_seconds=1", "hyperframes.timeout_seconds=4000",
                                        "hyperframes.format=mp4"])
def test_cli_settings_set_rejects_bad_hyperframes_values(assignment: str, capsys: pytest.CaptureFixture[str]) -> None:
    save_settings(Settings())
    assert cli.main(["settings", "set", assignment]) == 1
    assert "error" in capsys.readouterr().err
    assert load_settings() == Settings()


def test_path_settings_validation() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings.model_validate({"hyperframes": {"node_bin_dir": "a" * 501}})
    with pytest.raises(ValidationError):
        Settings.model_validate({"hyperframes": {"package_dir": "a\nb"}})
    with pytest.raises(ValidationError):
        Settings.model_validate({"hyperframes": {"package_dir": "a\x00b"}})
