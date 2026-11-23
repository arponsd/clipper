"""AnthropicLLM methods used with the voice-over off (fake SDK client, no network)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fakes import FakeLLM

from autoclipper.config import LLMSettings
from autoclipper.interfaces import CaptionsOnlyLLM
from autoclipper.llm import METADATA_SCHEMA, TRANSLATE_SCHEMA, AnthropicLLM, LLMError
from autoclipper.models import ClipScript, Moment, Transcript, Segment
from autoclipper.stages import script as scr


class _Create:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, *responses):
        self.messages = _Create(responses)
        self.beta = SimpleNamespace(messages=_Create(responses))


def resp(payload) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return SimpleNamespace(stop_reason="end_turn", stop_details=None, content=[SimpleNamespace(type="text", text=text)])


def _walk(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for v in schema.values():
            yield from _walk(v)
    elif isinstance(schema, list):
        for v in schema:
            yield from _walk(v)


@pytest.mark.parametrize("schema", [METADATA_SCHEMA, TRANSLATE_SCHEMA])
def test_new_schemas_are_strict(schema):
    for o in _walk(schema):
        assert o["additionalProperties"] is False and sorted(o["required"]) == sorted(o["properties"])


def test_protocols():
    assert isinstance(AnthropicLLM(LLMSettings(), client=FakeClient()), CaptionsOnlyLLM)
    assert isinstance(FakeLLM(), CaptionsOnlyLLM)


def test_write_publish_metadata_hindi():
    client = FakeClient(resp({"title": "देखिए यह पल", "description": "विवरण", "hashtags": ["#हिंदी"]}))
    s = AnthropicLLM(LLMSettings(), client=client).write_publish_metadata(
        lang="hi", clip_transcript="[0.0-5.0] hello", moment=Moment(start=0, end=20, hook="h", reason="r"),
        duration=20)
    assert s == ClipScript(narration="", title="देखिए यह पल", description="विवरण", hashtags=["#हिंदी"])
    kw = client.beta.messages.calls[0]
    assert kw["output_config"]["format"]["schema"] is METADATA_SCHEMA
    user = kw["messages"][0]["content"]
    assert "Devanagari" in user and "[0.0-5.0] hello" in user and "narration" not in user
    assert "no narrator" in kw["system"]


def test_write_publish_metadata_invalid():
    with pytest.raises(LLMError, match="invalid post metadata"):
        AnthropicLLM(LLMSettings(), client=FakeClient(resp({"title": 5}))).write_publish_metadata(
            lang="en", clip_transcript="", moment=Moment(start=0, end=20), duration=20)


def test_translate_segments_keeps_ids():
    client = FakeClient(resp({"segments": [{"id": 3, "text": " नमस्ते  दुनिया "}, {"id": 7, "text": ""},
                                           {"id": "x", "text": "bad"}]}))
    out = AnthropicLLM(LLMSettings(), client=client).translate_segments(
        lang="hi", source_lang="en", segments=[{"id": 3, "text": "hello world"}, {"id": 7, "text": "ok"}])
    assert out == {3: "नमस्ते दुनिया"}
    kw = client.beta.messages.calls[0]
    assert kw["output_config"]["format"]["schema"] is TRANSLATE_SCHEMA
    user = kw["messages"][0]["content"]
    assert "from English into Hindi" in user and '"id": 3' in user and "Devanagari" in user
    with pytest.raises(LLMError):
        AnthropicLLM(LLMSettings(), client=FakeClient(resp({"segments": []}))).translate_segments(
            lang="en", source_lang="hi", segments=[{"id": 0, "text": "x"}])


def _t() -> Transcript:
    return Transcript(language="en", duration=30, segments=[Segment(start=0, end=10, text="hello there")])


def test_write_metadata_script_and_fallback(tmp_path):
    llm = FakeLLM()
    s = scr.write_metadata_script(llm=llm, transcript=_t(), moment=Moment(start=0, end=10), lang="en",
                                  out_json=tmp_path / "a.json")
    assert s.narration == "" and s.title == "Captions title en"
    assert [n for n, _ in llm.calls] == ["write_publish_metadata"]

    class OldLLM:  # no write_publish_metadata: a normal script whose narration is dropped
        def __init__(self):
            self.inner = FakeLLM()

        def write_script(self, **kw):
            return self.inner.write_script(**kw)

    s2 = scr.write_metadata_script(llm=OldLLM(), transcript=_t(), moment=Moment(start=0, end=10), lang="hi",
                                   out_json=tmp_path / "b.json")
    assert s2.narration == "" and s2.title == "Test title hi"
    assert json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))["narration"] == ""


def test_add_narration_keeps_title(tmp_path):
    base = ClipScript(narration="", title="Mine", description="My d", hashtags=["#mine"])
    out = scr.add_narration(llm=FakeLLM(), transcript=_t(), moment=Moment(start=0, end=10), lang="en",
                            script=base, out_json=tmp_path / "s.json")
    assert out.title == "Mine" and out.hashtags == ["#mine"] and out.narration.strip()
    assert scr.load_script(tmp_path / "s.json") == out
