from fakes import FakeLLM, FakeTranscriber, FakeVoicebox, make_tone, make_video
from autoclipper.interfaces import LLM, Transcriber, VoiceboxAPI


def test_fakes_satisfy_protocols(tmp_path):
    assert isinstance(FakeLLM(), LLM)
    assert isinstance(FakeVoicebox(), VoiceboxAPI)
    assert isinstance(FakeTranscriber(), Transcriber)
    p = FakeVoicebox().synthesize("a b c", profile_id="x", language="en", engine="e", dst=tmp_path / "v.wav")
    assert p.stat().st_size > 0
    assert make_video(tmp_path / "s.mp4", seconds=2).stat().st_size > 0
