"""Voice-over on/off ("captions only"): settings, per-job override, pipeline, staleness, web, CLI, doctor.

No real Anthropic/Voicebox/whisper: FakeLLM / FakeVoicebox / FakeTranscriber, a synthetic ffmpeg video.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from fakes import FakeLLM, FakeTranscriber, FakeVoicebox, fake_transcript, make_video
from test_web_support import BASE, Stub, assert_error_shape, make_client, make_job_on_disk, wait_for

from autoclipper import cli, ffmpeg
from autoclipper.config import Settings, load_settings, save_settings
from autoclipper.jobstore import JobStore, jobs_root
from autoclipper.models import Moment, Segment, Transcript, Word
from autoclipper.pipeline import Deps, effective_settings, job_voiceover, rerender, run_job
from autoclipper.publish import service as publish_service
from autoclipper.stages import original_captions as oc
from autoclipper.stages.captions import CaptionsError, captions_voiceover
from autoclipper.stages.render import read_render_stamp, render_is_current
from autoclipper.web.auth import TOKEN_HEADER
from autoclipper.web.worker import Worker, read_options


# --------------------------------------------------------------------------------------------- helpers
def settings(voiceover: bool = True, loudnorm: bool = True) -> Settings:
    s = Settings()
    s.voiceover = voiceover
    s.voicebox.english.profile_id = "fake-en"
    s.voicebox.hindi.profile_id = "fake-hi"
    s.clips.count = 1
    s.clips.min_seconds = 10
    s.clips.max_seconds = 12
    s.audio.loudnorm = loudnorm
    return s


class Ctx:
    def __init__(self, tmp_path: Path, transcript_language: str = "en"):
        self.video = make_video(tmp_path / "src.mp4", seconds=30)
        self.llm = FakeLLM()
        self.voicebox = FakeVoicebox()
        self.transcriber = FakeTranscriber(duration=30)
        if transcript_language != "en":
            real = self.transcriber.transcribe

            def transcribe(audio, language=None, on_progress=None):
                t = real(audio, language, on_progress)
                if Path(audio).name == "audio.wav":
                    t.language = transcript_language
                return t

            self.transcriber.transcribe = transcribe  # type: ignore[method-assign]

    def deps(self) -> Deps:
        return Deps(llm=self.llm, voicebox=self.voicebox, transcriber=self.transcriber)

    def calls(self, name: str) -> list[dict]:
        return [kw for n, kw in self.llm.calls if n == name]


def words(out: Path) -> list[dict]:
    return json.loads((out / "words.json").read_text(encoding="utf-8"))


def mean_volume(path: Path) -> float:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:a:0",
                           "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", proc.stderr)
    assert m, proc.stderr[-500:]
    return float(m.group(1))


def record_renders(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = ffmpeg.run

    def run(cmd, *a, **kw):
        if str(kw.get("desc", "")).startswith("render"):
            seen.append([str(x) for x in cmd])
        return real(cmd, *a, **kw)

    monkeypatch.setattr(ffmpeg, "run", run)
    return seen


# --------------------------------------------------------------------------------------------- settings
def test_setting_default_on_and_old_settings_file_still_loads():
    assert Settings().voiceover is True
    data = Settings().model_dump()
    data.pop("voiceover")
    assert Settings.model_validate(data).voiceover is True  # settings.json from before this feature


def test_cli_settings_set_voiceover():
    s = cli.apply_setting(Settings(), "voiceover=false")
    assert s.voiceover is False
    assert cli.apply_setting(s, "voiceover=true").voiceover is True
    with pytest.raises(cli.CLIError):
        cli.apply_setting(Settings(), "voiceover=maybe")


def test_cli_run_flags():
    p = cli.build_parser()
    assert p.parse_args(["run", "x.mp4"]).voiceover is None
    assert p.parse_args(["run", "x.mp4", "--no-voiceover"]).voiceover is False
    assert p.parse_args(["run", "--job", "j", "--voiceover"]).voiceover is True
    with pytest.raises(SystemExit):
        p.parse_args(["run", "x.mp4", "--voiceover", "--no-voiceover"])


def test_effective_settings_override():
    assert effective_settings(settings(True), voiceover=False).voiceover is False
    assert effective_settings(settings(False)).voiceover is False
    assert effective_settings(settings(False), voiceover=True).voiceover is True


def test_doctor_skips_voicebox_when_off():
    called = []
    checks = cli.check_voicebox(settings(False), client_factory=lambda s: called.append(s))
    assert called == [] and len(checks) == 1
    assert checks[0].level == "ok" and "voice-over is off" in checks[0].detail
    s = settings(True)
    s.voicebox.hindi.profile_id = ""
    on = cli.check_voicebox(s, client_factory=lambda _s: FakeVoicebox())
    assert any(c.level == "fail" for c in on)  # unchanged when on


# --------------------------------------------------------------------------------------------- captions units
def _transcript() -> Transcript:
    w = lambda s, e, t: Word(start=s, end=e, text=t)  # noqa: E731
    return Transcript(language="en", duration=30, segments=[
        Segment(start=8.0, end=11.0, text="before edge", words=[w(8.0, 9.0, " before"), w(9.5, 10.6, " edge,")]),
        Segment(start=11.0, end=14.0, text="inside line", words=[w(11.0, 12.0, " inside"), w(12.2, 13.9, " line")]),
        Segment(start=14.0, end=16.0, text="no word timings here"),  # no words: kept (all inside)
        Segment(start=19.5, end=23.0, text="mostly outside"),  # no words, <50% inside: dropped
    ])


def test_clip_segments_rebase_and_clamp():
    segs = oc.clip_segments(_transcript(), 10.0, 20.0)
    assert [s.id for s in segs] == [0, 1, 2]
    edge, inside, nowords = segs
    assert edge.text == "edge," and edge.start == 0.0 and edge.end == pytest.approx(0.6)
    assert [(x.start, x.end, x.text) for x in inside.words] == [(1.0, 2.0, "inside"), (2.2, 3.9, "line")]
    assert nowords.start == 4.0 and nowords.end == 6.0 and len(nowords.words) == 4
    ws = oc.source_words(segs)
    assert [x.text for x in ws] == ["edge,", "inside", "line", "no", "word", "timings", "here"]
    assert all(a.end <= b.start + 1e-9 and a.start <= a.end for a, b in zip(ws, ws[1:]))
    assert ws[-1].end <= 10.0


def test_translated_words_span_original_lines():
    segs = oc.clip_segments(_transcript(), 10.0, 20.0)
    out = oc.translated_words(segs, {1: "अंदर की पंक्ति", 2: "यहाँ समय नहीं"})  # id 0 missing: left out
    assert [x.text for x in out] == ["अंदर", "की", "पंक्ति", "यहाँ", "समय", "नहीं"]
    assert out[0].start == pytest.approx(1.0) and out[2].end == pytest.approx(3.9)
    assert out[3].start == pytest.approx(4.0) and out[-1].end == pytest.approx(6.0)
    # by character length: the 6-letter word gets more time than the 2-letter one
    assert (out[2].end - out[2].start) > (out[1].end - out[1].start)


def test_same_language():
    assert oc.same_language("en", "en") and oc.same_language("EN-us", "en") and oc.same_language("hi", "hi")
    assert not oc.same_language("en", "hi") and not oc.same_language(None, "en")


def test_translation_cache_and_missing_translator(tmp_path: Path):
    segs = oc.clip_segments(_transcript(), 10.0, 20.0)
    calls = []

    def tr(**kw):
        calls.append(kw)
        return {s["id"]: f"t{s['id']}" for s in kw["segments"]}

    a = oc.translate_lines(segs, lang="hi", source_lang="en", translate=tr, out_dir=tmp_path)
    b = oc.translate_lines(segs, lang="hi", source_lang="en", translate=tr, out_dir=tmp_path)
    assert a == b == {0: "t0", 1: "t1", 2: "t2"} and len(calls) == 1
    assert calls[0]["segments"] == [{"id": 0, "text": "edge,"}, {"id": 1, "text": "inside line"},
                                    {"id": 2, "text": "no word timings here"}]
    other = oc.clip_segments(_transcript(), 11.0, 20.0)  # different source lines -> cache miss
    oc.translate_lines(other, lang="hi", source_lang="en", translate=tr, out_dir=tmp_path)
    assert len(calls) == 2
    with pytest.raises(CaptionsError):
        oc.translate_lines(segs, lang="en", source_lang="hi", translate=None, out_dir=tmp_path / "x")
    with pytest.raises(CaptionsError):
        oc.translate_lines(segs, lang="en", source_lang="hi", translate=lambda **kw: {}, out_dir=tmp_path / "y")


# --------------------------------------------------------------------------------------------- pipeline
def test_pipeline_voiceover_off_no_voicebox_captions_from_transcript(tmp_path: Path, monkeypatch):
    c = Ctx(tmp_path)
    renders = record_renders(monkeypatch)
    store = run_job(str(c.video), settings=settings(False, loudnorm=False), deps=c.deps(), langs=["en", "hi"],
                    log=lambda _l: None)
    assert store.job.status == "done"
    assert c.voicebox.calls == []  # no Voicebox at all
    assert c.calls("write_script") == []
    assert sorted(x["lang"] for x in c.calls("write_publish_metadata")) == ["en", "hi"]
    tr = c.calls("translate_segments")
    assert len(tr) == 1 and tr[0]["lang"] == "hi" and tr[0]["source_lang"] == "en"  # one call per clip+lang
    clip = store.job.clips[0]
    for lang in ("en", "hi"):
        st = clip.langs[lang]
        assert st.error is None and st.final
        assert st.stages["voice"].status == "skipped" and st.stages["voice"].message == "voice-over off"
        out = store.clip_dir(0, lang)
        script = json.loads((out / "script.json").read_text(encoding="utf-8"))
        assert script["narration"] == "" and script["title"] == f"Captions title {lang}"
        meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
        assert meta["title"] == f"Captions title {lang}" and meta["hashtags"]
        assert not (out / "voice.wav").exists()
        assert captions_voiceover(out) is False
        assert read_render_stamp(out)["voiceover"] is False
    m = clip.moment
    en_words = words(store.clip_dir(0, "en"))
    src = [w for s in fake_transcript(30).segments for w in s.words if m.start - 0.01 <= w.start and w.end <= m.end + 0.01]
    assert [w["text"] for w in en_words][: len(src)] == [w.text for w in src]
    assert en_words[0]["start"] == pytest.approx(src[0].start - m.start, abs=1e-3)  # re-based to clip time
    hi_words = words(store.clip_dir(0, "hi"))
    assert hi_words and hi_words[0]["text"] == "अनुवाद"
    assert json.loads((store.clip_dir(0, "hi") / "captions.json").read_text())["source"] == "translation"
    assert "Noto Sans Devanagari" in (store.clip_dir(0, "hi") / "captions.ass").read_text(encoding="utf-8")
    # render graph: the cut is the only input, no voice, no amix/ducking
    assert len(renders) == 2
    for cmd in renders:
        assert cmd.count("-i") == 1 and not any(x.endswith("voice.wav") for x in cmd)
        graph = cmd[cmd.index("-filter_complex") + 1]
        assert "amix" not in graph and "eval=frame" not in graph and "volume=1" in graph
    # the audio is the original at its normal level (loudnorm off for the comparison)
    cut = store.clip_dir(0) / "cut.mp4"
    for lang in ("en", "hi"):
        assert abs(mean_volume(store.clip_dir(0, lang) / "final.mp4") - mean_volume(cut)) < 1.0


def test_source_language_hindi_needs_no_translation_for_hi(tmp_path: Path):
    c = Ctx(tmp_path, transcript_language="hi")
    store = run_job(str(c.video), settings=settings(False), deps=c.deps(), langs=["hi"], log=lambda _l: None)
    assert store.job.status == "done"
    assert c.calls("translate_segments") == []
    assert json.loads((store.clip_dir(0, "hi") / "captions.json").read_text())["source"] == "transcript"


def test_translation_failure_fails_only_that_clip_lang(tmp_path: Path):
    c = Ctx(tmp_path)

    def broken(**_kw):
        raise RuntimeError("translator down")

    c.llm.translate_segments = broken  # type: ignore[method-assign]
    store = run_job(str(c.video), settings=settings(False), deps=c.deps(), langs=["en", "hi"], log=lambda _l: None)
    assert store.job.status == "done"
    langs = store.job.clips[0].langs
    assert langs["en"].error is None and langs["en"].final
    assert langs["hi"].error and "translator down" in langs["hi"].error and langs["hi"].stages["captions"].status == "failed"


def test_hyperframes_engine_uses_transcript_words(tmp_path: Path, monkeypatch):
    from fakes import make_alpha_overlay

    from autoclipper.stages import hyperframes as hf

    seen: list[list[dict]] = []

    def fake_overlay(*, words_json, cut, lang, settings, out_dir, on_progress=None, runner=None, env=None):
        seen.append(json.loads(Path(words_json).read_text(encoding="utf-8")))
        info = ffmpeg.probe(Path(cut))
        dst = Path(out_dir) / "captions_overlay.webm"
        make_alpha_overlay(dst, info.duration, info.width, info.height, "webm")
        return dst

    env = hf.HFEnv(node=Path("/n/node"), cli=Path("/n/hf.mjs"), node_version="v22.1.0", hf_version="0.9.52",
                   gsap=Path("/n/gsap.min.js"))
    monkeypatch.setattr(hf, "probe_env", lambda _s: env)
    monkeypatch.setattr(hf, "make_overlay", fake_overlay)
    renders = record_renders(monkeypatch)
    c = Ctx(tmp_path)
    s = settings(False)
    s.captions.engine = "hyperframes"
    store = run_job(str(c.video), settings=s, deps=c.deps(), langs=["en", "hi"], log=lambda _l: None)
    for lang in ("en", "hi"):
        st = store.job.clips[0].langs[lang]
        out = store.clip_dir(0, lang)
        assert st.error is None and st.warning is None and st.final
        stamp = read_render_stamp(out)
        assert stamp["engine_used"] == "hyperframes" and stamp["voiceover"] is False
    assert [w["text"] for w in seen[1]][:1] == ["अनुवाद"] and seen[0] == words(store.clip_dir(0, "en"))
    for cmd in renders:  # cut + overlay, still no voice input
        assert cmd.count("-i") == 2 and not any(x.endswith("voice.wav") for x in cmd)
        assert "amix" not in cmd[cmd.index("-filter_complex") + 1]
    assert c.voicebox.calls == []


# --------------------------------------------------------------------------------------------- override + staleness
def test_per_job_override_is_stored_and_used_by_rerender(tmp_path: Path):
    c = Ctx(tmp_path)
    s = settings(True)
    save_settings(s)
    store = run_job(str(c.video), settings=s, deps=c.deps(), langs=["en"], voiceover=False, log=lambda _l: None)
    assert store.job.voiceover is False and c.voicebox.calls == []
    job = JobStore.load(store.job.id).job
    assert job.voiceover is False and job_voiceover(job, s) is False
    rerender(store.job.id, 0, "en", settings=s, deps=c.deps(), log=lambda _l: None)
    assert c.voicebox.calls == []  # the job's own mode wins over the (on) setting
    assert c.calls("translate_segments") == []
    # resume without an override keeps the job's mode
    run_job("", settings=s, deps=c.deps(), langs=["en"], job_id=store.job.id, log=lambda _l: None)
    assert c.voicebox.calls == []
    # a job without a stored mode predates the option: voice-over on, whatever the setting
    j2 = JobStore.create("x")
    assert job_voiceover(j2.job, settings(False)) is True and job_voiceover(j2.job, s) is True


def test_toggle_on_off_on_staleness(tmp_path: Path):
    c = Ctx(tmp_path)
    on, off = settings(True), settings(False)
    store = run_job(str(c.video), settings=on, deps=c.deps(), langs=["en"], log=lambda _l: None)
    out = store.clip_dir(0, "en")
    n_voice = len(c.voicebox.calls)
    assert n_voice >= 1 and render_is_current(out, on) and not render_is_current(out, off)
    narration = json.loads((out / "script.json").read_text(encoding="utf-8"))["narration"]
    first_final = (out / "final.mp4").stat().st_mtime_ns

    # a Settings change alone never switches an existing job
    logs0: list[str] = []
    run_job("", settings=off, deps=c.deps(), langs=["en"], job_id=store.job.id, log=logs0.append)
    assert JobStore.load(store.job.id).job.voiceover is True
    assert (out / "final.mp4").stat().st_mtime_ns == first_final and render_is_current(out, on)

    # on -> off (run --job ID --no-voiceover): voice skipped (files kept), captions + render redone
    store = run_job("", settings=on, deps=c.deps(), langs=["en"], job_id=store.job.id, voiceover=False,
                    log=lambda _l: None)
    st = store.job.clips[0].langs["en"]
    assert st.error is None and st.stages["voice"].message == "voice-over off"
    assert len(c.voicebox.calls) == n_voice and (out / "voice.wav").exists()
    assert captions_voiceover(out) is False and render_is_current(out, off)
    assert (out / "final.mp4").stat().st_mtime_ns != first_final
    assert json.loads((out / "script.json").read_text(encoding="utf-8"))["narration"] == narration  # kept

    # resume with nothing changed (and the setting back on): everything current, still off
    logs: list[str] = []
    run_job("", settings=on, deps=c.deps(), langs=["en"], job_id=store.job.id, log=logs.append)
    assert any("captions 00/en 100% skipped" in ln for ln in logs) and any("render 00/en 100% skipped" in ln for ln in logs)

    # off -> on: the kept voice still matches -> no new Voicebox call, captions from the voice again
    store = run_job("", settings=off, deps=c.deps(), langs=["en"], job_id=store.job.id, voiceover=True,
                    log=lambda _l: None)
    assert len(c.voicebox.calls) == n_voice
    assert captions_voiceover(out) is True and not (out / "captions.json").exists()
    assert "voiceover" not in read_render_stamp(out) and render_is_current(out, on)


def test_off_first_then_on_writes_narration_and_keeps_title(tmp_path: Path):
    c = Ctx(tmp_path)
    store = run_job(str(c.video), settings=settings(False), deps=c.deps(), langs=["en"], log=lambda _l: None)
    out = store.clip_dir(0, "en")
    sp = out / "script.json"
    s = json.loads(sp.read_text(encoding="utf-8"))
    s["title"] = "My edited title"
    sp.write_text(json.dumps(s), encoding="utf-8")
    js = JobStore.load(store.job.id)
    assert js.job.voiceover is False  # fixed at creation from the setting
    js.job.voiceover = True  # what `run --job ID --voiceover` stores
    js.save()
    final = rerender(store.job.id, 0, "en", settings=settings(False), deps=c.deps(), log=lambda _l: None)
    assert final.is_file()
    s2 = json.loads(sp.read_text(encoding="utf-8"))
    assert s2["title"] == "My edited title" and s2["narration"].strip()
    assert len(c.voicebox.calls) == 1 and (out / "voice.wav").exists()
    assert render_is_current(out, settings(True))


# --------------------------------------------------------------------------------------------- publish
def test_publish_voice_stale_ignored_for_captions_only_render():
    store = make_job_on_disk(clips=1, langs=("en",))
    out = store.clip_dir(0, "en")
    sp = out / "script.json"
    s = json.loads(sp.read_text(encoding="utf-8"))
    s["narration"] = "changed narration"
    sp.write_text(json.dumps(s), encoding="utf-8")
    assert publish_service.voice_stale(store.job.id, 0, "en") is True
    (out / "render.json").write_text(json.dumps({"caption_engine": "ass", "engine_used": "ass", "warning": None,
                                                 "voiceover": False}), encoding="utf-8")
    assert publish_service.voice_stale(store.job.id, 0, "en") is False


# --------------------------------------------------------------------------------------------- web
class VoStub(Stub):
    def run_job(self, source, *, settings, deps, langs, count, min_seconds, max_seconds, job_id, log,
                voiceover=None):
        with self.lock:
            self.calls.append(("run_job", job_id, dict(voiceover=voiceover)))
        self._body(log)


@pytest.fixture
def vo_api():
    stub = VoStub()
    client, worker, _ = make_client(worker=Worker(run_job=stub.run_job, rerender=stub.rerender), stub=stub)
    with client:
        yield client, worker, stub


def test_web_settings_voiceover_put_and_422(vo_api):
    client, _, _ = vo_api
    cur = client.get("/api/settings").json()
    assert cur["voiceover"] is True
    schema = client.get("/api/settings/schema").json()
    assert schema["properties"]["voiceover"]["type"] == "boolean"
    for bad in ("no", 0, None, [False]):
        r = client.put("/api/settings", json={**cur, "voiceover": bad})
        assert "voiceover" in assert_error_shape(r, 422, "validation failed")["errors"]
    assert load_settings().voiceover is True
    r = client.put("/api/settings", json={**cur, "voiceover": False})
    assert r.status_code == 200 and r.json()["voiceover"] is False
    assert load_settings().voiceover is False


def test_web_settings_auth(vo_api):
    client, _, _ = vo_api
    from fastapi.testclient import TestClient

    bare = TestClient(client.app, base_url=BASE)
    body = {**client.get("/api/settings").json(), "voiceover": False}
    assert_error_shape(bare.put("/api/settings", json=body), 401)
    assert_error_shape(bare.put("/api/settings", json=body, headers={TOKEN_HEADER: "x" * 43}), 403)
    assert_error_shape(bare.post("/api/jobs", json={"url": "https://e.com/v", "options": {"voiceover": False}}), 401)
    assert load_settings().voiceover is True


def test_web_new_job_voiceover_option(vo_api):
    client, worker, stub = vo_api
    r = client.post("/api/jobs", json={"url": "https://example.com/v.mp4", "options": {"voiceover": False}})
    assert r.status_code == 201
    job_id = r.json()["id"]
    assert read_options(job_id).voiceover is False
    assert json.loads((jobs_root() / job_id / "options.json").read_text())["voiceover"] is False
    assert JobStore.load(job_id).job.voiceover is False
    assert wait_for(lambda: any(c[1] == job_id for c in stub.calls))
    # the mode lives in job.json; the worker does not pass it (run_job keeps the stored one)
    assert [c[2] for c in stub.calls if c[1] == job_id] == [{"voiceover": None}]
    d = client.get(f"/api/jobs/{job_id}").json()
    assert d["voiceover"] is False and d["voiceover_override"] is False and d["options"]["voiceover"] is False
    # without the option: the current setting is fixed in the job
    r2 = client.post("/api/jobs", json={"url": "https://example.com/w.mp4", "options": {"langs": ["en"]}})
    j2 = r2.json()["id"]
    assert json.loads((jobs_root() / j2 / "options.json").read_text())["voiceover"] is True
    assert JobStore.load(j2).job.voiceover is True
    assert wait_for(lambda: any(c[1] == j2 for c in stub.calls))
    # turning the setting off afterwards does not switch the existing job
    body = {**client.get("/api/settings").json(), "voiceover": False}
    body.pop("publish", None)
    assert client.put("/api/settings", json=body).status_code == 200
    d2 = client.get(f"/api/jobs/{j2}").json()
    assert d2["voiceover"] is True and all(cl.get("voiceover", True) for c in d2["clips"]
                                           for cl in (c.get("langs") or {}).values())
    # ... and a job created now (no option) gets the new setting
    r3 = client.post("/api/jobs", json={"url": "https://example.com/z.mp4"})
    assert JobStore.load(r3.json()["id"]).job.voiceover is False


@pytest.mark.parametrize("bad", ["false", 1, "off", {"x": 1}])
def test_web_new_job_voiceover_422(vo_api, bad):
    client, _, _ = vo_api
    r = client.post("/api/jobs", json={"url": "https://example.com/v.mp4", "options": {"voiceover": bad}})
    assert "options.voiceover" in assert_error_shape(r, 422, "validation failed")["errors"]
    assert JobStore.list_ids() == []


def _off_job_on_disk():
    store = make_job_on_disk(clips=1, langs=("en", "hi"))
    store.job.voiceover = False
    for lang in ("en", "hi"):
        out = store.clip_dir(0, lang)
        (out / "render.json").write_text(json.dumps({"caption_engine": "ass", "engine_used": "ass",
                                                     "warning": None, "voiceover": False}), encoding="utf-8")
        store.job.clips[0].langs[lang].stages["voice"].status = "skipped"
        store.job.clips[0].langs[lang].stages["voice"].message = "voice-over off"
    store.save()
    return store


def test_web_detail_shows_voiceover_off(vo_api):
    client, _, _ = vo_api
    store = _off_job_on_disk()
    d = client.get(f"/api/jobs/{store.job.id}").json()
    assert d["voiceover"] is False and d["voiceover_override"] is False
    en = d["clips"][0]["langs"]["en"]
    assert en["voiceover"] is False and en["rendered_voiceover"] is False and en["render_stale"] is False
    assert en["stages"]["voice"] == {"status": "skipped", "progress": 1.0, "message": "voice-over off"}
    # the job follows the setting once its override is cleared: settings on -> stale render
    store.job.voiceover = None
    store.save()
    en = client.get(f"/api/jobs/{store.job.id}").json()["clips"][0]["langs"]["en"]
    assert en["voiceover"] is True and en["rendered_voiceover"] is False and en["render_stale"] is True


def test_web_put_script_empty_narration_only_when_off(vo_api):
    client, _, _ = vo_api
    off = _off_job_on_disk()
    body = {"narration": "", "title": "Captions only", "description": "d", "hashtags": ["#a"]}
    r = client.put(f"/api/jobs/{off.job.id}/clips/0/hi/script", json=body)
    assert r.status_code == 200, r.text
    assert r.json() == {"script": {"narration": "", "title": "Captions only", "description": "d", "hashtags": ["#a"],
                                   "hook": "", "keywords": []},  # G4/G5 (additive)
                        "voice_stale": False, "style_stale": False}
    meta = json.loads((off.clip_dir(0, "hi") / "metadata.json").read_text(encoding="utf-8"))
    assert meta["title"] == "Captions only"  # publish metadata follows the edit
    on = make_job_on_disk(clips=1, langs=("en",))
    before = (on.clip_dir(0, "en") / "script.json").read_bytes()
    r = client.put(f"/api/jobs/{on.job.id}/clips/0/en/script", json=body)
    assert "narration" in assert_error_shape(r, 422, "validation failed")["errors"]
    assert (on.clip_dir(0, "en") / "script.json").read_bytes() == before
    assert_error_shape(client.put(f"/api/jobs/{off.job.id}/clips/7/en/script", json=body), 404)
    assert_error_shape(client.put("/api/jobs/nope-123/clips/0/en/script", json=body), 404)
    assert_error_shape(client.get("/api/jobs/nope-123"), 404)


def test_web_rerender_works_for_captions_only_job(vo_api):
    client, _, stub = vo_api
    off = _off_job_on_disk()
    r = client.post(f"/api/jobs/{off.job.id}/clips/0/hi/rerender")
    assert r.status_code == 202
    assert wait_for(lambda: ("rerender", off.job.id, 0, "hi") in stub.calls)
