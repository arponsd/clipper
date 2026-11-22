"""Settings, schema, Voicebox profiles, doctor, queue and task routes."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_web_support import (BASE, FakeProfilesClient, Stub, api, api_idle, assert_error_shape,  # noqa: F401
                              assert_keys, assert_task, make_client, make_job_on_disk, read_json, wait_for)

from autoclipper.config import Settings, settings_path
from autoclipper.voicebox import VoiceboxError
from autoclipper.web.auth import TOKEN_HEADER


# ---- settings -------------------------------------------------------------------------------------
def test_get_settings_and_schema(api):
    client, _, _ = api
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.json() == Settings.model_validate(read_json(settings_path())).model_dump()
    assert set(r.json()) == set(Settings.model_fields)
    r = client.get("/api/settings/schema")
    assert r.status_code == 200
    schema = r.json()
    assert schema == Settings.model_json_schema()
    assert "$defs" in schema and "properties" in schema


def test_put_settings_writes_file(api):
    client, _, _ = api
    s = client.get("/api/settings").json()
    s["captions"]["font_size"] = 90
    s["voicebox"]["hindi"]["profile_id"] = "हिंदी-प्रोफ़ाइल"
    s["clips"]["reframe"] = "blur"
    s["languages"] = ["hi"]
    r = client.put("/api/settings", json=s)
    assert r.status_code == 200, r.text
    assert r.json() == s
    on_disk = read_json(settings_path())
    assert on_disk == s
    assert "हिंदी-प्रोफ़ाइल" in settings_path().read_text(encoding="utf-8")  # ensure_ascii=False
    assert not list(settings_path().parent.glob(".settings.json.*.tmp"))
    assert client.get("/api/settings").json() == s


@pytest.mark.parametrize("mutate,field", [
    (lambda s: s.update(bogus=1), "bogus"),
    (lambda s: s["captions"].update(bogus=1), "captions.bogus"),
    (lambda s: s["voicebox"]["english"].update(x=1), "voicebox.english.x"),
    (lambda s: s["captions"].update(font_size="big"), "captions.font_size"),
    (lambda s: s["captions"].update(font_size="78"), "captions.font_size"),
    (lambda s: s["clips"].update(count=0), "clips.count"),
    (lambda s: s["clips"].update(count=-3), "clips.count"),
    (lambda s: s["clips"].update(count=10 ** 20), "clips.count"),
    (lambda s: s["clips"].update(min_seconds=60, max_seconds=30), "clips.max_seconds"),
    (lambda s: s["clips"].update(reframe="zoom"), "clips.reframe"),
    (lambda s: s["llm"].update(effort="ultra"), "llm.effort"),
    (lambda s: s["llm"].update(max_tokens=1), "llm.max_tokens"),
    (lambda s: s["audio"].update(original_volume=-0.1), "audio.original_volume"),
    (lambda s: s.update(languages=[]), "languages"),
    (lambda s: s.update(languages=["fr"]), "languages.0"),
    (lambda s: s.update(languages=["en", "en"]), "languages"),
    (lambda s: s.update(captions="x"), "captions"),
    (lambda s: s["whisper"].update(model=5), "whisper.model"),
])
def test_put_settings_validation(api, mutate, field):
    client, _, _ = api
    before = settings_path().read_bytes()
    s = client.get("/api/settings").json()
    mutate(s)
    body = assert_error_shape(client.put("/api/settings", json=s), 422, "validation failed")
    assert field in body["errors"], body["errors"]
    assert settings_path().read_bytes() == before


def test_put_settings_very_long_string_is_saved_or_rejected_cleanly(api):
    client, _, _ = api
    s = client.get("/api/settings").json()
    s["llm"]["model"] = "m" * 100_000
    r = client.put("/api/settings", json=s)
    assert r.status_code in (200, 422)
    assert "Traceback" not in r.text and len(r.text) < 300_000


def test_put_settings_auth(api):
    client, _, _ = api
    s = client.get("/api/settings").json()
    bare = TestClient(client.app, base_url=BASE)
    assert_error_shape(bare.put("/api/settings", json=s), 401)
    assert_error_shape(bare.put("/api/settings", json=s, headers={TOKEN_HEADER: "x"}), 403)
    assert_error_shape(client.put("/api/settings", json=s, headers={"Origin": "http://evil.com"}), 403)
    assert_error_shape(bare.get("/api/settings"), 401)
    assert_error_shape(bare.get("/api/settings/schema"), 401)


def test_get_settings_broken_file_clean_error(api):
    client, _, _ = api
    settings_path().write_text('{"clips": {"count": 999}}', encoding="utf-8")
    body = assert_error_shape(client.get("/api/settings"), 500)
    assert "clips.count" in body["errors"]
    # the settings page can still fix it with a PUT
    assert client.put("/api/settings", json=Settings().model_dump()).status_code == 200
    assert client.get("/api/settings").status_code == 200


# ---- voicebox profiles ----------------------------------------------------------------------------
def test_profiles_mapped_leniently():
    fake = FakeProfilesClient()
    client, _, _ = make_client(voicebox_factory=lambda s: fake)
    r = client.get("/api/voicebox/profiles")
    assert r.status_code == 200
    assert r.json() == {"profiles": [{"id": "p1", "name": "Narrator", "language": "en"},
                                     {"id": "p2", "name": "Hindi", "language": None}]}
    for p in r.json()["profiles"]:
        assert_keys(p, {"id": str, "name": str, "language": (str, type(None))})
    assert fake.closed


@pytest.mark.parametrize("exc", [VoiceboxError("connection refused"), OSError("boom"), RuntimeError("x")])
def test_profiles_unreachable_503(exc):
    client, _, _ = make_client(voicebox_factory=lambda s: FakeProfilesClient(fail=exc))
    body = assert_error_shape(client.get("/api/voicebox/profiles"), 503)
    assert body["message"] == "Voicebox is not reachable at http://127.0.0.1:17493 - is it running?"


def test_profiles_factory_gets_settings_and_auth():
    seen = []

    def factory(s):
        seen.append(s.voicebox.url)
        return FakeProfilesClient(profiles=[])

    client, _, _ = make_client(voicebox_factory=factory)
    assert client.get("/api/voicebox/profiles").json() == {"profiles": []}
    assert seen == ["http://127.0.0.1:17493"]
    bare = TestClient(client.app, base_url=BASE)
    assert_error_shape(bare.get("/api/voicebox/profiles"), 401)
    assert_error_shape(bare.get("/api/voicebox/profiles", headers={TOKEN_HEADER: "x"}), 403)


# ---- doctor ---------------------------------------------------------------------------------------
def test_doctor(api):
    client, _, _ = api
    r = client.get("/api/doctor")
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, {"checks": list, "problems": int, "warnings": int})
    assert body["problems"] == 1 and body["warnings"] == 1
    for c in body["checks"]:
        assert_keys(c, {"name": str, "level": str, "detail": str})
        assert c["level"] in ("ok", "warn", "fail")
    bare = TestClient(client.app, base_url=BASE)
    assert_error_shape(bare.get("/api/doctor"), 401)
    assert_error_shape(bare.get("/api/doctor", headers={TOKEN_HEADER: "x"}), 403)


# ---- queue / tasks --------------------------------------------------------------------------------
def test_queue_snapshot_positions(api_idle):
    client, worker, _ = api_idle
    r = client.get("/api/queue")
    assert r.json() == {"running": None, "queued": [], "recent": []}
    ids = [client.post("/api/jobs", json={"url": f"https://e.com/{i}"}).json()["task"]["id"] for i in range(3)]
    q = client.get("/api/queue").json()
    assert_keys(q, {"running": type(None), "queued": list, "recent": list})
    assert [t["id"] for t in q["queued"]] == ids
    assert [t["position"] for t in q["queued"]] == [1, 2, 3]
    for t in q["queued"]:
        assert_task(t)
    t = client.get(f"/api/tasks/{ids[2]}").json()
    assert_task(t)
    assert t["position"] == 3 and t["state"] == "queued"


def test_queue_running_and_recent():
    stub = Stub(block=True)
    client, worker, _ = make_client(stub=stub)
    with client:
        a = client.post("/api/jobs", json={"url": "https://e.com/a"}).json()["task"]["id"]
        b = client.post("/api/jobs", json={"url": "https://e.com/b"}).json()["task"]["id"]
        assert stub.started.wait(5)
        q = client.get("/api/queue").json()
        assert q["running"]["id"] == a and q["running"]["position"] == 0 and q["running"]["state"] == "running"
        assert_task(q["running"])
        assert [t["id"] for t in q["queued"]] == [b] and q["queued"][0]["position"] == 1
        stub.release.set()
        assert worker.wait_idle(10)
        q = client.get("/api/queue").json()
        assert q["running"] is None and q["queued"] == []
        assert [t["id"] for t in q["recent"]] == [b, a]  # newest first
        assert all(t["state"] == "done" and t["position"] is None for t in q["recent"])


def test_cancel_queued_task(api_idle):
    client, worker, _ = api_idle
    job = client.post("/api/jobs", json={"url": "https://e.com/a"}).json()
    tid = job["task"]["id"]
    r = client.post(f"/api/tasks/{tid}/cancel")
    assert r.status_code == 200
    assert_task(r.json())
    assert r.json()["state"] == "cancelled" and r.json()["position"] is None
    assert client.get("/api/queue").json()["queued"] == []
    assert_error_shape(client.post(f"/api/tasks/{tid}/cancel"), 409)  # finished -> 409


def test_cancel_running_task_202_then_cancelled():
    stub = Stub(loop_log=True)
    client, worker, _ = make_client(stub=stub)
    with client:
        job = client.post("/api/jobs", json={"url": "https://e.com/a"}).json()
        tid = job["task"]["id"]
        assert stub.started.wait(5)
        r = client.post(f"/api/tasks/{tid}/cancel")
        assert r.status_code == 202 and r.json()["state"] == "running"
        assert worker.wait_idle(10)
        t = client.get(f"/api/tasks/{tid}").json()
        assert t["state"] == "cancelled"
        d = client.get(f"/api/jobs/{job['id']}").json()  # the worker marked the job interrupted
        assert d["status"] == "failed" and d["interrupted"] is True and d["task"] is None


def test_task_404_422_auth(api):
    client, _, _ = api
    assert_error_shape(client.get(f"/api/tasks/{'a' * 32}"), 404)
    assert_error_shape(client.post(f"/api/tasks/{'a' * 32}/cancel"), 404)
    for bad in ("xyz", "A" * 32, "a" * 33, "a" * 31):
        assert "task_id" in assert_error_shape(client.get(f"/api/tasks/{bad}"), 422)["errors"]
        assert "task_id" in assert_error_shape(client.post(f"/api/tasks/{bad}/cancel"), 422)["errors"]
    bare = TestClient(client.app, base_url=BASE)
    assert_error_shape(bare.get("/api/queue"), 401)
    assert_error_shape(bare.get(f"/api/tasks/{'a' * 32}"), 401)
    assert_error_shape(bare.post(f"/api/tasks/{'a' * 32}/cancel", headers={TOKEN_HEADER: "x"}), 403)
    assert_error_shape(client.post(f"/api/tasks/{'a' * 32}/cancel", headers={"Origin": "http://evil.com"}), 403)
