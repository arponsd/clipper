"""End-to-end pipeline + CLI tests with fakes (no Anthropic API, no Voicebox, no whisper model).

A 40 s synthetic video goes through the real ffmpeg stages; LLM, Voicebox and the transcriber are fakes.
The expensive job (2 clips x en/hi) runs once per module and is shared by the tests that read it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fakes import FakeLLM, FakeTranscriber, FakeVoicebox, make_video

from autoclipper import cli, pipeline
from autoclipper.config import Settings, load_settings
from autoclipper.jobstore import CLIP_STAGES, JOB_STAGES, JobStore
from autoclipper.pipeline import Deps, PipelineError, effective_settings, rerender, run_job
from autoclipper.stages.voice import load_voice_result, narration_hash

LANGS = ["en", "hi"]


# ---------------------------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------------------------
def make_settings() -> Settings:
    s = Settings()
    s.voicebox.english.profile_id = "fake-en"
    s.voicebox.english.engine = "qwen"  # english engine is configurable; hindi is forced to chatterbox
    s.voicebox.hindi.profile_id = "fake-hi"
    s.clips.count = 2
    s.clips.min_seconds = 10
    s.clips.max_seconds = 15
    return s


def make_deps(voicebox: FakeVoicebox | None = None) -> Deps:
    return Deps(llm=FakeLLM(), voicebox=voicebox or FakeVoicebox(), transcriber=FakeTranscriber(duration=40))


def ffprobe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def forbid_real_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_settings):  # pragma: no cover - only runs if laziness is broken
        raise AssertionError("a real client was constructed")

    for name in ("_make_llm", "_make_voicebox", "_make_transcriber"):
        monkeypatch.setattr(pipeline, name, boom)


class Shared:
    def __init__(self, data_dir: Path, job_id: str, deps: Deps, logs: list[str], settings: Settings):
        self.data_dir, self.job_id, self.deps, self.logs, self.settings = data_dir, job_id, deps, logs, settings


@pytest.fixture(scope="module")
def shared_job(tmp_path_factory: pytest.TempPathFactory) -> Shared:
    root = tmp_path_factory.mktemp("e2e")
    data = root / "data"
    data.mkdir()
    video = make_video(root / "input video.mp4", seconds=40)  # space in path on purpose
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("AUTOCLIPPER_DATA_DIR", str(data))
        deps, logs, settings = make_deps(), [], make_settings()
        store = run_job(str(video), settings=settings, deps=deps, langs=LANGS, log=logs.append)
    return Shared(data, store.job.id, deps, logs, settings)


@pytest.fixture
def job(shared_job: Shared, monkeypatch: pytest.MonkeyPatch) -> Shared:
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(shared_job.data_dir))
    return shared_job


@pytest.fixture
def private_job(shared_job: Shared, _isolated_data_dir: Path) -> Shared:
    """A copy of the shared job in this test's own data dir, for tests that change files or job.json.
    The shared job stays untouched, so test outcomes do not depend on the order tests run in."""
    src = shared_job.data_dir / "jobs" / shared_job.job_id
    shutil.copytree(src, _isolated_data_dir / "jobs" / shared_job.job_id)
    return Shared(_isolated_data_dir, shared_job.job_id, shared_job.deps, [], shared_job.settings)


@pytest.fixture
def small_video(tmp_path: Path) -> Path:
    return make_video(tmp_path / "small.mp4", seconds=40)


# ---------------------------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------------------------
def test_run_job_produces_final_clips(job: Shared) -> None:
    store = JobStore.load(job.job_id)
    j = store.job
    assert j.status == "done" and j.error is None
    assert set(j.stages) == set(JOB_STAGES)
    assert all(st.status == "done" for st in j.stages.values()), j.stages
    assert len(j.clips) == 2
    for i, clip in enumerate(j.clips):
        assert clip.index == i
        assert clip.cut == f"clips/{i:02d}/cut.mp4"
        assert set(clip.langs) == set(LANGS)
        for lang in LANGS:
            state = clip.langs[lang]
            assert state.error is None
            assert list(state.stages) == CLIP_STAGES
            assert all(st.status == "done" and st.progress == 1.0 for st in state.stages.values()), state.stages
            assert state.final == f"clips/{i:02d}/{lang}/final.mp4"

            d = store.clip_dir(i, lang)
            for name in ("script.json", "voice.wav", "voice.json", "words.json", "captions.ass",
                         "final.mp4", "metadata.json"):
                assert (d / name).stat().st_size > 0, name

            info = ffprobe(d / "final.mp4")
            video = [s for s in info["streams"] if s["codec_type"] == "video"]
            audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
            assert (video[0]["width"], video[0]["height"]) == (1080, 1920)
            assert audio, "final.mp4 must keep an audio track"
            cut_dur = float(ffprobe(store.clip_dir(i) / "cut.mp4")["format"]["duration"])
            assert abs(float(info["format"]["duration"]) - cut_dur) < 0.5

            meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
            assert set(meta) == {"language", "title", "description", "hashtags"}
            assert meta["language"] == lang
            assert isinstance(meta["title"], str) and meta["title"]
            assert isinstance(meta["description"], str) and meta["description"]
            assert isinstance(meta["hashtags"], list) and meta["hashtags"]
            assert all(isinstance(h, str) and h.startswith("#") for h in meta["hashtags"])

    # Hindi narration is Devanagari, stored unescaped
    raw = (store.clip_dir(0, "hi") / "script.json").read_text(encoding="utf-8")
    assert "परीक्षण" in raw


def test_run_job_calls_and_progress_lines(job: Shared) -> None:
    vb = job.deps.voicebox
    assert isinstance(vb, FakeVoicebox)
    assert len(vb.calls) == 4  # 2 clips x 2 languages
    engines = {c["language"]: c["engine"] for c in vb.calls}
    assert engines == {"en": "qwen", "hi": "chatterbox"}
    profiles = {c["language"]: c["profile_id"] for c in vb.calls}
    assert profiles == {"en": "fake-en", "hi": "fake-hi"}
    prefix = f"[{job.job_id}] "
    assert all(line.startswith(prefix) for line in job.logs)
    assert any(line.startswith(prefix + "transcribe 100%") for line in job.logs)
    assert any(line.startswith(prefix + "render 01/hi 100%") for line in job.logs)


def test_rerun_is_idempotent_and_builds_no_clients(job: Shared, monkeypatch: pytest.MonkeyPatch) -> None:
    forbid_real_clients(monkeypatch)  # deps=None: nothing may be built since every output exists
    final = JobStore.load(job.job_id).clip_dir(0, "en") / "final.mp4"
    before = final.stat().st_mtime_ns
    logs: list[str] = []
    store = run_job("", settings=job.settings, job_id=job.job_id, langs=LANGS, log=logs.append)
    assert store.job.status == "done"
    assert final.stat().st_mtime_ns == before
    assert any("skipped" in line for line in logs)
    # stages that ran before stay "done" (not downgraded to "skipped")
    assert all(st.status == "done" for st in store.job.stages.values())
    assert all(st.status == "done" for c in store.job.clips for ls in c.langs.values()
               for st in ls.stages.values())


def test_rerender_title_edit_keeps_voice(private_job: Shared) -> None:
    job = private_job
    store = JobStore.load(job.job_id)
    d = store.clip_dir(1, "en")
    script = json.loads((d / "script.json").read_text(encoding="utf-8"))
    script["title"] = "Edited title"
    (d / "script.json").write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    voice_before = (d / "voice.wav").stat().st_mtime_ns
    vb = FakeVoicebox()
    final = rerender(job.job_id, 1, "en", settings=job.settings,
                     deps=Deps(llm=FakeLLM(), voicebox=vb, transcriber=FakeTranscriber()), log=lambda _l: None)
    assert final == d / "final.mp4" and final.stat().st_size > 0
    assert vb.calls == []
    assert (d / "voice.wav").stat().st_mtime_ns == voice_before
    assert json.loads((d / "metadata.json").read_text(encoding="utf-8"))["title"] == "Edited title"
    st = JobStore.load(job.job_id).job.clips[1].langs["en"]
    assert st.error is None and all(s.status == "done" for k, s in st.stages.items() if k != "voice")
    assert st.stages["voice"].status == "done"


def test_rerender_narration_edit_regenerates_voice(private_job: Shared) -> None:
    job = private_job
    store = JobStore.load(job.job_id)
    d = store.clip_dir(0, "hi")
    script = json.loads((d / "script.json").read_text(encoding="utf-8"))
    script["narration"] = "यह नया संपादित वर्णन है"
    (d / "script.json").write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    vb = FakeVoicebox()
    rerender(job.job_id, 0, "hi", settings=job.settings,
             deps=Deps(llm=FakeLLM(), voicebox=vb, transcriber=FakeTranscriber()), log=lambda _l: None)
    assert len(vb.calls) == 1
    assert vb.calls[0]["text"] == "यह नया संपादित वर्णन है"
    assert vb.calls[0]["engine"] == "chatterbox"
    result = load_voice_result(d)
    assert result is not None and result.narration_hash == narration_hash("यह नया संपादित वर्णन है")
    words = json.loads((d / "words.json").read_text(encoding="utf-8"))
    assert [w["text"] for w in words] == "यह नया संपादित वर्णन है".split()
    assert ffprobe(d / "final.mp4")["streams"][0]["width"] in (1080, 1920)


def test_mutating_tests_leave_shared_job_untouched(job: Shared, private_job: Shared) -> None:
    assert private_job.data_dir != job.data_dir
    shared_hi = job.data_dir / "jobs" / job.job_id / "clips" / "00" / "hi" / "script.json"
    assert "परीक्षण" in shared_hi.read_text(encoding="utf-8")


def test_voice_shortening_writes_back_and_resume_is_free(small_video: Path) -> None:
    """TESTS-3: a slow voice forces shortening. script.json, voice.json and words.json must agree on
    the shortened narration, and a resume must not pay Voicebox or the LLM again."""
    s = make_settings()
    s.clips.count = 1
    vb, llm = FakeVoicebox(seconds_per_word=0.8), FakeLLM()
    store = run_job(str(small_video), settings=s, deps=Deps(llm=llm, voicebox=vb,
                    transcriber=FakeTranscriber(duration=40)), langs=["en"], log=lambda _l: None)
    assert store.job.status == "done", store.job
    assert any(name == "shorten" for name, _ in llm.calls)
    d = store.clip_dir(0, "en")
    voice = json.loads((d / "voice.json").read_text(encoding="utf-8"))
    assert voice["shortened"] is True
    script = json.loads((d / "script.json").read_text(encoding="utf-8"))
    assert script["narration"] == voice["narration"]
    words = json.loads((d / "words.json").read_text(encoding="utf-8"))
    assert " ".join(w["text"] for w in words) == " ".join(voice["narration"].split())

    vb2, llm2 = FakeVoicebox(seconds_per_word=0.8), FakeLLM()
    again = run_job("", settings=s, job_id=store.job.id, langs=["en"], log=lambda _l: None,
                    deps=Deps(llm=llm2, voicebox=vb2, transcriber=FakeTranscriber(duration=40)))
    assert again.job.status == "done"
    assert vb2.calls == []
    assert [c for c in llm2.calls if c[0] == "shorten"] == []


def test_rerender_errors(job: Shared) -> None:
    deps = make_deps()
    with pytest.raises(PipelineError, match="not found"):
        rerender("nope", 0, "en", settings=job.settings, deps=deps)
    with pytest.raises(PipelineError, match="does not exist"):
        rerender(job.job_id, 99, "en", settings=job.settings, deps=deps)
    with pytest.raises(PipelineError, match="unsupported"):
        rerender(job.job_id, 0, "fr", settings=job.settings, deps=deps)


# ---------------------------------------------------------------------------------------------
# failure isolation and validation
# ---------------------------------------------------------------------------------------------
class HindiFailsVoicebox(FakeVoicebox):
    def synthesize(self, text, *, profile_id, language, engine, dst, on_progress=None):
        if language == "hi":
            raise RuntimeError("voicebox exploded for hindi")
        return super().synthesize(text, profile_id=profile_id, language=language, engine=engine,
                                  dst=dst, on_progress=on_progress)


def test_clip_lang_failure_does_not_stop_others(small_video: Path) -> None:
    s = make_settings()
    s.clips.count = 1
    store = run_job(str(small_video), settings=s, deps=make_deps(HindiFailsVoicebox()), langs=LANGS,
                    log=lambda _l: None)
    j = store.job
    assert j.status == "done"
    en, hi = j.clips[0].langs["en"], j.clips[0].langs["hi"]
    assert en.error is None and (store.dir / en.final).stat().st_size > 0
    assert hi.error and "voicebox exploded for hindi" in hi.error
    assert hi.stages["script"].status == "done"
    assert hi.stages["voice"].status == "failed"
    assert hi.stages["render"].status == "pending"
    on_disk = json.loads((store.dir / "job.json").read_text(encoding="utf-8"))
    assert on_disk["clips"][0]["langs"]["hi"]["error"].startswith("voice:")


def test_all_clip_langs_failing_marks_job_failed(small_video: Path) -> None:
    s = make_settings()
    s.clips.count = 1
    store = run_job(str(small_video), settings=s, deps=make_deps(FakeVoicebox(fail=True)), langs=["en"],
                    log=lambda _l: None)
    assert store.job.status == "failed"
    assert store.job.clips[0].langs["en"].error


def test_missing_source_fails_job(tmp_path: Path) -> None:
    with pytest.raises(PipelineError):
        run_job(str(tmp_path / "missing.mp4"), settings=make_settings(), deps=make_deps(), log=lambda _l: None)
    [job_id] = JobStore.list_ids()
    j = JobStore.load(job_id).job
    assert j.status == "failed" and j.error and j.error.startswith("ingest:")
    assert j.stages["ingest"].status == "failed"


def test_resume_unknown_job() -> None:
    with pytest.raises(PipelineError, match="not found"):
        run_job("", settings=make_settings(), deps=make_deps(), job_id="does-not-exist")


@pytest.mark.parametrize("kwargs, match", [
    ({"count": 0}, "clips.count"),
    ({"count": 1000}, "clips.count"),
    ({"min_seconds": 50, "max_seconds": 20}, "greater than max"),
    ({"min_seconds": -1}, "clips.min_seconds"),
    ({"langs": ["fr"]}, "unsupported"),
    ({"langs": []}, "no languages"),
])
def test_effective_settings_rejects_bad_overrides(kwargs: dict, match: str) -> None:
    with pytest.raises(PipelineError, match=match):
        effective_settings(Settings(), **kwargs)


def test_effective_settings_applies_overrides() -> None:
    s = effective_settings(Settings(), langs=["hi", "hi"], count=3, min_seconds=15, max_seconds=30)
    assert s.languages == ["hi"] and s.clips.count == 3
    assert (s.clips.min_seconds, s.clips.max_seconds) == (15, 30)


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------
def test_cli_run_end_to_end(small_video: Path, monkeypatch: pytest.MonkeyPatch,
                            capsys: pytest.CaptureFixture[str]) -> None:
    s = make_settings()
    s.clips.count = 1
    from autoclipper.config import save_settings

    save_settings(s)
    monkeypatch.setattr(pipeline, "_make_llm", lambda _s: FakeLLM())
    monkeypatch.setattr(pipeline, "_make_voicebox", lambda _s: FakeVoicebox())
    monkeypatch.setattr(pipeline, "_make_transcriber", lambda _s: FakeTranscriber(duration=40))
    code = cli.main(["run", str(small_video), "--clips", "1", "--langs", "en", "--min", "10", "--max", "15"])
    out = capsys.readouterr().out
    assert code == 0, out
    [job_id] = JobStore.list_ids()
    assert f"[{job_id}] render 00/en 100%" in out
    assert "status=done" in out
    assert (JobStore.load(job_id).clip_dir(0, "en") / "final.mp4").stat().st_size > 0

    assert cli.main(["status", job_id]) == 0
    assert "en: script=done voice=done captions=done render=done" in capsys.readouterr().out
    assert cli.main(["jobs"]) == 0
    assert f"{job_id}  done" in capsys.readouterr().out


def test_cli_status_and_rerender_unknown_job(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["status", "nope"]) == 1
    assert "error: job 'nope' not found" in capsys.readouterr().err
    assert cli.main(["rerender", "nope", "0", "en"]) == 1
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["run"],
    ["run", "x.mp4", "--langs", "fr"],
    ["run", "x.mp4", "--clips", "0"],
    ["run", "x.mp4", "--clips", "abc"],
    ["run", "x.mp4", "--min", "-5"],
    ["rerender", "job", "0", "fr"],
    ["rerender", "job", "-1", "en"],
    ["settings"],
    ["bogus"],
])
def test_cli_usage_errors_exit_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(argv) == 2
    capsys.readouterr()


def test_cli_jobs_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["jobs"]) == 0
    assert "no jobs yet" in capsys.readouterr().out


def test_cli_settings_show_and_set(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["settings", "show"]) == 0
    shown = capsys.readouterr().out
    assert '"model": "claude-opus-5-5"' in shown

    assert cli.main(["settings", "set", "clips.count=3", "voicebox.english.profile_id=abc-123",
                     "llm.use_fallbacks=false", "whisper.language=null", "languages=en"]) == 0
    capsys.readouterr()
    s = load_settings()
    assert s.clips.count == 3 and isinstance(s.clips.count, int)
    assert s.voicebox.english.profile_id == "abc-123"
    assert s.llm.use_fallbacks is False
    assert s.whisper.language is None
    assert s.languages == ["en"]

    assert cli.main(["settings", "set", 'languages=["en","hi"]', 'captions.font_hi="Noto Sans Devanagari"']) == 0
    assert load_settings().languages == ["en", "hi"]


@pytest.mark.parametrize("assignment, match", [
    ("clips.count=abc", "clips.count"),
    ("clips.count=0", "clips.count"),
    ("clips.min_seconds=170", "must not exceed"),
    ("llm.effort=extreme", "llm.effort"),
    ("nope.key=1", "unknown setting 'nope'"),
    ("clips.nope=1", "unknown setting 'clips.nope'"),
    ("clips=1", "is a group"),
    ("clips.count", "expected key.path=value"),
    ("languages=fr", "languages"),
])
def test_cli_settings_set_rejects_bad_values(assignment: str, match: str,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    before = load_settings()
    assert cli.main(["settings", "set", assignment]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and match in err, err
    assert load_settings() == before  # nothing saved


def test_parse_value() -> None:
    assert cli.parse_value("3") == 3
    assert cli.parse_value("1.5") == 1.5
    assert cli.parse_value("true") is True
    assert cli.parse_value("null") is None
    assert cli.parse_value("hello world") == "hello world"
    assert cli.parse_value('["a"]') == ["a"]


# ---------------------------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------------------------
def _proc(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_doctor_node_versions() -> None:
    which = lambda name: f"/usr/bin/{name}"  # noqa: E731
    c16 = cli.check_node(run=lambda _a: _proc("v16.20.2\n"), which=which)
    assert c16.level == "warn" and "22+" in c16.detail
    assert cli.check_node(run=lambda _a: _proc("v22.3.0\n"), which=which).level == "ok"
    assert cli.check_node(which=lambda _n: None).level == "warn"
    assert cli.parse_node_major("v16.20.2") == 16 and cli.parse_node_major("junk") is None


def test_doctor_filters() -> None:
    which = lambda name: f"/usr/bin/{name}"  # noqa: E731
    good = " T.. amix              N->A       Audio mixing.\n ... atempo            A->A       Tempo.\n" \
           " ... ass               V->V       Render ASS.\n ... subtitles         V->V       Render text.\n"
    assert cli.check_filters(run=lambda _a: _proc(good), which=which).level == "ok"
    bad = cli.check_filters(run=lambda _a: _proc(" T.. amix N->A x\n"), which=which)
    assert bad.level == "fail" and "subtitles" in bad.detail
    assert cli.check_filters(which=lambda _n: None).level == "fail"


def test_doctor_fonts(tmp_path: Path) -> None:
    empty = tmp_path / "fonts"
    empty.mkdir()
    fc = "Noto Sans,Noto Sans Regular\nNoto Sans Devanagari\nDejaVu Sans\n"
    ok = cli.check_fonts(run=lambda _a: _proc(fc), which=lambda _n: "/usr/bin/fc-list", fonts_dir=empty)
    assert ok.level == "ok" and "system" in ok.detail
    miss = cli.check_fonts(run=lambda _a: _proc("DejaVu Sans\n"), which=lambda _n: "/usr/bin/fc-list",
                           fonts_dir=empty)
    assert miss.level == "fail" and "Noto Sans Devanagari" in miss.detail
    (empty / "NotoSans-Regular.ttf").write_bytes(b"x")
    (empty / "NotoSansDevanagari-Regular.ttf").write_bytes(b"x")
    local = cli.check_fonts(which=lambda _n: None, fonts_dir=empty)
    assert local.level == "ok" and "assets/fonts" in local.detail


def test_doctor_voicebox_and_key() -> None:
    s = make_settings()
    checks = cli.check_voicebox(s, client_factory=lambda _s: FakeVoicebox())
    assert [c.level for c in checks] == ["ok", "ok", "ok"]

    class Down:
        def health(self) -> None:
            raise RuntimeError("Voicebox is not reachable at http://127.0.0.1:17493 - is it running?")

    s.voicebox.hindi.profile_id = ""
    down = cli.check_voicebox(s, client_factory=lambda _s: Down())
    assert down[0].level == "fail" and "not reachable" in down[0].detail
    assert down[2].level == "fail" and "voicebox.hindi.profile_id" in down[2].detail

    assert cli.check_api_key(s, environ={"ANTHROPIC_API_KEY": "sk-test"}).level == "ok"
    missing = cli.check_api_key(s, environ={}, profile_available=False)
    assert missing.level == "fail" and ".env" in missing.detail and "ant auth login" in missing.detail
    profile = cli.check_api_key(s, environ={}, profile_available=True)
    assert profile.level == "ok" and "ant auth login" in profile.detail


def _patch_doctor_all_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = lambda name: cli.Check(name, "ok", "fake")  # noqa: E731
    monkeypatch.setattr(cli, "check_binaries", lambda *a, **k: [ok("ffmpeg"), ok("ffprobe")])
    monkeypatch.setattr(cli, "check_filters", lambda *a, **k: ok("ffmpeg filters"))
    monkeypatch.setattr(cli, "check_fonts", lambda *a, **k: ok("fonts"))
    monkeypatch.setattr(cli, "check_voicebox", lambda *a, **k: [ok("voicebox")])
    monkeypatch.setattr(cli, "check_node", lambda *a, **k: ok("node"))


def test_doctor_command_runs(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _patch_doctor_all_ok(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "[OK  ] ffmpeg" in out and "node" in out and "all checks passed" in out
    assert "sk-test" not in out  # never print the key
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-test"

    # one required check fails -> exit 1
    monkeypatch.setattr(cli, "check_fonts", lambda *a, **k: cli.Check("fonts", "fail", "missing"))
    assert cli.main(["doctor"]) == 1
    assert "[FAIL] fonts" in capsys.readouterr().out

    # only a warning (node) -> still exit 0
    monkeypatch.setattr(cli, "check_fonts", lambda *a, **k: cli.Check("fonts", "ok", "fake"))
    monkeypatch.setattr(cli, "check_node", lambda *a, **k: cli.Check("node", "warn", "v16"))
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "[WARN] node" in out and "0 problem(s), 1 warning(s)" in out


def test_cli_debug_flag_after_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["status", "nope", "--debug"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" in err and "error: job 'nope' not found" in err
    assert cli.main(["--debug", "status", "nope"]) == 1
    assert "Traceback" in capsys.readouterr().err
    assert cli.main(["status", "nope"]) == 1
    assert "Traceback" not in capsys.readouterr().err
