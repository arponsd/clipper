"""QA co-lead sign-off for the step-2 review fixes: independent reproductions of each accepted failure
scenario plus edge cases the fixer's tests did not cover. Conftest-isolated data dir, stub run_job.
"""
from __future__ import annotations

import json
import logging
import time

import pytest
from fastapi.testclient import TestClient
from test_web_support import (BASE, Stub, api, api_idle, assert_error_shape, make_client,  # noqa: F401
                              make_job_on_disk, make_settings, wait_for)

from autoclipper.config import save_settings, settings_path
from autoclipper.jobstore import jobs_root
from autoclipper.models import Job
from autoclipper.web.auth import TokenRedactFilter, allowed_hosts_for
from autoclipper.web.worker import JobOptions, Worker

INVALID_JSON = {"message": "validation failed", "errors": {"body": ["invalid JSON"]}}


def job_dirs() -> set[str]:
    return {p.name for p in jobs_root().iterdir()} if jobs_root().exists() else set()


def load(job_id: str) -> Job:
    return Job.model_validate_json((jobs_root() / job_id / "job.json").read_text(encoding="utf-8"))


# ---- API-1 ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("peer,gets_token", [
    ("192.168.1.99", False), ("10.0.0.5", False), ("::ffff:192.168.1.9", False), ("testclient", False),
    ("127.0.0.1", True), ("::1", True), ("::ffff:127.0.0.1", True),  # dual-stack bind on "::"
])
def test_index_token_only_for_loopback_peer(peer, gets_token):
    client, _, _ = make_client(allowed_hosts=allowed_hosts_for("0.0.0.0"))
    token = client.app.state.token
    r = TestClient(client.app, base_url=BASE, client=(peer, 1)).get("/", headers={"Host": "127.0.0.1:8765"})
    assert r.status_code == 200
    assert (token in r.text) is gets_token
    assert "__AUTOCLIPPER_TOKEN__" not in r.text


# ---- API-2: every JSON-body route, not just jobs/settings -----------------------------------------
DEEP = "[" * 100000 + "]" * 100000
BIGINT = '{"url": ' + "9" * 5000 + "}"


@pytest.mark.parametrize("body", [DEEP, BIGINT])
def test_put_script_unparseable_json_is_422_and_script_unchanged(api, body):
    client, _, _ = api
    store = make_job_on_disk(clips=1, langs=("en",))
    path = store.clip_dir(0, "en") / "script.json"
    before = path.read_bytes()
    r = client.put(f"/api/jobs/{store.job.id}/clips/0/en/script", content=body,
                   headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and r.json() == INVALID_JSON
    assert path.read_bytes() == before


@pytest.mark.parametrize("body", [DEEP, BIGINT])
def test_post_jobs_unparseable_json_no_job_and_no_idempotency_record(api, body):
    client, _, _ = api
    before = job_dirs()
    r = client.post("/api/jobs", content=body, headers={"Content-Type": "application/json",
                                                        "Idempotency-Key": "qa-1"})
    assert r.status_code == 422 and r.json() == INVALID_JSON
    assert job_dirs() == before


# ---- API-3 / API-4: zero port and other boundaries --------------------------------------------------
@pytest.mark.parametrize("url", ["http://a.b:0/x", "http://a.b:65536/x", "http://a.b:99999/x",
                                 "http://a.b:abc/x", "http://a.b:-1/x", "ftp://a.b/x", "http:///x"])
def test_post_jobs_bad_url_is_422_no_job(api_idle, url):
    client, _, _ = api_idle
    before = job_dirs()
    body = assert_error_shape(client.post("/api/jobs", json={"url": url}), 422)
    assert list(body["errors"]) == ["url"]
    assert job_dirs() == before


@pytest.mark.parametrize("url", ["http://a.b:1/x", "http://a.b:65535/x", "https://a.b/x"])
def test_post_jobs_port_boundaries_accepted(api_idle, url):
    client, _, _ = api_idle
    assert client.post("/api/jobs", json={"url": url}).status_code == 201


@pytest.mark.parametrize("url", ["http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:x",
                                 "ws://127.0.0.1:17493", "http://:17493", "http://h/" + "a" * 2048])
def test_put_settings_bad_voicebox_url_422_file_unchanged(api, url):
    client, _, _ = api
    before = settings_path().read_bytes()
    s = client.get("/api/settings").json()
    s["voicebox"]["url"] = url
    body = assert_error_shape(client.put("/api/settings", json=s), 422)
    assert "voicebox.url" in body["errors"]
    assert settings_path().read_bytes() == before


def test_put_settings_margin_v_against_new_height_in_same_put(api):
    client, _, _ = api
    before = settings_path().read_bytes()
    s = client.get("/api/settings").json()
    s["clips"]["height"] = 1000
    s["captions"]["margin_v"] = 1001
    body = assert_error_shape(client.put("/api/settings", json=s), 422)
    assert "captions.margin_v" in body["errors"]
    assert settings_path().read_bytes() == before
    s["captions"]["margin_v"] = 1000
    r = client.put("/api/settings", json=s)
    assert r.status_code == 200
    assert json.loads(settings_path().read_text())["captions"]["margin_v"] == 1000


@pytest.mark.parametrize("color", ["&H00FFFFF", "&H00FFFFFFF", "00FFFFFF", "&h00FFFFFF", "&H00GGFFFF",
                                   "&H00FFFFFF\n", ""])
def test_put_settings_bad_colour_422(api, color):
    client, _, _ = api
    before = settings_path().read_bytes()
    s = client.get("/api/settings").json()
    s["captions"]["highlight_color"] = color
    body = assert_error_shape(client.put("/api/settings", json=s), 422)
    assert "captions.highlight_color" in body["errors"]
    assert settings_path().read_bytes() == before


# ---- UX-1 ------------------------------------------------------------------------------------------
def test_put_script_eight_hashtags_ok_nine_rejected(api):
    client, _, _ = api
    store = make_job_on_disk(clips=1, langs=("en",))
    url = f"/api/jobs/{store.job.id}/clips/0/en/script"
    path = store.clip_dir(0, "en") / "script.json"
    cur = client.get(url).json()
    base = {k: cur[k] for k in ("narration", "title", "description")}
    before = path.read_bytes()
    body = assert_error_shape(client.put(url, json={**base, "hashtags": [f"#t{i}" for i in range(9)]}), 422)
    assert "hashtags" in body["errors"]
    assert path.read_bytes() == before
    r = client.put(url, json={**base, "hashtags": [f"#t{i}" for i in range(8)]})
    assert r.status_code == 200, r.text
    assert json.loads(path.read_text(encoding="utf-8"))["hashtags"] == [f"#t{i}" for i in range(8)]


# ---- UX-5: a record shaped exactly like uvicorn's access log ------------------------------------------
def test_redact_filter_on_uvicorn_access_record():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("127.0.0.1:5000", "GET", "/api/jobs/x/clips/0/en/video?token=s3cr3t-T0k&x=1",
                             "1.1", 206), None)
    assert TokenRedactFilter().filter(rec) is True
    line = rec.getMessage()
    assert "s3cr3t" not in line
    assert "token=***&x=1" in line


# ---- WORKER-1/3/4 independent reproductions --------------------------------------------------------
def test_stop_with_running_a_and_queued_b(settings_saved_qa):
    stub = Stub(block=True)
    w = Worker(run_job=stub.run_job, rerender=stub.rerender)
    w.start()
    a = w.submit_job("https://example.com/a.mp4", options=JobOptions())
    assert stub.started.wait(5)
    b = w.submit_job("https://example.com/b.mp4", options=JobOptions())
    stub.release.set()
    w.stop(timeout=10)
    assert not w.alive
    jb = load(b.job_id)
    assert jb.status == "failed" and "interrupt" in (jb.error or "").lower()
    assert w.status(b.id).state == "cancelled"
    assert w.status(a.id).state == "cancelled"  # ended while stopping (WORKER-3)
    assert load(a.job_id).status == "failed"
    assert not (jobs_root() / a.job_id / "job.lock").exists()


def test_cancel_cycles_keep_tables_bounded_and_404_for_evicted(api_idle):
    client, w, _ = api_idle
    ids = []
    for _ in range(200):
        t = w.submit_job("https://example.com/v.mp4", options=JobOptions())
        ids.append(t.id)
        r = client.post(f"/api/tasks/{t.id}/cancel")
        assert r.status_code == 200
    assert len(w._cancel) == 0
    assert len(w._tasks) <= w._recent.maxlen
    assert_error_shape(client.get(f"/api/tasks/{ids[0]}"), 404)
    assert client.get(f"/api/tasks/{ids[-1]}").status_code == 200


@pytest.fixture
def settings_saved_qa():
    save_settings(make_settings())


# ---- static HEAD -----------------------------------------------------------------------------------
def test_static_head_missing_file_is_404_and_get_has_no_cache(api):
    client, _, _ = api
    assert client.head("/static/nope.js").status_code == 404
    r = client.get("/static/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    h = client.head("/static/app.js")
    assert h.status_code == 200 and h.content == b"" and h.headers["cache-control"] == "no-cache"


# ---- QA finds: "$" accepted a trailing newline; port 0 was accepted ----------------------------------
def test_put_settings_extension_with_trailing_newline_422(api):
    client, _, _ = api
    before = settings_path().read_bytes()
    s = client.get("/api/settings").json()
    s["web"]["allowed_extensions"] = [".mp4\n"]
    body = assert_error_shape(client.put("/api/settings", json=s), 422)
    assert any(k.startswith("web.allowed_extensions") for k in body["errors"])
    assert settings_path().read_bytes() == before


def test_idempotency_key_with_trailing_newline_rejected_by_regex():
    from autoclipper.web.server import ASS_COLOR_RE, IDEMPOTENCY_RE

    assert IDEMPOTENCY_RE.match("k\n") is None and IDEMPOTENCY_RE.match("k") is not None
    assert ASS_COLOR_RE.match("&H00FFFFFF\n") is None and ASS_COLOR_RE.match("&H00ffffff") is not None
