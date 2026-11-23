"""VoiceboxClient tests: httpx.MockTransport only, no real Voicebox server."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from autoclipper.config import VoiceboxSettings
from autoclipper.voicebox import VoiceboxTimeout, VoiceboxClient, VoiceboxError


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def make_client(handler, *, timeout: float = 600.0, poll: float = 1.0):
    clock = FakeClock()
    client = VoiceboxClient("http://vb.test:17493/", poll_interval=poll, timeout=timeout,
                            transport=httpx.MockTransport(handler), sleep=clock.sleep, clock=clock)
    return client, clock


def test_generate_posts_payload_and_reads_id():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"id": "abc"})

    client, _ = make_client(handler)
    gid = client.generate("नमस्ते दुनिया", profile_id="p1", language="hi", engine="chatterbox")
    assert gid == "abc"
    assert seen[0].method == "POST" and seen[0].url.path == "/generate"
    assert str(seen[0].url).startswith("http://vb.test:17493/")
    assert json.loads(seen[0].content) == {"text": "नमस्ते दुनिया", "profile_id": "p1",
                                           "language": "hi", "engine": "chatterbox"}


def test_generate_accepts_generation_id_and_int():
    client, _ = make_client(lambda r: httpx.Response(200, json={"generation_id": 42}))
    assert client.generate("hi", profile_id="p", language="en", engine="e") == "42"


def test_generate_without_id_raises():
    client, _ = make_client(lambda r: httpx.Response(200, json={"status": "ok"}))
    with pytest.raises(VoiceboxError, match="no id"):
        client.generate("hi", profile_id="p", language="en", engine="e")


def test_generate_empty_text_raises_without_request():
    calls = []
    client, _ = make_client(lambda r: calls.append(r) or httpx.Response(200, json={"id": "x"}))
    with pytest.raises(VoiceboxError, match="empty"):
        client.generate("   ", profile_id="p", language="en", engine="e")
    assert calls == []


def test_http_error_includes_detail():
    client, _ = make_client(lambda r: httpx.Response(422, json={"detail": "profile not found"}))
    with pytest.raises(VoiceboxError, match="HTTP 422.*profile not found"):
        client.generate("hi", profile_id="nope", language="en", engine="e")


def test_wait_polls_until_completed_case_insensitive():
    statuses = iter(["pending", "Generating", "COMPLETED"])
    progress: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/history/g1"
        return httpx.Response(200, json={"id": "g1", "status": next(statuses), "progress": 50})

    client, clock = make_client(handler, poll=0.5)
    data = client.wait("g1", on_progress=lambda f, m: progress.append(f))
    assert data["status"] == "COMPLETED"
    assert clock.sleeps == [0.5, 0.5]
    assert progress[0] == 0.5 and progress[-1] == 1.0


@pytest.mark.parametrize("status,field", [("failed", "error"), ("Error", "message")])
def test_wait_failure_raises_with_server_message(status, field):
    client, _ = make_client(lambda r: httpx.Response(200, json={"status": status, field: "GPU OOM"}))
    with pytest.raises(VoiceboxError, match="GPU OOM"):
        client.wait("g1")


def test_wait_times_out():
    client, clock = make_client(lambda r: httpx.Response(200, json={"status": "pending"}),
                                timeout=5.0, poll=1.0)
    with pytest.raises(VoiceboxError, match="did not finish within 5s"):
        client.wait("g1")
    assert 4 <= len(clock.sleeps) <= 6


def test_download_writes_bytes(tmp_path: Path):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/audio/g1"
        return httpx.Response(200, content=b"RIFFdata")

    client, _ = make_client(handler)
    out = client.download("g1", tmp_path / "sub dir" / "v.wav")
    assert out.read_bytes() == b"RIFFdata"


def test_download_empty_raises(tmp_path: Path):
    client, _ = make_client(lambda r: httpx.Response(200, content=b""))
    with pytest.raises(VoiceboxError, match="empty audio"):
        client.download("g1", tmp_path / "v.wav")


def test_synthesize_full_flow(tmp_path: Path):
    calls: list[str] = []
    history = iter([{"status": "processing"}, {"status": "completed"}])

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.method} {req.url.path}")
        if req.url.path == "/generate":
            return httpx.Response(200, json={"generation_id": "z9"})
        if req.url.path == "/history/z9":
            return httpx.Response(200, json=next(history))
        if req.url.path == "/audio/z9":
            return httpx.Response(200, content=b"WAVBYTES")
        return httpx.Response(404)

    client, _ = make_client(handler)
    progress: list[float] = []
    dst = client.synthesize("hello", profile_id="p", language="en", engine="kokoro",
                            dst=tmp_path / "voice_raw.wav", on_progress=lambda f, m: progress.append(f))
    assert dst.read_bytes() == b"WAVBYTES"
    assert calls == ["POST /generate", "GET /history/z9", "GET /history/z9", "GET /audio/z9"]
    assert progress[0] == 0.0 and progress[-1] == 1.0
    assert all(0.0 <= p <= 1.0 for p in progress) and progress == sorted(progress)


@pytest.mark.parametrize("body", [[{"id": "a"}, {"id": "b"}], {"profiles": [{"id": "a"}, {"id": "b"}]}])
def test_list_profiles_accepts_list_or_wrapped(body):
    client, _ = make_client(lambda r: httpx.Response(200, json=body))
    assert [p["id"] for p in client.list_profiles()] == ["a", "b"]


def test_health_ok_and_connect_error_message():
    client, _ = make_client(lambda r: httpx.Response(200, json=[]))
    client.health()

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=req)

    client, _ = make_client(down)
    with pytest.raises(VoiceboxError) as ei:
        client.health()
    assert str(ei.value) == "Voicebox is not reachable at http://vb.test:17493 - is it running?"


def test_non_json_response_raises():
    client, _ = make_client(lambda r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(VoiceboxError, match="non-JSON"):
        client.list_profiles()


def test_from_settings_uses_values():
    s = VoiceboxSettings(url="http://x.test:1", poll_interval=0.25, timeout_seconds=12)
    c = VoiceboxClient.from_settings(s, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert (c.url, c.poll_interval, c.timeout) == ("http://x.test:1", 0.25, 12)
    c.close()


def test_implements_protocol():
    from autoclipper.interfaces import VoiceboxAPI

    client, _ = make_client(lambda r: httpx.Response(200, json=[]))
    assert isinstance(client, VoiceboxAPI)


# ----------------------------------------------------------------------------- FAILURES-6 cancel


def _generating_server(cancel_status: int = 200):
    reqs: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        reqs.append(req)
        if req.url.path.endswith("/cancel"):
            return httpx.Response(cancel_status, json={"message": "cancelled"})
        return httpx.Response(200, json={"id": "g1", "status": "generating"})

    return handler, reqs


def _cancels(reqs: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in reqs if r.url.path.endswith("/cancel")]


def test_wait_timeout_sends_exactly_one_cancel():
    handler, reqs = _generating_server()
    client, _ = make_client(handler, timeout=3.0, poll=1.0)
    with pytest.raises(VoiceboxError, match="did not finish within 3s") as ei:
        client.wait("g1")
    # the public VoiceboxTimeout (a VoiceboxError), never the internal _WaitTimeout
    assert type(ei.value) is VoiceboxTimeout
    (cancel,) = _cancels(reqs)
    assert cancel.method == "POST" and cancel.url.path == "/generate/g1/cancel"
    assert reqs[-1] is cancel  # no polling after the cancel


def test_wait_interrupt_sends_cancel_and_reraises():
    handler, reqs = _generating_server()
    client, clock = make_client(handler)

    def interrupted_sleep(_s: float) -> None:
        raise KeyboardInterrupt

    client._sleep = interrupted_sleep
    with pytest.raises(KeyboardInterrupt):
        client.wait("g1")
    assert [r.url.path for r in _cancels(reqs)] == ["/generate/g1/cancel"]


def test_synthesize_interrupt_from_progress_callback_cancels(tmp_path: Path):
    handler, reqs = _generating_server()
    client, _ = make_client(handler)

    def progress(f: float, msg: str) -> None:
        if msg == "generating":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        client.synthesize("hi", profile_id="p", language="en", engine="e", dst=tmp_path / "v.wav",
                          on_progress=progress)
    assert len(_cancels(reqs)) == 1


@pytest.mark.parametrize("failure", ["http500", "connect"])
def test_cancel_is_best_effort(failure):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/cancel"):
            if failure == "connect":
                raise httpx.ConnectError("gone", request=req)
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"status": "generating"})

    client, _ = make_client(handler, timeout=1.0)
    assert client.cancel("g1") is False
    with pytest.raises(VoiceboxError, match="did not finish"):  # timeout error still wins
        client.wait("g1")


def test_failed_generation_is_not_cancelled():
    reqs: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        reqs.append(req)
        return httpx.Response(200, json={"status": "failed", "error": "Generation cancelled"})

    client, _ = make_client(handler)
    with pytest.raises(VoiceboxError, match="Generation cancelled"):
        client.wait("g1")
    assert _cancels(reqs) == []
