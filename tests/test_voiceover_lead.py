"""Lead fixes for the voice-over on/off feature (review findings, 2026-11-23).

- A job's voice-over mode is fixed when it is created; a Settings change never switches an existing job.
- Partial caption translations are completed on the next render instead of being cached for good.
- Non-numeric ids from a translator are ignored (CaptionsError when nothing usable), never a bare ValueError.
- Translated lines: a zero-length source line is held long enough to read; long pauses are skipped.
- A clip with no speech + the hyperframes engine renders without a false "HyperFrames failed" warning.
- Clip editor: an empty narration may be saved while the voice-over is on if none was written yet.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_voiceover_off import Ctx, record_renders, settings, vo_api  # noqa: F401 - fixture re-export
from test_web_support import assert_error_shape, make_job_on_disk

from autoclipper.jobstore import JobStore
from autoclipper.models import Segment, Transcript, Word
from autoclipper.pipeline import job_voiceover, rerender, run_job
from autoclipper.stages import original_captions as oc
from autoclipper.stages.captions import CaptionsError
from autoclipper.stages.render import read_render_stamp


# --------------------------------------------------------------------------------------------- fixed mode
def test_cli_new_job_stores_the_setting_and_resume_ignores_a_later_change(tmp_path: Path):
    c = Ctx(tmp_path)
    on, off = settings(True), settings(False)
    store = run_job(str(c.video), settings=on, deps=c.deps(), langs=["en"], log=lambda _l: None)
    assert JobStore.load(store.job.id).job.voiceover is True
    n_voice = len(c.voicebox.calls)
    out = store.clip_dir(0, "en")
    final_mtime = (out / "final.mp4").stat().st_mtime_ns
    # the user turns the setting off, then resumes / re-renders the old job: it stays a voice-over job
    run_job("", settings=off, deps=c.deps(), langs=["en"], job_id=store.job.id, log=lambda _l: None)
    rerender(store.job.id, 0, "en", settings=off, deps=c.deps(), log=lambda _l: None)
    job = JobStore.load(store.job.id).job
    assert job.voiceover is True and job_voiceover(job, off) is True
    assert job.clips[0].langs["en"].stages["voice"].message != "voice-over off"
    assert "voiceover" not in read_render_stamp(out) and not (out / "captions.json").exists()
    assert c.calls("translate_segments") == [] and len(c.voicebox.calls) == n_voice
    assert (out / "final.mp4").stat().st_mtime_ns != final_mtime  # rerender re-rendered, in voice-over mode

    # and the other way round: a captions-only job stays captions-only when the setting is turned on
    (tmp_path / "b").mkdir()
    c2 = Ctx(tmp_path / "b")
    s2 = run_job(str(c2.video), settings=off, deps=c2.deps(), langs=["en"], log=lambda _l: None)
    assert JobStore.load(s2.job.id).job.voiceover is False
    rerender(s2.job.id, 0, "en", settings=on, deps=c2.deps(), log=lambda _l: None)
    assert c2.voicebox.calls == [] and c2.calls("write_script") == []
    assert read_render_stamp(s2.clip_dir(0, "en"))["voiceover"] is False


def test_legacy_job_without_mode_is_voiceover_on(vo_api):  # noqa: F811
    client, _, _ = vo_api
    store = make_job_on_disk(clips=1, langs=("en",))
    assert store.job.voiceover is None
    body = {**client.get("/api/settings").json(), "voiceover": False}
    body.pop("publish", None)
    assert client.put("/api/settings", json=body).status_code == 200
    d = client.get(f"/api/jobs/{store.job.id}").json()
    en = d["clips"][0]["langs"]["en"]
    assert d["voiceover"] is True and d["voiceover_override"] is None
    assert en["voiceover"] is True and en["render_stale"] is False


# --------------------------------------------------------------------------------------------- translation
def _segs() -> list[oc.ClipSegment]:
    w = lambda s, e, t: Word(start=s, end=e, text=t)  # noqa: E731
    t = Transcript(language="en", duration=30, segments=[
        Segment(start=0.0, end=2.0, text="a b", words=[w(0.0, 1.0, "a"), w(1.0, 2.0, "b")]),
        Segment(start=3.0, end=5.0, text="c d", words=[w(3.0, 4.0, "c"), w(4.0, 5.0, "d")]),
    ])
    return oc.clip_segments(t, 0.0, 10.0)


def test_partial_translation_is_completed_on_the_next_render(tmp_path: Path):
    segs = _segs()
    calls: list[dict] = []
    answers = [{0: "X Y"}, {0: "ignored", 1: "Z W"}]

    def tr(**kw):
        calls.append(kw)
        return answers[len(calls) - 1]

    first = oc.translate_lines(segs, lang="hi", source_lang="en", translate=tr, out_dir=tmp_path)
    assert first == {0: "X Y"}
    second = oc.translate_lines(segs, lang="hi", source_lang="en", translate=tr, out_dir=tmp_path)
    assert len(calls) == 2 and second[1] == "Z W" and second[0] in ("X Y", "ignored")
    # now complete: served from the cache without another call
    third = oc.translate_lines(segs, lang="hi", source_lang="en", translate=tr, out_dir=tmp_path)
    assert third == second and len(calls) == 2
    cached = json.loads((tmp_path / oc.TRANSLATION_CACHE).read_text(encoding="utf-8"))
    assert sorted(s["id"] for s in cached["segments"]) == [0, 1]


def test_non_numeric_translation_ids_are_ignored(tmp_path: Path):
    segs = _segs()
    out = oc.translate_lines(segs, lang="hi", source_lang="en",
                             translate=lambda **kw: {"a": "x", " 1 ": "Z", None: "y", 0: 5}, out_dir=tmp_path)
    assert out == {1: "Z"}
    with pytest.raises(CaptionsError):
        oc.translate_lines(segs, lang="hi", source_lang="en", translate=lambda **kw: {"a": "x"},
                           out_dir=tmp_path / "b")
    with pytest.raises(CaptionsError):
        oc.translate_lines(segs, lang="hi", source_lang="en", translate=lambda **kw: ["not", "a", "dict"],
                           out_dir=tmp_path / "c")


def test_zero_length_line_is_held_readable_but_not_into_the_next_line():
    w = lambda s, e, t: Word(start=s, end=e, text=t)  # noqa: E731
    segs = [oc.ClipSegment(id=0, start=1.0, end=1.0, text="Yes.", words=[w(1.0, 1.0, "Yes.")]),
            oc.ClipSegment(id=1, start=1.5, end=1.5, text="No.", words=[w(1.5, 1.5, "No.")])]
    out = oc.translated_words(segs, {0: "हाँ जी", 1: "नहीं"}, clip_end=10.0)
    first, second = out[:2], out[2:]
    assert first[0].start == pytest.approx(1.0) and first[-1].end == pytest.approx(1.5)  # up to the next line
    assert second[0].start == pytest.approx(1.5) and second[-1].end == pytest.approx(2.3)  # min 0.8 s
    assert all(x.end > x.start for x in out)
    # the last line never runs past the clip end
    last = oc.translated_words(segs[1:], {1: "नहीं"}, clip_end=1.7)
    assert last[-1].end == pytest.approx(1.7)


def test_translated_words_skip_a_long_pause_inside_a_line():
    w = lambda s, e, t: Word(start=s, end=e, text=t)  # noqa: E731
    seg = oc.ClipSegment(id=0, start=0.0, end=10.0, text="a b c d e",
                         words=[w(0.0, 1.0, "a"), w(1.0, 2.0, "b"), w(2.0, 3.0, "d"), w(9.5, 10.0, "e")])
    out = oc.translated_words([seg], {0: "aaaa bbbb cccc dddd"}, clip_end=10.0)
    assert len(out) == 4
    # spoken time = 3.0 + 0.5 s; no word is shown inside the 3.0..9.5 silence
    for x in out:  # no word starts, ends or is shown inside the silence
        assert x.end <= 3.0 + 1e-6 or x.start >= 9.5 - 1e-6
    assert out[0].start == pytest.approx(0.0) and out[-1].end == pytest.approx(10.0)
    assert any(x.start >= 9.5 - 1e-6 for x in out)


# --------------------------------------------------------------------------------------------- render
def test_hyperframes_no_speech_clip_has_no_false_fallback_warning(tmp_path: Path, monkeypatch):
    from autoclipper.stages import hyperframes as hf
    from autoclipper.stages import original_captions

    def boom(**_kw):
        raise AssertionError("no overlay should be made for a clip without words")

    monkeypatch.setattr(hf, "make_overlay", boom)
    monkeypatch.setattr(hf, "probe_env", lambda _s: (_ for _ in ()).throw(AssertionError("no probe")))
    monkeypatch.setattr(original_captions, "clip_segments", lambda *a, **k: [])  # nothing is said
    renders = record_renders(monkeypatch)
    c = Ctx(tmp_path)
    s = settings(False)
    s.captions.engine = "hyperframes"
    store = run_job(str(c.video), settings=s, deps=c.deps(), langs=["en", "hi"], log=lambda _l: None)
    for lang in ("en", "hi"):
        st = store.job.clips[0].langs[lang]
        out = store.clip_dir(0, lang)
        assert st.error is None and st.warning is None and st.final, (lang, st)
        assert json.loads((out / "words.json").read_text(encoding="utf-8")) == []
        stamp = read_render_stamp(out)
        assert stamp["engine_used"] == "hyperframes" and not stamp.get("warning") and stamp["voiceover"] is False
    assert c.calls("translate_segments") == []
    for cmd in renders:
        assert cmd.count("-i") == 1 and not any(x.endswith("voice.wav") for x in cmd)


# --------------------------------------------------------------------------------------------- clip editor
def test_put_script_empty_narration_with_voiceover_on_only_when_none_written(vo_api):  # noqa: F811
    client, _, _ = vo_api
    store = make_job_on_disk(clips=1, langs=("en",))
    store.job.voiceover = True
    store.save()
    sp = store.clip_dir(0, "en") / "script.json"
    s = json.loads(sp.read_text(encoding="utf-8"))
    body = {"narration": "", "title": "New title", "description": "d", "hashtags": ["#a"]}
    # a narration exists: emptying it is still refused
    r = client.put(f"/api/jobs/{store.job.id}/clips/0/en/script", json=body)
    assert "narration" in assert_error_shape(r, 422, "validation failed")["errors"]
    # a script written while the voice-over was off (no narration yet): the title edit can be saved
    s["narration"] = ""
    sp.write_text(json.dumps(s), encoding="utf-8")
    r = client.put(f"/api/jobs/{store.job.id}/clips/0/en/script", json=body)
    assert r.status_code == 200, r.text
    assert json.loads(sp.read_text(encoding="utf-8"))["title"] == "New title"
