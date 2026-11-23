"""Lead triage fixes for the voice-over mix + Demucs build (regression tests for the review findings).

No real Anthropic / Voicebox / Demucs: fakes, a sleeping Python child for the cancel test, and synthetic
ffmpeg media.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fakes import FakeTranscriber, FakeVoicebox, make_tone
from test_audio_mix import (MIN_ASS, ModeStub, FakeDemucs, _ffmpeg, clip_layout, loud, make_cut, make_voice_wav,
                            sep_settings)
from test_web_support import assert_error_shape, make_client, make_job_on_disk, read_json, wait_for

from autoclipper import separation
from autoclipper.config import SeparationSettings, Settings
from autoclipper.models import ClipScript
from autoclipper.stages import render as render_stage
from autoclipper.stages.captions import make_captions
from autoclipper.stages.render import (RenderError, audio_key, read_render_stamp, render_clip, render_is_current,
                                       render_settings_changed, write_metadata, write_render_stamp)
from autoclipper.stages.script import _clean_title, fix_title_quotes
from autoclipper.stages.voice import looks_truncated, make_voice
from autoclipper.config import VoiceProfile
from autoclipper.web.worker import Worker


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("lead")
    return {"dir": d, "loud": make_cut(d / "loud.mp4", gain=6.0),
            "voice": make_voice_wav(d / "voice.wav", seconds=3.0, gain=0.3)}


# ============================================================================ Demucs process handling
class Stop(BaseException):
    pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie is dead for our purposes
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return False


@pytest.mark.skipif(os.name == "nt", reason="POSIX process check")
def test_run_cancellable_kills_child_on_cancel_and_timeout(tmp_path):
    pidfile = tmp_path / "pid"
    code = f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    calls = []

    def poll():
        calls.append(1)
        if pidfile.exists() and len(calls) >= 2:
            raise Stop()

    t0 = time.monotonic()
    with pytest.raises(Stop):
        separation.run_cancellable([sys.executable, "-c", code], env=None, timeout=60, cwd=str(tmp_path), poll=poll)
    assert time.monotonic() - t0 < 15
    pid = int(pidfile.read_text())
    assert wait_for(lambda: not _alive(pid), timeout=5)

    pidfile.unlink()
    with pytest.raises(subprocess.TimeoutExpired):
        separation.run_cancellable([sys.executable, "-c", code], env=None, timeout=1.5, cwd=str(tmp_path))
    assert wait_for(lambda: not _alive(int(pidfile.read_text())), timeout=5)
    ok = separation.run_cancellable([sys.executable, "-c", "import sys; print('hi'); sys.stderr.write('err')"],
                                    env=None, timeout=30, cwd=str(tmp_path))
    assert ok.returncode == 0 and ok.stdout.strip() == "hi" and ok.stderr == "err"


def test_cancel_during_separation_propagates_and_cleans_temp(media, tmp_path):
    s = sep_settings(tmp_path)
    cut, out = clip_layout(tmp_path, media)
    seen = {}

    def runner(cmd, *, env, timeout, cwd, poll=None):
        seen["poll"] = poll
        assert poll is not None
        poll()  # the worker's cancel point raises
        raise AssertionError("not reached")

    def cancel():
        raise Stop()

    with pytest.raises(Stop):  # a cancel is not a separation failure: no fallback render
        render_clip(cut=cut, out_dir=out, settings=s, sep_runner=runner, cancel_check=cancel)
    assert not (out / "final.mp4").exists() and not list(cut.parent.glob("demucs-*"))


def test_stale_demucs_temp_dirs_are_removed(media, tmp_path):
    s = sep_settings(tmp_path)
    cut, out = clip_layout(tmp_path, media)
    (cut.parent / "demucs-old123").mkdir()
    (cut.parent / "demucs-old123" / "in.wav").write_bytes(b"x" * 100)
    render_clip(cut=cut, out_dir=out, settings=s, sep_runner=FakeDemucs())
    assert not list(cut.parent.glob("demucs-*"))


def test_demucs_env_has_no_anthropic_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("anthropic_other", "x")
    monkeypatch.setenv("KEEP_ME", "1")
    env = separation._env(SeparationSettings())
    assert not any(k.upper().startswith("ANTHROPIC_") for k in env) and env["KEEP_ME"] == "1"
    fake = FakeDemucs()
    separation.check_demucs(SeparationSettings(python=sys.executable), runner=fake)
    assert "ANTHROPIC_API_KEY" not in fake.calls[0]["env"]


def test_demucs_error_tail_has_no_ansi_codes(media, tmp_path):
    class AnsiFail(FakeDemucs):
        def __call__(self, cmd, *, env, timeout, cwd):
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "\x1b[1mFATAL:\x1b[0m Cannot use a Transformer model "
                                                          "with a longer segment\r50%|###  |\n")

    assert separation._tail("\x1b[31mred\x1b[0m\n") == "red"
    s = sep_settings(tmp_path)
    cut, out = clip_layout(tmp_path, media)
    o = render_clip(cut=cut, out_dir=out, settings=s, sep_runner=AnsiFail())
    assert "\x1b" not in o.audio_warning and "FATAL: Cannot use a Transformer model" in o.audio_warning


def test_segment_limit_for_transformer_models():
    with pytest.raises(ValueError, match="at most 7"):
        SeparationSettings(model="htdemucs", segment=8)
    with pytest.raises(ValueError):
        SeparationSettings(model="htdemucs_ft", segment=30)
    assert SeparationSettings(model="mdx_extra", segment=30).segment == 30
    s = SeparationSettings.model_construct(model="htdemucs", segment=30)  # e.g. an unvalidated object
    assert separation.effective_segment(s) == 7


def test_settings_api_rejects_long_htdemucs_segment():
    client, _, _ = make_client(start=False)
    saved = client.get("/api/settings").json()
    bad = json.loads(json.dumps(saved))
    bad["separation"]["segment"] = 30
    body = assert_error_shape(client.put("/api/settings", json=bad), 422)
    assert "at most 7" in json.dumps(body)
    bad["separation"]["model"] = "mdx_extra"
    assert client.put("/api/settings", json=bad).status_code == 200


def test_failed_separation_is_not_retried_for_the_other_language(media, tmp_path):
    s = sep_settings(tmp_path)
    cut, out = clip_layout(tmp_path, media)
    hi = cut.parent / "hi"
    hi.mkdir()
    for name in ("voice.wav", "captions.ass"):
        (hi / name).write_bytes((out / name).read_bytes())
    fake = FakeDemucs(rc=1)
    cache: dict = {}
    o1 = render_clip(cut=cut, out_dir=out, settings=s, sep_runner=fake, hf_env_cache=cache)
    o2 = render_clip(cut=cut, out_dir=hi, settings=s, sep_runner=fake, hf_env_cache=cache)
    assert len(fake.calls) == 1
    assert "Demucs failed" in o1.audio_warning and "not retried" in o2.audio_warning
    assert o2.audio["background"] == "original"
    # a new run (fresh cache) tries again
    render_clip(cut=cut, out_dir=hi, settings=s, sep_runner=fake, hf_env_cache={})
    assert len(fake.calls) == 2


def test_no_separation_without_a_voice(media, tmp_path):
    s = sep_settings(tmp_path)
    cut, out = clip_layout(tmp_path, media)
    (out / "voice.wav").unlink()
    fake = FakeDemucs()
    with pytest.raises(RenderError, match="voice file not found"):
        render_clip(cut=cut, out_dir=out, settings=s, sep_runner=fake)
    assert fake.calls == []


def test_separation_timing_reported(media, tmp_path):
    s = sep_settings(tmp_path)
    cut, out = clip_layout(tmp_path, media)
    o1 = render_clip(cut=cut, out_dir=out, settings=s, sep_runner=FakeDemucs())
    assert o1.audio["separation_cached"] is False and isinstance(o1.audio["separation_seconds"], float)
    o2 = render_clip(cut=cut, out_dir=out, settings=s, sep_runner=FakeDemucs())
    assert o2.audio["separation_cached"] is True


# ============================================================================ report fields
def test_trimmed_clip_reports_no_outside_gain_and_quiet_stem_note(media, tmp_path):
    class QuietStem(FakeDemucs):
        def __call__(self, cmd, *, env, timeout, cwd):
            self.calls.append(cmd)
            out = Path(cmd[cmd.index("-o") + 1]) / cmd[cmd.index("-n") + 1]
            out.mkdir(parents=True)
            _ffmpeg("-f", "lavfi", "-i", "sine=frequency=550:sample_rate=44100:duration=8",
                    "-af", "volume=0.0003", "-ac", "2", str(out / "no_vocals.wav"))
            return subprocess.CompletedProcess(cmd, 0, "", "")

    s = sep_settings(tmp_path)
    s.audio = loud(short_voice="trim_clip")
    cut, out = clip_layout(tmp_path, media)
    o = render_clip(cut=cut, out_dir=out, settings=s, sep_runner=QuietStem())
    assert o.audio["trimmed_to"] is not None and o.audio["background_gain_db_outside"] is None
    assert "nearly silent after vocal removal" in o.audio["background_note"]
    # a normal stem has no note
    s2 = sep_settings(tmp_path / "b")
    cut2, out2 = clip_layout(tmp_path / "b", media)
    o2 = render_clip(cut=cut2, out_dir=out2, settings=s2, sep_runner=FakeDemucs())
    assert "background_note" not in o2.audio and o2.audio["background_gain_db_outside"] is not None


# ============================================================================ audio settings staleness
def test_audio_key_makes_old_and_changed_voiceover_renders_stale(media, tmp_path):
    s = Settings()
    s.audio = loud()
    cut, out = clip_layout(tmp_path, media)
    (out / "final.mp4").write_bytes(b"x" * 100)
    # a render made before this build (no audio_key)
    (out / "render.json").write_text(json.dumps({"caption_engine": "ass", "engine_used": "ass"}))
    assert not render_is_current(out, s) and render_settings_changed(out, s)
    o = render_stage.RenderOutcome(path=out / "final.mp4", engine_used="ass", warning=None)
    write_render_stamp(out, s, o)
    assert read_render_stamp(out)["audio_key"] == audio_key(s)
    assert render_is_current(out, s) and not render_settings_changed(out, s)
    s.audio.background_db_below_voice = 20
    assert not render_is_current(out, s)
    s.audio.background_db_below_voice = 18
    s.separation.background = "no_vocals"
    assert not render_is_current(out, s)
    s.separation.background = "original"
    s.audio.original_volume = 0.5  # gain-mix-only setting: no effect on a loudness-mix render
    assert render_is_current(out, s)
    # voice-over off renders do not depend on the voice-over mix settings
    off = s.model_copy(update={"voiceover": False})
    write_render_stamp(out, off, o)
    assert "audio_key" not in read_render_stamp(out)
    off.audio.background_db_below_voice = 30
    assert render_is_current(out, off)


def test_old_title_stray_quote_cleaned_in_metadata(tmp_path):
    script = ClipScript(narration="n", title='Slipped the Surly Bonds of Earth" – Reagan', description="d",
                        hashtags=[])
    write_metadata(script, "en", tmp_path / "metadata.json")
    assert read_json(tmp_path / "metadata.json")["title"] == "Slipped the Surly Bonds of Earth – Reagan"


@pytest.mark.parametrize("raw,expected", [
    ('Pocket PC With A 5" Screen', 'Pocket PC With A 5" Screen'),
    ('27" monitor vs 32"', '27" monitor vs 32"'),
    ("“Slipped the bonds” – Reagan”", "“Slipped the bonds” – Reagan"),
    ("“Open only – Reagan", "Open only – Reagan"),
    ("a “b” c", "a “b” c"),
])
def test_title_quotes_inch_marks_and_curly_pairs(raw, expected):
    assert _clean_title(raw) == expected and fix_title_quotes(raw) == expected


# ============================================================================ cut-short voice takes
class ShortThenFull(FakeVoicebox):
    """First take is cut short (0.05 s/word), later ones normal; or always cut short."""

    def __init__(self, always: bool = False):
        super().__init__(seconds_per_word=0.3)
        self.always = always
        self.texts: list[str] = []

    def synthesize(self, text, *, profile_id, language, engine, dst, on_progress=None):
        self.texts.append(text)
        spw = 0.05 if (self.always or len(self.texts) == 1) else 0.3
        return make_tone(Path(dst), seconds=max(0.5, len(text.split()) * spw))


NARRATION = " ".join(f"w{i}" for i in range(60))


def test_cut_short_take_is_retried_and_recovers(tmp_path):
    vb = ShortThenFull()
    called = []
    r = make_voice(client=vb, narration=NARRATION, lang="hi", voice=VoiceProfile(profile_id="p"),
                   clip_duration=20.0, out_dir=tmp_path, max_speedup=1.25, wps=3.35,
                   lengthen=lambda t, n: called.append(n) or t)
    assert vb.texts == [NARRATION, NARRATION] and not r.truncated and r.warnings == []
    assert r.raw_duration == pytest.approx(18.0, abs=0.2) and not list(tmp_path.glob("*.first.wav"))


def test_cut_short_twice_is_marked_truncated_and_never_lengthened(tmp_path):
    vb = ShortThenFull(always=True)
    called = []
    r = make_voice(client=vb, narration=NARRATION, lang="hi", voice=VoiceProfile(profile_id="p"),
                   clip_duration=30.0, out_dir=tmp_path, max_speedup=1.25, wps=3.35,
                   lengthen=lambda t, n: called.append(n) or t)
    assert len(vb.texts) == 2 and called == [] and r.truncated and not r.lengthened
    assert "cut the narration short" in r.warnings[0]
    spoken = r.spoken_narration.split()
    assert 3 <= len(spoken) <= 15 and spoken == NARRATION.split()[:len(spoken)]
    # captions show only the spoken prefix, inside the voice
    s = Settings()
    make_captions(narration=NARRATION, voice_wav=tmp_path / "voice.wav", lang="hi",
                  transcriber=FakeTranscriber(), settings=s, out_dir=tmp_path)
    words = json.loads((tmp_path / "words.json").read_text(encoding="utf-8"))
    assert [w["text"] for w in words] == spoken


def test_no_truncation_check_without_wps_or_for_normal_takes(tmp_path):
    assert not looks_truncated(NARRATION, 3.0, None)
    assert not looks_truncated("a b c d e", 0.5, 3.0)  # too few words to tell
    assert looks_truncated(NARRATION, 5.0, 3.35) and not looks_truncated(NARRATION, 12.0, 3.35)


def test_old_cut_short_voice_gets_prefix_captions(tmp_path):
    # voice.json from before the check (no "truncated"): a 3 s voice for a 60-word Hindi text
    make_tone(tmp_path / "voice.wav", seconds=3.0)
    make_captions(narration=NARRATION, voice_wav=tmp_path / "voice.wav", lang="hi",
                  transcriber=FakeTranscriber(), settings=Settings(), out_dir=tmp_path)
    words = json.loads((tmp_path / "words.json").read_text(encoding="utf-8"))
    assert 5 <= len(words) <= 15 and max(w["end"] for w in words) <= 3.01
    assert min(w["end"] - w["start"] for w in words) > 0.15  # readable, not 60 flashes


# ============================================================================ PUT /mode fixes
def test_mode_current_mode_while_plain_resume_runs_is_not_409(tmp_path):
    stub = ModeStub(block=True)
    client, worker, _ = make_client(stub=stub, worker=Worker(run_job=stub.run_job, rerender=stub.rerender))
    with client:
        store = make_job_on_disk(clips=1, status="failed")
        assert client.post(f"/api/jobs/{store.job.id}/resume").status_code == 202
        assert stub.started.wait(5)
        r = client.put(f"/api/jobs/{store.job.id}/mode", json={"voiceover": True})  # legacy job = on
        assert r.status_code == 200, r.text
        assert r.json()["changed"] is False and r.json()["task"]["state"] == "running"
        assert_error_shape(client.put(f"/api/jobs/{store.job.id}/mode", json={"voiceover": False}), 409)
        stub.release.set()
        assert worker.wait_idle(10)
    assert len(stub.calls) == 1 and stub.calls[0][2] == {"voiceover": None}
    assert read_json(store.dir / "job.json").get("voiceover") is None  # no side effect


def test_mode_same_mode_rerenders_old_mix_renders(tmp_path):
    stub = ModeStub()
    client, worker, _ = make_client(stub=stub, worker=Worker(run_job=stub.run_job, rerender=stub.rerender))
    with client:
        store = make_job_on_disk(clips=1)  # finals without audio_key = made with the old mix
        d = client.get(f"/api/jobs/{store.job.id}").json()["clips"][0]["langs"]["en"]
        assert d["audio_stale"] is True and d["render_stale"] is True
        r = client.put(f"/api/jobs/{store.job.id}/mode", json={"voiceover": True})
        assert r.status_code == 202 and r.json()["changed"] is False and r.json()["task"]["kind"] == "run_job"
        assert wait_for(lambda: stub.calls and worker.wait_idle(5))
        assert stub.calls[-1] == ("run_job", store.job.id, {"voiceover": None})


def test_trim_never_cuts_speech_after_the_last_caption_word(media, tmp_path):
    # a cut-short take whose captions align only up to 1.0 s while the voice speaks for 3 s
    s = Settings()
    s.audio = loud(short_voice="trim_clip")
    cut, out = clip_layout(tmp_path, media)
    (out / "words.json").write_text(json.dumps([{"start": 0.1, "end": 1.0, "text": "a"}]), encoding="utf-8")
    o = render_clip(cut=cut, out_dir=out, settings=s)
    assert o.audio["voice_end"] == pytest.approx(3.0, abs=0.05)
    assert o.audio["trimmed_to"] == pytest.approx(4.0, abs=0.05)
