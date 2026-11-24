"""M2: sentence-by-sentence narration (voice_chunks + make_voice chunking). Fakes only - no Voicebox, no network."""
from __future__ import annotations

import json
import wave
from pathlib import Path

import httpx
import pytest
from fakes import FakeLLM, FakeTranscriber, make_tone, make_video
from pydantic import ValidationError
from test_web_support import BASE, api, assert_error_shape, read_json  # noqa: F401

from autoclipper import ffmpeg
from autoclipper.config import Settings, VoiceboxSettings, VoiceProfile, save_settings, settings_path
from autoclipper.models import ClipScript, Word
from autoclipper.stages.captions import make_captions
from autoclipper.stages.voice import VoiceResult, load_voice_result, make_voice, narration_hash
from autoclipper.stages.voice_chunks import (ChunkOptions, chunk_overlong, chunk_truncated, concat_wavs,
                                             derive_seed, normalize_chunk, split_narration, split_sentences,
                                             spoken_words, wav_seconds)
from autoclipper.voicebox import VoiceboxClient, VoiceboxError, VoiceboxTimeout

HI = VoiceProfile(profile_id="fake-hi", engine="chatterbox")
EN = VoiceProfile(profile_id="fake-en", engine="chatterbox")
WPS = 3.0

# Seven Hindi sentences, ~115 words (shape of the clip-00 narration that came back 10 s long)
HINDI = (
    "सोचो, अगर एक घंटे के खेल में जो भी गाड़ी तुम उठाओगे, वही असली दुनिया में तुम्हारे घर के बाहर खड़ी मिलेगी। "
    "यही शर्त है आज के खिलाड़ी के सामने। "
    "घड़ी शुरू होते ही वो सीधा एक बड़ा ट्रक उठा लेता है, लेकिन ये घर के लिए नहीं, बल्कि पैसे जुटाने का तरीका है। "
    "अब असली मोड़ सुनो, इस ट्रक की कीमत करीब बीस हजार डॉलर है, यानी एक घंटे में खेल के अंदर इतने पैसे कमाने होंगे। "
    "और उसका प्लान क्या है? "
    "सीधा पास की एक छोटी दुकान पर धावा बोलना, बिना किसी तैयारी के, बस काउंटर के पीछे कूदो और तिजोरी खोलो। "
    "पर एक बात तय है, नई गाड़ी का पूरा सपना अब उसी दुकान की कमाई पर टिका हुआ है।")


def norm(t: str) -> str:
    return " ".join(t.split())


# ================================================================================ splitting
def test_split_hindi_danda_and_question_mark():
    s = split_sentences("पहला वाक्य है। दूसरा वाक्य? तीसरा भी! चौथा॥ पाँचवाँ")
    assert s == ["पहला वाक्य है।", "दूसरा वाक्य?", "तीसरा भी!", "चौथा॥", "पाँचवाँ"]


def test_split_free_standing_danda_stays_with_its_sentence():
    assert split_sentences("यह पहला है । यह दूसरा है ।") == ["यह पहला है ।", "यह दूसरा है ।"]
    assert spoken_words("यह पहला है ।") == 3  # the danda is not a word


def test_split_english_abbreviations_initials_and_numbers():
    text = ('Dr. Smith met Mr. J. R. Tolkien in the U.S. at 3.5 p.m. today. He paid $20,000.50 for it. '
            'Wow! "Really?" she asked. Prices rose, e.g. rent, etc. and food. The end... finally. Done')
    assert split_sentences(text) == [
        "Dr. Smith met Mr. J. R. Tolkien in the U.S. at 3.5 p.m. today.", "He paid $20,000.50 for it.", "Wow!",
        '"Really?" she asked.', "Prices rose, e.g. rent, etc. and food.", "The end... finally.", "Done"]


def test_split_closing_quotes_belong_to_the_sentence():
    assert split_sentences('He said "stop." Then left.') == ['He said "stop."', "Then left."]
    assert split_sentences("उसने कहा, “रुको।” फिर चला गया।") == ["उसने कहा, “रुको।”", "फिर चला गया।"]


def test_split_narration_merges_short_pieces_and_caps_long_ones():
    chunks = split_narration(HINDI, max_chars=150, min_chars=60)
    assert all(len(c) <= 150 for c in chunks)
    assert norm(" ".join(chunks)) == norm(HINDI)
    # "यही शर्त है आज के खिलाड़ी के सामने।" (short) is merged with a neighbour, so it is not a call of its own
    assert not any(c == "यही शर्त है आज के खिलाड़ी के सामने।" for c in chunks)
    assert 4 <= len(chunks) <= 7
    long_sentence = ", ".join(["यह एक बहुत लंबा हिस्सा है"] * 20) + "।"
    pieces = split_narration(long_sentence, max_chars=100, min_chars=10)
    assert len(pieces) > 1 and all(len(p) <= 100 for p in pieces) and norm(" ".join(pieces)) == norm(long_sentence)
    # no commas at all: split between words
    words = " ".join(["शब्द"] * 80)
    pieces = split_narration(words, max_chars=60, min_chars=0)
    assert all(len(p) <= 60 for p in pieces) and norm(" ".join(pieces)) == words


ENGLISH = ("Imagine this: whatever you pick up in the game in one hour, someone buys it for real, for your dad. "
           "That is the deal, and the clock is already ticking. He grabs the first truck he sees, but there is "
           "a catch. He has to earn enough cash to match its real price before time runs out. So what is the plan? "
           "Straight to a shop, behind the counter, like he has done this before. One hour, one truck, one lucky "
           "father. And honestly? Start clearing out the garage.")


@pytest.mark.parametrize("text", [HINDI, ENGLISH, "One. Two. Three.", "x", "", "   ", "a? b! c. d", "।।।",
                                  "A long first sentence that is well over the minimum. Short. Tiny."])
def test_split_narration_never_loses_text(text):
    for max_chars, min_chars in ((50, 10), (150, 60), (180, 30), (100, 99), (40, 0)):
        chunks = split_narration(text, max_chars=max_chars, min_chars=min_chars)
        assert norm(" ".join(chunks)) == norm(text), (max_chars, min_chars)
        assert all(c.strip() for c in chunks)


def test_split_narration_a_short_question_travels_with_its_answer():
    chunks = split_narration(ENGLISH, max_chars=150, min_chars=60)
    assert any(c.startswith("So what is the plan? Straight to a shop") for c in chunks)
    assert all(len(c) >= 60 for c in chunks)


def test_chunk_checks_are_devanagari_aware():
    t = "यह एक छोटा सा वाक्य है ।"  # 6 words + a free-standing danda
    assert spoken_words(t) == 6
    assert chunk_truncated(t, 0.5, WPS) and not chunk_truncated(t, 1.6, WPS)
    assert chunk_overlong(t, 12.7, WPS) and not chunk_overlong(t, 3.0, WPS)
    assert not chunk_truncated("दो शब्द", 0.1, WPS)  # too few words to judge
    assert not chunk_truncated(t, 0.1, None)  # no rate, no check


# ================================================================================ audio helpers
def test_concat_wavs_durations_and_spans(tmp_path):
    files = []
    for i, sec in enumerate((1.0, 2.0, 0.5)):
        src = make_tone(tmp_path / f"t{i}.src.wav", seconds=sec)
        files.append(normalize_chunk(src, tmp_path / f"t{i}.wav", sample_rate=44100, trim_edges=False))
    spans = concat_wavs(files, tmp_path / "joined.wav", gap_seconds=0.2)
    assert wav_seconds(tmp_path / "joined.wav") == pytest.approx(3.9, abs=0.01)
    assert [round(s, 2) for s, _ in spans] == [0.0, 1.2, 3.4]
    assert [round(d, 2) for _, d in spans] == [1.0, 2.0, 0.5]
    assert not list(tmp_path.glob("*.part.wav"))


def test_concat_rejects_mixed_formats(tmp_path):
    a = make_tone(tmp_path / "a.wav", seconds=0.5, sample_rate=24000)
    b = make_tone(tmp_path / "b.wav", seconds=0.5, sample_rate=16000)
    with pytest.raises(ValueError):
        concat_wavs([a, b], tmp_path / "j.wav", gap_seconds=0.1)
    assert not (tmp_path / "j.wav").exists()


def test_normalize_chunk_trims_edge_silence(tmp_path):
    src = tmp_path / "padded.wav"
    ffmpeg.run(["-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=24000:duration=1", "-filter_complex",
                "[0]atrim=0:0.8[a];[0]atrim=0:1.0[c];[a][1][c]concat=n=3:v=0:a=1", "-c:a", "pcm_s16le", str(src)])
    assert wav_seconds(src) == pytest.approx(2.8, abs=0.02)
    out = normalize_chunk(src, tmp_path / "n.wav", sample_rate=44100, trim_edges=True)
    assert 1.0 <= wav_seconds(out) <= 1.2
    with wave.open(str(out)) as w:
        assert (w.getframerate(), w.getnchannels()) == (44100, 1)
    # a silent chunk is not trimmed to nothing
    silent = tmp_path / "silent.wav"
    ffmpeg.run(["-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "1", "-c:a", "pcm_s16le", str(silent)])
    assert wav_seconds(normalize_chunk(silent, tmp_path / "s.wav", sample_rate=44100)) == pytest.approx(1.0, abs=0.02)


# ================================================================================ fakes
class ChunkVoicebox:
    """Speaks at ``wps`` words/s; ``plan`` maps (call number) -> 'short' | 'long' | 'timeout' | 'fail'.
    ``stop_after_chars`` mimics Chatterbox on long input: only the first N characters get spoken."""

    def __init__(self, wps: float = WPS, plan: dict[int, str] | None = None, stop_after_chars: int | None = None,
                 bad_text: dict[str, str] | None = None):
        self.wps = wps
        self.plan = plan or {}
        self.bad_text = bad_text or {}
        self.stop_after_chars = stop_after_chars
        self.calls: list[dict] = []

    def health(self):
        return None

    def list_profiles(self):
        return [{"id": "fake-hi"}]

    def synthesize(self, text, *, profile_id, language, engine, dst, on_progress=None, timeout=None, seed=None):
        self.calls.append(dict(text=text, profile_id=profile_id, language=language, engine=engine,
                               timeout=timeout, seed=seed))
        what = self.plan.get(len(self.calls)) or self.bad_text.get(norm(text))
        if on_progress:
            on_progress(0.5, "generating")
        if what == "timeout":
            raise VoiceboxTimeout(f"did not finish within {timeout:.0f}s")
        if what == "fail":
            raise VoiceboxError("generation failed: boom")
        spoken = text if self.stop_after_chars is None else text[: self.stop_after_chars]
        sec = spoken_words(spoken) / self.wps
        if what == "short":
            sec *= 0.2
        elif what == "long":
            sec = sec * 5 + 3
        return make_tone(Path(dst), seconds=max(0.3, sec))


def opts(**kw) -> ChunkOptions:
    base = dict(max_chars=150, min_chars=60, gap_seconds=0.2, timeout=240.0, retry_timeout=120.0, seed=None)
    base.update(kw)
    return ChunkOptions(**base)


def voice(tmp_path, vb, text=HINDI, clip=40.0, chunking=None, lang="hi", **kw):
    return make_voice(client=vb, narration=text, lang=lang, voice=HI if lang == "hi" else EN, clip_duration=clip,
                      out_dir=tmp_path, max_speedup=1.25, wps=WPS, chunking=chunking, **kw)


def hindi_seconds(text=HINDI) -> float:
    return spoken_words(text) / WPS


# ================================================================================ make_voice chunked
def test_regression_clip00_long_hindi_take_cut_short_without_chunking(tmp_path):
    """Before M2: one Voicebox call for the whole text; Chatterbox stops after ~150 characters."""
    vb = ChunkVoicebox(stop_after_chars=150)
    r = voice(tmp_path, vb, chunking=None)
    assert len(vb.calls) == 2  # the take + the one whole-take retry
    assert r.truncated and r.raw_duration < 0.5 * hindi_seconds() and r.chunks == []
    assert "cut the narration short twice" in r.warnings[0]


def test_regression_clip00_chunked_take_spans_the_clip(tmp_path):
    vb = ChunkVoicebox(stop_after_chars=150)  # the same engine, but every chunk is short enough
    chunks = split_narration(HINDI, max_chars=150, min_chars=60)
    r = voice(tmp_path, vb, chunking=opts(max_chars=150))
    assert [c["text"] for c in vb.calls] == chunks
    assert not r.truncated and r.warnings == [] and len(r.chunks) == len(chunks) > 1
    gaps = 0.2 * (len(chunks) - 1)
    assert r.raw_duration == pytest.approx(hindi_seconds() + gaps, abs=0.3)
    assert r.raw_duration > 0.85 * 38.25 * 0.9  # spans the 38 s clip instead of 10 s
    # every call: same profile, language, engine and seed (a consistent voice); per-chunk timeout
    assert {(c["profile_id"], c["language"], c["engine"], c["seed"], c["timeout"]) for c in vb.calls} == {
        ("fake-hi", "hi", "chatterbox", derive_seed("fake-hi", "hi"), 240.0)}
    data = json.loads((tmp_path / "voice.json").read_text(encoding="utf-8"))
    assert data["chunk_gap"] == 0.2 and len(data["chunks"]) == len(chunks)
    for a, b in zip(data["chunks"], data["chunks"][1:]):
        assert b["start"] == pytest.approx(a["start"] + a["duration"] + 0.2, abs=0.01)
    assert set(data["chunks"][0]) >= {"index", "text", "words", "start", "duration", "expected", "attempts",
                                      "cached", "truncated", "overlong"}
    assert (tmp_path / "voice_raw.wav").is_file() and not list(tmp_path.glob("voice_norm*.wav"))


def test_chunk_cut_short_is_retried_once_with_shorter_timeout_and_new_seed(tmp_path):
    vb = ChunkVoicebox(plan={2: "short"})
    r = voice(tmp_path, vb, chunking=opts(seed=7))
    n = len(split_narration(HINDI, max_chars=150, min_chars=60))
    assert len(vb.calls) == n + 1
    assert vb.calls[1]["text"] == vb.calls[2]["text"]
    assert (vb.calls[1]["timeout"], vb.calls[1]["seed"]) == (240.0, 7)
    assert (vb.calls[2]["timeout"], vb.calls[2]["seed"]) == (120.0, 8)
    assert not r.truncated and r.warnings == [] and r.chunks[1].attempts == 2 and not r.chunks[1].truncated
    assert r.raw_duration == pytest.approx(hindi_seconds() + 0.2 * (n - 1), abs=0.3)


def test_chunk_cut_short_twice_is_kept_with_a_warning(tmp_path):
    first, second = split_narration(HINDI, max_chars=150, min_chars=60)[:2]
    vb = ChunkVoicebox(bad_text={norm(second): "short"})
    r = voice(tmp_path, vb, chunking=opts(), lengthen=lambda t, n: pytest.fail("never lengthened"))
    assert r.truncated and r.chunks[1].truncated and r.chunks[1].attempts == 2
    assert len(r.warnings) == 1 and "sentence 2/" in r.warnings[0] and "short twice" in r.warnings[0]
    # captions: the cut sentence contributes only its spoken prefix, the other sentences stay whole
    spoken = r.spoken_narration.split()
    assert spoken[:len(first.split())] == first.split()
    assert len(spoken) < len(HINDI.split()) and spoken[-3:] == HINDI.split()[-3:]
    data = load_voice_result(tmp_path)
    assert data is not None and data.truncated and data.warnings == r.warnings


def test_chunk_far_too_long_is_retried_and_the_better_take_kept(tmp_path):
    vb = ChunkVoicebox(plan={1: "long"})
    r = voice(tmp_path, vb, chunking=opts())
    assert r.chunks[0].attempts == 2 and not r.chunks[0].overlong and r.warnings == []
    vb2 = ChunkVoicebox(plan={1: "long", 2: "long"})
    r2 = voice(tmp_path / "b", vb2, chunking=opts())
    assert r2.chunks[0].overlong and "far too long twice" in r2.warnings[0] and not r2.truncated


def test_chunk_timeout_is_retried_then_fails_cleanly(tmp_path):
    vb = ChunkVoicebox(plan={1: "timeout"})
    r = voice(tmp_path, vb, chunking=opts(timeout=30.0, retry_timeout=15.0))
    assert [c["timeout"] for c in vb.calls[:2]] == [30.0, 30.0]  # a retry after a timeout gets the full wait
    assert r.chunks[0].attempts == 2 and r.warnings == []
    vb2 = ChunkVoicebox(plan={1: "timeout", 2: "fail"})
    with pytest.raises(VoiceboxError, match=r"chunk 1/\d+ failed twice"):
        voice(tmp_path / "b", vb2, chunking=opts(timeout=30.0))
    assert not (tmp_path / "b" / "voice.json").exists()
    assert not list((tmp_path / "b" / "voice_chunks").glob("*.try*"))


def test_lengthen_timeout_caps_every_chunk(tmp_path):
    vb = ChunkVoicebox()
    short = "यह छोटा वाक्य है और इसमें कुछ ही शब्द हैं।"
    longer = short + " " + "यह दूसरा वाक्य थोड़ा लंबा है ताकि आवाज़ पूरी क्लिप भर सके।"
    r = voice(tmp_path, vb, text=short, clip=8.0, chunking=opts(), lengthen=lambda t, n: longer,
              lengthen_timeout=50.0)
    # the 50 s budget is shared by the lengthened take's chunks: each waits at most what is left of it
    assert r.lengthened and all(49.0 < c["timeout"] <= 50.0 for c in vb.calls[1:])


def test_progress_reports_each_chunk_and_cancel_cleans_up(tmp_path):
    msgs: list[tuple[float, str]] = []
    r = voice(tmp_path, ChunkVoicebox(), chunking=opts(), on_progress=lambda f, m="": msgs.append((f, m)))
    n = len(r.chunks)
    assert [f for f, _ in msgs] == sorted(f for f, _ in msgs)
    for i in range(1, n + 1):
        assert any(f"chunk {i}/{n}" in m for _, m in msgs)

    class Stop(BaseException):
        pass

    def cancel(f, m=""):
        if "chunk 2/" in m:
            raise Stop()

    out = tmp_path / "cancelled"
    with pytest.raises(Stop):
        voice(out, ChunkVoicebox(), chunking=opts(), on_progress=cancel)
    assert not (out / "voice.json").exists() and not (out / "voice.wav").exists()
    assert not list((out / "voice_chunks").glob("*.try*"))


def test_unchanged_sentences_are_reused_on_an_edited_narration(tmp_path):
    vb = ChunkVoicebox()
    r1 = voice(tmp_path, vb, chunking=opts())
    n = len(r1.chunks)
    edited = HINDI.replace("और उसका प्लान क्या है?", "और उसकी योजना क्या है?")
    vb2 = ChunkVoicebox()
    r2 = voice(tmp_path, vb2, text=edited, chunking=opts())
    assert len(vb2.calls) == 1 and "योजना" in vb2.calls[0]["text"]
    assert sum(c.cached for c in r2.chunks) == n - 1
    # only the chunks of the current take stay in the cache
    assert len(list((tmp_path / "voice_chunks").glob("*.wav"))) == len(r2.chunks)
    # the same narration voiced again (voice.wav deleted) is a fresh take, not the cached one
    (tmp_path / "voice.wav").unlink()
    vb3 = ChunkVoicebox()
    voice(tmp_path, vb3, text=edited, chunking=opts())
    assert len(vb3.calls) == len(r2.chunks)
    # cache off: no chunk folder left
    vb4 = ChunkVoicebox()
    voice(tmp_path / "nocache", vb4, chunking=ChunkOptions(cache=False))
    assert not (tmp_path / "nocache" / "voice_chunks").exists()


def test_old_style_fake_without_timeout_or_seed_still_works(tmp_path):
    from fakes import FakeVoicebox

    vb = FakeVoicebox(seconds_per_word=1 / WPS)
    r = voice(tmp_path, vb, chunking=opts())
    assert len(vb.calls) == len(r.chunks) > 1 and r.warnings == []


def test_chunked_take_still_fits_the_clip_with_tempo(tmp_path):
    vb = ChunkVoicebox()
    r = voice(tmp_path, vb, clip=hindi_seconds() / 1.15, chunking=opts())
    assert 1.0 < r.tempo <= 1.25 and not r.trimmed
    assert r.final_duration == pytest.approx(r.clip_duration, abs=0.05)


def test_old_voice_json_without_chunks_still_validates():
    old = {"path": "/x/voice.wav", "narration": "a", "narration_hash": narration_hash("a"), "raw_duration": 1.0,
           "final_duration": 1.0, "clip_duration": 2.0, "tempo": 1.0, "shortened": False, "trimmed": False}
    r = VoiceResult.model_validate(old)
    assert r.chunks == [] and r.chunk_gap is None


# ================================================================================ captions on joined audio
def test_captions_fallback_spreads_each_sentence_over_its_chunk(tmp_path):
    vb = ChunkVoicebox(plan={2: "long", 3: "long"})  # chunk 2 is kept far too long: uneven spans
    r = voice(tmp_path, vb, chunking=opts(), clip=80.0)
    make_captions(narration=HINDI, voice_wav=tmp_path / "voice.wav", lang="hi", transcriber=FakeTranscriber(),
                  settings=Settings(), out_dir=tmp_path)
    words = json.loads((tmp_path / "words.json").read_text(encoding="utf-8"))
    assert [w["text"] for w in words] == HINDI.split()
    k = 0
    for c in r.chunks:
        first = words[k]
        assert first["start"] == pytest.approx(c.start / r.tempo, abs=0.02)
        k += len(c.text.split())
        assert words[k - 1]["end"] == pytest.approx((c.start + c.duration) / r.tempo, abs=0.02)


def test_captions_use_recognized_timing_on_the_joined_voice(tmp_path):
    text = "पहला वाक्य यहाँ है और यह चलता है। दूसरा वाक्य भी यहाँ है और यह भी चलता है।"
    r = voice(tmp_path, ChunkVoicebox(), text=text, clip=20.0, chunking=opts(min_chars=0))
    assert len(r.chunks) == 2
    second_start = r.chunks[1].start
    rec = [Word(start=i * 0.3, end=i * 0.3 + 0.25, text=w) for i, w in enumerate(text.split()[:8])]
    rec += [Word(start=second_start + i * 0.3, end=second_start + i * 0.3 + 0.25, text=w)
            for i, w in enumerate(text.split()[8:])]
    make_captions(narration=text, voice_wav=tmp_path / "voice.wav", lang="hi",
                  transcriber=FakeTranscriber(words_for_voice=rec), settings=Settings(), out_dir=tmp_path)
    words = json.loads((tmp_path / "words.json").read_text(encoding="utf-8"))
    assert words[8]["start"] == pytest.approx(second_start, abs=0.01)
    assert max(w["end"] for w in words) <= r.final_duration + 0.01


# ================================================================================ client + settings
def test_voicebox_client_sends_seed_only_when_given():
    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/generate":
            bodies.append(json.loads(req.content))
            return httpx.Response(200, json={"id": "g1"})
        if req.url.path.startswith("/history/"):
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(200, content=b"RIFF")

    c = VoiceboxClient(transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    c.generate("hi", profile_id="p", language="hi", engine="chatterbox")
    c.generate("hi", profile_id="p", language="hi", engine="chatterbox", seed=42)
    assert "seed" not in bodies[0] and bodies[1]["seed"] == 42


def test_chunk_options_from_settings():
    vb = VoiceboxSettings(timeout_seconds=100, chunk_timeout_seconds=240, chunk_retry_timeout_seconds=60, seed=5)
    o = ChunkOptions.from_settings(vb)
    assert (o.timeout, o.retry_timeout, o.seed, o.max_chars, o.min_chars, o.gap_seconds) == (100, 60, 5, 150, 60, 0.2)
    assert ChunkOptions.from_settings(VoiceboxSettings(chunking=False)) is None
    s = Settings()
    assert s.voicebox.chunking is True and s.cross_field_errors() == {}
    bad = Settings.model_validate({"voicebox": {"chunk_min_chars": 200, "chunk_max_chars": 100}})
    assert "voicebox.chunk_min_chars" in bad.cross_field_errors()


@pytest.mark.parametrize("field,value", [
    ("chunk_max_chars", 0), ("chunk_max_chars", 39), ("chunk_max_chars", 10 ** 9), ("chunk_max_chars", "180"),
    ("chunk_max_chars", 180.5), ("chunk_min_chars", -1), ("chunk_gap_seconds", -0.1), ("chunk_gap_seconds", 2),
    ("chunk_timeout_seconds", 0), ("chunk_retry_timeout_seconds", 5), ("seed", -1), ("seed", 2 ** 31),
    ("seed", "x"), ("chunking", "yes"),
])
def test_settings_model_rejects_bad_chunk_values(field, value):
    with pytest.raises(ValidationError):
        VoiceboxSettings.model_validate({field: value}, strict=True)


@pytest.mark.parametrize("mutate,field", [
    (lambda v: v.update(chunk_max_chars=0), "voicebox.chunk_max_chars"),
    (lambda v: v.update(chunk_max_chars="big"), "voicebox.chunk_max_chars"),
    (lambda v: v.update(chunk_max_chars=10 ** 12), "voicebox.chunk_max_chars"),
    (lambda v: v.update(chunk_min_chars=-5), "voicebox.chunk_min_chars"),
    (lambda v: v.update(chunk_min_chars=180, chunk_max_chars=180), "voicebox.chunk_min_chars"),
    (lambda v: v.update(chunk_gap_seconds=-1), "voicebox.chunk_gap_seconds"),
    (lambda v: v.update(chunk_gap_seconds=99), "voicebox.chunk_gap_seconds"),
    (lambda v: v.update(chunk_timeout_seconds=0), "voicebox.chunk_timeout_seconds"),
    (lambda v: v.update(chunk_retry_timeout_seconds=-1), "voicebox.chunk_retry_timeout_seconds"),
    (lambda v: v.update(seed=-1), "voicebox.seed"),
    (lambda v: v.update(seed="1"), "voicebox.seed"),
    (lambda v: v.update(chunking="on"), "voicebox.chunking"),
    (lambda v: v.update(chunk_bogus=1), "voicebox.chunk_bogus"),
])
def test_put_settings_rejects_bad_chunk_values_with_422(api, mutate, field):
    client, _, _ = api
    before = settings_path().read_bytes()
    s = client.get("/api/settings").json()
    mutate(s["voicebox"])
    body = assert_error_shape(client.put("/api/settings", json=s), 422, "validation failed")
    assert field in body["errors"], body["errors"]
    assert settings_path().read_bytes() == before


def test_put_settings_saves_chunk_settings(api):
    client, _, _ = api
    s = client.get("/api/settings").json()
    assert {"chunking", "chunk_max_chars", "chunk_min_chars", "chunk_gap_seconds", "chunk_timeout_seconds",
            "chunk_retry_timeout_seconds", "seed"} <= set(s["voicebox"])
    assert s["voicebox"]["chunking"] is True and s["voicebox"]["seed"] is None
    s["voicebox"].update(chunking=False, chunk_max_chars=250, chunk_min_chars=0, chunk_gap_seconds=0.0,
                         chunk_timeout_seconds=60.0, chunk_retry_timeout_seconds=30.0, seed=123)
    r = client.put("/api/settings", json=s)
    assert r.status_code == 200, r.text
    assert r.json()["voicebox"] == s["voicebox"]
    assert read_json(settings_path())["voicebox"] == s["voicebox"]


def test_put_settings_chunk_fields_need_auth(api):
    from fastapi.testclient import TestClient

    from autoclipper.web.auth import TOKEN_HEADER

    client, _, _ = api
    s = client.get("/api/settings").json()
    s["voicebox"]["chunk_max_chars"] = 200
    bare = TestClient(client.app, base_url=BASE)
    before = settings_path().read_bytes()
    assert_error_shape(bare.put("/api/settings", json=s), 401)
    assert_error_shape(bare.put("/api/settings", json=s, headers={TOKEN_HEADER: "x"}), 403)
    assert settings_path().read_bytes() == before


# ================================================================================ pipeline
def _pipe_settings(**vb) -> Settings:
    s = Settings()
    s.voicebox.english.profile_id = "fake-en"
    s.voicebox.hindi.profile_id = "fake-hi"
    s.clips.count = 1
    s.clips.min_seconds = 10
    s.clips.max_seconds = 12
    for k, v in vb.items():
        setattr(s.voicebox, k, v)
    return s


def test_pipeline_generates_sentence_by_sentence_and_reports_it(tmp_path):
    from autoclipper.pipeline import Deps, run_job

    video = make_video(tmp_path / "src.mp4", seconds=30)
    s = _pipe_settings()
    s.clips.min_seconds = 20  # a 20 s clip: the ~10 s two-sentence narration fits as it is
    s.clips.max_seconds = 20
    save_settings(s)

    class TwoSentences(FakeLLM):
        def write_script(self, **kw):
            sc = super().write_script(**kw)
            return sc.model_copy(update={"narration": "This is the first sentence of the story, and it runs on for a good while "
                                                      "so that it stands alone as a chunk. And here is the second "
                                                      "sentence of it, which is also long enough to need a call of its own."})

    vb = ChunkVoicebox(wps=4.0)  # ~10 s of voice: fits the 10-12 s clip without shortening
    logs: list[str] = []
    store = run_job(str(video), settings=s, deps=Deps(llm=TwoSentences(), voicebox=vb,
                                                      transcriber=FakeTranscriber(duration=30)),
                    langs=["en"], log=logs.append)
    r = load_voice_result(store.clip_dir(0, "en"))
    assert r is not None and len(r.chunks) >= 2  # (the short take may also be lengthened once)
    assert all(len(c["text"]) <= s.voicebox.chunk_max_chars for c in vb.calls)
    assert any(f"{len(r.chunks)} sentences" in line for line in logs)
    assert vb.calls[0]["timeout"] == 240.0 and vb.calls[0]["seed"] == derive_seed("fake-en", "en")


def test_voice_is_current_regenerates_an_old_cut_short_take_only_with_chunking(tmp_path):
    from autoclipper.pipeline import _voice_is_current

    out = tmp_path / "hi"
    out.mkdir()
    make_tone(out / "voice.wav", seconds=10.0)
    old = {"path": str(out / "voice.wav"), "narration": HINDI, "narration_hash": narration_hash(HINDI),
           "raw_duration": 10.12, "final_duration": 10.12, "clip_duration": 38.25, "tempo": 1.0,
           "shortened": False, "trimmed": False, "lengthened": False, "spoken_narration": None}
    (out / "voice.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    script = ClipScript(narration=HINDI, title="t", description="d", hashtags=[])
    s = Settings()
    assert not _voice_is_current(out, script, s, "hi")  # cut short + made in one call: regenerate
    s.voicebox.chunking = False
    assert _voice_is_current(out, script, s, "hi")
    assert _voice_is_current(out, script)  # no settings: old behaviour
    full = dict(old, raw_duration=36.0)
    (out / "voice.json").write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")
    assert _voice_is_current(out, script, Settings(), "hi")


def test_captions_recognise_sentence_by_sentence_when_the_whole_voice_fails(tmp_path):
    """Whisper on a whole Hindi voice can drop long stretches; each chunk is then recognised on its own."""
    r = voice(tmp_path, ChunkVoicebox(), chunking=opts(), clip=60.0)
    chunk_words = [c.text.split() for c in r.chunks]

    class PerChunk(FakeTranscriber):
        def transcribe(self, audio, language=None, on_progress=None):
            from autoclipper.models import Segment, Transcript

            self.calls.append((str(audio), language))
            name = Path(audio).name
            if not name.startswith(".captions_chunk"):
                return Transcript(language="hi", duration=0.0, segments=[])  # whole voice: nothing usable
            ws = chunk_words[int(name[len(".captions_chunk"):-4])]
            words = [Word(start=0.1 + 0.3 * i, end=0.1 + 0.3 * i + 0.25, text=w) for i, w in enumerate(ws)]
            return Transcript(language="hi", duration=words[-1].end,
                              segments=[Segment(start=0.0, end=words[-1].end, text=" ".join(ws), words=words)])

    tr = PerChunk()
    make_captions(narration=HINDI, voice_wav=tmp_path / "voice.wav", lang="hi", transcriber=tr,
                  settings=Settings(), out_dir=tmp_path)
    assert len(tr.calls) == 1 + len(r.chunks)
    words = json.loads((tmp_path / "words.json").read_text(encoding="utf-8"))
    k = 0
    for c, ws in zip(r.chunks, chunk_words):
        assert words[k]["start"] == pytest.approx(c.start + 0.1, abs=0.01)
        assert words[k + 1]["start"] == pytest.approx(c.start + 0.4, abs=0.01)
        k += len(ws)
    assert not list(tmp_path.glob(".captions_chunk*"))


def test_clamp_to_segments_keeps_words_inside_their_chunk():
    from autoclipper.stages.captions import clamp_to_segments

    ws = [Word(start=0.0, end=1.0, text="a"), Word(start=1.0, end=2.8, text="b"),  # b runs into sentence 2
          Word(start=2.4, end=2.9, text="c"), Word(start=3.0, end=4.3, text="d")]  # c starts in the pause
    out = clamp_to_segments(ws, [(0.0, 2.0, 2), (2.6, 4.0, 2)])
    assert [(w.start, w.end) for w in out] == [(0.0, 1.0), (1.0, 2.6), (2.6, 2.9), (3.0, 4.3)]
    assert clamp_to_segments(ws, [(0.0, 2.0, 3)]) == ws  # counts differ: unchanged


# ================================================================================ review fixes (team H)
def test_split_devanagari_abbreviation_does_not_end_a_sentence():
    assert split_sentences("उनका नाम था डॉ. रमेश शर्मा और वे आए। प्रो. वर्मा भी थे.") == [
        "उनका नाम था डॉ. रमेश शर्मा और वे आए।", "प्रो. वर्मा भी थे."]
    assert split_sentences("यह ई.पू. में हुआ था. फिर सब बदला।") == ["यह ई.पू. में हुआ था.", "फिर सब बदला।"]
    assert split_sentences("वह घर गया है. फिर लौटा।") == ["वह घर गया है.", "फिर लौटा।"]  # "है." still ends one


def test_split_english_sentence_ending_on_an_abbreviation_or_initials():
    assert split_sentences("He lives in the U.S. It is big.") == ["He lives in the U.S.", "It is big."]
    assert split_sentences("We met at 5 p.m. Tomorrow we fly.") == ["We met at 5 p.m.", "Tomorrow we fly."]
    assert split_sentences("The answer is no. He left.") == ["The answer is no.", "He left."]
    assert split_sentences("They moved to D.C. He stayed.") == ["They moved to D.C.", "He stayed."]
    # no sentence opener after it: still one sentence
    assert split_sentences("The U.S. Army and No. 5 and J. R. Tolkien.") == ["The U.S. Army and No. 5 and J. R. Tolkien."]
    assert split_sentences("Dr. He is a name, e.g. The Beatles.") == ["Dr. He is a name, e.g. The Beatles."]


def test_glued_danda_gets_a_space_and_splits(tmp_path):
    from autoclipper.stages.voice_chunks import space_after_danda

    assert space_after_danda("वाक्य।दूसरा वाक्य॥तीसरा। “रुको।” ठीक।") == "वाक्य। दूसरा वाक्य॥ तीसरा। “रुको।” ठीक।"
    assert split_sentences(space_after_danda("पहला वाक्य।दूसरा वाक्य")) == ["पहला वाक्य।", "दूसरा वाक्य"]
    a = "यह पहला लंबा वाक्य है जो अपने आप में साठ अक्षरों से ज़्यादा लंबा होना चाहिए ताकि अलग रहे।"
    b = "और यह दूसरा लंबा वाक्य भी उतना ही लंबा है ताकि वह भी अपनी अलग आवाज़ में बने।"
    vb = ChunkVoicebox()
    r = make_voice(client=vb, narration=a + b, lang="hi", voice=HI, clip_duration=60.0, out_dir=tmp_path / "a",
                   max_speedup=1.25, wps=WPS, chunking=opts())
    assert [c["text"] for c in vb.calls] == [a, b] and r.narration == a + " " + b
    # one call per narration (chunking off): the text is left as it is
    r2 = make_voice(client=ChunkVoicebox(), narration=a + b, lang="hi", voice=HI, clip_duration=60.0,
                    out_dir=tmp_path / "b", max_speedup=1.25, wps=WPS)
    assert r2.narration == a + b


def test_short_unmergeable_chunk_is_still_checked_for_rambling(tmp_path):
    assert chunk_overlong("दो शब्द", 5.0, WPS) and not chunk_overlong("दो शब्द", 1.5, WPS)
    assert not chunk_truncated("दो शब्द", 0.1, WPS)  # a cut-short check needs more words
    vb = ChunkVoicebox(plan={1: "long"})
    r = voice(tmp_path, vb, text="नमस्ते दोस्तों।", clip=3.0, chunking=opts())
    assert len(vb.calls) == 2 and r.chunks[0].attempts == 2 and not r.chunks[0].overlong


def _lengthen_case():
    short = "यह छोटा वाक्य है और इसमें कुछ ही शब्द हैं।"
    extra = ["यह दूसरा वाक्य थोड़ा लंबा है ताकि आवाज़ पूरी क्लिप भर सके और कुछ और शब्द भी जुड़ें।",
             "तीसरा वाक्य भी लंबा है ताकि यह अपने आप में एक अलग हिस्सा बन जाए और अलग से बोला जाए।",
             "चौथा वाक्य कहानी को आगे बढ़ाता है और बताता है कि आगे क्या होने वाला है इस खेल में।",
             "पाँचवाँ वाक्य कहानी खत्म करता है और देखने वालों को अगले हिस्से के लिए तैयार करता है।"]
    longer = " ".join([short] + extra)
    return short, longer


def test_lengthened_take_with_a_chunk_cut_short_twice_is_not_used(tmp_path):
    """Reviewer finding 1: a cut-short chunk in the lengthened take must not slip through as truncated=False."""
    short, longer = _lengthen_case()
    pieces = split_narration(longer, max_chars=150, min_chars=60)
    assert len(pieces) >= 3
    vb = ChunkVoicebox(bad_text={norm(pieces[1]): "short"})
    clip = spoken_words(longer) / WPS + 2
    r = voice(tmp_path, vb, text=short, clip=clip, chunking=opts(), lengthen=lambda t, n: longer)
    assert len(vb.calls) > 2  # the lengthened take was generated (and its chunk 2 retried)
    assert not r.lengthened and r.narration == short and not r.truncated
    assert [c.text for c in r.chunks] == [short] and r.spoken_narration is None
    assert any("lengthened voice short twice, so it was not used" in w and "2/" in w for w in r.warnings)
    data = load_voice_result(tmp_path)
    assert data is not None and data.narration == short and not data.lengthened


def test_shortened_take_with_a_chunk_cut_short_is_marked_truncated(tmp_path):
    """Reviewer finding 1 (shorten side): the final take decides ``truncated`` and the spoken text."""
    shorter = " ".join(_lengthen_case()[1].split("।")[1].split()) + "। " + " ".join(_lengthen_case()[1].split("।")[2].split()) + "।"
    pieces = split_narration(shorter, max_chars=150, min_chars=60)
    vb = ChunkVoicebox(bad_text={norm(pieces[-1]): "short"})
    clip = spoken_words(shorter) / WPS + 1  # the full HINDI take is far too long for it
    r = voice(tmp_path, vb, clip=clip, chunking=opts(), shorten=lambda t, n: shorter)
    assert r.shortened and r.narration == shorter
    assert r.truncated and r.chunks[-1].truncated and r.spoken_narration
    assert len(r.spoken_narration.split()) < len(shorter.split())
    assert any("short twice" in w for w in r.warnings)


def test_lengthen_budget_is_shared_by_all_chunks(tmp_path, monkeypatch):
    """Reviewer finding 2: lengthen_timeout bounds the whole lengthened take, not each of its chunks."""
    import autoclipper.stages.voice as voice_mod

    now = [1000.0]
    monkeypatch.setattr(voice_mod, "_clock", lambda: now[0])

    class Slow(ChunkVoicebox):
        def synthesize(self, text, *, profile_id, language, engine, dst, on_progress=None, timeout=None,
                       seed=None):
            if timeout is not None and timeout < 30:
                self.calls.append(dict(text=text, timeout=timeout, seed=seed))
                now[0] += timeout
                raise VoiceboxTimeout(f"did not finish within {timeout:.0f}s")  # like the real client
            now[0] += 30.0  # every generation takes 30 s
            return super().synthesize(text, profile_id=profile_id, language=language, engine=engine, dst=dst,
                                      on_progress=on_progress, timeout=timeout, seed=seed)

    short, longer = _lengthen_case()
    n = len(split_narration(longer, max_chars=150, min_chars=60))
    assert n >= 4
    vb = Slow()
    clip = spoken_words(longer) / WPS + 2
    r = voice(tmp_path, vb, text=short, clip=clip, chunking=opts(), lengthen=lambda t, n: longer,
              lengthen_timeout=100.0)
    waits = [c["timeout"] for c in vb.calls[1:]]
    assert waits == pytest.approx([100.0, 70.0, 40.0, 10.0])  # then the budget is used up
    assert not r.lengthened and r.narration == short
    assert any("took longer than 100s" in w for w in r.warnings)
    assert not list((tmp_path / "voice_chunks").glob("*.try*"))


def test_resume_after_a_failed_first_run_reuses_the_generated_chunks(tmp_path):
    """Reviewer finding 4: no voice.json yet (first run failed at chunk 3) - chunks 1-2 are not made again."""
    n = len(split_narration(HINDI, max_chars=150, min_chars=60))
    assert n >= 4
    vb = ChunkVoicebox(plan={3: "fail", 4: "fail"})
    with pytest.raises(VoiceboxError, match="chunk 3/"):
        voice(tmp_path, vb, chunking=opts())
    assert not (tmp_path / "voice.json").exists()
    vb2 = ChunkVoicebox()
    r = voice(tmp_path, vb2, chunking=opts())
    assert len(vb2.calls) == n - 2 and [c.cached for c in r.chunks[:2]] == [True, True]
    assert not (tmp_path / "voice_chunks" / ".pending").exists()
    # another text after a failed run (or a re-cut clip): a fresh take, nothing reused
    out = tmp_path / "other"
    with pytest.raises(VoiceboxError):
        voice(out, ChunkVoicebox(plan={3: "fail", 4: "fail"}), chunking=opts())
    vb3 = ChunkVoicebox()
    voice(out, vb3, text=ENGLISH, lang="en", chunking=opts())
    assert len(vb3.calls) == len(split_narration(ENGLISH, max_chars=150, min_chars=60))
