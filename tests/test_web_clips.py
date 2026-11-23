"""Clip review routes: script GET/PUT, rerender, video (Range) and download."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from test_web_support import (BASE, Stub, api, api_idle, assert_error_shape, assert_keys, assert_task,  # noqa: F401
                              make_client, make_job_on_disk, read_json, wait_for)

from autoclipper.web.auth import TOKEN_HEADER

GOOD = {"narration": "  A brand   new narration  ", "title": "New title", "description": "New description",
        "hashtags": ["#one", "two", "#one"]}


def url(store, clip=0, lang="en", what="script"):
    return f"/api/jobs/{store.job.id}/clips/{clip}/{lang}/{what}"


# ---- GET script ---------------------------------------------------------------------------------
def test_get_script(api):
    client, _, _ = api
    store = make_job_on_disk()
    r = client.get(url(store, 1, "hi"))
    assert r.status_code == 200
    assert_keys(r.json(), {"narration": str, "title": str, "description": str, "hashtags": list,
                           "hook": str, "keywords": list})  # G4/G5 (additive)
    assert r.json()["narration"] == "यह एक परीक्षण है"


def test_get_script_404s_and_422s(api):
    client, _, _ = api
    store = make_job_on_disk(clips=1, langs=("en",))
    assert_error_shape(client.get(f"/api/jobs/20990101-000000-ffffff/clips/0/en/script"), 404, "not found")
    assert_error_shape(client.get(url(store, 5)), 404, f"clip 5 not found in job {store.job.id}")
    assert_error_shape(client.get(url(store, 0, "hi")), 404)  # lang not in this clip
    (store.clip_dir(0, "en") / "script.json").unlink()
    assert_error_shape(client.get(url(store)), 404)
    for bad, field in (("fr", "lang"), ("EN", "lang")):
        assert field in assert_error_shape(client.get(url(store, 0, bad)), 422)["errors"]
    for bad in ("-1", "1000", "abc", "1.5", "99999999999999999999"):
        r = client.get(f"/api/jobs/{store.job.id}/clips/{bad}/en/script")
        assert "clip" in assert_error_shape(r, 422)["errors"]


# ---- PUT script ---------------------------------------------------------------------------------
def test_put_script_saves_normalized_and_metadata(api):
    client, _, _ = api
    store = make_job_on_disk()
    before_job = (store.dir / "job.json").read_bytes()
    r = client.put(url(store), json=GOOD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert_keys(body, {"script": dict, "voice_stale": bool, "style_stale": bool})  # style_stale: G4/G5 (additive)
    assert body["voice_stale"] is True  # narration changed, voice not regenerated
    assert body["script"]["narration"] == "A brand new narration"
    assert body["script"]["hashtags"] == ["#one", "#two"]
    out = store.clip_dir(0, "en")
    assert read_json(out / "script.json") == body["script"]
    assert read_json(out / "metadata.json") == {"language": "en", "title": "New title",
                                                "description": "New description", "hashtags": ["#one", "#two"]}
    assert (store.dir / "job.json").read_bytes() == before_job  # job.json untouched (single writer)
    # same narration as the voice -> not stale
    r = client.put(url(store), json={**GOOD, "narration": "this is a test narration"})
    assert r.json()["voice_stale"] is False


def test_put_script_hindi_title_normalized(api):
    client, _, _ = api
    store = make_job_on_disk()
    title = "हिंदी शीर्षक " * 10  # 130 chars: valid (<= 200), stored <= 70
    r = client.put(url(store, 0, "hi"), json={**GOOD, "title": title, "narration": "नमस्ते दुनिया"})
    assert r.status_code == 200
    assert len(r.json()["script"]["title"]) <= 70
    saved = (store.clip_dir(0, "hi") / "script.json").read_text(encoding="utf-8")
    assert "नमस्ते दुनिया" in saved  # ensure_ascii=False


def test_put_script_without_final_skips_metadata(api):
    client, _, _ = api
    store = make_job_on_disk(final=False)
    meta = store.clip_dir(0, "en") / "metadata.json"
    assert not meta.exists()
    assert client.put(url(store), json=GOOD).status_code == 200
    assert not meta.exists()
    assert read_json(store.clip_dir(0, "en") / "script.json")["title"] == "New title"


@pytest.mark.parametrize("patch,field", [
    ({"narration": ""}, "narration"), ({"narration": "   \n "}, "narration"), ({"narration": "x" * 5001}, "narration"),
    ({"narration": 5}, "narration"), ({"narration": None}, "narration"),
    ({"title": ""}, "title"), ({"title": "  "}, "title"), ({"title": "t" * 201}, "title"), ({"title": ["a"]}, "title"),
    ({"description": "d" * 5001}, "description"), ({"description": 1}, "description"),
    ({"hashtags": "#a"}, "hashtags"), ({"hashtags": ["#a"] * 31}, "hashtags"), ({"hashtags": [""]}, "hashtags.0"),
    ({"hashtags": ["x" * 101]}, "hashtags.0"), ({"hashtags": [1]}, "hashtags.0"), ({"hashtags": [" "]}, "hashtags"),
    ({"extra": True}, "extra"),
])
def test_put_script_validation(api, patch, field):
    client, _, _ = api
    store = make_job_on_disk()
    before = (store.clip_dir(0, "en") / "script.json").read_bytes()
    r = client.put(url(store), json={**GOOD, **patch})
    assert field in assert_error_shape(r, 422, "validation failed")["errors"], r.json()
    assert (store.clip_dir(0, "en") / "script.json").read_bytes() == before


@pytest.mark.parametrize("missing", ["narration", "title"])
def test_put_script_missing_fields(api, missing):
    client, _, _ = api
    store = make_job_on_disk()
    body = {k: v for k, v in GOOD.items() if k != missing}
    assert missing in assert_error_shape(client.put(url(store), json=body), 422)["errors"]


def test_put_script_boundaries_ok(api):
    client, _, _ = api
    store = make_job_on_disk()
    body = {"narration": "n" * 5000, "title": "t" * 200, "description": "", "hashtags": []}
    assert client.put(url(store), json=body).status_code == 200


def test_put_script_404_auth(api):
    client, _, _ = api
    store = make_job_on_disk(clips=1, langs=("en",))
    assert_error_shape(client.put("/api/jobs/20990101-000000-ffffff/clips/0/en/script", json=GOOD), 404)
    assert_error_shape(client.put(url(store, 3), json=GOOD), 404)
    assert_error_shape(client.put(url(store, 0, "hi"), json=GOOD), 404)
    bare = TestClient(client.app, base_url=BASE)
    assert_error_shape(bare.put(url(store), json=GOOD), 401)
    assert_error_shape(bare.put(url(store), json=GOOD, headers={TOKEN_HEADER: "bad"}), 403)
    assert_error_shape(client.put(url(store), json=GOOD, headers={"Origin": "http://evil.com"}), 403)


def test_put_script_409_while_rendering():
    stub = Stub(block=True)
    client, worker, _ = make_client(stub=stub)
    with client:
        store = make_job_on_disk()
        r = client.post(url(store, 0, "en", "rerender"))
        assert r.status_code == 202
        assert stub.started.wait(5)
        assert_error_shape(client.put(url(store, 0, "en"), json=GOOD), 409, "clip is being rendered")
        # another clip-lang of the same job is fine while only this rerender runs
        assert client.put(url(store, 0, "hi"), json=GOOD).status_code == 200
        stub.release.set()
        assert worker.wait_idle(10)
        assert client.put(url(store, 0, "en"), json=GOOD).status_code == 200


def test_put_script_409_while_run_job_running():
    stub = Stub(block=True)
    client, worker, _ = make_client(stub=stub)
    with client:
        store = make_job_on_disk(status="failed", error="interrupted")
        assert client.post(f"/api/jobs/{store.job.id}/resume").status_code == 202
        assert stub.started.wait(5)
        assert_error_shape(client.put(url(store, 1, "hi"), json=GOOD), 409)
        stub.release.set()
        assert worker.wait_idle(10)


# ---- rerender -----------------------------------------------------------------------------------
def test_rerender_queued_then_idempotent(api_idle):
    client, worker, _ = api_idle
    store = make_job_on_disk()
    r1 = client.post(url(store, 0, "hi", "rerender"))
    assert r1.status_code == 202
    assert_keys(r1.json(), {"task": dict})
    t = r1.json()["task"]
    assert_task(t)
    assert t["kind"] == "rerender" and t["clip"] == 0 and t["lang"] == "hi" and t["state"] == "queued"
    assert t["position"] == 1
    r2 = client.post(url(store, 0, "hi", "rerender"))
    assert r2.status_code == 200 and r2.json()["task"]["id"] == t["id"]
    r3 = client.post(url(store, 1, "hi", "rerender"))
    assert r3.status_code == 202 and r3.json()["task"]["position"] == 2
    # detail shows the clip-lang task
    d = client.get(f"/api/jobs/{store.job.id}").json()
    assert d["clips"][0]["langs"]["hi"]["task"]["id"] == t["id"]
    assert d["clips"][0]["langs"]["en"]["task"] is None


def test_rerender_runs_through_worker():
    stub = Stub()
    client, worker, _ = make_client(stub=stub)
    with client:
        store = make_job_on_disk()
        r = client.post(url(store, 1, "en", "rerender"))
        assert r.status_code == 202
        assert worker.wait_idle(10)
        assert stub.calls == [("rerender", store.job.id, 1, "en")]
        t = client.get(f"/api/tasks/{r.json()['task']['id']}").json()
        assert t["state"] == "done" and t["position"] is None and t["finished_at"] is not None


def test_rerender_while_running_rerender_queues_new_one():
    stub = Stub(block=True)
    client, worker, _ = make_client(stub=stub)
    with client:
        store = make_job_on_disk()
        r1 = client.post(url(store, 0, "en", "rerender"))
        assert stub.started.wait(5)
        r2 = client.post(url(store, 0, "en", "rerender"))
        assert r2.status_code == 202 and r2.json()["task"]["id"] != r1.json()["task"]["id"]
        assert r2.json()["task"]["position"] == 1
        stub.release.set()
        assert worker.wait_idle(10)
        assert len(stub.calls) == 2


def test_rerender_409_not_ready_and_run_job_active(api_idle):
    client, _, _ = api_idle
    no_cut = make_job_on_disk(cut=False)
    assert_error_shape(client.post(url(no_cut, 0, "en", "rerender")), 409, "clip is not ready to re-render")
    no_script = make_job_on_disk(script=False)
    assert_error_shape(client.post(url(no_script, 0, "en", "rerender")), 409, "clip is not ready to re-render")
    store = make_job_on_disk(status="failed", error="interrupted")
    assert client.post(f"/api/jobs/{store.job.id}/resume").status_code == 202  # run_job queued
    assert_error_shape(client.post(url(store, 0, "en", "rerender")), 409)


def test_rerender_404_422_auth(api):
    client, _, _ = api
    store = make_job_on_disk(clips=1, langs=("en",))
    assert_error_shape(client.post("/api/jobs/20990101-000000-ffffff/clips/0/en/rerender"), 404)
    assert_error_shape(client.post(url(store, 1, "en", "rerender")), 404)
    assert_error_shape(client.post(url(store, 0, "hi", "rerender")), 404)
    assert "lang" in assert_error_shape(client.post(url(store, 0, "de", "rerender")), 422)["errors"]
    assert "clip" in assert_error_shape(client.post(f"/api/jobs/{store.job.id}/clips/-3/en/rerender"), 422)["errors"]
    bare = TestClient(client.app, base_url=BASE)
    assert_error_shape(bare.post(url(store, 0, "en", "rerender")), 401)
    assert_error_shape(bare.post(url(store, 0, "en", "rerender"), headers={TOKEN_HEADER: "x"}), 403)


# ---- video / download ---------------------------------------------------------------------------
CONTENT = bytes(range(256)) * 40


def test_video_full_and_range(api):
    client, _, _ = api
    store = make_job_on_disk()
    r = client.get(url(store, 0, "en", "video"))
    assert r.status_code == 200 and r.content == CONTENT
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["cache-control"] == "no-store"
    r = client.get(url(store, 0, "en", "video"), headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 100-199/{len(CONTENT)}"
    assert r.content == CONTENT[100:200]
    r = client.get(url(store, 0, "en", "video"), headers={"Range": "bytes=10000-"})
    assert r.status_code == 206 and r.content == CONTENT[10000:]
    r = client.get(url(store, 0, "en", "video"), headers={"Range": "bytes=-16"})
    assert r.status_code == 206 and r.content == CONTENT[-16:]


def test_video_unsatisfiable_and_malformed_range(api):
    client, _, _ = api
    store = make_job_on_disk()
    r = client.get(url(store, 0, "en", "video"), headers={"Range": "bytes=999999-"})
    assert_error_shape(r, 416)
    assert r.headers["content-range"] == f"bytes */{len(CONTENT)}"
    r = client.get(url(store, 0, "en", "video"), headers={"Range": "pages=1-2"})
    assert r.status_code == 200 and r.content == CONTENT  # malformed Range is ignored


def test_video_with_query_token(api):
    client, _, _ = api
    store = make_job_on_disk()
    bare = TestClient(client.app, base_url=BASE)
    r = bare.get(url(store, 0, "hi", "video") + f"?token={client.app.state.token}&v=123",
                 headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == CONTENT[:10]
    assert_error_shape(bare.get(url(store, 0, "hi", "video") + "?token=wrong"), 403)
    assert_error_shape(bare.get(url(store, 0, "hi", "video")), 401)


def test_download_attachment(api):
    client, _, _ = api
    store = make_job_on_disk()
    r = client.get(url(store, 1, "hi", "download"))
    assert r.status_code == 200 and r.content == CONTENT
    cd = r.headers["content-disposition"]
    assert cd == f'attachment; filename="{store.job.id}_clip01_hi.mp4"'
    cd.encode("ascii")
    r = client.get(url(store, 1, "hi", "download"), headers={"Range": "bytes=0-0"})
    assert r.status_code == 206 and r.content == CONTENT[:1]


def test_media_404s(api):
    client, _, _ = api
    store = make_job_on_disk(final=False)
    for what in ("video", "download"):
        assert_error_shape(client.get(url(store, 0, "en", what)), 404, "final.mp4")
        assert_error_shape(client.get(url(store, 9, "en", what)), 404)
        assert_error_shape(client.get(f"/api/jobs/20990101-000000-ffffff/clips/0/en/{what}"), 404)
        assert "lang" in assert_error_shape(client.get(url(store, 0, "xx", what)), 422)["errors"]
