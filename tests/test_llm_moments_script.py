"""Tests for llm.py, stages/transcribe.py, stages/moments.py and stages/script.py (ai builder).

No network: the Anthropic client is a fake object or an httpx2.MockTransport; Whisper is faked.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoclipper import llm as llm_mod
from autoclipper.config import ClipSettings, LLMSettings, WhisperSettings
from autoclipper.interfaces import LLM, Transcriber
from autoclipper.llm import (FALLBACK_BETA, AnthropicLLM, LLMError, LLMRefusal, LLMTruncated,
                             MOMENTS_SCHEMA, SCRIPT_SCHEMA, SHORTEN_SCHEMA)
from autoclipper.models import ClipScript, Moment, Segment, Transcript, Word
from autoclipper.stages import moments as mom
from autoclipper.stages import script as scr
from autoclipper.stages import transcribe as tr
from fakes import FakeLLM, FakeTranscriber, fake_transcript, make_tone


# ============================================================================ fake client


class _Create:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeClient:
    def __init__(self, *responses):
        self.messages = _Create(list(responses))
        self.beta = SimpleNamespace(messages=_Create(list(responses)))


def resp(text=None, stop_reason="end_turn", stop_details=None, blocks=None):
    content = blocks if blocks is not None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(stop_reason=stop_reason, stop_details=stop_details, content=content)


def settings(**kw) -> LLMSettings:
    return LLMSettings(**kw)


# ============================================================================ llm.py


def test_anthropic_llm_satisfies_protocol():
    assert isinstance(AnthropicLLM(settings(), client=FakeClient()), LLM)


def _no_ambient_credentials(monkeypatch, tmp_path):
    """Hide any real key and `ant auth login` profile from the SDK."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
                "ANTHROPIC_CONFIG_DIR", "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE",
                "ANTHROPIC_FEDERATION_RULE_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))


def test_missing_credentials_raises_clear_error(monkeypatch, tmp_path):
    _no_ambient_credentials(monkeypatch, tmp_path)
    monkeypatch.delenv("AC_TEST_KEY", raising=False)
    with pytest.raises(LLMError, match="no Anthropic credentials.*ant auth login.*AC_TEST_KEY"):
        AnthropicLLM(settings(api_key_env="AC_TEST_KEY"))
    monkeypatch.setenv("AC_TEST_KEY", "   ")
    with pytest.raises(LLMError, match="no Anthropic credentials"):
        AnthropicLLM(settings(api_key_env="AC_TEST_KEY"))


def test_ant_login_profile_used_when_no_key(monkeypatch, tmp_path):
    import anthropic

    _no_ambient_credentials(monkeypatch, tmp_path)
    monkeypatch.delenv("AC_TEST_KEY", raising=False)
    calls = []

    class ProfileClient:
        api_key = None
        auth_token = None
        credentials = object()  # what the SDK sets from an `ant auth login` profile
        beta = object()

    def fake_anthropic(*args, **kwargs):
        calls.append(kwargs)
        return ProfileClient()

    monkeypatch.setattr(anthropic, "Anthropic", fake_anthropic)
    llm = AnthropicLLM(settings(api_key_env="AC_TEST_KEY"))
    assert isinstance(llm.client, ProfileClient)
    assert calls == [{}]  # zero-arg construction, so the SDK resolves the profile


def test_api_key_present_builds_real_client_without_calling(monkeypatch):
    monkeypatch.setenv("AC_TEST_KEY", "sk-test")
    llm = AnthropicLLM(settings(api_key_env="AC_TEST_KEY"))
    assert llm.client is not None and hasattr(llm.client, "beta")


def test_complete_json_fallback_call_shape():
    client = FakeClient(resp('{"a": 1}'))
    out = AnthropicLLM(settings(effort="high"), client=client).complete_json(
        system="sys", user="hello", schema=SHORTEN_SCHEMA)
    assert out == {"a": 1}
    assert client.messages.calls == []
    (kw,) = client.beta.messages.calls
    assert kw["model"] == "claude-opus-5-5"
    assert kw["max_tokens"] == 16000
    assert kw["system"] == "sys"
    assert kw["messages"] == [{"role": "user", "content": "hello"}]
    assert kw["output_config"] == {"effort": "high",
                                   "format": {"type": "json_schema", "schema": SHORTEN_SCHEMA}}
    assert kw["betas"] == [FALLBACK_BETA] == ["server-side-fallback-2026-07-01"]
    assert kw["fallbacks"] == "default"
    for banned in ("thinking", "temperature", "top_p", "top_k"):
        assert banned not in kw
    assert all(m["role"] != "assistant" for m in kw["messages"])  # no prefill


def test_complete_json_without_fallbacks_uses_plain_messages():
    client = FakeClient(resp('{"a": 2}'))
    out = AnthropicLLM(settings(use_fallbacks=False), client=client).complete_json(
        system="s", user="u", schema=SHORTEN_SCHEMA)
    assert out == {"a": 2}
    assert client.beta.messages.calls == []
    (kw,) = client.messages.calls
    assert "betas" not in kw and "fallbacks" not in kw and "thinking" not in kw
    assert kw["output_config"]["effort"] == "medium"


def test_only_text_blocks_are_joined():
    blocks = [SimpleNamespace(type="thinking", thinking="hmm", text="IGNORED"),
              SimpleNamespace(type="fallback", text="IGNORED"),
              SimpleNamespace(type="text", text='{"narr'),
              SimpleNamespace(type="text", text='ation": "ok"}')]
    out = AnthropicLLM(settings(), client=FakeClient(resp(blocks=blocks))).complete_json(
        system="s", user="u", schema=SHORTEN_SCHEMA)
    assert out == {"narration": "ok"}


def test_refusal_with_and_without_details():
    det = SimpleNamespace(category="cyber", explanation="not allowed", type="refusal")
    with pytest.raises(LLMRefusal, match="category: cyber; explanation: not allowed"):
        AnthropicLLM(settings(), client=FakeClient(resp(None, "refusal", det, blocks=[]))).complete_json(
            system="s", user="u", schema=SHORTEN_SCHEMA)
    with pytest.raises(LLMRefusal, match="declined"):
        AnthropicLLM(settings(), client=FakeClient(resp(None, "refusal", None, blocks=[]))).complete_json(
            system="s", user="u", schema=SHORTEN_SCHEMA)


def test_refusal_checked_before_content():
    # content would parse fine, but stop_reason wins
    with pytest.raises(LLMRefusal):
        AnthropicLLM(settings(), client=FakeClient(resp('{"a":1}', "refusal"))).complete_json(
            system="s", user="u", schema=SHORTEN_SCHEMA)


def test_max_tokens_raises_truncated():
    with pytest.raises(LLMTruncated, match="max_tokens") as ei:
        AnthropicLLM(settings(), client=FakeClient(resp('{"a":', "max_tokens"))).complete_json(
            system="s", user="u", schema=SHORTEN_SCHEMA)
    msg = str(ei.value)
    assert "cut off at max_tokens=16000" in msg
    assert "`autoclipper settings set llm.max_tokens=32000`" in msg and "lower llm.effort" in msg
    assert issubclass(LLMTruncated, LLMError) and issubclass(LLMRefusal, LLMError)


def test_max_tokens_setting_is_the_budget_and_long_budgets_get_a_timeout():
    # FAILURES-8: llm.max_tokens drives moments/scripts; shorten stays <= 8000
    client = FakeClient(resp('{"moments": []}'), resp('{"narration": "x"}'))
    llm = AnthropicLLM(settings(max_tokens=32000), client=client)
    llm.pick_moments("[0-1] hi", count=1, min_seconds=1, max_seconds=2, video_duration=10)
    llm.shorten(lang="en", narration="a b c", max_words=2)
    big, small = client.beta.messages.calls
    assert big["max_tokens"] == 32000 and big["timeout"] == 1800
    assert small["max_tokens"] == 8000 and "timeout" not in small
    with pytest.raises(LLMTruncated, match="max_tokens=32000"):
        AnthropicLLM(settings(max_tokens=32000), client=FakeClient(resp('{"a":', "max_tokens"))).complete_json(
            system="s", user="u", schema=SHORTEN_SCHEMA)


def test_small_max_tokens_setting_caps_shorten_and_has_no_timeout():
    client = FakeClient(resp('{"narration": "x"}'), resp('{"a": 1}'))
    llm = AnthropicLLM(settings(max_tokens=4000, use_fallbacks=False), client=client)
    llm.shorten(lang="en", narration="a b c", max_words=2)
    llm.complete_json(system="s", user="u", schema=SHORTEN_SCHEMA)
    assert [c["max_tokens"] for c in client.messages.calls] == [4000, 4000]
    assert all("timeout" not in c for c in client.messages.calls)


def test_max_tokens_falls_back_when_setting_missing():
    client = FakeClient(resp('{"a": 1}'))
    llm = AnthropicLLM(SimpleNamespace(model="m", effort="low", use_fallbacks=False), client=client)
    llm.complete_json(system="s", user="u", schema=SHORTEN_SCHEMA)
    assert client.messages.calls[0]["max_tokens"] == 16000


def test_sdk_accepts_long_budget_with_timeout():
    # the real SDK (MockTransport, no network) must not reject max_tokens=32000 as "too long"
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=_message_json('{"moments": []}'))

    llm = AnthropicLLM(settings(max_tokens=32000), client=_sdk_client(handler))
    assert llm.pick_moments("[0-1] x", count=1, min_seconds=1, max_seconds=2, video_duration=5) == []
    assert seen["body"]["max_tokens"] == 32000


@pytest.mark.parametrize("text,match", [("not json at all", "invalid JSON"),
                                        ("[1, 2]", "not an object"),
                                        ("", "no text")])
def test_bad_content_raises_llm_error(text, match):
    with pytest.raises(LLMError, match=match):
        AnthropicLLM(settings(), client=FakeClient(resp(text))).complete_json(
            system="s", user="u", schema=SHORTEN_SCHEMA)


def test_unknown_exception_wrapped():
    with pytest.raises(LLMError, match="ValueError: boom"):
        AnthropicLLM(settings(), client=FakeClient(ValueError("boom"))).complete_json(
            system="s", user="u", schema=SHORTEN_SCHEMA)


def _walk_objects(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for v in schema.values():
            yield from _walk_objects(v)
    elif isinstance(schema, list):
        for v in schema:
            yield from _walk_objects(v)


@pytest.mark.parametrize("schema", [MOMENTS_SCHEMA, SCRIPT_SCHEMA, SHORTEN_SCHEMA])
def test_schemas_are_strict(schema):
    objs = list(_walk_objects(schema))
    assert objs
    for o in objs:
        assert o["additionalProperties"] is False
        assert sorted(o["required"]) == sorted(o["properties"])


def test_pick_moments_parses_and_skips_malformed():
    payload = {"moments": [{"start": 1, "end": 30, "hook": "h", "reason": "r", "score": 0.9},
                           {"start": "bad", "end": 3, "hook": "", "reason": "", "score": 0}]}
    client = FakeClient(resp(json.dumps(payload)))
    out = AnthropicLLM(settings(), client=client).pick_moments(
        "[0.0-5.0] hi", count=3, min_seconds=20, max_seconds=60, video_duration=100)
    assert out == [Moment(start=1, end=30, hook="h", reason="r", score=0.9)]
    kw = client.beta.messages.calls[0]
    assert kw["output_config"]["format"]["schema"] is MOMENTS_SCHEMA
    user = kw["messages"][0]["content"]
    assert "3 most viral" in user and "20" in user and "60" in user and "[0.0-5.0] hi" in user


def test_write_script_hindi_prompt_and_unicode():
    payload = {"narration": "यह कमाल का पल है", "title": "देखिए क्या हुआ",
               "description": "विवरण", "hashtags": ["#हिंदी"]}
    client = FakeClient(resp(json.dumps(payload, ensure_ascii=False)))
    s = AnthropicLLM(settings(), client=client).write_script(
        lang="hi", clip_transcript="[0.0-5.0] hello", moment=Moment(start=0, end=30),
        duration=30, target_words=59)
    assert s.narration == "यह कमाल का पल है" and s.hashtags == ["#हिंदी"]
    user = client.beta.messages.calls[0]["messages"][0]["content"]
    assert "Devanagari" in user and "Hindi" in user and "59" in user
    assert "NOT a translation" in client.beta.messages.calls[0]["system"]


def test_write_script_invalid_payload():
    client = FakeClient(resp('{"narration": "x"}'))
    with pytest.raises(LLMError, match="invalid script"):
        AnthropicLLM(settings(), client=client).write_script(
            lang="en", clip_transcript="", moment=Moment(start=0, end=30), duration=30, target_words=70)


def test_shorten_returns_text_and_rejects_empty():
    client = FakeClient(resp('{"narration": "  short one  "}'), resp('{"narration": ""}'))
    llm = AnthropicLLM(settings(), client=client)
    assert llm.shorten(lang="en", narration="a b c d e", max_words=3) == "short one"
    assert "at most 3 words" in client.beta.messages.calls[0]["messages"][0]["content"]
    with pytest.raises(LLMError, match="empty"):
        llm.shorten(lang="en", narration="a b c", max_words=2)


# ---- HTTP-level test through the real SDK (MockTransport, never the real API)

httpx2 = pytest.importorskip("httpx2")


def _sdk_client(handler):
    import anthropic

    return anthropic.Anthropic(api_key="test", max_retries=0,
                               http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def _message_json(text: str, stop_reason: str = "end_turn") -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason,
            "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}


def test_sdk_wire_request_with_fallback_beta():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        seen["path"] = request.url.path
        return httpx2.Response(200, json=_message_json('{"narration": "ok"}'))

    llm = AnthropicLLM(settings(), client=_sdk_client(handler))
    assert llm.shorten(lang="en", narration="a b c", max_words=2) == "ok"
    assert seen["path"].endswith("/v1/messages")
    assert FALLBACK_BETA in seen["beta"]
    body = seen["body"]
    assert body["fallbacks"] == "default"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in body and "temperature" not in body


def test_sdk_wire_request_without_fallback():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        return httpx2.Response(200, json=_message_json('{"narration": "ok"}'))

    AnthropicLLM(settings(use_fallbacks=False), client=_sdk_client(handler)).shorten(
        lang="en", narration="a b c", max_words=2)
    assert "fallbacks" not in seen["body"] and FALLBACK_BETA not in seen["beta"]


@pytest.mark.parametrize("status,match", [(401, "credentials"), (429, "rate limit"),
                                          (404, "not found"), (500, "API error 500")])
def test_sdk_http_errors_mapped(status, match):
    def handler(request):
        return httpx2.Response(status, json={"type": "error",
                                             "error": {"type": "x", "message": "nope"}})

    with pytest.raises(LLMError, match=match):
        AnthropicLLM(settings(), client=_sdk_client(handler)).shorten(
            lang="en", narration="a", max_words=1)


def test_sdk_connection_error_mapped():
    def handler(request):
        raise httpx2.ConnectError("down", request=request)

    with pytest.raises(LLMError, match="connect|timed out"):
        AnthropicLLM(settings(), client=_sdk_client(handler)).shorten(
            lang="en", narration="a", max_words=1)


def test_sdk_refusal_response():
    def handler(request):
        body = _message_json("", "refusal")
        body["content"] = []
        body["stop_details"] = {"type": "refusal", "category": "cyber", "explanation": "no"}
        return httpx2.Response(200, json=body)

    with pytest.raises(LLMRefusal, match="cyber"):
        AnthropicLLM(settings(), client=_sdk_client(handler)).shorten(
            lang="en", narration="a", max_words=1)


# ============================================================================ transcribe.py


def test_resolve_device(monkeypatch):
    monkeypatch.setattr(tr, "_cuda_device_count", lambda: 0)
    assert tr.resolve_device(WhisperSettings()) == ("cpu", "int8")
    monkeypatch.setattr(tr, "_cuda_device_count", lambda: 1)
    assert tr.resolve_device(WhisperSettings()) == ("cuda", "float16")
    assert tr.resolve_device(WhisperSettings(device="cpu", compute_type="float32")) == ("cpu", "float32")
    assert tr.resolve_device(WhisperSettings(device="cpu")) == ("cpu", "int8")


def test_resolve_device_this_machine_is_cpu():
    import ctranslate2

    if ctranslate2.get_cuda_device_count() == 0:
        assert tr.resolve_device(WhisperSettings()) == ("cpu", "int8")


class _FakeWhisperModel:
    instances: list = []

    def __init__(self, name, device, compute_type):
        self.args = (name, device, compute_type)
        self.calls: list[dict] = []
        _FakeWhisperModel.instances.append(self)

    def transcribe(self, path, **kw):
        self.calls.append(dict(path=path, **kw))
        W = SimpleNamespace
        segs = [SimpleNamespace(start=0.0, end=2.0, text=" Hello world ",
                                words=[W(start=0.0, end=0.8, word=" Hello"), W(start=0.9, end=2.0, word=" world")]),
                SimpleNamespace(start=2.5, end=4.0, text=" Bye", words=[W(start=2.5, end=4.0, word=" Bye")])]
        info = SimpleNamespace(duration=4.0, language=kw.get("language") or "en")
        return iter(segs), info


@pytest.fixture
def fake_whisper(monkeypatch):
    import faster_whisper

    _FakeWhisperModel.instances = []
    monkeypatch.setattr(faster_whisper, "WhisperModel", _FakeWhisperModel)
    monkeypatch.setattr(tr, "_cuda_device_count", lambda: 0)
    return _FakeWhisperModel


def test_whisper_transcriber_lazy_and_options(tmp_path, fake_whisper):
    # a real 4 s 16 kHz wav: the transcriber now decodes it itself and passes an ndarray
    audio = make_tone(tmp_path / "audio.wav", seconds=4.0, sample_rate=16000)
    wt = tr.WhisperTranscriber(WhisperSettings(model="tiny", language="hi"))
    assert isinstance(wt, Transcriber)
    assert fake_whisper.instances == []  # lazy: nothing loaded yet
    progress = []
    t = wt.transcribe(audio, on_progress=lambda f, m: progress.append(f))
    (model,) = fake_whisper.instances
    assert model.args == ("tiny", "cpu", "int8")
    call = model.calls[0]
    assert call["word_timestamps"] is True and call["vad_filter"] is True
    assert call["language"] == "hi"  # falls back to settings.language
    assert t.duration == 4.0 and t.language == "hi"
    assert [s.text for s in t.segments] == ["Hello world", "Bye"]
    assert [w.text for w in t.segments[0].words] == ["Hello", "world"]
    assert progress[0] == pytest.approx(0.5) and progress[-1] == 1.0
    wt.transcribe(audio, language="en")
    assert len(fake_whisper.instances) == 1  # model reused
    assert model.calls[1]["language"] == "en"


def test_whisper_missing_audio(tmp_path, fake_whisper):
    with pytest.raises(tr.TranscribeError, match="not found"):
        tr.WhisperTranscriber(WhisperSettings()).transcribe(tmp_path / "nope.wav")


def test_whisper_model_load_failure(tmp_path, monkeypatch):
    import faster_whisper

    def boom(*a, **k):
        raise OSError("no model")

    monkeypatch.setattr(faster_whisper, "WhisperModel", boom)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    with pytest.raises(tr.TranscribeError, match="could not load Whisper model"):
        tr.WhisperTranscriber(WhisperSettings()).transcribe(audio)


def test_run_transcribe_writes_and_is_idempotent(tmp_path):
    fake = FakeTranscriber(duration=20)
    out = tmp_path / "transcript.json"
    t1 = tr.run_transcribe(tmp_path / "audio.wav", out, fake)
    assert out.is_file() and len(fake.calls) == 1
    t2 = tr.run_transcribe(tmp_path / "audio.wav", out, fake)
    assert len(fake.calls) == 1 and t2 == t1


def test_run_transcribe_keeps_hindi_unescaped(tmp_path):
    class HiT:
        def transcribe(self, audio, language=None, on_progress=None):
            return Transcript(language="hi", duration=2, segments=[
                Segment(start=0, end=2, text="नमस्ते दुनिया", words=[Word(start=0, end=1, text="नमस्ते")])])

    out = tmp_path / "transcript.json"
    tr.run_transcribe(tmp_path / "audio.wav", out, HiT())
    raw = out.read_text(encoding="utf-8")
    assert "नमस्ते" in raw and "\\u" not in raw


def test_corrupt_transcript_is_moved_aside_and_regenerated(tmp_path):
    # FAILURES-3: a truncated transcript.json no longer blocks the job
    out = tmp_path / "transcript.json"
    out.write_text("{oops", encoding="utf-8")
    fake = FakeTranscriber(duration=20)
    msgs = []
    t = tr.run_transcribe(tmp_path / "audio.wav", out, fake, on_progress=lambda f, m: msgs.append(m))
    assert len(fake.calls) == 1
    assert (tmp_path / "transcript.json.corrupt").read_text(encoding="utf-8") == "{oops"
    assert tr.load_transcript(out) == t
    assert any("transcript.json.corrupt" in m for m in msgs)
    with pytest.raises(tr.TranscribeError, match="delete it"):
        tr.load_transcript(tmp_path / "transcript.json.corrupt")


def test_transcribe_voice_flattens_and_forces_language(tmp_path):
    words = [Word(start=0, end=0.5, text="यह"), Word(start=0.5, end=1.0, text="है")]
    fake = FakeTranscriber(words_for_voice=words)
    got = tr.transcribe_voice(tmp_path / "voice.wav", "hi", fake)
    assert got == words
    assert fake.calls == [(str(tmp_path / "voice.wav"), "hi")]
    assert tr.transcribe_voice(tmp_path / "voice.wav", "en", FakeTranscriber()) == []


# ============================================================================ moments.py


def test_format_transcript_all_and_window():
    t = fake_transcript(duration=20, seg_len=5)
    lines = mom.format_transcript(t).splitlines()
    assert lines[0] == "[0.0-5.0] word0a word0b word0c word0d" and len(lines) == 4
    window = mom.format_transcript(t, 6, 12).splitlines()
    assert [ln.split("]")[0] for ln in window] == ["[5.0-10.0", "[10.0-15.0"]
    assert mom.format_transcript(t, 5, 10).splitlines() == [lines[1]]


def V(raw, t=None, count=5, lo=20, hi=60):
    return mom.validate_moments(raw, t or fake_transcript(120, 5), count=count,
                                min_seconds=lo, max_seconds=hi)


def test_validate_snaps_to_segment_boundaries():
    (m,) = V([Moment(start=11.2, end=38.7, score=0.5, hook="h")])
    assert (m.start, m.end) == (10.0, 40.0) and m.hook == "h"


def test_validate_clamps_to_video():
    (m,) = V([Moment(start=-5, end=200, score=1)], hi=180)
    assert m.start == 0 and m.end == 120


def test_validate_extends_short_and_shrinks_long():
    (short,) = V([Moment(start=10, end=15, score=1)])
    assert short.start == 10 and short.end - short.start >= 20
    (long_,) = V([Moment(start=0, end=100, score=1)])
    assert long_.end - long_.start <= 60 and long_.end in {5.0 * k for k in range(25)}


def test_validate_short_at_end_moves_start_back():
    (m,) = V([Moment(start=110, end=118, score=1)])
    assert m.end == 120 and m.end - m.start >= 20 and m.start >= 60


def test_validate_drops_invalid():
    assert V([Moment(start=50, end=50), Moment(start=60, end=40),
              Moment(start=float("nan"), end=30), Moment(start=130, end=150)]) == []


def test_validate_overlaps_keep_higher_score_sorted_top_n():
    raw = [Moment(start=0, end=30, score=0.4, hook="low"),
           Moment(start=20, end=50, score=0.9, hook="high"),
           Moment(start=60, end=90, score=0.7, hook="mid"),
           Moment(start=90, end=115, score=0.8, hook="touch")]  # touching (not overlapping) mid
    got = V(raw)
    assert [m.hook for m in got] == ["high", "touch", "mid"]
    assert [m.hook for m in V(raw, count=2)] == ["high", "touch"]


def test_validate_video_shorter_than_min_uses_whole_video():
    t = fake_transcript(12, 4)
    (m,) = V([Moment(start=2, end=8, score=1)], t=t)
    assert (m.start, m.end) == (0, 12)


def test_validate_no_segments_keeps_times():
    t = Transcript(language="en", duration=100, segments=[])
    (m,) = V([Moment(start=12.3, end=40.1, score=1)], t=t)
    assert (m.start, m.end) == (12.3, 40.1)


def test_run_moments_validates_writes_and_reuses(tmp_path):
    t = fake_transcript(120, 5)
    llm = FakeLLM(moments=[Moment(start=1, end=29, score=0.5, hook="नमस्ते"),
                           Moment(start=40.5, end=70, score=0.9),
                           Moment(start=45, end=80, score=0.1)])
    out = tmp_path / "moments.json"
    cs = ClipSettings(count=5, min_seconds=20, max_seconds=60)
    got = mom.run_moments(t, llm, cs, out)
    assert [m.score for m in got] == [0.9, 0.5]
    assert got[1].start == 0 and got[1].end == 30
    assert llm.calls[0] == ("pick_moments", dict(count=5, min_seconds=20, max_seconds=60,
                                                 video_duration=120))
    raw = out.read_text(encoding="utf-8")
    assert "नमस्ते" in raw
    assert [Moment.model_validate(d) for d in json.loads(raw)] == got
    assert mom.run_moments(t, llm, cs, out) == got and len(llm.calls) == 1


def test_run_moments_errors(tmp_path):
    cs = ClipSettings()
    with pytest.raises(mom.MomentsError, match="empty"):
        mom.run_moments(Transcript(language="en", duration=10, segments=[]), FakeLLM(), cs,
                        tmp_path / "m.json")
    with pytest.raises(mom.MomentsError, match="none were valid"):
        mom.run_moments(fake_transcript(120), FakeLLM(moments=[Moment(start=200, end=230)]), cs,
                        tmp_path / "m2.json")
    assert not (tmp_path / "m2.json").exists()


# ============================================================================ script.py


def test_count_and_target_words():
    assert scr.count_words("  a  b\nc ") == 3
    assert scr.count_words("यह एक परीक्षण है") == 4
    assert scr.target_words(30, "en") == round(30 * 2.6 * 0.9) == 70
    assert scr.target_words(30, "hi") == round(30 * 2.2 * 0.9) == 59
    assert scr.target_words(1, "en") == 5
    assert scr.target_words(30, "xx") == 70


def test_normalize_script():
    s = scr.normalize_script(ClipScript(
        narration="  hello   world ", title="T" * 30 + " " + "word " * 20, description=" d ",
        hashtags=["viral", "#Viral", "#two words", "#a #b", "#हिंदी", "##", "c", "d", "e", "f", "g"]))
    assert len(s.title) <= 70 and not s.title.endswith(" ")
    assert s.narration == "hello world" and s.description == "d"
    assert s.hashtags == ["#viral", "#two", "#words", "#a", "#b", "#हिंदी", "#c", "#d"]
    assert all(" " not in h and h.startswith("#") for h in s.hashtags)


def test_normalize_title_without_spaces_hard_cut():
    assert len(scr.normalize_script(ClipScript(narration="x", title="x" * 100, description="")).title) == 70


def test_normalize_keeps_devanagari_matras():
    s = scr.normalize_script(ClipScript(narration="x", title="t", description="",
                                        hashtags=["#हिंदी", "#क्रिकेट"]))
    assert s.hashtags == ["#हिंदी", "#क्रिकेट"]


def test_write_script_hindi_writes_utf8(tmp_path):
    t = fake_transcript(120, 5)
    llm = FakeLLM()
    out = tmp_path / "clips" / "00" / "hi" / "script.json"
    s = scr.write_script(llm=llm, transcript=t, moment=Moment(start=10, end=40), lang="hi", out_json=out)
    assert llm.calls == [("write_script", dict(lang="hi", duration=30, target_words=59))]
    raw = out.read_text(encoding="utf-8")
    assert "यह" in raw and "\\u" not in raw
    assert scr.load_script(out) == s
    assert scr.count_words(s.narration) == 59


def test_write_script_passes_clip_transcript(tmp_path):
    seen = {}

    class Spy(FakeLLM):
        def write_script(self, **kw):
            seen.update(kw)
            return super().write_script(**kw)

    t = fake_transcript(120, 5)
    scr.write_script(llm=Spy(), transcript=t, moment=Moment(start=10, end=20), lang="en",
                     out_json=tmp_path / "s.json")
    assert seen["clip_transcript"] == mom.format_transcript(t, 10, 20)
    assert seen["moment"] == Moment(start=10, end=20)


def test_write_script_shortens_once_when_too_long(tmp_path):
    llm = FakeLLM(narration_words=200)
    s = scr.write_script(llm=llm, transcript=fake_transcript(120), moment=Moment(start=0, end=30),
                         lang="en", out_json=tmp_path / "s.json")
    assert [c[0] for c in llm.calls] == ["write_script", "shorten"]
    assert llm.calls[1][1] == dict(lang="en", max_words=70)
    assert scr.count_words(s.narration) == 70


def test_write_script_no_shorten_within_threshold(tmp_path):
    llm = FakeLLM(narration_words=80)  # 80 <= 70 * 1.15
    scr.write_script(llm=llm, transcript=fake_transcript(120), moment=Moment(start=0, end=30),
                     lang="en", out_json=tmp_path / "s.json")
    assert [c[0] for c in llm.calls] == ["write_script"]


def test_write_script_empty_narration(tmp_path):
    class Empty(FakeLLM):
        def write_script(self, **kw):
            return ClipScript(narration="  ", title="t", description="d")

    with pytest.raises(scr.ScriptError, match="empty"):
        scr.write_script(llm=Empty(), transcript=fake_transcript(), moment=Moment(start=0, end=30),
                         lang="en", out_json=tmp_path / "s.json")


def test_load_script_errors(tmp_path):
    with pytest.raises(scr.ScriptError, match="not found"):
        scr.load_script(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text('{"narration": 1', encoding="utf-8")
    with pytest.raises(scr.ScriptError, match="could not read"):
        scr.load_script(bad)


def test_make_shorten_fn():
    llm = FakeLLM()
    fn = scr.make_shorten_fn(llm, "hi")
    assert fn("एक दो तीन चार", 2) == "एक दो"
    assert llm.calls == [("shorten", dict(lang="hi", max_words=2))]


def test_end_to_end_with_anthropic_llm_and_fake_client(tmp_path):
    """moments + script through the real AnthropicLLM with a fake client."""
    t = fake_transcript(120, 5)
    client = FakeClient(
        resp(json.dumps({"moments": [{"start": 9, "end": 41, "hook": "h", "reason": "r", "score": 0.8}]})),
        resp(json.dumps({"narration": "one two three four five six", "title": "Title",
                         "description": "Desc", "hashtags": ["a", "b", "c"]})))
    llm = AnthropicLLM(settings(use_fallbacks=False), client=client)
    (m,) = mom.run_moments(t, llm, ClipSettings(), tmp_path / "moments.json")
    assert (m.start, m.end) == (10, 40)
    s = scr.write_script(llm=llm, transcript=t, moment=m, lang="en", out_json=tmp_path / "script.json")
    assert s.hashtags == ["#a", "#b", "#c"]
    assert len(client.messages.calls) == 2


# ============================================================================ FAILURES-3 atomic writers


def _boom(*a, **k):
    raise ValueError("cannot serialize")


def test_moments_and_script_writers_leave_no_partial_file(tmp_path, monkeypatch):
    out = tmp_path / "job"
    m = [Moment(start=0, end=30, hook="h", reason="r", score=0.9)]
    s = ClipScript(narration="n", title="t", description="d", hashtags=["#a"])
    mom.save_moments(m, out / "moments.json")
    scr.save_script(s, out / "script.json")
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    monkeypatch.setattr(mom.json, "dumps", _boom)
    monkeypatch.setattr(scr.json, "dumps", _boom)
    with pytest.raises(ValueError):
        mom.save_moments(m * 2, out / "moments.json")
    with pytest.raises(ValueError):
        scr.save_script(s, out / "script.json")
    with pytest.raises(ValueError):
        mom.save_moments(m, out / "new_moments.json")
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def test_atomic_helper_cleans_tmp_when_replace_fails(tmp_path, monkeypatch):
    from autoclipper.stages import _atomic

    out = tmp_path / "job"
    out.mkdir()
    monkeypatch.setattr(_atomic.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("locked")))
    with pytest.raises(OSError):
        scr.save_script(ClipScript(narration="n", title="t", description="d"), out / "script.json")
    assert list(out.iterdir()) == []
