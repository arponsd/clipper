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


def test_render_final_builds_ducking_graph(tmp_path: Path, monkeypatch):
    # Step 2 (product fix 1): render_final builds its own ducking graph instead of ffmpeg.mix_burn.
    cut = make_video(tmp_path / "cut.mp4", seconds=2.0, width=320, height=568)
    voice = make_tone(tmp_path / "voice.wav", seconds=1.0)
    ass = tmp_path / "captions.ass"
    ass.write_text("x", encoding="utf-8")
    seen: dict = {}

    def fake_run(args, **kw):
        seen.update(args=[str(a) for a in args], **kw)
        Path(args[-1]).write_bytes(b"x")

    monkeypatch.setattr(render.ffmpeg, "run", fake_run)
    monkeypatch.setattr(render, "resolve_fonts_dir", lambda: None)
    dst = tmp_path / "en" / "final.mp4"
    out = render_final(cut=cut, voice=voice, ass=ass, dst=dst,
                       audio=AudioSettings(original_volume=0.2, voice_volume=1.1, mix="gain", short_voice="keep"))
    assert out == dst and dst.is_file()
    args = seen["args"]
    assert args[args.index("-i") + 1] == str(cut) and str(voice) in args
    graph = args[args.index("-filter_complex") + 1]
    assert "ass=filename=" in graph and "fontsdir" not in graph
    assert "volume=1.1[a1]" in graph and ",0.2," in graph and ":eval=frame" in graph
    assert float(args[args.index("-t") + 1]) == pytest.approx(2.0, abs=0.1)
    assert seen["total"] == pytest.approx(2.0, abs=0.1)


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


# ------------------------------------------------------------------------------------ FAILURES-5


def test_voice_shorten_failure_falls_back_to_tempo_and_trim(tmp_path: Path, caplog):
    from autoclipper.llm import LLMRefusal

    def refusing(text: str, max_words: int) -> str:
        raise LLMRefusal("Claude declined this request")

    vb = FakeVoicebox(0.3)
    msgs: list[str] = []
    with caplog.at_level("WARNING"):
        r = make_voice(client=vb, narration=words(20), lang="en", voice=EN, clip_duration=3.0,
                       out_dir=tmp_path, max_speedup=1.25, shorten=refusing,
                       on_progress=lambda f, m: msgs.append(m))
    assert r.trimmed and not r.shortened and r.tempo == 1.25
    assert r.narration == words(20) and len(vb.calls) == 1
    assert r.final_duration == pytest.approx(3.0, abs=0.05)
    assert any("shortening failed: Claude declined" in m and "trimming instead" in m for m in msgs)
    assert "shortening failed" in caplog.text


# ------------------------------------------------------------------------------------ HINDI_CAPTIONS-1

HI22 = ("यह वीडियो आपको दिखाता है कि कैसे एक छोटा सा बदलाव आपकी पूरी ज़िंदगी को हमेशा के लिए "
        "बदल सकता है दोस्तों")


class _PrefixTranscriber:
    """Whisper on a trimmed take only hears the words that were actually spoken."""

    def __init__(self, heard: list[str], step: float = 0.4):
        self.words = [Word(start=i * step, end=i * step + step - 0.02, text=w) for i, w in enumerate(heard)]

    def transcribe(self, audio, language=None, on_progress=None):
        from autoclipper.models import Segment, Transcript

        return Transcript(language=language or "hi", duration=4.0, segments=[
            Segment(start=0, end=4, text=" ".join(w.text for w in self.words), words=self.words)])


def test_estimate_spoken_prefix():
    from autoclipper.stages.voice import estimate_spoken_prefix

    text = "aa bb cc dd"  # equal weights: each word 1/4 of the spoken time
    assert estimate_spoken_prefix(text, 8.0, 1.0, 4.0) == "aa bb"
    assert estimate_spoken_prefix(text, 8.0, 2.0, 4.0) == text
    assert estimate_spoken_prefix(text, 80.0, 1.0, 1.0) == "aa"  # never empty


def test_trimmed_voice_records_spoken_narration(tmp_path: Path):
    assert len(HI22.split()) == 22
    r = make_voice(client=FakeVoicebox(seconds_per_word=0.5), narration=HI22, lang="hi", voice=HI,
                   clip_duration=4.0, out_dir=tmp_path, max_speedup=1.25, shorten=lambda t, n: t)
    assert r.trimmed and r.tempo == 1.25
    assert r.narration == HI22 and r.narration_hash == narration_hash(HI22)  # unchanged
    spoken = r.spoken_narration.split()
    assert 8 <= len(spoken) <= 11 and HI22.startswith(r.spoken_narration)
    assert load_voice_result(tmp_path).spoken_narration == r.spoken_narration


def test_untrimmed_voice_has_no_spoken_narration_and_old_json_validates(tmp_path: Path):
    r = make_voice(client=FakeVoicebox(0.3), narration=words(5), lang="en", voice=EN, clip_duration=5.0,
                   out_dir=tmp_path, max_speedup=1.25)
    assert r.spoken_narration is None
    old = r.model_dump()
    del old["spoken_narration"]
    (tmp_path / "voice.json").write_text(json.dumps(old), encoding="utf-8")
    assert load_voice_result(tmp_path).spoken_narration is None


def _no_zero_length_events(ass: str) -> None:
    def cs(t: str) -> int:
        h, m, s = t.split(":")
        return round((int(h) * 3600 + int(m) * 60 + float(s)) * 100)

    for line in _dialogues(ass):
        start, end = line.split(",")[1:3]
        assert cs(end) >= cs(start) + 1, line


def test_trimmed_22_words_4s_captions_only_spoken_words(tmp_path: Path):
    r = make_voice(client=FakeVoicebox(seconds_per_word=0.5), narration=HI22, lang="hi", voice=HI,
                   clip_duration=4.0, out_dir=tmp_path, max_speedup=1.25, shorten=lambda t, n: t)
    heard = HI22.split()[:10]  # 4 s * 1.25 / 0.5 s per word
    ass = make_captions(narration=r.narration, voice_wav=Path(r.path), lang="hi",
                        transcriber=_PrefixTranscriber(heard), settings=Settings(), out_dir=tmp_path)
    ws = json.loads((tmp_path / "words.json").read_text(encoding="utf-8"))
    texts = [w["text"] for w in ws]
    assert texts == heard[:len(texts)] and 8 <= len(texts) <= 10
    assert "ज़िंदगी" not in texts and "दोस्तों" not in texts  # never spoken
    assert all(w["end"] <= 4.05 for w in ws)
    _no_zero_length_events(ass.read_text(encoding="utf-8"))


def test_trimmed_22_words_without_recognition_spreads_only_spoken_prefix(tmp_path: Path):
    r = make_voice(client=FakeVoicebox(seconds_per_word=0.5), narration=HI22, lang="hi", voice=HI,
                   clip_duration=4.0, out_dir=tmp_path, max_speedup=1.25)
    make_captions(narration=r.narration, voice_wav=Path(r.path), lang="hi", transcriber=FakeTranscriber(),
                  settings=Settings(), out_dir=tmp_path)
    ws = json.loads((tmp_path / "words.json").read_text(encoding="utf-8"))
    assert " ".join(w["text"] for w in ws) == r.spoken_narration
    assert len(ws) < 12


def test_captions_ignore_voice_json_for_other_narration(tmp_path: Path):
    make_voice(client=FakeVoicebox(seconds_per_word=0.5), narration=HI22, lang="hi", voice=HI,
               clip_duration=4.0, out_dir=tmp_path, max_speedup=1.25)
    other = "बिलकुल अलग नया पाठ"
    make_captions(narration=other, voice_wav=tmp_path / "voice.wav", lang="hi", transcriber=FakeTranscriber(),
                  settings=Settings(), out_dir=tmp_path)
    ws = json.loads((tmp_path / "words.json").read_text(encoding="utf-8"))
    assert [w["text"] for w in ws] == other.split()


def test_align_trimmed_drops_words_after_last_match():
    script = "one two three four five six".split()
    rec = [Word(start=0.0, end=0.4, text="one"), Word(start=0.5, end=0.9, text="two"),
           Word(start=1.0, end=1.4, text="three")]
    full = align_words(script, rec, 4.0)
    assert [w.text for w in full] == script
    cut = align_words(script, rec, 4.0, trimmed=True)
    assert [w.text for w in cut] == ["one", "two", "three"]


def test_build_ass_never_writes_zero_length_events():
    ws = [Word(start=1.004, end=1.004, text="a"), Word(start=1.006, end=1.006, text="b"),
          Word(start=1.006, end=1.009, text="c"), Word(start=3.995, end=3.995, text="d")]
    ass = build_ass(ws, lang="en", settings=CaptionSettings(words_per_line=2))
    _no_zero_length_events(ass)
    assert len(_dialogues(ass)) == 4


# ------------------------------------------------------------------------------------ HINDI_CAPTIONS-2


@pytest.mark.parametrize("a,b", [("ज़िंदगी", "जिंदगी"), ("हिन्दी", "हिंदी"), ("फ़ोन", "फोन"),
                                 ("हँसी", "हंसी"), ("क़ीमत", "कीमत")])
def test_norm_folds_devanagari_variants(a, b):
    assert a != b
    assert captions._norm(a) == captions._norm(b) != ""


def test_norm_strips_zero_width_and_keeps_distinct_words():
    assert captions._norm("क्‍या") == captions._norm("क्या")  # ZWJ
    assert captions._norm("क्‌ष") == captions._norm("क्ष")  # ZWNJ
    assert captions._norm("काम") != captions._norm("कम")
    assert captions._norm("Hello,") == "hello"
    # a nasal + virama that is not followed by a consonant stays as written
    assert captions._norm("न्") != captions._norm("ं")


def test_align_hindi_spelling_variants_seven_words():
    script = "ज़िंदगी फ़ोन क़ीमत हिन्दी ख़ास ज़रूर है".split()
    heard = "जिंदगी फोन कीमत हिंदी खास जरूर है".split()
    rec = [Word(start=i * 1.0, end=i * 1.0 + 0.3, text=w) for i, w in enumerate(heard)]
    a = [captions._norm(w) for w in script]
    b = [captions._norm(w.text) for w in rec]
    assert sum(x == y for x, y in zip(a, b)) / len(script) >= 0.3
    out = align_words(script, rec, 12.0)
    assert [w.text for w in out] == script  # display text unchanged
    assert out[-1].text == "है" and out[-1].start == pytest.approx(6.0, abs=0.05)
    assert [w.start for w in out] == pytest.approx([0, 1, 2, 3, 4, 5, 6])


# ------------------------------------------------------------------------------------ FAILURES-7


def test_make_captions_survives_voice_transcription_failure(tmp_path: Path, caplog):
    from autoclipper.stages.transcribe import TranscribeError

    class Broken:
        def transcribe(self, audio, language=None, on_progress=None):
            raise TranscribeError("could not decode audio voice.wav")

    voice = make_tone(tmp_path / "voice.wav", seconds=2.0)
    with caplog.at_level("WARNING"):
        ass = make_captions(narration="alpha beta gamma", voice_wav=voice, lang="en", transcriber=Broken(),
                            settings=Settings(), out_dir=tmp_path)
    assert ass.is_file() and len(_dialogues(ass.read_text(encoding="utf-8"))) == 3
    ws = json.loads((tmp_path / "words.json").read_text(encoding="utf-8"))
    assert [w["text"] for w in ws] == ["alpha", "beta", "gamma"]
    assert ws[-1]["end"] == pytest.approx(2.0, abs=0.05)
    assert "voice transcription failed" in caplog.text


def test_make_captions_survives_unexpected_transcriber_error(tmp_path: Path):
    class Boom:
        def transcribe(self, audio, language=None, on_progress=None):
            raise RuntimeError("CUDA driver crashed")

    voice = make_tone(tmp_path / "voice.wav", seconds=1.0)
    assert make_captions(narration="hi there", voice_wav=voice, lang="en", transcriber=Boom(),
                         settings=Settings(), out_dir=tmp_path).is_file()


# ------------------------------------------------------------------------------------ FAILURES-3


def _boom(*a, **k):
    raise ValueError("cannot serialize")


def test_captions_writers_leave_no_partial_file(tmp_path: Path, monkeypatch):
    voice = make_tone(tmp_path / "voice.wav", seconds=1.0)
    out = tmp_path / "en"
    make_captions(narration="alpha beta", voice_wav=voice, lang="en", transcriber=FakeTranscriber(),
                  settings=Settings(), out_dir=out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    monkeypatch.setattr(captions.json, "dumps", _boom)
    with pytest.raises(ValueError):
        make_captions(narration="gamma delta", voice_wav=voice, lang="en", transcriber=FakeTranscriber(),
                      settings=Settings(), out_dir=out)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    monkeypatch.undo()
    monkeypatch.setattr(captions, "build_ass", _boom)
    with pytest.raises(ValueError):
        make_captions(narration="gamma delta", voice_wav=voice, lang="en", transcriber=FakeTranscriber(),
                      settings=Settings(), out_dir=out)
    assert (out / "captions.ass").read_bytes() == before["captions.ass"]
    assert sorted(p.name for p in out.iterdir()) == ["captions.ass", "words.json"]


def test_voice_json_writer_leaves_no_partial_file(tmp_path: Path, monkeypatch):
    from autoclipper.stages import voice as voice_mod

    monkeypatch.setattr(voice_mod.json, "dumps", _boom)
    with pytest.raises(ValueError):
        make_voice(client=FakeVoicebox(0.3), narration=words(5), lang="en", voice=EN, clip_duration=5.0,
                   out_dir=tmp_path, max_speedup=1.25)
    assert not (tmp_path / "voice.json").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_metadata_writer_is_atomic(tmp_path: Path, monkeypatch):
    s = ClipScript(narration="n", title="t", description="d", hashtags=["#a"])
    dst = tmp_path / "out" / "metadata.json"
    write_metadata(s, "en", dst)
    before = dst.read_bytes()
    monkeypatch.setattr(render.json, "dumps", _boom)
    with pytest.raises(ValueError):
        write_metadata(s, "en", dst)
    assert dst.read_bytes() == before and sorted(p.name for p in dst.parent.iterdir()) == ["metadata.json"]
