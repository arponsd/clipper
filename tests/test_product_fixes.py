"""Step 2 product fixes (docs/CONTRACT_WEB.md sections 8 and 9), builder (product).

1. un-duck: the original audio is ducked only while the voice speaks, then ramps back;
2. narration length per language (measured words/s, fill ratio, one lengthen retry);
3. caption vertical position as a % of the height;
4. Hindi spelling rule in the prompt.

Fakes only (tests/fakes.py) + real ffmpeg on synthetic media. No network, no real Claude/Voicebox,
and the conftest isolates data/ per test.
"""
from __future__ import annotations

import json
import logging
import math
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import FakeLLM, FakeTranscriber, FakeVoicebox, fake_transcript, make_tone, make_video
from pydantic import ValidationError

from autoclipper import ffmpeg
from autoclipper.config import (AudioSettings, CaptionSettings, ScriptSettings, Settings, VoiceProfile,
                                WebSettings, load_settings, save_settings, settings_path)
from autoclipper.llm import LENGTHEN_SYSTEM, AnthropicLLM, LLMError, LLMSettings, language_rules
from autoclipper.models import Moment, Word
from autoclipper.pipeline import Deps, rerender, run_job
from autoclipper.stages import render as render_stage
from autoclipper.stages import script as scr
from autoclipper.stages.captions import build_ass, distribute_words
from autoclipper.stages.render import build_audio_graph, duck_envelope, render_final, speech_end
from autoclipper.stages.voice import VoiceResult, make_voice

EN = VoiceProfile(profile_id="fake-en", engine="chatterbox")
HI = VoiceProfile(profile_id="fake-hi", engine="chatterbox")


def words(n: int, lang: str = "en") -> str:
    base = "यह एक परीक्षण वाक्य है" if lang == "hi" else "alpha beta gamma delta epsilon"
    toks = base.split()
    return " ".join((toks * (n // len(toks) + 1))[:n])


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def volume_db(path: Path, start: float, length: float, *, band: int | None = None) -> tuple[float, float]:
    """(mean, max) dB of [start, start+length) of the audio, optionally through a sharp bandpass."""
    af = ",".join([f"bandpass=f={band}:width_type=h:w=60"] * 3 + ["volumedetect"]) if band else "volumedetect"
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info", "-ss", str(start),
                           "-t", str(length), "-i", str(path), "-vn", "-af", af, "-f", "null", "-"],
                          capture_output=True, text=True, check=True)
    mean = float(re.search(r"mean_volume:\s*(-?[0-9.]+|-inf) dB", proc.stderr).group(1))
    peak = float(re.search(r"max_volume:\s*(-?[0-9.]+|-inf) dB", proc.stderr).group(1))
    return mean, peak


DUCK_DB = 20 * math.log10(0.15)  # -16.5 dB


# ============================================================================ 8. settings


def test_new_settings_defaults_and_types():
    s = Settings()
    assert (s.audio.original_volume, s.audio.original_volume_after, s.audio.duck_ramp_seconds,
            s.audio.loudnorm, s.audio.target_lufs) == (0.15, 1.0, 0.4, True, -14.0)
    assert (s.script.wps_en, s.script.wps_hi, s.script.fill_ratio, s.script.lengthen_below) == \
        (3.0, 3.25, 0.88, 0.75)  # M2: wps_hi re-measured on complete chunked Hindi takes
    assert s.script.wps("en") == 3.0 and s.script.wps("hi") == 3.25 and s.script.wps("xx") == 3.0
    assert s.captions.position_pct == 72.0 and s.captions.margin_v == 520
    assert s.web.max_upload_mb == 4096 and ".mp4" in s.web.allowed_extensions and ".ogv" in s.web.allowed_extensions
    dump = s.model_dump()
    assert set(dump["script"]) == {"wps_en", "wps_hi", "fill_ratio", "lengthen_below"}
    assert set(dump["web"]) == {"max_upload_mb", "allowed_extensions"}
    assert {"original_volume_after", "duck_ramp_seconds", "loudnorm", "target_lufs"} <= set(dump["audio"])


def test_step1_settings_json_still_loads():
    """A data/settings.json written by step 1 (no new keys) loads, and new keys take defaults."""
    old = Settings().model_dump()
    for section, keys in (("audio", ("original_volume_after", "duck_ramp_seconds", "loudnorm", "target_lufs")),
                          ("captions", ("position_pct",))):
        for k in keys:
            old[section].pop(k)
    old.pop("script")
    old.pop("web")
    old["audio"]["original_volume"] = 0.3
    old["captions"]["margin_v"] = 400
    settings_path().write_text(json.dumps(old), encoding="utf-8")
    s = load_settings()
    assert s.audio.original_volume == 0.3 and s.captions.margin_v == 400
    assert s.audio.original_volume_after == 1.0 and s.captions.position_pct == 72.0
    assert s.script == ScriptSettings() and s.web == WebSettings()


def test_settings_roundtrip_on_disk():
    s = Settings()
    s.captions.position_pct = None
    s.script.wps_hi = 3.5
    s.audio.original_volume_after = 0.8
    save_settings(s)
    raw = json.loads(settings_path().read_text(encoding="utf-8"))
    assert raw["captions"]["position_pct"] is None and raw["script"]["wps_hi"] == 3.5
    assert raw["audio"]["original_volume_after"] == 0.8
    assert load_settings() == s


@pytest.mark.parametrize("model, field, value", [
    (AudioSettings, "original_volume_after", -0.1), (AudioSettings, "original_volume_after", 2.5),
    (AudioSettings, "duck_ramp_seconds", -1), (AudioSettings, "duck_ramp_seconds", 3),
    (AudioSettings, "target_lufs", -40), (AudioSettings, "target_lufs", 0),
    (AudioSettings, "loudnorm", "maybe"),
    (ScriptSettings, "wps_en", 0), (ScriptSettings, "wps_hi", 99), (ScriptSettings, "wps_en", "fast"),
    (ScriptSettings, "fill_ratio", 0.2), (ScriptSettings, "fill_ratio", 1.5),
    (ScriptSettings, "lengthen_below", 0.1), (ScriptSettings, "lengthen_below", 1.0),
    (CaptionSettings, "position_pct", 0), (CaptionSettings, "position_pct", 96), (CaptionSettings, "position_pct", -5),
    (CaptionSettings, "font_size", 10), (CaptionSettings, "font_size", 500),
    (CaptionSettings, "words_per_line", 0), (CaptionSettings, "words_per_line", 9),
    (WebSettings, "max_upload_mb", 0), (WebSettings, "max_upload_mb", 10**9), (WebSettings, "max_upload_mb", -1),
])
def test_new_and_bounded_fields_reject_out_of_range(model, field, value):
    with pytest.raises(ValidationError):
        model(**{field: value})


@pytest.mark.parametrize("section, field, value", [
    ("voicebox", "poll_interval", 0.0), ("voicebox", "poll_interval", 11),
    ("voicebox", "timeout_seconds", 1), ("voicebox", "timeout_seconds", 10**6),
    ("clips", "width", 100), ("clips", "width", 5000), ("clips", "height", 1081), ("clips", "width", 1079),
])
def test_step1_fields_have_bounds(section, field, value):
    data = Settings().model_dump()
    data[section][field] = value
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_position_pct_none_and_bounds_accept():
    assert CaptionSettings(position_pct=None).position_pct is None
    assert CaptionSettings(position_pct=5).position_pct == 5 and CaptionSettings(position_pct=95).position_pct == 95
    assert Settings.model_validate({**Settings().model_dump(),
                                    "clips": {**Settings().clips.model_dump(), "width": 720, "height": 1280}})


@pytest.mark.parametrize("exts", [
    [], [".MP4"], ["mp4"], ["."], [".mp4/"], [".toolongextension"], [".mp4", ".mp4"], [" .mp4"],
    [".m p4"], [".mp4"] * 31, [5],
])
def test_web_allowed_extensions_validator_rejects(exts):
    with pytest.raises(ValidationError):
        WebSettings(allowed_extensions=exts)


def test_web_allowed_extensions_validator_accepts():
    assert WebSettings(allowed_extensions=[".mp4", ".m4v", ".3gp"]).allowed_extensions == [".mp4", ".m4v", ".3gp"]


# ============================================================================ 9.1 ducking


def test_duck_envelope_strings():
    assert duck_envelope(0, 10, during=0.15, after=1.0, ramp=0.4) == \
        "if(lt(t,10.0),0.15,if(lt(t,10.4),0.15+(0.85)*(t-10.0)/0.4,1.0))"
    # ramp down ends at vs, ramp up starts at ve
    assert duck_envelope(2, 10, during=0.15, after=1.0, ramp=0.4) == (
        "if(lt(t,1.6),1.0,if(lt(t,2.0),1.0+(-0.85)*(t-1.6)/0.4,"
        "if(lt(t,10.0),0.15,if(lt(t,10.4),0.15+(0.85)*(t-10.0)/0.4,1.0))))")
    assert duck_envelope(0, 5, during=0.2, after=0.9, ramp=0) == "if(lt(t,5.0),0.2,0.9)"
    assert duck_envelope(3, 3, during=0.2, after=0.9, ramp=0.4) == "0.9"  # empty window
    # vs shorter than the ramp: the ramp down starts at 0
    assert duck_envelope(0.2, 4, during=0.1, after=1.0, ramp=0.4).startswith("if(lt(t,0.0),1.0,if(lt(t,0.2),")


def test_audio_graph_strings():
    a = AudioSettings(original_volume=0.15, original_volume_after=1.0, voice_volume=1.2)
    g = build_audio_graph(orig="[0:a]", orig_stereo="aformat=channel_layouts=stereo", voice_label="[1:a]",
                          audio=a, voice_end=8.0)
    assert "volume='if(lt(t,8.0),0.15," in g and ":eval=frame[a0]" in g
    assert "[1:a]aformat=channel_layouts=mono,pan=stereo|c0=c0|c1=c0,volume=1.2[a1]" in g
    assert g.endswith("amix=inputs=2:normalize=0:duration=first,loudnorm=I=-14.0:TP=-1.5:LRA=11,"
                      "aresample=48000,alimiter=limit=0.97:level=0[a]")
    g2 = build_audio_graph(orig="[0:a]", orig_stereo="x", voice_label="[1:a]",
                           audio=a.model_copy(update={"loudnorm": False}), voice_end=8.0)
    assert "loudnorm" not in g2 and g2.endswith("duration=first,alimiter=limit=0.97:level=0[a]")
    g3 = build_audio_graph(orig="[0:a]", orig_stereo="x", voice_label=None,
                           audio=a.model_copy(update={"original_volume_after": 0.7}))
    assert g3.startswith("[0:a]x,volume=0.7,loudnorm=") and "amix" not in g3


def test_speech_end_from_words_json_and_silence(tmp_path: Path):
    voice = tmp_path / "voice.wav"
    # 2 s tone + 1.5 s digital silence
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=660:sample_rate=24000:duration=2",
            "-af", "apad=pad_dur=1.5", "-ac", "1", "-c:a", "pcm_s16le", str(voice))
    assert ffmpeg.duration(voice) == pytest.approx(3.5, abs=0.05)
    assert speech_end(voice) == pytest.approx(2.0, abs=0.1)  # no words.json -> silencedetect
    (tmp_path / "words.json").write_text(json.dumps([{"start": 0, "end": 1.0, "text": "a"},
                                                     {"start": 1.0, "end": 2.6, "text": "b"}]), encoding="utf-8")
    assert speech_end(voice) == pytest.approx(2.6)
    (tmp_path / "words.json").write_text(json.dumps([{"start": 0, "end": 99, "text": "a"}]), encoding="utf-8")
    assert speech_end(voice) == pytest.approx(3.5, abs=0.05)  # never past the voice
    (tmp_path / "words.json").write_text("not json", encoding="utf-8")
    assert speech_end(voice) == pytest.approx(2.0, abs=0.1)
    (tmp_path / "full").mkdir()
    full = make_tone(tmp_path / "full" / "v.wav", 1.5)
    assert speech_end(full) == pytest.approx(1.5, abs=0.05)  # no trailing silence


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> SimpleNamespace:
    d = tmp_path_factory.mktemp("duck")
    cut = make_video(d / "cut.mp4", seconds=8.0, width=320, height=568)  # 440 Hz original
    voice = make_tone(d / "voice.wav", seconds=3.0, freq=660)
    return SimpleNamespace(dir=d, cut=cut, voice=voice, orig_db=volume_db(cut, 4.5, 3.0, band=440)[0])


def test_render_ducks_only_while_voice_plays(media, tmp_path: Path):
    audio = AudioSettings(loudnorm=False, mix="gain", short_voice="keep")  # the fixed-gain mix (the loudness mix: test_audio_mix.py)
    final = render_final(cut=media.cut, voice=media.voice, ass=None, dst=tmp_path / "final.mp4", audio=audio)
    assert ffmpeg.probe(final).duration == pytest.approx(8.0, abs=0.1)
    during = volume_db(final, 0.5, 2.0, band=440)[0]
    after = volume_db(final, 4.0, 3.5, band=440)[0]
    # tail: back at the original level (original_volume_after=1.0) - this was ~-16.5 dB before the fix
    assert after == pytest.approx(media.orig_db, abs=1.5)
    # while the voice plays: ducked by original_volume=0.15 (-16.5 dB)
    assert during - after == pytest.approx(DUCK_DB, abs=2.5)
    # the voice itself is present while it plays and gone after
    assert volume_db(final, 0.5, 2.0, band=660)[0] > volume_db(final, 4.0, 3.5, band=660)[0] + 20
    # the ramp: halfway through it the level is between the two
    mid = volume_db(final, 3.15, 0.1, band=440)[0]
    assert during + 2 < mid < after - 1
    assert volume_db(final, 0, 8)[1] <= -0.2


def test_render_tail_uses_original_volume_after(media, tmp_path: Path):
    audio = AudioSettings(loudnorm=False, original_volume_after=0.5, mix="gain", short_voice="keep")
    final = render_final(cut=media.cut, voice=media.voice, ass=None, dst=tmp_path / "f.mp4", audio=audio)
    after = volume_db(final, 4.0, 3.5, band=440)[0]
    assert after - media.orig_db == pytest.approx(20 * math.log10(0.5), abs=1.5)


def test_render_speech_end_from_words_json(media, tmp_path: Path):
    """words.json next to the voice says the speech ends at 1.5 s: the original comes back from there."""
    d = tmp_path / "en"
    d.mkdir()
    voice = d / "voice.wav"
    voice.write_bytes(media.voice.read_bytes())
    (d / "words.json").write_text(json.dumps([{"start": 0, "end": 1.5, "text": "x"}]), encoding="utf-8")
    final = render_final(cut=media.cut, voice=voice, ass=None, dst=d / "final.mp4",
                         audio=AudioSettings(loudnorm=False, mix="gain", short_voice="keep"))
    assert volume_db(final, 2.2, 0.7, band=440)[0] == pytest.approx(media.orig_db, abs=1.5)
    assert volume_db(final, 0.3, 1.0, band=440)[0] - media.orig_db == pytest.approx(DUCK_DB, abs=2.5)


def test_render_with_loudnorm_tail_is_not_silent_and_never_clips(media, tmp_path: Path):
    final = render_final(cut=media.cut, voice=media.voice, ass=None, dst=tmp_path / "f.mp4",
                         audio=AudioSettings(mix="gain", short_voice="keep"))  # loudnorm on, -14 LUFS
    info = ffmpeg.probe(final)
    assert info.duration == pytest.approx(8.0, abs=0.1) and info.has_audio and info.audio_channels == 2
    during_total = volume_db(final, 0.5, 2.0)[0]
    tail = volume_db(final, 4.5, 3.0)[0]
    assert tail > during_total - 6, "the tail must be about as loud as the narrated part"
    assert volume_db(final, 0, 8)[1] <= -0.2


def test_render_loud_inputs_are_limited(tmp_path: Path):
    cut = tmp_path / "loud.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x568:rate=25:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
            "-af", "volume=7.8", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(cut))
    voice = tmp_path / "v.wav"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=660:sample_rate=24000:duration=2", "-af", "volume=7.0",
            "-ac", "1", "-c:a", "pcm_s16le", str(voice))
    for ln in (True, False):
        final = render_final(cut=cut, voice=voice, ass=None, dst=tmp_path / f"f{ln}.mp4",
                             audio=AudioSettings(loudnorm=ln, voice_volume=2.0, original_volume_after=2.0))
        assert volume_db(final, 0, 4)[1] <= -0.2, ln


def test_render_without_voice_plays_original_at_after_level(media, tmp_path: Path):
    final = render_final(cut=media.cut, voice=None, ass=None, dst=tmp_path / "f.mp4",
                         audio=AudioSettings(loudnorm=False, original_volume_after=0.5, original_volume=0.0))
    assert ffmpeg.probe(final).duration == pytest.approx(8.0, abs=0.1)
    assert volume_db(final, 1, 6, band=440)[0] - media.orig_db == pytest.approx(-6.0, abs=1.5)


def test_render_failure_keeps_old_final(media, tmp_path: Path, monkeypatch):
    dst = tmp_path / "final.mp4"
    dst.write_bytes(b"old")

    def boom(args, **_kw):
        Path(args[-1]).write_bytes(b"partial")
        raise ffmpeg.FFmpegError("render failed")

    monkeypatch.setattr(render_stage.ffmpeg, "run", boom)
    with pytest.raises(ffmpeg.FFmpegError):
        render_final(cut=media.cut, voice=media.voice, ass=None, dst=dst, audio=AudioSettings())
    assert dst.read_bytes() == b"old" and not list(tmp_path.glob("*.part.*"))


# ============================================================================ 9.2 narration length


def test_target_words_uses_settings_and_keeps_step1_default():
    assert scr.target_words(30, "en") == 70 and scr.target_words(30, "hi") == 59  # step-1 constants
    assert scr.target_words(30, "en", wps=3.0, fill_ratio=0.88) == round(30 * 3.0 * 0.88) == 79
    assert scr.target_words(30, "hi", wps=3.35, fill_ratio=0.88) == round(30 * 3.35 * 0.88) == 88
    assert scr.target_words(0.5, "hi", wps=3.35, fill_ratio=0.88) == scr.MIN_TARGET_WORDS
    assert scr.target_words(-5, "en", wps=3.0) == scr.MIN_TARGET_WORDS


def test_measured_rates_fill_the_sample_clips():
    """With the rates measured on job 20261122-185849-65a6bd, a narration of target_words words spoken at
    those rates fills about 85-95 % of the clip (Hindi used to reach only 61-65 %)."""
    s = ScriptSettings()
    for lang, measured_wps, clip in (("en", 2.97, 30.24), ("en", 3.03, 28.84),
                                     ("hi", 3.52, 30.24), ("hi", 3.21, 28.84)):
        n = scr.target_words(clip, lang, wps=s.wps(lang), fill_ratio=s.fill_ratio)
        fill = n * 1.05 / measured_wps / clip  # the LLM overshoots by ~5 %
        assert 0.80 <= fill <= 0.97, (lang, fill)


def test_write_script_with_settings_passes_target_seconds(tmp_path: Path):
    llm = FakeLLM()
    out = tmp_path / "hi" / "script.json"
    s = scr.write_script(llm=llm, transcript=fake_transcript(120, 5), moment=Moment(start=10, end=40),
                         lang="hi", out_json=out, script_settings=ScriptSettings())
    # wps_hi 3.25 (M2): round(30 x 3.25 x 0.88) = 86
    assert llm.calls == [("write_script", dict(lang="hi", duration=30, target_words=86, target_seconds=26.4))]
    assert json.loads(out.read_text(encoding="utf-8"))["narration"] == s.narration
    assert scr.count_words(s.narration) == 86
    llm2 = FakeLLM()
    scr.write_script(llm=llm2, transcript=fake_transcript(120, 5), moment=Moment(start=10, end=40),
                     lang="en", out_json=tmp_path / "en.json",
                     script_settings=ScriptSettings(wps_en=2.5, fill_ratio=0.9))
    assert llm2.calls[0][1] == dict(lang="en", duration=30, target_words=68, target_seconds=27.0)


# ---- llm.py prompts (fake SDK client: nothing leaves the process)


class _Create:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def _client(*texts):
    rs = [SimpleNamespace(stop_reason="end_turn", stop_details=None,
                          content=[SimpleNamespace(type="text", text=t)]) for t in texts]
    return SimpleNamespace(messages=_Create(list(rs)), beta=SimpleNamespace(messages=_Create(list(rs))))


SCRIPT_JSON = json.dumps({"narration": "n", "title": "t", "description": "d", "hashtags": ["#a"]})


def test_script_prompt_states_duration_rate_and_word_range():
    client = _client(SCRIPT_JSON)
    AnthropicLLM(LLMSettings(), client=client).write_script(
        lang="hi", clip_transcript="[0.0-5.0] x", moment=Moment(start=0, end=30), duration=30.2,
        target_words=89, target_seconds=26.6)
    user = client.beta.messages.calls[0]["messages"][0]["content"]
    assert "Clip length: 30.2 seconds" in user
    assert "about 26.6 seconds" in user
    assert f"{89 / 26.6:.2f} words per second" in user and "Hindi narrator" in user
    assert "between 85 and 93 words (target 89)" in user
    assert "अट्ठाईस" in user


def test_script_prompt_without_target_seconds_is_step1_wording():
    client = _client(SCRIPT_JSON)
    AnthropicLLM(LLMSettings(), client=client).write_script(
        lang="en", clip_transcript="", moment=Moment(start=0, end=30), duration=30, target_words=70)
    user = client.beta.messages.calls[0]["messages"][0]["content"]
    assert "about 70 words (never more than 77)" in user and "words per second" not in user


def test_lengthen_prompt_and_errors():
    client = _client('{"narration": "  a much longer narration  "}', '{"narration": ""}')
    llm = AnthropicLLM(LLMSettings(), client=client)
    assert llm.lengthen(lang="en", narration="a b c", target_words=40) == "a much longer narration"
    call = client.beta.messages.calls[0]
    assert call["system"] == LENGTHEN_SYSTEM
    assert "between 38 and 42 words (target 40)" in call["messages"][0]["content"]
    assert "it has 3 words" in call["messages"][0]["content"]
    assert call["max_tokens"] <= 8000
    with pytest.raises(LLMError, match="empty"):
        llm.lengthen(lang="hi", narration="a b", target_words=10)


def test_hindi_spelling_rule():
    rules = language_rules("hi")
    assert "अट्ठाईस" in rules and "अठ्ठाईस" in rules and "nukta" in rules
    assert "nukta" not in language_rules("en")


# ---- voice stage lengthen


class LengthenSpy:
    def __init__(self, n_words: int | None = None, exc: Exception | None = None):
        self.n_words, self.exc = n_words, exc
        self.calls: list[tuple[str, int]] = []

    def __call__(self, narration: str, target: int) -> str:
        self.calls.append((narration, target))
        if self.exc:
            raise self.exc
        return words(self.n_words or target)


class ShortenSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, narration, max_words):
        self.calls.append(max_words)
        return " ".join(narration.split()[:max_words])


def _voice(tmp_path, *, narration, clip=10.0, spw=0.3, lengthen=None, shorten=None, lang="en", **kw):
    vb = FakeVoicebox(seconds_per_word=spw)
    r = make_voice(client=vb, narration=narration, lang=lang, voice=HI if lang == "hi" else EN,
                   clip_duration=clip, out_dir=tmp_path / lang, max_speedup=1.25, shorten=shorten,
                   lengthen=lengthen, **kw)
    return r, vb


def test_voice_lengthen_happy_path(tmp_path: Path):
    spy, sh = LengthenSpy(), ShortenSpy()
    r, vb = _voice(tmp_path, narration=words(5), lengthen=spy, shorten=sh)  # 1.5 s on 10 s < 7.5 s
    assert spy.calls == [(words(5), math.ceil(5 * 10 * 0.88 / 1.5))]  # 30 words
    assert len(vb.calls) == 2 and sh.calls == []
    assert r.lengthened and not r.shortened and not r.trimmed and r.tempo == 1.0
    assert r.narration == words(30) and r.raw_duration == pytest.approx(9.0, abs=0.05)
    vj = json.loads((tmp_path / "en" / "voice.json").read_text(encoding="utf-8"))
    assert vj["lengthened"] is True and vj["narration"] == words(30)
    assert ffmpeg.duration(tmp_path / "en" / "voice.wav") == pytest.approx(9.0, abs=0.05)
    assert sorted(p.name for p in (tmp_path / "en").iterdir()) == ["voice.json", "voice.wav", "voice_raw.wav"]


def test_voice_lengthen_with_tempo_never_shortens(tmp_path: Path):
    spy, sh = LengthenSpy(n_words=40), ShortenSpy()  # 12 s on a 10 s clip: fits at x1.2
    r, vb = _voice(tmp_path, narration=words(5), lengthen=spy, shorten=sh)
    assert r.lengthened and r.tempo == pytest.approx(1.2, abs=0.01) and not r.trimmed and sh.calls == []
    assert r.final_duration <= 10.05


def test_voice_lengthen_too_long_keeps_original_take(tmp_path: Path):
    spy, sh = LengthenSpy(n_words=60), ShortenSpy()  # 18 s > 12.5 s: does not fit
    r, vb = _voice(tmp_path, narration=words(5), lengthen=spy, shorten=sh)
    assert len(spy.calls) == 1 and len(vb.calls) == 2 and sh.calls == []
    assert not r.lengthened and not r.trimmed and not r.shortened and r.tempo == 1.0
    assert r.narration == words(5) and r.raw_duration == pytest.approx(1.5, abs=0.05)
    out = tmp_path / "en"
    assert ffmpeg.duration(out / "voice.wav") == pytest.approx(1.5, abs=0.05)
    assert ffmpeg.duration(out / "voice_raw.wav") == pytest.approx(1.5, abs=0.05)
    assert not list(out.glob("*.orig.wav"))


def test_voice_lengthen_llm_error_keeps_original(tmp_path: Path, caplog):
    spy = LengthenSpy(exc=RuntimeError("refused"))
    with caplog.at_level(logging.WARNING):
        r, vb = _voice(tmp_path, narration=words(5), lengthen=spy)
    assert len(vb.calls) == 1 and not r.lengthened and r.narration == words(5)
    assert "lengthening failed" in caplog.text


def test_voice_lengthen_voicebox_error_keeps_original(tmp_path: Path, caplog):
    class FlakyVB(FakeVoicebox):
        def synthesize(self, text, **kw):
            if self.calls:
                self.calls.append({"text": text})
                raise RuntimeError("voicebox died")
            return super().synthesize(text, **kw)

    vb = FlakyVB(seconds_per_word=0.3)
    with caplog.at_level(logging.WARNING):
        r = make_voice(client=vb, narration=words(5), lang="en", voice=EN, clip_duration=10.0,
                       out_dir=tmp_path / "en", max_speedup=1.25, lengthen=LengthenSpy())
    assert len(vb.calls) == 2 and not r.lengthened and r.narration == words(5)
    assert ffmpeg.duration(tmp_path / "en" / "voice.wav") == pytest.approx(1.5, abs=0.05)
    assert "keeping the original voice" in caplog.text


def test_voice_lengthen_not_needed_or_not_longer(tmp_path: Path):
    spy = LengthenSpy()
    r, _ = _voice(tmp_path / "a", narration=words(26), lengthen=spy)  # 7.8 s >= 7.5 s
    assert spy.calls == [] and not r.lengthened
    same = LengthenSpy(n_words=5)
    r, vb = _voice(tmp_path / "b", narration=words(5), lengthen=same)
    assert len(same.calls) == 1 and len(vb.calls) == 1 and not r.lengthened
    r, vb = _voice(tmp_path / "c", narration=words(5), lengthen=None)  # step-1 behaviour
    assert len(vb.calls) == 1 and not r.lengthened


def test_voice_lengthen_threshold_is_a_setting(tmp_path: Path):
    spy = LengthenSpy()
    r, _ = _voice(tmp_path, narration=words(20), lengthen=spy, lengthen_below=0.5, fill_ratio=0.9)  # 6 s >= 5 s
    assert spy.calls == []
    r, _ = _voice(tmp_path / "x", narration=words(20), lengthen=spy, lengthen_below=0.7, fill_ratio=0.9)
    assert spy.calls == [(words(20), math.ceil(20 * 10 * 0.9 / 6.0))] and r.lengthened


def test_voice_result_old_json_defaults_lengthened_false():
    old = dict(path="/x/voice.wav", narration="a", narration_hash="h", raw_duration=1, final_duration=1,
               clip_duration=2, tempo=1, shortened=False, trimmed=False)
    assert VoiceResult.model_validate(old).lengthened is False


# ---- pipeline wiring


def _settings() -> Settings:
    s = Settings()
    s.voicebox.english.profile_id = "fake-en"
    s.voicebox.hindi.profile_id = "fake-hi"
    s.clips.count = 1
    s.clips.min_seconds = 10
    s.clips.max_seconds = 15
    return s


def quiet(_line: str) -> None:
    return None


@pytest.fixture
def video(tmp_path: Path) -> Path:
    return make_video(tmp_path / "src.mp4", seconds=40)


def test_pipeline_passes_script_settings_and_lengthens_new_scripts(video: Path):
    s = _settings()
    llm = FakeLLM(narration_words=6)
    store = run_job(str(video), settings=s, deps=Deps(llm=llm, voicebox=FakeVoicebox(),
                                                      transcriber=FakeTranscriber(duration=40)),
                    langs=["en", "hi"], log=quiet)
    assert store.job.status == "done"
    ws = [c for c in llm.calls if c[0] == "write_script"]
    dur = store.job.clips[0].moment.end - store.job.clips[0].moment.start
    by_lang = {c[1]["lang"]: c[1] for c in ws}
    assert by_lang["en"]["target_words"] == round(dur * 3.0 * 0.88)
    assert by_lang["hi"]["target_words"] == round(dur * Settings().script.wps_hi * 0.88)
    assert by_lang["hi"]["target_seconds"] == pytest.approx(round(dur * 0.88, 1))
    assert [c[1]["lang"] for c in llm.calls if c[0] == "lengthen"] == ["en", "hi"]
    for lang in ("en", "hi"):
        d = store.clip_dir(0, lang)
        script = json.loads((d / "script.json").read_text(encoding="utf-8"))
        voice = json.loads((d / "voice.json").read_text(encoding="utf-8"))
        assert voice["lengthened"] is True and script["narration"] == voice["narration"]
        assert len(script["narration"].split()) > 6
        cap = json.loads((d / "words.json").read_text(encoding="utf-8"))
        assert [w["text"] for w in cap] == script["narration"].split()
        assert (d / "final.mp4").is_file()


def test_rerender_and_resume_never_lengthen_user_edits(video: Path):
    s = _settings()
    llm = FakeLLM()
    st = run_job(str(video), settings=s, deps=Deps(llm=llm, voicebox=FakeVoicebox(),
                                                   transcriber=FakeTranscriber(duration=40)),
                 langs=["en"], log=quiet)
    assert not [c for c in llm.calls if c[0] == "lengthen"]
    d = st.clip_dir(0, "en")
    script = json.loads((d / "script.json").read_text(encoding="utf-8"))
    script["narration"] = "short user edit"
    (d / "script.json").write_text(json.dumps(script), encoding="utf-8")
    llm2 = FakeLLM()
    rerender(st.job.id, 0, "en", settings=s, deps=Deps(llm=llm2, voicebox=FakeVoicebox(),
                                                       transcriber=FakeTranscriber()), log=quiet)
    assert llm2.calls == []
    assert json.loads((d / "script.json").read_text(encoding="utf-8"))["narration"] == "short user edit"
    assert json.loads((d / "voice.json").read_text(encoding="utf-8"))["lengthened"] is False

    script["narration"] = "another short edit"
    (d / "script.json").write_text(json.dumps(script), encoding="utf-8")
    llm3 = FakeLLM()
    run_job("", settings=s, deps=Deps(llm=llm3, voicebox=FakeVoicebox(), transcriber=FakeTranscriber(40)),
            job_id=st.job.id, langs=["en"], log=quiet)
    assert llm3.calls == []
    assert json.loads((d / "script.json").read_text(encoding="utf-8"))["narration"] == "another short edit"


def test_pipeline_llm_without_lengthen_still_works(video: Path):
    class OldLLM:  # a step-1 LLM: no lengthen attribute at all
        def __init__(self):
            self.inner = FakeLLM(narration_words=6)

        def pick_moments(self, *a, **k):
            return self.inner.pick_moments(*a, **k)

        def write_script(self, **k):
            return self.inner.write_script(**k)

        def shorten(self, **k):
            return self.inner.shorten(**k)

    store = run_job(str(video), settings=_settings(), deps=Deps(llm=OldLLM(), voicebox=FakeVoicebox(),
                                                                transcriber=FakeTranscriber(40)),
                    langs=["en"], log=quiet)
    assert store.job.status == "done"
    assert len(json.loads((store.clip_dir(0, "en") / "script.json").read_text())["narration"].split()) == 6


# ============================================================================ 9.3 caption position


def _style(ass: str) -> list[str]:
    return ass.split("Style: Default,", 1)[1].splitlines()[0].split(",")


def _dialogues(ass: str) -> list[str]:
    return [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]


def test_caption_position_pct_default():
    ass = build_ass(distribute_words(["one", "two", "three", "four"], 2.0), lang="en",
                    settings=CaptionSettings())
    style = _style(ass)
    assert style[17] == "5"  # Alignment (Name is stripped: Fontname is index 0)
    d = _dialogues(ass)
    assert len(d) == 4
    assert all(line.split(",,", 2)[2].startswith("{\\an5\\pos(540,1382)}") for line in d)


def test_caption_position_pct_custom_size():
    ass = build_ass(distribute_words(["नमस्ते", "दुनिया"], 1.0), lang="hi",
                    settings=CaptionSettings(position_pct=50), width=720, height=1280)
    assert all("{\\an5\\pos(360,640)}" in line for line in _dialogues(ass))


def test_caption_position_none_is_legacy_margin_v():
    ass = build_ass(distribute_words(["one", "two"], 1.0), lang="en",
                    settings=CaptionSettings(position_pct=None, margin_v=300))
    style = _style(ass)
    assert style[17] == "2" and style[20] == "300"
    assert "\\pos(" not in ass and "\\an5" not in ass


def test_caption_burn_lands_near_position(tmp_path: Path):
    """Real libass burn: the bright text rows are centred near 72 % of the height."""
    w, h = 360, 640
    cut = tmp_path / "black.mp4"
    _ffmpeg("-f", "lavfi", "-i", f"color=c=black:size={w}x{h}:rate=25:duration=1",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(cut))
    for pct in (72, 30):
        ass = tmp_path / f"c{pct}.ass"
        ass.write_text(build_ass([Word(start=0, end=1, text="HELLO")], lang="en",
                                 settings=CaptionSettings(font_size=40, position_pct=pct), width=w, height=h),
                       encoding="utf-8")
        final = render_final(cut=cut, voice=None, ass=ass, dst=tmp_path / f"f{pct}.mp4",
                             audio=AudioSettings(loudnorm=False))
        frame = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "0.5", "-i", str(final),
                                "-frames:v", "1", "-vf", "format=gray", "-f", "rawvideo", "-"],
                               capture_output=True, check=True).stdout
        rows = [y for y in range(h) if max(frame[y * w:(y + 1) * w]) > 150]
        assert rows, "captions must be burned in"
        centre = (rows[0] + rows[-1]) / 2 / h * 100
        assert centre == pytest.approx(pct, abs=3), (pct, centre)
