"""Regression tests for the reviewed fixes C1-C5, R2, R3 and B2 (see docs/TEAM_LOG.md, Fixer section).

Everything runs against fakes and real ffmpeg on synthetic media; no network, no real Voicebox/Claude/Whisper.
"""
from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import pytest
from fakes import FakeLLM, FakeTranscriber, FakeVoicebox, make_tone, make_video

from autoclipper import cli, ffmpeg
from autoclipper.config import Settings, VoiceProfile
from autoclipper.jobstore import JobStore
from autoclipper.models import Moment, Word
from autoclipper.pipeline import Deps, rerender, run_job
from autoclipper.stages import captions as captions_stage
from autoclipper.stages import render as render_stage
from autoclipper.stages.captions import build_ass, make_captions
from autoclipper.stages.render import render_final
from autoclipper.stages.voice import VoiceError, make_voice


def make_settings(count: int = 1) -> Settings:
    s = Settings()
    s.voicebox.english.profile_id = "fake-en"
    s.voicebox.hindi.profile_id = "fake-hi"
    s.clips.count = count
    s.clips.min_seconds = 10
    s.clips.max_seconds = 15
    return s


def deps(llm: FakeLLM | None = None, voicebox=None, transcriber=None) -> Deps:
    return Deps(llm=llm or FakeLLM(), voicebox=voicebox or FakeVoicebox(),
                transcriber=transcriber or FakeTranscriber(duration=40))


def quiet(_line: str) -> None:
    return None


def no_running(job_json: dict) -> bool:
    states = list(job_json["stages"].values())
    for c in job_json["clips"]:
        for ls in c["langs"].values():
            states.extend(ls["stages"].values())
    return all(st["status"] != "running" for st in states)


@pytest.fixture
def video(tmp_path: Path) -> Path:
    return make_video(tmp_path / "src.mp4", seconds=40)


# ------------------------------------------------------------------------------- C1 / R1
class FailingTranscriber(FakeTranscriber):
    """Source transcription works; voice transcription (captions stage) fails."""

    def transcribe(self, audio, language=None, on_progress=None):
        if Path(audio).name != "audio.wav":
            raise RuntimeError("whisper exploded")
        return super().transcribe(audio, language, on_progress)


@pytest.mark.parametrize("failing", ["render", "captions"])
def test_resume_after_failed_downstream_rebuilds_stale_outputs(video: Path, failing: str,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    s = make_settings()
    store = run_job(str(video), settings=s, deps=deps(), langs=["en"], log=quiet)
    job_id = store.job.id
    d = store.clip_dir(0, "en")
    final = d / "final.mp4"
    old_final_bytes = final.read_bytes()
    old_mtime = final.stat().st_mtime_ns

    new_narration = "a completely fresh narration about rockets"
    script = json.loads((d / "script.json").read_text(encoding="utf-8"))
    script["narration"] = new_narration
    (d / "script.json").write_text(json.dumps(script), encoding="utf-8")

    # run 2: voice regenerates, then a later stage fails
    with monkeypatch.context() as mp:
        if failing == "render":
            def boom(**_kw):
                raise RuntimeError("final.mp4 is locked by a player")
            mp.setattr(render_stage, "render_final", boom)
            tr = FakeTranscriber(duration=40)
        else:
            # ASR errors now fall back to even word spread (FAILURES-7), so fail captions directly.
            def boom_captions(**_kw):
                raise RuntimeError("captions exploded")
            mp.setattr(captions_stage, "make_captions", boom_captions)
            tr = FakeTranscriber(duration=40)
        vb2 = FakeVoicebox()
        st2 = run_job("", settings=s, deps=deps(voicebox=vb2, transcriber=tr), job_id=job_id,
                      langs=["en"], log=quiet)
    assert len(vb2.calls) == 1
    assert st2.job.clips[0].langs["en"].error.startswith(f"{failing}:")
    assert not final.exists(), "stale final.mp4 must be removed once its inputs were regenerated"
    if failing == "captions":
        assert not (d / "captions.ass").exists() and not (d / "words.json").exists()

    # run 3: working fakes -> voice is current (not regenerated) but captions/render are rebuilt
    vb3 = FakeVoicebox()
    st3 = run_job("", settings=s, deps=deps(voicebox=vb3), job_id=job_id, langs=["en"], log=quiet)
    assert vb3.calls == []
    assert st3.job.status == "done" and st3.job.clips[0].langs["en"].error is None
    assert final.is_file() and final.stat().st_size > 0
    assert final.stat().st_mtime_ns != old_mtime or final.read_bytes() != old_final_bytes
    ws = json.loads((d / "words.json").read_text(encoding="utf-8"))
    assert [w["text"] for w in ws] == new_narration.split()
    ass = (d / "captions.ass").read_text(encoding="utf-8")
    assert "rockets" in ass and "this is a test" not in ass


# ------------------------------------------------------------------------------- C2 / R4
def test_repicked_moments_rebuild_clips_and_move_old_dirs_aside(video: Path) -> None:
    s = make_settings(count=1)
    store = run_job(str(video), settings=s, deps=deps(), langs=["en"], log=quiet)
    job_id = store.job.id
    old_moment = store.job.clips[0].moment
    old_final = (store.clip_dir(0, "en") / "final.mp4").read_bytes()
    (store.dir / "moments.json").unlink()

    new = [Moment(start=20.0, end=32.0, hook="new a", reason="r", score=0.9),
           Moment(start=5.0, end=17.0, hook="new b", reason="r", score=0.8)]
    llm = FakeLLM(moments=new)
    logs: list[str] = []
    st = run_job("", settings=s, deps=deps(llm=llm), job_id=job_id, langs=["en"], count=2, log=logs.append)
    j = st.job
    on_disk = [Moment.model_validate(m) for m in
               json.loads((st.dir / "moments.json").read_text(encoding="utf-8"))]
    assert len(on_disk) == 2
    assert [c.moment for c in j.clips] == on_disk
    assert [c.index for c in j.clips] == [0, 1]
    assert j.clips[0].moment != old_moment

    stale = st.dir / "clips" / "00.stale-1"
    assert stale.is_dir(), "the old clip dir is renamed aside, never deleted"
    assert (stale / "en" / "final.mp4").read_bytes() == old_final
    assert any("00.stale-1" in line for line in logs)

    assert j.status == "done"
    for c in j.clips:
        assert c.cut == f"clips/{c.index:02d}/cut.mp4"
        assert (st.dir / c.cut).stat().st_size > 0
        ls = c.langs["en"]
        assert ls.error is None and all(x.status == "done" for x in ls.stages.values())
        assert (st.dir / ls.final).stat().st_size > 0
    cut_dur = ffmpeg.duration(st.dir / "clips" / "00" / "cut.mp4")
    assert cut_dur == pytest.approx(on_disk[0].duration, abs=0.3)

    # a second re-pick picks the next free stale name
    (st.dir / "moments.json").unlink()
    llm2 = FakeLLM(moments=[Moment(start=25.0, end=37.0, hook="c", reason="r", score=0.9)])
    run_job("", settings=s, deps=deps(llm=llm2), job_id=job_id, langs=["en"], count=1, log=quiet)
    assert (st.dir / "clips" / "00.stale-2").is_dir()
    assert (st.dir / "clips" / "01.stale-1").is_dir()  # index 1 is no longer present
    assert len(JobStore.load(job_id).job.clips) == 1


def test_overrides_on_existing_moments_log_a_warning(video: Path) -> None:
    s = make_settings(count=1)
    store = run_job(str(video), settings=s, deps=deps(), langs=["en"], log=quiet)
    logs: list[str] = []
    llm = FakeLLM()
    run_job("", settings=s, deps=deps(llm=llm), job_id=store.job.id, langs=["en"], count=3, log=logs.append)
    assert llm.calls == [], "moments.json is reused"
    warn = [line for line in logs if "warning" in line]
    assert warn and "--clips 3" in warn[0] and "ignored for an existing job" in warn[0]
    assert "moments.json" in warn[0]
    # no warning when nothing differs
    logs.clear()
    run_job("", settings=s, deps=deps(), job_id=store.job.id, langs=["en"], count=1, log=logs.append)
    assert not any("warning" in line for line in logs)


# ------------------------------------------------------------------------------- C3
def test_build_ass_uses_smart_wrapping() -> None:
    ass = build_ass([Word(start=0, end=1, text="hi")], lang="en", settings=Settings().captions)
    assert "WrapStyle: 0" in ass
    assert "WrapStyle: 2" not in ass


def _gray_frame(path: Path, at: float, width: int, height: int) -> bytes:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", str(path),
                          "-frames:v", "1", "-vf", "format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    assert len(out) == width * height
    return out


def test_long_caption_line_wraps_inside_margins(tmp_path: Path) -> None:
    w, h = 1080, 1920
    cut = tmp_path / "black.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c=black:size={w}x{h}:rate=25:duration=2",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(cut)], check=True)
    voice = make_tone(tmp_path / "voice.wav", seconds=2.0)
    text = "unbelievable transformation everywhere".split()
    words = [Word(start=0.0, end=0.6, text=text[0]), Word(start=0.6, end=1.2, text=text[1]),
             Word(start=1.2, end=2.0, text=text[2])]
    ass = tmp_path / "captions.ass"
    ass.write_text(build_ass(words, lang="en", settings=Settings().captions, width=w, height=h),
                   encoding="utf-8")
    final = render_final(cut=cut, voice=voice, ass=ass, dst=tmp_path / "final.mp4", audio=Settings().audio)
    frame = _gray_frame(final, 0.9, w, h)
    edge = 20
    bright_cols = [x for y in range(h) for x in (*range(edge), *range(w - edge, w))
                   if frame[y * w + x] > 100]
    assert not bright_cols, f"{len(bright_cols)} bright pixels in the outer {edge}px columns"
    assert max(frame) > 200, "captions must actually be burned in"


# ------------------------------------------------------------------------------- C4
@pytest.mark.parametrize("assignment, path, expected", [
    ("voicebox.english.profile_id=12345", ("voicebox", "english", "profile_id"), "12345"),
    ("captions.primary_color=1e5", ("captions", "primary_color"), "1e5"),
    ("whisper.language=123", ("whisper", "language"), "123"),  # str | None field, currently None
    ("whisper.language=null", ("whisper", "language"), None),
    ('voicebox.english.profile_id="777"', ("voicebox", "english", "profile_id"), "777"),
    ("clips.count=3", ("clips", "count"), 3),
    ("clips.min_seconds=12.5", ("clips", "min_seconds"), 12.5),
    ("llm.use_fallbacks=false", ("llm", "use_fallbacks"), False),
])
def test_apply_setting_keeps_numeric_looking_strings(assignment: str, path: tuple[str, ...],
                                                     expected: object) -> None:
    new = cli.apply_setting(Settings(), assignment)
    value: object = new
    for p in path:
        value = getattr(value, p)
    assert value == expected and type(value) is type(expected)


# ------------------------------------------------------------------------------- C5 / R5
class InterruptingVoicebox(FakeVoicebox):
    def synthesize(self, *a, **kw):
        raise KeyboardInterrupt


class InterruptingTranscriber(FakeTranscriber):
    def transcribe(self, audio, language=None, on_progress=None):
        raise KeyboardInterrupt


def _job_json(job_id: str) -> dict:
    return json.loads((JobStore.load(job_id).dir / "job.json").read_text(encoding="utf-8"))


def test_ctrl_c_in_clip_stage_marks_job_failed(video: Path) -> None:
    s = make_settings()
    with pytest.raises(KeyboardInterrupt):
        run_job(str(video), settings=s, deps=deps(voicebox=InterruptingVoicebox()), langs=["en"], log=quiet)
    [job_id] = JobStore.list_ids()
    j = _job_json(job_id)
    assert j["status"] == "failed" and j["error"] == "interrupted"
    assert no_running(j)
    ls = j["clips"][0]["langs"]["en"]
    assert ls["stages"]["voice"] == {"status": "failed", "progress": 0.0, "message": "interrupted"}
    assert ls["error"] == "voice: interrupted"
    # resuming works and finishes the job
    st = run_job("", settings=s, deps=deps(), job_id=job_id, langs=["en"], log=quiet)
    assert st.job.status == "done" and st.job.error is None


def test_ctrl_c_in_job_stage_marks_job_failed(video: Path) -> None:
    with pytest.raises(KeyboardInterrupt):
        run_job(str(video), settings=make_settings(), deps=deps(transcriber=InterruptingTranscriber()),
                langs=["en"], log=quiet)
    [job_id] = JobStore.list_ids()
    j = _job_json(job_id)
    assert j["status"] == "failed" and j["error"] == "interrupted"
    assert j["stages"]["transcribe"]["status"] == "failed"
    assert j["stages"]["transcribe"]["message"] == "interrupted"
    assert no_running(j)


def test_ctrl_c_in_rerender_marks_job_failed(video: Path) -> None:
    s = make_settings()
    store = run_job(str(video), settings=s, deps=deps(), langs=["en"], log=quiet)
    d = store.clip_dir(0, "en")
    script = json.loads((d / "script.json").read_text(encoding="utf-8"))
    script["narration"] = "edited so the voice must regenerate"
    (d / "script.json").write_text(json.dumps(script), encoding="utf-8")
    with pytest.raises(KeyboardInterrupt):
        rerender(store.job.id, 0, "en", settings=s, deps=deps(voicebox=InterruptingVoicebox()), log=quiet)
    j = _job_json(store.job.id)
    assert j["status"] == "failed" and j["error"] == "interrupted" and no_running(j)
    assert j["clips"][0]["langs"]["en"]["stages"]["voice"]["status"] == "failed"


# ------------------------------------------------------------------------------- R2
def test_progress_callback_error_kills_ffmpeg_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = make_video(tmp_path / "long.mp4", seconds=30)
    procs: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def spy(*a, **kw):
        p = real_popen(*a, **kw)
        if "-progress" in a[0]:  # the tracked ffmpeg run, not ffprobe/subprocess.run helpers
            procs.append(p)
        return p

    monkeypatch.setattr(ffmpeg.subprocess, "Popen", spy)
    calls = [0]

    def on_progress(_f: float, _m: str = "") -> None:
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError("job.json write failed")

    dst = tmp_path / "cut.mp4"
    with pytest.raises(RuntimeError, match="job.json write failed"):
        ffmpeg.cut_vertical(src, dst, 0, 30, on_progress=on_progress)
    assert len(procs) == 1
    assert procs[0].poll() is not None, "ffmpeg child must not keep running"
    assert procs[0].stdout.closed and procs[0].stderr.closed
    assert not dst.exists()
    assert not list(tmp_path.glob("*.part*")), "no half-written .part file may remain"


# ------------------------------------------------------------------------------- R3
def _write_empty_wav(dst: Path) -> Path:
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"")
    return dst


class EmptyVoicebox(FakeVoicebox):
    def synthesize(self, text, *, profile_id, language, engine, dst, on_progress=None):
        self.calls.append(dict(text=text, language=language))
        return _write_empty_wav(Path(dst))


def test_make_voice_rejects_empty_audio(tmp_path: Path) -> None:
    out = tmp_path / "en"
    with pytest.raises(VoiceError, match=r"Voicebox returned empty audio \(0\.0s\) for en"):
        make_voice(client=EmptyVoicebox(), narration="hello there world", lang="en",
                   voice=VoiceProfile(profile_id="p"), clip_duration=5.0, out_dir=out, max_speedup=1.25)
    assert not (out / "voice.json").exists()
    assert not (out / "voice.wav").exists()
    assert not (out / "voice_norm.wav").exists() and not (out / "voice_raw.wav").exists()


class EmptyAfterShortenVoicebox(FakeVoicebox):
    def synthesize(self, text, *, profile_id, language, engine, dst, on_progress=None):
        self.calls.append(dict(text=text))
        if len(self.calls) == 1:
            return make_tone(Path(dst), seconds=10.0)
        return _write_empty_wav(Path(dst))


def test_make_voice_rejects_empty_audio_after_shortening(tmp_path: Path) -> None:
    out = tmp_path / "en"
    vb = EmptyAfterShortenVoicebox()
    with pytest.raises(VoiceError, match="empty audio"):
        make_voice(client=vb, narration=" ".join(["word"] * 30), lang="en", voice=VoiceProfile(profile_id="p"),
                   clip_duration=3.0, out_dir=out, max_speedup=1.25,
                   shorten=lambda t, n: " ".join(t.split()[:n]))
    assert len(vb.calls) == 2
    assert not (out / "voice.json").exists()


def test_pipeline_empty_voice_fails_clip_and_retries(video: Path) -> None:
    s = make_settings()
    store = run_job(str(video), settings=s, deps=deps(voicebox=EmptyVoicebox()), langs=["en"], log=quiet)
    ls = store.job.clips[0].langs["en"]
    assert ls.stages["voice"].status == "failed" and ls.error.startswith("voice: Voicebox returned empty audio")
    assert store.job.status == "failed"
    vb = FakeVoicebox()
    st = run_job("", settings=s, deps=deps(voicebox=vb), job_id=store.job.id, langs=["en"], log=quiet)
    assert len(vb.calls) == 1, "the voice is retried on the next run"
    assert st.job.status == "done" and st.job.clips[0].langs["en"].error is None


# ------------------------------------------------------------------------------- B2
def _assert_monotonic(ws: list[dict]) -> None:
    for a, b in zip(ws, ws[1:]):
        assert a["start"] <= a["end"] <= b["start"] <= b["end"]


def test_make_captions_follows_recognized_hindi_timings(tmp_path: Path) -> None:
    voice = make_tone(tmp_path / "voice.wav", seconds=4.0)
    narration = "यह एक नया परीक्षण वाक्य है"
    # Whisper heard everything except "नया", and appended a danda to the last word.
    rec = [Word(start=0.10, end=0.40, text="यह"), Word(start=0.50, end=0.80, text="एक"),
           Word(start=1.60, end=2.20, text="परीक्षण"), Word(start=2.30, end=2.90, text="वाक्य"),
           Word(start=3.00, end=3.40, text="है।")]
    make_captions(narration=narration, voice_wav=voice, lang="hi",
                  transcriber=FakeTranscriber(words_for_voice=rec), settings=Settings(), out_dir=tmp_path / "hi")
    ws = json.loads((tmp_path / "hi" / "words.json").read_text(encoding="utf-8"))
    assert [w["text"] for w in ws] == narration.split()
    matched = {0: rec[0], 1: rec[1], 3: rec[2], 4: rec[3], 5: rec[4]}
    for i, r in matched.items():
        assert (ws[i]["start"], ws[i]["end"]) == (r.start, r.end), ws[i]
    # the unmatched word sits in the gap between its matched neighbours
    assert 0.80 <= ws[2]["start"] < ws[2]["end"] <= 1.60
    _assert_monotonic(ws)
    assert "परीक्षण" in (tmp_path / "hi" / "captions.ass").read_text(encoding="utf-8")


class EchoTranscriber(FakeTranscriber):
    """For voice audio, 'recognizes' the narration from the sibling script.json with fixed timings."""

    def transcribe(self, audio, language=None, on_progress=None):
        if Path(audio).name == "audio.wav":
            return super().transcribe(audio, language, on_progress)
        script = json.loads((Path(audio).parent / "script.json").read_text(encoding="utf-8"))
        self.words_for_voice = [Word(start=0.2 + 0.4 * k, end=0.5 + 0.4 * k, text=t)
                                for k, t in enumerate(script["narration"].split())]
        return super().transcribe(audio, language, on_progress)


def test_pipeline_uses_aligned_words_for_en_and_hi(video: Path) -> None:
    s = make_settings()
    s.clips.min_seconds = 10
    llm = FakeLLM(narration_words=8)
    llm.lengthen = None  # step 2: an 8-word take would be lengthened; this test is about alignment only
    store = run_job(str(video), settings=s, deps=deps(llm=llm, transcriber=EchoTranscriber(duration=40)),
                    langs=["en", "hi"], log=quiet)
    assert store.job.status == "done"
    for lang in ("en", "hi"):
        ws = json.loads((store.clip_dir(0, lang) / "words.json").read_text(encoding="utf-8"))
        assert len(ws) == 8
        for k, w in enumerate(ws):
            assert (w["start"], w["end"]) == pytest.approx((0.2 + 0.4 * k, 0.5 + 0.4 * k)), (lang, k, w)
        _assert_monotonic(ws)


# =============================================================================================
# Second review round (docs/TEAM_LOG.md, "Team Lead (triage)" #2) - fixer_a
# =============================================================================================
import os  # noqa: E402
import threading  # noqa: E402

from autoclipper import jobstore as jobstore_mod  # noqa: E402
from autoclipper import pipeline as pipeline_mod  # noqa: E402
from autoclipper.models import Job, StageState  # noqa: E402
from autoclipper.pipeline import PipelineError  # noqa: E402


# ------------------------------------------------------------------------------- PIPELINE-1
def test_concurrent_saves_from_two_jobstores_never_collide() -> None:
    first = JobStore.create("src.mp4")
    job_id = first.job.id
    stores = [JobStore.load(job_id), JobStore.load(job_id)]
    errors: list[BaseException] = []
    bad_reads: list[str] = []
    stop = threading.Event()

    def writer(store: JobStore, n: int) -> None:
        try:
            for i in range(200):
                store.job.stages["ingest"].progress = (i % 100) / 100
                store.job.stages["ingest"].message = f"writer {n} save {i}"
                store.save()
        except BaseException as e:  # noqa: BLE001 - the test reports any error
            errors.append(e)

    def reader() -> None:
        path = first.dir / "job.json"
        while not stop.is_set():
            text = path.read_text(encoding="utf-8")
            try:
                Job.model_validate_json(text)
            except ValueError:
                bad_reads.append(text)

    threads = [threading.Thread(target=writer, args=(s, n)) for n, s in enumerate(stores)]
    r = threading.Thread(target=reader)
    r.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    r.join()
    assert errors == []
    assert bad_reads == []
    Job.model_validate_json((first.dir / "job.json").read_text(encoding="utf-8"))
    assert list(first.dir.glob("*.tmp")) == []  # no tmp files left behind


def test_concurrent_saves_from_two_processes_never_collide() -> None:
    """QA: the in-process RLock serializes threads, so the thread test above also passes with the old
    shared `job.json.tmp`. The real PIPELINE-1 race is two processes (worker + CLI rerender) saving
    one job. With a shared tmp name one process's os.replace steals the other's tmp file
    (FileNotFoundError); unique tmp names make that impossible."""
    import sys

    first = JobStore.create("src.mp4")
    code = (
        "from autoclipper.jobstore import JobStore\n"
        f"s = JobStore.load({first.job.id!r})\n"
        "for i in range(400):\n"
        "    s.job.stages['ingest'].message = str(i)\n"
        "    s.save()\n"
    )
    procs = [subprocess.Popen([sys.executable, "-c", code], stderr=subprocess.PIPE, text=True)
             for _ in range(3)]
    errs = [p.communicate(timeout=120)[1] for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0], "\n".join(e[-500:] for e in errs if e)
    Job.model_validate_json((first.dir / "job.json").read_text(encoding="utf-8"))
    assert list(first.dir.glob("*.tmp")) == []


def test_save_removes_its_tmp_file_when_replace_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    store = JobStore.create("src.mp4")

    def broken(_src, _dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(jobstore_mod.os, "replace", broken)
    with pytest.raises(OSError, match="disk on fire"):
        store.save()
    assert list(store.dir.glob("*.tmp")) == []


# ------------------------------------------------------------------------------- WINDOWS-2
def test_save_retries_replace_on_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    store = JobStore.create("src.mp4")
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "The process cannot access the file (WinError 32)")
        return real_replace(src, dst)

    monkeypatch.setattr(jobstore_mod, "REPLACE_RETRY_DELAY", 0.0)
    monkeypatch.setattr(jobstore_mod.os, "replace", flaky)
    store.job.error = "after retry"
    store.save()
    assert calls["n"] == 2
    assert JobStore.load(store.job.id).job.error == "after retry"
    assert list(store.dir.glob("*.tmp")) == []


def test_save_gives_up_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    store = JobStore.create("src.mp4")
    calls = {"n": 0}

    def locked(_src, _dst):
        calls["n"] += 1
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(jobstore_mod, "REPLACE_RETRY_DELAY", 0.0)
    monkeypatch.setattr(jobstore_mod.os, "replace", locked)
    with pytest.raises(PermissionError):
        store.save()
    assert calls["n"] == jobstore_mod.REPLACE_RETRIES
    assert list(store.dir.glob("*.tmp")) == []


def test_progress_callback_swallows_save_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    store = JobStore.create("src.mp4")
    logs: list[str] = []
    rep = pipeline_mod._Reporter(store, logs.append)
    on_progress = rep.progress("ingest")

    def failing_set_stage(*_a, **_k):
        raise PermissionError(13, "job.json is locked")

    monkeypatch.setattr(store, "set_stage", failing_set_stage)
    on_progress(0.5, "halfway")  # must not raise (it would abort ffmpeg)
    on_progress(1.0, "end")
    assert any("could not save progress" in line for line in logs)


# ------------------------------------------------------------------------------- PIPELINE-2
def test_recut_clip_regenerates_voice_captions_and_render(video: Path) -> None:
    s = make_settings(count=2)
    store = run_job(str(video), settings=s, deps=deps(), langs=["en"], log=quiet)
    job_id = store.job.id
    cut0 = store.clip_dir(0) / "cut.mp4"
    final0 = store.clip_dir(0, "en") / "final.mp4"
    final1 = store.clip_dir(1, "en") / "final.mp4"
    script0 = (store.clip_dir(0, "en") / "script.json").read_bytes()
    before0, before1 = final0.stat().st_mtime_ns, final1.stat().st_mtime_ns
    cut0.unlink()

    vb = FakeVoicebox()
    llm = FakeLLM()
    again = run_job("", settings=s, deps=deps(llm=llm, voicebox=vb), job_id=job_id, langs=["en"], log=quiet)
    assert again.job.status == "done"
    # clip 0 was re-cut, so its voice was regenerated (and only clip 0's)
    assert vb.calls, "clip 0 was re-cut, so its voice must be generated again"
    assert {Path(c["dst"]).parent.parent.name for c in vb.calls} == {"00"}
    assert final0.stat().st_mtime_ns != before0
    assert final1.stat().st_mtime_ns == before1
    assert (store.clip_dir(0, "en") / "script.json").read_bytes() == script0  # script kept
    assert not any(c[0] == "write_script" for c in llm.calls)
    st = again.job.clips[0].langs["en"]
    assert st.error is None and st.final == "clips/00/en/final.mp4"
    assert all(x.status == "done" for x in st.stages.values() if x is not st.stages["script"])


def test_recut_resets_downstream_state_before_voice_runs(video: Path) -> None:
    s = make_settings()
    store = run_job(str(video), settings=s, deps=deps(), langs=["en"], log=quiet)
    (store.clip_dir(0) / "cut.mp4").unlink()
    with pytest.raises(KeyboardInterrupt):
        run_job("", settings=s, deps=deps(voicebox=InterruptingVoicebox()), job_id=store.job.id,
                langs=["en"], log=quiet)
    j = _job_json(store.job.id)
    ls = j["clips"][0]["langs"]["en"]
    assert ls["final"] is None
    assert ls["stages"]["captions"]["status"] == "pending"
    assert ls["stages"]["render"]["status"] == "pending"
    d = store.clip_dir(0, "en")
    for name in ("voice.json", "words.json", "captions.ass", "final.mp4"):
        assert not (d / name).exists(), name
    assert (d / "script.json").exists()


# ------------------------------------------------------------------------------- PIPELINE-3
def test_failed_voice_regeneration_resets_captions_and_render(video: Path) -> None:
    s = make_settings()
    store = run_job(str(video), settings=s, deps=deps(), langs=["en"], log=quiet)
    d = store.clip_dir(0, "en")
    script = json.loads((d / "script.json").read_text(encoding="utf-8"))
    script["narration"] = "a new narration that needs a new voice"
    (d / "script.json").write_text(json.dumps(script), encoding="utf-8")

    seen: list[dict] = []

    class Spy(FakeVoicebox):
        def synthesize(self, *a, **kw):
            seen.append(_job_json(store.job.id)["clips"][0]["langs"]["en"])  # state while voice runs
            raise RuntimeError("voicebox down")

    run_job("", settings=s, deps=deps(voicebox=Spy()), job_id=store.job.id, langs=["en"], log=quiet)
    for ls in (seen[0], _job_json(store.job.id)["clips"][0]["langs"]["en"]):
        assert ls["final"] is None
        assert ls["stages"]["captions"]["status"] not in ("done", "skipped")
        assert ls["stages"]["render"]["status"] not in ("done", "skipped")
        assert ls["stages"]["captions"]["progress"] == 0.0
    assert not (d / "final.mp4").exists()


def test_captions_regeneration_resets_render(video: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = make_settings()
    store = run_job(str(video), settings=s, deps=deps(), langs=["en"], log=quiet)
    (store.clip_dir(0, "en") / "captions.ass").unlink()

    def boom_captions(**_kw):
        raise RuntimeError("captions exploded")

    monkeypatch.setattr(captions_stage, "make_captions", boom_captions)
    run_job("", settings=s, deps=deps(), job_id=store.job.id, langs=["en"], log=quiet)
    ls = _job_json(store.job.id)["clips"][0]["langs"]["en"]
    assert ls["error"].startswith("captions:")
    assert ls["final"] is None
    assert ls["stages"]["render"]["status"] == "pending" and ls["stages"]["render"]["progress"] == 0.0
    assert not (store.clip_dir(0, "en") / "final.mp4").exists()


# ------------------------------------------------------------------------------- PIPELINE-4
class InterruptOnSecondCall(FakeVoicebox):
    def synthesize(self, text, **kw):
        if len(self.calls) >= 1:
            raise KeyboardInterrupt
        return super().synthesize(text, **kw)


def test_rerender_keeps_failed_status_while_clips_are_unfinished(video: Path) -> None:
    s = make_settings(count=2)
    with pytest.raises(KeyboardInterrupt):
        run_job(str(video), settings=s, deps=deps(voicebox=InterruptOnSecondCall()), langs=["en"], log=quiet)
    [job_id] = JobStore.list_ids()
    j = _job_json(job_id)
    assert j["status"] == "failed"
    assert j["clips"][0]["langs"]["en"]["error"] is None  # clip 0 finished, clip 1 interrupted

    rerender(job_id, 0, "en", settings=s, deps=deps(), log=quiet)
    j = _job_json(job_id)
    assert j["status"] == "failed" and j["error"] == "interrupted"


def test_rerender_marks_job_done_when_everything_is_finished(video: Path) -> None:
    s = make_settings()
    store = run_job(str(video), settings=s, deps=deps(), langs=["en"], log=quiet)
    store.job.status = "failed"
    store.job.error = "earlier failure"
    store.save()
    rerender(store.job.id, 0, "en", settings=s, deps=deps(), log=quiet)
    j = _job_json(store.job.id)
    assert j["status"] == "done" and j["error"] is None


# ------------------------------------------------------------------------------- PIPELINE-5
def test_rerender_unknown_language_leaves_no_phantom_state(video: Path) -> None:
    s = make_settings()
    store = run_job(str(video), settings=s, deps=deps(), langs=["en"], log=quiet)
    with pytest.raises(PipelineError, match="script.json not found"):
        rerender(store.job.id, 0, "hi", settings=s, deps=deps(), log=quiet)
    j = _job_json(store.job.id)
    assert set(j["clips"][0]["langs"]) == {"en"}
    assert not (store.dir / "clips" / "00" / "hi").exists()


# ------------------------------------------------------------------------------- FAILURES-3
def test_truncated_moments_json_is_moved_aside_and_regenerated(video: Path) -> None:
    s = make_settings()
    store = run_job(str(video), settings=s, deps=deps(), langs=["en"], log=quiet)
    moments = store.dir / "moments.json"
    good = moments.read_text(encoding="utf-8")
    moments.write_text(good[: len(good) // 2], encoding="utf-8")  # torn write
    logs: list[str] = []
    llm = FakeLLM()
    again = run_job("", settings=s, deps=deps(llm=llm), job_id=store.job.id, langs=["en"], log=logs.append)
    assert again.job.status == "done"
    assert (store.dir / "moments.json.corrupt").read_text(encoding="utf-8") == good[: len(good) // 2]
    assert json.loads(moments.read_text(encoding="utf-8"))  # valid again
    assert any(c[0] == "pick_moments" for c in llm.calls)
    assert any("moments.json is corrupt" in line for line in logs)


def test_invalid_moment_entries_are_treated_as_corrupt(video: Path) -> None:
    s = make_settings()
    store = run_job(str(video), settings=s, deps=deps(), langs=["en"], log=quiet)
    (store.dir / "moments.json").write_text('[{"start": "soon"}]', encoding="utf-8")
    again = run_job("", settings=s, deps=deps(), job_id=store.job.id, langs=["en"], log=quiet)
    assert again.job.status == "done"
    assert (store.dir / "moments.json.corrupt").exists()


def test_truncated_transcript_json_is_moved_aside_and_regenerated(video: Path) -> None:
    s = make_settings()
    store = run_job(str(video), settings=s, deps=deps(), langs=["en"], log=quiet)
    tr = store.dir / "transcript.json"
    tr.write_text(tr.read_text(encoding="utf-8")[:100], encoding="utf-8")
    transcriber = FakeTranscriber(duration=40)
    again = run_job("", settings=s, deps=deps(transcriber=transcriber), job_id=store.job.id, langs=["en"],
                    log=quiet)
    assert again.job.status == "done"
    assert (store.dir / "transcript.json.corrupt").exists()
    assert any(Path(a).name == "audio.wav" for a, _ in transcriber.calls)


# ------------------------------------------------------------------------------- WINDOWS-1
def test_check_fonts_finds_windows_system_fonts(tmp_path: Path) -> None:
    assets = tmp_path / "assets_fonts"
    assets.mkdir()
    system = tmp_path / "system"
    system.mkdir()
    (system / "NotoSans-Regular.ttf").write_bytes(b"x")
    (system / "NotoSansDevanagari-Regular.ttf").write_bytes(b"x")
    c = cli.check_fonts(which=lambda _n: None, fonts_dir=assets, system_font_dirs=[system])
    assert c.level == "ok", c.detail
    assert "Noto Sans (system)" in c.detail and "Noto Sans Devanagari (system)" in c.detail
    # only the Devanagari face: Noto Sans itself is still missing
    (system / "NotoSans-Regular.ttf").unlink()
    miss = cli.check_fonts(which=lambda _n: None, fonts_dir=assets, system_font_dirs=[system])
    assert miss.level == "fail" and "Noto Sans" in miss.detail


def test_windows_font_dirs_from_environment() -> None:
    dirs = cli.windows_font_dirs({"WINDIR": r"C:\Windows", "LOCALAPPDATA": r"C:\Users\u\AppData\Local"})
    assert [d.as_posix() for d in dirs] == [Path(r"C:\Windows").joinpath("Fonts").as_posix(),
                                           Path(r"C:\Users\u\AppData\Local", "Microsoft", "Windows",
                                                "Fonts").as_posix()]


# ------------------------------------------------------------------------------- FAILURES-8
def test_llm_max_tokens_setting(capsys: pytest.CaptureFixture[str]) -> None:
    from autoclipper.config import load_settings

    assert Settings().llm.max_tokens == 16000
    assert cli.main(["settings", "set", "llm.max_tokens=32000"]) == 0
    assert load_settings().llm.max_tokens == 32000
    for bad in ("llm.max_tokens=100", "llm.max_tokens=100000", "llm.max_tokens=lots"):
        assert cli.main(["settings", "set", bad]) == 1
        assert "llm.max_tokens" in capsys.readouterr().err
    assert load_settings().llm.max_tokens == 32000


# ------------------------------------------------------------------------------- TESTS-2
def test_suite_guard_blocks_real_clients_and_network() -> None:
    import socket

    assert not any(k.startswith("ANTHROPIC_") for k in os.environ)
    with pytest.raises(RuntimeError, match="real client in tests"):
        pipeline_mod._make_llm(Settings())
    with pytest.raises(RuntimeError, match="network access in tests is blocked"):
        socket.create_connection(("192.0.2.1", 443), timeout=0.5)


@pytest.mark.real_clients
def test_real_clients_marker_lifts_the_factory_guard() -> None:
    from autoclipper.voicebox import VoiceboxClient

    client = pipeline_mod._make_voicebox(Settings())  # builds the client object only; no request is made
    assert isinstance(client, VoiceboxClient)
