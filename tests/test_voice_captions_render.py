"""Voice, captions and render stages. Uses FakeVoicebox / FakeTranscriber - no network."""
from __future__ import annotations

import json
import re
import wave
from pathlib import Path

import pytest
from fakes import FakeTranscriber, FakeVoicebox, make_tone, make_video

from autoclipper import ffmpeg
from autoclipper.config import AudioSettings, CaptionSettings, Settings, VoiceProfile
from autoclipper.models import ClipScript, Word
from autoclipper.stages import captions, render
from autoclipper.stages.captions import (align_words, build_ass, distribute_words, make_captions,
                                         split_words)
from autoclipper.stages.render import render_final, resolve_fonts_dir, write_metadata
from autoclipper.stages.voice import (VoiceError, VoiceResult, engine_for, load_voice_result,
                                      make_voice, narration_hash)
from autoclipper.voicebox import VoiceboxError

EN = VoiceProfile(profile_id="fake-en", engine="kokoro")
HI = VoiceProfile(profile_id="fake-hi", engine="kokoro")  # engine must be overridden for hi


def words(n: int, lang: str = "en") -> str:
    base = ["यह", "एक", "परीक्षण", "है"] if lang == "hi" else ["alpha", "beta", "gamma", "delta"]
    return " ".join((base * (n // len(base) + 1))[:n])


def wav_format(p: Path) -> tuple[int, int]:
    with wave.open(str(p), "rb") as w:
        return w.getframerate(), w.getnchannels()


class ShortenSpy:
    def __init__(self, same: bool = False):
        self.calls: list[tuple[str, int]] = []
        self.same = same

    def __call__(self, text: str, max_words: int) -> str:
        self.calls.append((text, max_words))
        return text if self.same else " ".join(text.split()[:max_words])


# ------------------------------------------------------------------------------------ voice


def test_narration_hash_and_engine():
    assert narration_hash("  hello ") == narration_hash("hello")
    assert len(narration_hash("x")) == 64
    assert engine_for("hi", VoiceProfile(engine="kokoro")) == "chatterbox"
    assert engine_for("en", VoiceProfile(engine="kokoro")) == "kokoro"


def test_voice_fits_without_change(tmp_path: Path):
    vb = FakeVoicebox(seconds_per_word=0.3)
    progress: list[float] = []
    r = make_voice(client=vb, narration=words(10), lang="en", voice=EN, clip_duration=5.0,
                   out_dir=tmp_path, max_speedup=1.25, on_progress=lambda f, m: progress.append(f))
    assert (r.tempo, r.shortened, r.trimmed) == (1.0, False, False)
    assert r.final_duration == pytest.approx(3.0, abs=0.05)
    assert Path(r.path) == (tmp_path / "voice.wav").resolve()
    assert wav_format(tmp_path / "voice.wav") == (44100, 1)
    assert (tmp_path / "voice_raw.wav").is_file()
    assert not (tmp_path / "voice_norm.wav").exists()
    assert vb.calls[0]["engine"] == "kokoro" and vb.calls[0]["profile_id"] == "fake-en"
    saved = json.loads((tmp_path / "voice.json").read_text(encoding="utf-8"))
    assert saved["narration_hash"] == narration_hash(words(10))
    assert set(saved) == set(VoiceResult.model_fields)
    assert progress[0] == 0.0 and progress[-1] == 1.0 and progress == sorted(progress)


def test_voice_speeds_up_within_cap(tmp_path: Path):
    r = make_voice(client=FakeVoicebox(0.3), narration=words(10), lang="en", voice=EN,
                   clip_duration=2.7, out_dir=tmp_path, max_speedup=1.25)
    assert r.tempo == pytest.approx(3.0 / 2.7, rel=0.02)
    assert not r.trimmed and not r.shortened
    assert r.final_duration <= 2.7 + 0.05


def test_voice_shortens_once_and_regenerates(tmp_path: Path):
    vb, spy = FakeVoicebox(0.3), ShortenSpy()
    r = make_voice(client=vb, narration=words(20), lang="en", voice=EN, clip_duration=3.0,
                   out_dir=tmp_path, max_speedup=1.25, shorten=spy)
    # raw 6.0s; max_words = floor(20 * 3*1.25/6 * 0.95) = 11
    assert [c[1] for c in spy.calls] == [11]
    assert len(vb.calls) == 2
    assert r.shortened and not r.trimmed
    assert r.narration == words(11) and r.narration_hash == narration_hash(words(11))
    assert r.raw_duration == pytest.approx(3.3, abs=0.05)
    assert r.tempo == pytest.approx(1.1, rel=0.02)
    assert r.final_duration <= 3.05


def test_voice_trims_when_no_shorten(tmp_path: Path):
    r = make_voice(client=FakeVoicebox(0.3), narration=words(20), lang="en", voice=EN,
                   clip_duration=3.0, out_dir=tmp_path, max_speedup=1.25)
    assert r.trimmed and r.tempo == 1.25 and not r.shortened
    assert r.final_duration == pytest.approx(3.0, abs=0.05)
    assert r.narration == words(20)


def test_voice_trims_when_shorten_does_not_help(tmp_path: Path):
    vb, spy = FakeVoicebox(0.3), ShortenSpy(same=True)
    r = make_voice(client=vb, narration=words(20), lang="en", voice=EN, clip_duration=3.0,
                   out_dir=tmp_path, max_speedup=1.25, shorten=spy)
    assert len(spy.calls) == 1 and len(vb.calls) == 1
    assert r.trimmed and not r.shortened


def test_voice_hindi_forces_chatterbox(tmp_path: Path):
    vb = FakeVoicebox(0.3)
    r = make_voice(client=vb, narration=words(6, "hi"), lang="hi", voice=HI, clip_duration=5.0,
                   out_dir=tmp_path, max_speedup=1.25)
    assert vb.calls[0]["engine"] == "chatterbox" and vb.calls[0]["language"] == "hi"
    raw = (tmp_path / "voice.json").read_text(encoding="utf-8")
    assert "परीक्षण" in raw  # ensure_ascii=False
    assert r.narration == words(6, "hi")


@pytest.mark.parametrize("lang,key", [("en", "english"), ("hi", "hindi")])
def test_voice_missing_profile_is_actionable(tmp_path: Path, lang: str, key: str):
    vb = FakeVoicebox()
    with pytest.raises(VoiceboxError) as ei:
        make_voice(client=vb, narration="hello there", lang=lang, voice=VoiceProfile(profile_id=""),
                   clip_duration=5.0, out_dir=tmp_path, max_speedup=1.25)
    msg = str(ei.value)
    assert "autoclipper profiles" in msg and f"voicebox.{key}.profile_id=" in msg
    assert vb.calls == []


def test_voice_rejects_empty_narration_and_bad_duration(tmp_path: Path):
    with pytest.raises(VoiceError, match="narration is empty"):
        make_voice(client=FakeVoicebox(), narration="  ", lang="en", voice=EN, clip_duration=5,
                   out_dir=tmp_path, max_speedup=1.25)
    with pytest.raises(VoiceError, match="positive"):
        make_voice(client=FakeVoicebox(), narration="hi", lang="en", voice=EN, clip_duration=0,
                   out_dir=tmp_path, max_speedup=1.25)


def test_voicebox_failure_propagates(tmp_path: Path):
    with pytest.raises(RuntimeError, match="fake voicebox failure"):
        make_voice(client=FakeVoicebox(fail=True), narration="hi there", lang="en", voice=EN,
                   clip_duration=5, out_dir=tmp_path, max_speedup=1.25)


def test_load_voice_result(tmp_path: Path):
    assert load_voice_result(tmp_path) is None
    r = make_voice(client=FakeVoicebox(), narration=words(4), lang="en", voice=EN,
                   clip_duration=5.0, out_dir=tmp_path, max_speedup=1.25)
    assert load_voice_result(tmp_path) == r
    (tmp_path / "voice.json").write_text("{broken", encoding="utf-8")
    assert load_voice_result(tmp_path) is None


# --------------------------------------------------------------------------------- captions


def test_split_and_distribute():
    assert split_words("  a  bb\nccc ") == ["a", "bb", "ccc"]
    ws = distribute_words(["a", "bbb"], 4.0, start=1.0)
    assert [(w.start, w.end) for w in ws] == [(1.0, 2.0), (2.0, 5.0)]
    assert distribute_words([], 3.0) == []


def test_align_exact_match_uses_recognized_timing():
    rec = [Word(start=0.1, end=0.4, text="Hello"), Word(start=0.5, end=0.9, text="world!")]
    out = align_words(["hello,", "World"], rec, 1.0)
    assert [(w.start, w.end, w.text) for w in out] == [(0.1, 0.4, "hello,"), (0.5, 0.9, "World")]


def test_align_interpolates_unmatched_between_neighbours():
    rec = [Word(start=0.0, end=0.5, text="one"), Word(start=2.0, end=2.5, text="four")]
    out = align_words(["one", "two", "three", "four"], rec, 3.0)
    assert [w.text for w in out] == ["one", "two", "three", "four"]
    assert out[1].start == pytest.approx(0.5) and out[2].end == pytest.approx(2.0)
    starts = [w.start for w in out]
    assert starts == sorted(starts)


def test_align_hindi_matches_with_marks():
    rec = [Word(start=0.0, end=0.4, text="यह"), Word(start=0.4, end=1.0, text="परीक्षण।")]
    out = align_words(["यह", "परीक्षण"], rec, 1.0)
    assert [(w.start, w.end) for w in out] == [(0.0, 0.4), (0.4, 1.0)]


def test_align_low_match_falls_back_to_distribute():
    rec = [Word(start=0.0, end=0.3, text="zzz")]
    out = align_words(["alpha", "beta", "gamma", "delta"], rec, 2.0)
    assert out == distribute_words(["alpha", "beta", "gamma", "delta"], 2.0)
    assert align_words(["a", "b"], [], 2.0) == distribute_words(["a", "b"], 2.0)


def _dialogues(ass: str) -> list[str]:
    return [line for line in ass.splitlines() if line.startswith("Dialogue:")]


def test_build_ass_structure_highlight_and_font():
    ws = distribute_words(["one", "two", "three", "four", "five"], 5.0)
    cs = CaptionSettings(words_per_line=3)
    ass = build_ass(ws, lang="en", settings=cs)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass and "WrapStyle: 0" in ass
    assert re.search(r"^Style: Default,Noto Sans,78,", ass, re.M)
    d = _dialogues(ass)
    assert len(d) == 5
    hl = "{\\c&H00E5FF&}"
    assert d[0].endswith(f"{hl}one{{\\c&HFFFFFF&}} two three")
    assert d[1].endswith(f"one {hl}two{{\\c&HFFFFFF&}} three")
    assert d[3].endswith(f"{hl}four{{\\c&HFFFFFF&}} five")
    assert d[0].startswith("Dialogue: 0,0:00:00.00,0:00:00.")
    assert f",{cs.margin_v},1" in ass.split("Style: Default", 1)[1].splitlines()[0]


def test_build_ass_hindi_font_and_escaping():
    ws = distribute_words(["नमस्ते", "{x}", "a\\Nb"], 3.0)
    ass = build_ass(ws, lang="hi", settings=CaptionSettings(words_per_line=3))
    assert "Style: Default,Noto Sans Devanagari," in ass
    text = _dialogues(ass)[0].split(",,", 1)[1]
    assert "\\{x\\}" in text
    assert "a\\Nb" not in text and "a\\⁠Nb" in text


def test_build_ass_times_are_contiguous_within_line():
    ws = [Word(start=0, end=0.5, text="a"), Word(start=0.6, end=1.0, text="b"),
          Word(start=1.2, end=1.5, text="c"), Word(start=3.0, end=3.5, text="d")]
    d = _dialogues(build_ass(ws, lang="en", settings=CaptionSettings(words_per_line=2)))
    times = [line.split(",")[1:3] for line in d]
    assert times[0] == ["0:00:00.00", "0:00:00.60"]   # until next word in the line
    assert times[1] == ["0:00:00.60", "0:00:01.20"]   # held to next line (gap <= 0.5 s)
    assert times[3] == ["0:00:03.00", "0:00:03.50"]
    assert build_ass([], lang="en", settings=CaptionSettings()).count("Dialogue:") == 0


def test_make_captions_with_fake_transcriber_uses_fallback(tmp_path: Path):
    voice = make_tone(tmp_path / "voice.wav", seconds=2.0)
    tr = FakeTranscriber()
    narration = "यह एक परीक्षण वाक्य है"
    ass = make_captions(narration=narration, voice_wav=voice, lang="hi", transcriber=tr,
                        settings=Settings(), out_dir=tmp_path / "hi")
    assert ass == tmp_path / "hi" / "captions.ass"
    assert tr.calls == [(str(voice), "hi")]
    raw = (tmp_path / "hi" / "words.json").read_text(encoding="utf-8")
    assert "परीक्षण" in raw
    ws = [Word(**w) for w in json.loads(raw)]
    assert [w.text for w in ws] == narration.split()
    assert ws[0].start == 0.0 and ws[-1].end == pytest.approx(2.0, abs=0.05)
    assert len(_dialogues(ass.read_text(encoding="utf-8"))) == 5


def test_make_captions_uses_recognized_words(tmp_path: Path):
    voice = make_tone(tmp_path / "voice.wav", seconds=2.0)
    rec = [Word(start=0.2, end=0.6, text="hello"), Word(start=1.0, end=1.6, text="world")]
    make_captions(narration="Hello world", voice_wav=voice, lang="en",
                  transcriber=FakeTranscriber(words_for_voice=rec), settings=Settings(),
                  out_dir=tmp_path)
    ws = json.loads((tmp_path / "words.json").read_text(encoding="utf-8"))
    assert [(w["start"], w["end"]) for w in ws] == [(0.2, 0.6), (1.0, 1.6)]


def test_make_captions_missing_voice(tmp_path: Path):
    with pytest.raises(captions.CaptionsError, match="voice file not found"):
        make_captions(narration="x", voice_wav=tmp_path / "nope.wav", lang="en",
                      transcriber=FakeTranscriber(), settings=Settings(), out_dir=tmp_path)


# ----------------------------------------------------------------------------------- render


def test_resolve_fonts_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(render.config, "FONTS_DIR", tmp_path / "fonts")
    assert resolve_fonts_dir() is None
    (tmp_path / "fonts").mkdir()
    (tmp_path / "fonts" / "README.txt").write_text("x", encoding="utf-8")
    assert resolve_fonts_dir() is None
    (tmp_path / "fonts" / "NotoSans.TTF").write_bytes(b"\0")
    assert resolve_fonts_dir() == tmp_path / "fonts"


def test_render_final_delegates_to_mix_burn(tmp_path: Path, monkeypatch):
    cut = make_video(tmp_path / "cut.mp4", seconds=2.0, width=320, height=568)
    voice = make_tone(tmp_path / "voice.wav", seconds=1.0)
    ass = tmp_path / "captions.ass"
    ass.write_text("x", encoding="utf-8")
    seen: dict = {}

    def fake_mix_burn(video, dst, **kw):
        seen.update(video=video, dst=dst, **kw)
        return dst

    monkeypatch.setattr(render.ffmpeg, "mix_burn", fake_mix_burn)
    monkeypatch.setattr(render, "resolve_fonts_dir", lambda: None)
    dst = tmp_path / "en" / "final.mp4"
    out = render_final(cut=cut, voice=voice, ass=ass, dst=dst,
                       audio=AudioSettings(original_volume=0.2, voice_volume=1.1))
    assert out == dst and dst.parent.is_dir()
    assert seen["video"] == cut and seen["voice"] == voice and seen["ass"] == ass
    assert (seen["original_volume"], seen["voice_volume"], seen["fonts_dir"]) == (0.2, 1.1, None)
    assert seen["duration"] == pytest.approx(2.0, abs=0.1)


def test_render_final_missing_inputs(tmp_path: Path):
    with pytest.raises(render.RenderError, match="cut clip not found"):
        render_final(cut=tmp_path / "cut.mp4", voice=None, ass=None, dst=tmp_path / "f.mp4",
                     audio=AudioSettings())
    cut = make_video(tmp_path / "cut.mp4", seconds=1.0, width=320, height=568)
    with pytest.raises(render.RenderError, match="voice file not found"):
        render_final(cut=cut, voice=tmp_path / "v.wav", ass=None, dst=tmp_path / "f.mp4",
                     audio=AudioSettings())


def test_end_to_end_voice_captions_render(tmp_path: Path):
    """Real ffmpeg: fake voice -> captions -> final.mp4 with burned subs and mixed audio."""
    clip_dir = tmp_path / "clip dir" / "00"  # space in path exercises filter escaping
    clip_dir.mkdir(parents=True)
    cut = make_video(clip_dir / "cut.mp4", seconds=3.0, width=540, height=960)
    out_dir = clip_dir / "hi"
    r = make_voice(client=FakeVoicebox(0.3), narration=words(14, "hi"), lang="hi", voice=HI,
                   clip_duration=ffmpeg.duration(cut), out_dir=out_dir, max_speedup=1.25,
                   shorten=ShortenSpy())
    assert r.final_duration <= 3.05
    ass = make_captions(narration=r.narration, voice_wav=Path(r.path), lang="hi",
                        transcriber=FakeTranscriber(), settings=Settings(), out_dir=out_dir)
    final = render_final(cut=cut, voice=Path(r.path), ass=ass, dst=out_dir / "final.mp4",
                         audio=Settings().audio)
    info = ffmpeg.probe(final)
    assert info.has_video and info.has_audio
    assert info.duration == pytest.approx(3.0, abs=0.15)
    meta = write_metadata(ClipScript(narration=r.narration, title="शीर्षक", description="d",
                                     hashtags=["#a", "#b"]), "hi", out_dir / "metadata.json")
    raw = meta.read_text(encoding="utf-8")
    assert json.loads(raw) == {"language": "hi", "title": "शीर्षक", "description": "d",
                               "hashtags": ["#a", "#b"]}
    assert "शीर्षक" in raw
