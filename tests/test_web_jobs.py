"""Uploads, job creation (+ Idempotency-Key), job list/detail/log and resume."""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from test_web_support import (BASE, Stub, age_job_json, api, api_idle, assert_error_shape, assert_keys,  # noqa: F401
                              assert_summary, assert_task, make_client, make_job_on_disk, read_json, wait_for)

from autoclipper.config import Settings, data_dir, save_settings
from autoclipper.jobstore import JobStore, jobs_root
from autoclipper.models import Job
from autoclipper.web.auth import TOKEN_HEADER
from autoclipper.web.worker import uploads_root

VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"x" * 5000


def upload(client, data=VIDEO, filename="talk.mp4", ctype="application/octet-stream"):
    return client.post(f"/api/uploads?filename={filename}", content=data, headers={"Content-Type": ctype})


# ---- uploads ------------------------------------------------------------------------------------
def test_upload_happy_path(api):
    client, _, _ = api
    r = upload(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert_keys(body, {"upload_id": str, "filename": str, "size": int})
    assert len(body["upload_id"]) == 32 and body["filename"] == "talk.mp4" and body["size"] == len(VIDEO)
    d = uploads_root() / body["upload_id"]
    assert (d / "source.mp4").read_bytes() == VIDEO
    assert not list(d.glob("*.part*"))


def test_upload_big_body_is_streamed_in_chunks(api):
    client, _, _ = api
    data = bytes(range(256)) * (3 * 4096 + 7)  # ~3 MiB, not a multiple of the chunk size
    r = upload(client, data=data, filename="Big.MKV", ctype="video/x-matroska")
    assert r.status_code == 201, r.text
    assert (uploads_root() / r.json()["upload_id"] / "source.mkv").read_bytes() == data


@pytest.mark.parametrize("filename", ["", "a/b.mp4", "..\\x.mp4", "..mp4", "x.exe", "noext", "x.mp4%00.txt",
                                      "a" * 256 + ".mp4", "%2e%2e%2fetc.mp4", " .mp4"])
def test_upload_bad_filename_422(api, filename):
    client, _, _ = api
    body = assert_error_shape(upload(client, filename=filename), 422)
    assert "filename" in body["errors"]
    assert not uploads_root().exists() or not any(uploads_root().iterdir())


def test_upload_missing_filename_422(api):
    client, _, _ = api
    r = client.post("/api/uploads", content=VIDEO, headers={"Content-Type": "application/octet-stream"})
    assert "filename" in assert_error_shape(r, 422)["errors"]


def test_upload_empty_body_422(api):
    client, _, _ = api
    body = assert_error_shape(upload(client, data=b""), 422)
    assert "body" in body["errors"]
    assert not any(uploads_root().iterdir())


def test_upload_multipart_415(api):
    client, _, _ = api
    r = upload(client, ctype="multipart/form-data; boundary=x")
    assert_error_shape(r, 415)
    assert_error_shape(upload(client, ctype="text/plain"), 415)


def test_upload_too_large_413_by_content_length_and_by_stream(api, monkeypatch):
    client, _, _ = api
    import autoclipper.web.server as server
    from test_web_support import make_settings

    s = make_settings()
    if getattr(s, "web", None) is not None:
        s.web.max_upload_mb = 1
        save_settings(s)
    else:  # section 8 not landed yet: the backend falls back to its defaults
        monkeypatch.setattr(server, "DEFAULT_MAX_UPLOAD_MB", 1)
    big = b"x" * (1024 * 1024 + 1)
    assert_error_shape(upload(client, data=big), 413)

    def gen():  # no Content-Length (chunked): the streaming counter must stop it
        for _ in range(3):
            yield b"y" * (600 * 1024)

    r = client.post("/api/uploads?filename=a.mp4", content=gen(),
                    headers={"Content-Type": "application/octet-stream"})
    assert_error_shape(r, 413)
    assert not any(uploads_root().iterdir())  # partial file and dir removed
    assert upload(client, data=b"z" * (1024 * 1024)).status_code == 201  # exactly at the limit is fine


def test_upload_auth(api):
    client, _, _ = api
    bare = TestClient(client.app, base_url=BASE)
    assert_error_shape(upload(bare), 401)
    assert_error_shape(bare.post("/api/uploads?filename=a.mp4", content=VIDEO,
                                 headers={TOKEN_HEADER: "nope"}), 403)
    assert_error_shape(client.post("/api/uploads?filename=a.mp4", content=VIDEO,
                                   headers={"Origin": "http://evil.com"}), 403)


def test_worker_start_cleans_partial_uploads(tmp_path):
    d = uploads_root() / ("a" * 32)
    d.mkdir(parents=True)
    (d / "source.part.mp4").write_bytes(b"x")
    keep = uploads_root() / ("b" * 32)
    keep.mkdir()
    (keep / "source.mp4").write_bytes(b"x")
    client, worker, _ = make_client()
    with client:
        assert not d.exists()
        assert (keep / "source.mp4").exists()


# ---- POST /api/jobs ------------------------------------------------------------------------------
def test_create_job_from_url(api_idle):
    client, worker, _ = api_idle
    r = client.post("/api/jobs", json={"url": "https://example.com/watch?v=1",
                                       "options": {"clips": 3, "min_seconds": 20, "max_seconds": 45,
                                                   "langs": ["hi"]}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert_summary(body)
    assert body["status"] == "queued" and body["source"] == "https://example.com/watch?v=1"
    assert body["task"]["kind"] == "run_job" and body["task"]["state"] == "queued" and body["task"]["position"] == 1
    jdir = jobs_root() / body["id"]
    job = Job.model_validate_json((jdir / "job.json").read_text(encoding="utf-8"))
    assert job.status == "queued"
    assert read_json(jdir / "options.json") == {"clips": 3, "min_seconds": 20, "max_seconds": 45, "langs": ["hi"]}


def test_create_job_from_upload_moves_file(api_idle):
    client, _, _ = api_idle
    up = upload(client, filename="My Talk.MOV").json()
    r = client.post("/api/jobs", json={"upload_id": up["upload_id"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source"] == "upload:My Talk.MOV"
    jdir = jobs_root() / body["id"]
    assert (jdir / "source.mov").read_bytes() == VIDEO
    assert not (uploads_root() / up["upload_id"]).exists()
    assert read_json(jdir / "options.json") == {"clips": None, "min_seconds": None, "max_seconds": None,
                                                "langs": None}
    # claimed: a second job with the same upload id -> 404
    assert_error_shape(client.post("/api/jobs", json={"upload_id": up["upload_id"]}), 404, "not found")


def test_create_job_unknown_upload_404(api):
    client, _, _ = api
    assert_error_shape(client.post("/api/jobs", json={"upload_id": "0" * 32}), 404, f"upload {'0' * 32} not found")
    assert JobStore.list_ids() == []


@pytest.mark.parametrize("payload,field", [
    ({}, "url"),
    ({"url": "https://a.b/v", "upload_id": "0" * 32}, "upload_id"),
    ({"url": ""}, "url"),
    ({"url": "x" * 2049}, "url"),
    ({"url": "file:///etc/passwd"}, "url"),
    ({"url": "/home/user/video.mp4"}, "url"),
    ({"url": "C:\\videos\\a.mp4"}, "url"),
    ({"url": "ftp://a.b/v.mp4"}, "url"),
    ({"url": "javascript:alert(1)"}, "url"),
    ({"url": "https://"}, "url"),
    ({"url": "https://a.b/v x"}, "url"),
    ({"url": 123}, "url"),
    ({"upload_id": "XYZ"}, "upload_id"),
    ({"upload_id": 5}, "upload_id"),
    ({"url": "https://a.b/v", "extra": 1}, "extra"),
    ({"url": "https://a.b/v", "options": {"clips": 0}}, "options.clips"),
    ({"url": "https://a.b/v", "options": {"clips": -1}}, "options.clips"),
    ({"url": "https://a.b/v", "options": {"clips": 31}}, "options.clips"),
    ({"url": "https://a.b/v", "options": {"clips": 10 ** 30}}, "options.clips"),
    ({"url": "https://a.b/v", "options": {"clips": "3"}}, "options.clips"),
    ({"url": "https://a.b/v", "options": {"clips": 2.5}}, "options.clips"),
    ({"url": "https://a.b/v", "options": {"clips": True}}, "options.clips"),
    ({"url": "https://a.b/v", "options": {"min_seconds": 4}}, "options.min_seconds"),
    ({"url": "https://a.b/v", "options": {"max_seconds": 181}}, "options.max_seconds"),
    ({"url": "https://a.b/v", "options": {"min_seconds": 40, "max_seconds": 30}}, "options.max_seconds"),
    ({"url": "https://a.b/v", "options": {"langs": []}}, "options.langs"),
    ({"url": "https://a.b/v", "options": {"langs": ["en", "en"]}}, "options.langs"),
    ({"url": "https://a.b/v", "options": {"langs": ["fr"]}}, "options.langs.0"),
    ({"url": "https://a.b/v", "options": {"langs": "en"}}, "options.langs"),
    ({"url": "https://a.b/v", "options": {"bogus": 1}}, "options.bogus"),
    ({"url": "https://a.b/v", "options": []}, "options"),
])
def test_create_job_validation_422(api, payload, field):
    client, _, _ = api
    body = assert_error_shape(client.post("/api/jobs", json=payload), 422, "validation failed")
    assert field in body["errors"], body
    assert JobStore.list_ids() == []


def test_create_job_auth(api):
    client, _, _ = api
    bare = TestClient(client.app, base_url=BASE)
    assert_error_shape(bare.post("/api/jobs", json={"url": "https://a.b/v"}), 401)
    assert_error_shape(bare.post("/api/jobs", json={"url": "https://a.b/v"}, headers={TOKEN_HEADER: "bad"}), 403)
    assert JobStore.list_ids() == []


# ---- idempotency ------------------------------------------------------------------------------------
def test_idempotency_replay_conflict_and_persistence(api_idle):
    client, worker, _ = api_idle
    payload = {"url": "https://example.com/v", "options": {"clips": 2}}
    r1 = client.post("/api/jobs", json=payload, headers={"Idempotency-Key": "k-1"})
    assert r1.status_code == 201
    r2 = client.post("/api/jobs", json=payload, headers={"Idempotency-Key": "k-1"})
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]
    assert r2.json()["task"]["id"] == r1.json()["task"]["id"]
    assert_summary(r2.json())
    assert len(JobStore.list_ids()) == 1
    assert len(worker.snapshot()["queued"]) == 1
    r3 = client.post("/api/jobs", json={"url": "https://example.com/other"}, headers={"Idempotency-Key": "k-1"})
    assert_error_shape(r3, 409, "Idempotency-Key was already used for a different request")
    store = read_json(data_dir() / "web" / "idempotency.json")
    assert store["k-1"]["job_id"] == r1.json()["id"]
    # without a key: always a new job
    assert client.post("/api/jobs", json=payload).status_code == 201
    assert len(JobStore.list_ids()) == 2


def test_idempotency_survives_restart_and_expires():
    client, _, _ = make_client(start=False)
    with client:
        r1 = client.post("/api/jobs", json={"url": "https://e.com/v"}, headers={"Idempotency-Key": "abc"})
        assert r1.status_code == 201
    client2, _, _ = make_client(start=False)
    with client2:
        r2 = client2.post("/api/jobs", json={"url": "https://e.com/v"}, headers={"Idempotency-Key": "abc"})
        assert r2.status_code == 200 and r2.json()["id"] == r1.json()["id"]
        # age the record past 24 h -> pruned, the key works again
        p = data_dir() / "web" / "idempotency.json"
        data = read_json(p)
        data["abc"]["created_at"] = time.time() - 25 * 3600
        p.write_text(json.dumps(data), encoding="utf-8")
        r3 = client2.post("/api/jobs", json={"url": "https://e.com/v"}, headers={"Idempotency-Key": "abc"})
        assert r3.status_code == 201 and r3.json()["id"] != r1.json()["id"]


@pytest.mark.parametrize("key", ["", "x" * 201, "has space", "ü"])
def test_idempotency_key_validation(api, key):
    client, _, _ = api
    r = client.post("/api/jobs", json={"url": "https://e.com/v"}, headers={"Idempotency-Key": key.encode("utf-8")})
    assert "Idempotency-Key" in assert_error_shape(r, 422)["errors"]
    assert JobStore.list_ids() == []


def test_idempotent_upload_replay_does_not_touch_upload(api_idle):
    client, _, _ = api_idle
    up = upload(client).json()
    r1 = client.post("/api/jobs", json={"upload_id": up["upload_id"]}, headers={"Idempotency-Key": "u1"})
    assert r1.status_code == 201
    r2 = client.post("/api/jobs", json={"upload_id": up["upload_id"]}, headers={"Idempotency-Key": "u1"})
    assert r2.status_code == 200 and r2.json()["id"] == r1.json()["id"]


# ---- list / detail / log -----------------------------------------------------------------------------
def test_list_jobs(api):
    client, _, _ = api
    a = make_job_on_disk(job_id="20260101-000000-aaaaaa")
    b = make_job_on_disk(job_id="20260102-000000-bbbbbb", status="failed", error="interrupted", final=False)
    bad = jobs_root() / "20260103-000000-cccccc"
    bad.mkdir()
    (bad / "job.json").write_text("{broken", encoding="utf-8")
    r = client.get("/api/jobs")
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, {"jobs": list, "total": int, "limit": int, "offset": int})
    assert body["total"] == 3 and body["limit"] == 50 and body["offset"] == 0
    ids = [j["id"] for j in body["jobs"]]
    assert ids == [bad.name, b.job.id, a.job.id]  # newest first
    for j in body["jobs"]:
        assert_summary(j)
    broken, jb, ja = body["jobs"]
    assert broken["status"] == "failed" and broken["error"] == "job.json unreadable"
    assert ja["rendered"] == 4 and ja["failed"] == 0 and ja["clip_count"] == 2 and ja["langs"] == ["en", "hi"]
    assert jb["interrupted"] is True and jb["rendered"] == 0
    r = client.get("/api/jobs?limit=1&offset=1")
    assert [j["id"] for j in r.json()["jobs"]] == [b.job.id] and r.json()["total"] == 3
    assert client.get("/api/jobs?offset=100000").json()["jobs"] == []


@pytest.mark.parametrize("q,field", [("limit=0", "limit"), ("limit=201", "limit"), ("limit=-5", "limit"),
                                     ("limit=abc", "limit"), ("offset=-1", "offset"), ("offset=100001", "offset"),
                                     ("limit=99999999999999999999999", "limit"), ("limit=", "limit")])
def test_list_jobs_bad_query_422(api, q, field):
    client, _, _ = api
    assert field in assert_error_shape(client.get(f"/api/jobs?{q}"), 422)["errors"]


def test_job_detail_structure(api):
    client, _, _ = api
    store = make_job_on_disk(clips=1)
    (jobs_root() / store.job.id / "options.json").write_text(
        json.dumps({"clips": 1, "min_seconds": None, "max_seconds": None, "langs": ["en", "hi"]}), encoding="utf-8")
    # make hi stale: edit script narration without regenerating the voice
    sp = store.clip_dir(0, "hi") / "script.json"
    s = read_json(sp)
    s["narration"] = "नया पाठ"
    sp.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    r = client.get(f"/api/jobs/{store.job.id}")
    assert r.status_code == 200
    d = r.json()
    from test_web_support import SUMMARY_KEYS

    assert_keys(d, {**SUMMARY_KEYS, "stages": dict, "options": (dict, type(None)), "clips": list})
    assert set(d["stages"]) >= {"ingest", "transcribe", "moments", "cut"}
    for st in d["stages"].values():
        assert_keys(st, {"status": str, "progress": float, "message": str})
    assert d["options"] == {"clips": 1, "min_seconds": None, "max_seconds": None, "langs": ["en", "hi"]}
    (c,) = d["clips"]
    assert_keys(c, {"index": int, "moment": dict, "cut": bool, "langs": dict})
    assert_keys(c["moment"], {"start": float, "end": float, "duration": float, "hook": str, "reason": str,
                              "score": float})
    assert c["moment"]["duration"] == 12.5 and c["cut"] is True
    en = c["langs"]["en"]
    assert_keys(en, {"stages": dict, "error": (str, type(None)), "final": bool, "video_url": (str, type(None)),
                     "download_url": (str, type(None)), "script": (dict, type(None)),
                     "metadata": (dict, type(None)), "voice": (dict, type(None)), "voice_stale": bool,
                     "warning": (str, type(None)), "caption_engine": (str, type(None)),
                     "caption_style": (str, type(None)), "render_stale": bool,
                     "task": (dict, type(None)), "publish_url": str})  # step 4: publish_url (additive)
    assert set(en["stages"]) == {"script", "voice", "captions", "render"}
    assert en["final"] is True and en["video_url"] == f"/api/jobs/{store.job.id}/clips/0/en/video"
    assert en["download_url"] == f"/api/jobs/{store.job.id}/clips/0/en/download"
    assert_keys(en["script"], {"narration": str, "title": str, "description": str, "hashtags": list})
    assert_keys(en["metadata"], {"language": str, "title": str, "description": str, "hashtags": list})
    assert_keys(en["voice"], {"duration": float, "clip_duration": float, "tempo": float, "shortened": bool,
                              "trimmed": bool, "lengthened": bool})
    assert en["voice_stale"] is False and c["langs"]["hi"]["voice_stale"] is True
    assert c["langs"]["hi"]["script"]["narration"] == "नया पाठ"
    assert en["task"] is None


def test_job_detail_missing_outputs(api):
    client, _, _ = api
    store = make_job_on_disk(clips=1, langs=("en",), final=False, script=False, voice=False, cut=False,
                             status="running")
    en = client.get(f"/api/jobs/{store.job.id}").json()["clips"][0]["langs"]["en"]
    assert en["final"] is False and en["video_url"] is None and en["download_url"] is None
    assert en["script"] is None and en["metadata"] is None and en["voice"] is None and en["voice_stale"] is False
    d = client.get(f"/api/jobs/{store.job.id}").json()
    assert d["clips"][0]["cut"] is False and d["options"] is None


@pytest.mark.parametrize("bad", ["..", "a.b", "-abc", "x" * 65, "a%2Fb", "ab$"])
def test_job_detail_bad_id_422(api, bad):
    client, _, _ = api
    r = client.get(f"/api/jobs/{bad}")
    assert r.status_code in (404, 422)  # "a%2Fb" may be routed as two segments -> unknown path 404
    assert_error_shape(r, r.status_code)
    if bad not in ("..", "a%2Fb"):
        assert "job_id" in r.json()["errors"]


def test_job_detail_404_and_auth(api):
    client, _, _ = api
    assert_error_shape(client.get("/api/jobs/20990101-000000-ffffff"), 404, "job 20990101-000000-ffffff not found")
    bare = TestClient(client.app, base_url=BASE)
    assert_error_shape(bare.get("/api/jobs/x"), 401)
    assert_error_shape(bare.get("/api/jobs/x", headers={TOKEN_HEADER: "bad"}), 403)


def test_job_detail_unreadable_job_json(api):
    client, _, _ = api
    d = jobs_root() / "20260103-000000-cccccc"
    d.mkdir(parents=True)
    (d / "job.json").write_text("{", encoding="utf-8")
    r = client.get(f"/api/jobs/{d.name}")
    assert r.status_code == 200 and r.json()["error"] == "job.json unreadable" and r.json()["clips"] == []


def test_job_log(api):
    client, _, _ = api
    store = make_job_on_disk(clips=1)
    assert client.get(f"/api/jobs/{store.job.id}/log").json() == {"lines": []}
    (store.dir / "web.log").write_text("".join(f"line {i} हिंदी\n" for i in range(300)), encoding="utf-8")
    r = client.get(f"/api/jobs/{store.job.id}/log?tail=3")
    assert r.status_code == 200 and r.json() == {"lines": ["line 297 हिंदी", "line 298 हिंदी", "line 299 हिंदी"]}
    assert len(client.get(f"/api/jobs/{store.job.id}/log").json()["lines"]) == 200
    for q in ("tail=0", "tail=2001", "tail=-1", "tail=x"):
        assert "tail" in assert_error_shape(client.get(f"/api/jobs/{store.job.id}/log?{q}"), 422)["errors"]
    assert_error_shape(client.get("/api/jobs/nojob/log"), 404)
    assert_error_shape(TestClient(client.app, base_url=BASE).get(f"/api/jobs/{store.job.id}/log"), 401)


# ---- resume ------------------------------------------------------------------------------------------
def test_resume_interrupted_job_queues_with_stored_options(api):
    client, worker, stub = api
    store = make_job_on_disk(status="failed", error="interrupted", final=False)
    (store.dir / "options.json").write_text(json.dumps({"clips": 2, "min_seconds": 15, "max_seconds": None,
                                                        "langs": ["hi"]}), encoding="utf-8")
    r = client.post(f"/api/jobs/{store.job.id}/resume")
    assert r.status_code == 202, r.text
    body = r.json()
    assert_keys(body, {"job": dict, "task": dict})
    assert_summary(body["job"])
    assert_task(body["task"])
    assert body["task"]["kind"] == "run_job" and body["task"]["job_id"] == store.job.id
    assert worker.wait_idle(10)
    assert stub.calls == [("run_job", store.job.id, dict(source="https://example.com/v.mp4", langs=["hi"], count=2,
                                                         min_seconds=15, max_seconds=None))]


def test_resume_without_options_uses_job_langs(api):
    client, worker, stub = api
    store = make_job_on_disk(langs=("hi",), status="failed", error="interrupted")
    assert client.post(f"/api/jobs/{store.job.id}/resume").status_code == 202
    assert worker.wait_idle(10)
    assert stub.calls[0][2]["langs"] == ["hi"]


def test_resume_idempotent_while_active(api_idle):
    client, worker, _ = api_idle
    store = make_job_on_disk(status="failed", error="interrupted")
    r1 = client.post(f"/api/jobs/{store.job.id}/resume")
    r2 = client.post(f"/api/jobs/{store.job.id}/resume")
    assert r1.status_code == 202 and r2.status_code == 200
    assert r1.json()["task"]["id"] == r2.json()["task"]["id"]
    assert len(worker.snapshot()["queued"]) == 1
    assert r2.json()["job"]["task"]["id"] == r1.json()["task"]["id"]


def test_resume_complete_job_409(api):
    client, _, _ = api
    store = make_job_on_disk(status="done")
    assert_error_shape(client.post(f"/api/jobs/{store.job.id}/resume"), 409, "job is already complete")
    # a done job with a failed clip-lang can be resumed
    store.job.clips[0].langs["en"].error = "voice: boom"
    store.save()
    assert client.post(f"/api/jobs/{store.job.id}/resume").status_code == 202


def test_resume_running_in_other_process_409(api):
    client, _, _ = api
    store = make_job_on_disk(status="running")
    assert_error_shape(client.post(f"/api/jobs/{store.job.id}/resume"), 409)
    age_job_json(store.job.id)
    assert client.post(f"/api/jobs/{store.job.id}/resume").status_code == 202


def test_resume_404_422_auth(api):
    client, _, _ = api
    assert_error_shape(client.post("/api/jobs/20990101-000000-ffffff/resume"), 404)
    assert "job_id" in assert_error_shape(client.post("/api/jobs/-bad/resume"), 422)["errors"]
    bare = TestClient(client.app, base_url=BASE)
    assert_error_shape(bare.post("/api/jobs/x/resume"), 401)
    assert_error_shape(bare.post("/api/jobs/x/resume", headers={TOKEN_HEADER: "bad"}), 403)
    assert_error_shape(client.post("/api/jobs/x/resume", headers={"Origin": "http://evil.com"}), 403)
