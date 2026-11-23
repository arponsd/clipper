"""Step 4 web API per clip: GET/POST /api/jobs/{id}/clips/{n}/{lang}/publish (view, dry run, live publish
through the worker, idempotency, the 409 matrix) and JobDetail.publish_url (CONTRACT_STEP4 sections 5, 9).

The worker runs the real `runner.run_publish` against tests/publish_fakes.FakeNet (no network), with a no-op
sleep so polling and backoff are instant."""
from __future__ import annotations

import functools
import json
import logging

import pytest
from publish_fakes import (ALL_SECRETS, CHANNEL, IG, PAGE, FakeNet, connect_all, grep_secrets, make_job,
                           meta_error, set_publish, tree_text)
from test_web_support import TASK_KEYS, Stub, assert_error_shape, assert_keys, make_client, read_json, wait_for

from autoclipper.jobstore import jobs_root
from autoclipper.publish import runner
from autoclipper.web.worker import Worker

JOB = "20261123-120000-pubtst"
PLATFORMS = ["youtube", "facebook", "instagram"]
URL = f"/api/jobs/{JOB}/clips/1/hi/publish"

PLAN_KEYS = {"platform": str, "lang": str, "job_id": str, "clip": int, "dry_run": bool, "created_at": float,
             "api_version": str, "account": (dict, type(None)), "video": dict, "metadata": dict,
             "requests": list, "limits": dict, "problems": list, "warnings": list,
             "cover": (dict, type(None)), "schedule": (dict, type(None))}  # G7/G6 additive
VIDEO_KEYS = {"path": str, "size": int, "duration": (int, float), "width": int, "height": int, "sha256": str}
REQ_KEYS = {"step", "method", "url", "headers", "body", "note"}
STATUS_KEYS = {"platform": str, "lang": str, "job_id": str, "clip": int, "state": str, "progress": (int, float),
               "message": str, "attempts": int, "error": (dict, type(None)), "remote_id": (str, type(None)),
               "url": (str, type(None)), "account": (dict, type(None)), "video_sha256": (str, type(None)),
               "video_size": (int, type(None)), "idempotency_key": (str, type(None)),
               "started_at": (float, type(None)), "updated_at": (float, type(None)),
               "published_at": (float, type(None)),
               "scheduled_for": (float, type(None)), "cover": (dict, type(None))}  # G6/G7 additive
VIEW_PLATFORM_KEYS = {"enabled": bool, "connected": bool, "account": (dict, type(None)), "plan": (dict, type(None)),
                      "status": (dict, type(None)), "published_render_outdated": bool, "problems": list}
PUB_TASK_KEYS = {**TASK_KEYS, "platforms": (list, type(None))}


def pub_dir(clip=1, lang="hi"):
    return jobs_root() / JOB / "clips" / f"{clip:02d}" / lang / "publish"


def assert_pub_task(t: dict, platforms: list[str]) -> None:
    assert_keys(t, PUB_TASK_KEYS)
    assert t["kind"] == "publish" and t["job_id"] == JOB and t["clip"] == 1 and t["lang"] == "hi"
    assert t["platforms"] == platforms


def assert_plan(plan: dict, platform: str) -> None:
    assert_keys(plan, PLAN_KEYS)
    assert plan["platform"] == platform and plan["lang"] == "hi" and plan["job_id"] == JOB and plan["clip"] == 1
    assert_keys(plan["video"], VIDEO_KEYS)
    assert len(plan["video"]["sha256"]) == 64
    assert plan["api_version"] == ("v3" if platform == "youtube" else "v27.0")
    assert plan["requests"]
    for r in plan["requests"]:
        assert set(r) == REQ_KEYS
        assert r["url"].startswith(("https://", "<session URI>")), r["url"]
        for k, v in r["headers"].items():
            if k.lower() == "authorization":
                assert v.endswith("***"), v


def assert_status(st: dict) -> None:
    assert_keys(st, STATUS_KEYS)
    assert "resume" not in st
    assert st["state"] in ("queued", "uploading", "processing", "publishing", "published", "failed", "interrupted")


def assert_view(v: dict) -> None:
    assert_keys(v, {"dry_run": bool, "final": bool, "final_sha256": (str, type(None)), "task": (dict, type(None)),
                    "platforms": dict})
    assert set(v["platforms"]) == set(PLATFORMS)
    for p, x in v["platforms"].items():
        assert_keys(x, VIEW_PLATFORM_KEYS)
        if x["plan"] is not None:
            assert_plan(x["plan"], p)
        if x["status"] is not None:
            assert_status(x["status"])
    if v["task"] is not None:
        assert_keys(v["task"], PUB_TASK_KEYS)


@pytest.fixture
def net() -> FakeNet:
    return FakeNet()


def _app(net, start=True):
    stub = Stub()
    worker = Worker(run_job=stub.run_job, rerender=stub.rerender,
                    publish=functools.partial(runner.run_publish, http=net.client(), sleep=lambda s: None))
    return make_client(worker=worker, stub=stub, start=start, publish_http=net.client)


@pytest.fixture
def papi(net):
    client, worker, stub = _app(net)
    with client:
        make_job(JOB)
        yield client, worker, net
        stub.release.set()


@pytest.fixture
def papi_idle(net):
    """Worker thread not started: a submitted task stays queued."""
    client, worker, stub = _app(net, start=False)
    with client:
        make_job(JOB)
        yield client, worker, net


def live(client, platforms=None, key="k-1", **extra):
    body = {"platforms": platforms or PLATFORMS, "dry_run": False, "confirm": True, **extra}
    return client.post(URL, json=body, headers={"Idempotency-Key": key})


# =================================================================================================
# GET view
# =================================================================================================
def test_view_fresh_not_connected(papi):
    client, _, net = papi
    r = client.get(URL)
    assert r.status_code == 200, r.text
    v = r.json()
    assert_view(v)
    assert v["dry_run"] is True and v["final"] is True and v["task"] is None
    assert len(v["final_sha256"]) == 64
    for p in PLATFORMS:
        x = v["platforms"][p]
        assert x["enabled"] is False and x["connected"] is False and x["plan"] is None and x["status"] is None
        assert x["problems"]
    assert net.requests == []


def test_view_connected(papi):
    client, _, _ = papi
    connect_all(dry_run=True)
    v = client.get(URL).json()
    assert_view(v)
    yt = v["platforms"]["youtube"]
    assert yt["enabled"] and yt["connected"] and yt["account"] == {"id": CHANNEL["hi"][0], "name": CHANNEL["hi"][1]}
    assert v["platforms"]["facebook"]["account"]["id"] == PAGE["hi"][0]
    assert v["platforms"]["instagram"]["account"]["id"] == IG["hi"][0]


def test_view_404_and_422(papi):
    client, _, _ = papi
    make_job("20261123-120001-enonly", clips={0: ["en"]})
    assert_error_shape(client.get("/api/jobs/20990101-000000-nojob/clips/1/hi/publish"), 404, "not found")
    assert_error_shape(client.get(f"/api/jobs/{JOB}/clips/7/hi/publish"), 404, "clip 7")
    assert_error_shape(client.get("/api/jobs/20261123-120001-enonly/clips/0/hi/publish"), 404, "language hi")
    for bad in ("fr", "HI"):
        assert "lang" in assert_error_shape(client.get(f"/api/jobs/{JOB}/clips/1/{bad}/publish"), 422)["errors"]
    for bad in ("-1", "1000", "x", "99999999999999999999"):
        assert "clip" in assert_error_shape(client.get(f"/api/jobs/{JOB}/clips/{bad}/hi/publish"), 422)["errors"]
    assert "job_id" in assert_error_shape(client.get("/api/jobs/-bad/clips/1/hi/publish"), 422)["errors"]


def test_view_without_final(papi):
    client, _, _ = papi
    make_job("20261123-120002-nofinal", with_final=False)
    v = client.get("/api/jobs/20261123-120002-nofinal/clips/1/hi/publish").json()
    assert_view(v)
    assert v["final"] is False and v["final_sha256"] is None


def test_job_detail_has_publish_url(papi):
    client, _, _ = papi
    d = client.get(f"/api/jobs/{JOB}").json()
    for c in d["clips"]:
        for lang, entry in c["langs"].items():
            assert entry["publish_url"] == f"/api/jobs/{JOB}/clips/{c['index']}/{lang}/publish"
            assert client.get(entry["publish_url"]).status_code == 200


# =================================================================================================
# dry run
# =================================================================================================
def test_dry_run_plans_without_network(papi):
    client, _, net = papi
    connect_all(dry_run=True)
    before = (jobs_root() / JOB / "job.json").read_bytes()
    r = client.post(URL, json={"platforms": PLATFORMS})
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d) == {"dry_run", "plans"} and d["dry_run"] is True
    assert set(d["plans"]) == set(PLATFORMS)
    for p, plan in d["plans"].items():
        assert_plan(plan, p)
        assert plan["dry_run"] is True and plan["problems"] == []
        f = pub_dir() / f"{p}.json"
        assert read_json(f) == plan
        assert not (pub_dir() / f"{p}.status.json").exists()
    yt = d["plans"]["youtube"]
    assert any(x["url"].startswith("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable")
               for x in yt["requests"])
    assert yt["metadata"]["status"]["privacyStatus"] == "private"
    assert all("/v27.0/" in x["url"] for x in d["plans"]["facebook"]["requests"])
    assert net.requests == []  # nothing sent anywhere
    assert grep_secrets(r.text, tree_text()) == []
    assert (jobs_root() / JOB / "job.json").read_bytes() == before
    v = client.get(URL).json()
    assert v["platforms"]["youtube"]["plan"] == yt


def test_dry_run_not_connected_lists_problems(papi):
    client, _, net = papi
    r = client.post(URL, json={"platforms": ["facebook"], "dry_run": True})
    assert r.status_code == 200
    plan = r.json()["plans"]["facebook"]
    assert_plan(plan, "facebook")
    assert plan["account"] is None and plan["problems"]
    assert (pub_dir() / "facebook.json").is_file()
    assert net.requests == []


def test_dry_run_forced_when_settings_live(papi):
    client, _, net = papi
    connect_all(dry_run=False)
    r = client.post(URL, json={"platforms": ["youtube"], "dry_run": True})
    assert r.status_code == 200 and r.json()["dry_run"] is True
    assert net.requests == []
    assert not (pub_dir() / "youtube.status.json").exists()


def test_dry_run_settings_default_used_when_null(papi):
    client, _, net = papi
    r = client.post(URL, json={"platforms": ["instagram"], "dry_run": None})
    assert r.status_code == 200 and r.json()["dry_run"] is True
    assert net.requests == []


# =================================================================================================
# validation
# =================================================================================================
@pytest.mark.parametrize("body,key", [
    ({}, "platforms"),
    ({"platforms": []}, "platforms"),
    ({"platforms": PLATFORMS + ["youtube"]}, "platforms"),
    ({"platforms": ["youtube", "youtube"]}, "platforms"),
    ({"platforms": ["tiktok"]}, "platforms.0"),
    ({"platforms": ["YouTube"]}, "platforms.0"),
    ({"platforms": "youtube"}, "platforms"),
    ({"platforms": [1]}, "platforms.0"),
    ({"platforms": ["youtube"], "confirm": "yes"}, "confirm"),
    ({"platforms": ["youtube"], "confirm": 1}, "confirm"),
    ({"platforms": ["youtube"], "dry_run": "true"}, "dry_run"),
    ({"platforms": ["youtube"], "force": None}, "force"),
    ({"platforms": ["youtube"], "extra": True}, "extra"),
    ({"platforms": ["youtube"], "privacy": "public"}, "privacy"),
])
def test_publish_body_422(papi, body, key):
    client, _, net = papi
    b = assert_error_shape(client.post(URL, json=body), 422)
    assert key in b["errors"], b
    assert not pub_dir().exists()
    assert net.requests == []


@pytest.mark.parametrize("raw", [b"[]", b"{", b"null", b"", b"1" * 5000])
def test_publish_bad_json(papi, raw):
    client, _, _ = papi
    assert_error_shape(client.post(URL, content=raw, headers={"Content-Type": "application/json"}), 422)


@pytest.mark.parametrize("key", ["", "has space", "x" * 201, "k\n", "a/b"])
def test_publish_bad_idempotency_key(papi, key):
    client, _, _ = papi
    r = client.post(URL, json={"platforms": ["youtube"]}, headers={"Idempotency-Key": key})
    assert "Idempotency-Key" in assert_error_shape(r, 422)["errors"]


def test_live_requires_key_and_confirm(papi):
    client, _, net = papi
    connect_all(dry_run=False)
    r = client.post(URL, json={"platforms": ["youtube"], "dry_run": False, "confirm": True})
    assert "Idempotency-Key" in assert_error_shape(r, 422)["errors"]
    r = client.post(URL, json={"platforms": ["youtube"], "dry_run": False}, headers={"Idempotency-Key": "a1"})
    b = assert_error_shape(r, 422)
    assert b["errors"]["confirm"] == ["set confirm to true to publish for real"]
    # dry_run null + settings live = live, so confirm is needed as well
    r = client.post(URL, json={"platforms": ["youtube"]}, headers={"Idempotency-Key": "a2"})
    assert "confirm" in assert_error_shape(r, 422)["errors"]
    assert net.requests == []
    assert not (pub_dir() / "youtube.status.json").exists()


# =================================================================================================
# 409 matrix
# =================================================================================================
def test_409_live_while_settings_dry_run(papi):
    client, worker, net = papi
    connect_all(dry_run=True)
    r = live(client)
    assert_error_shape(r, 409, "dry-run mode")
    assert net.requests == [] and worker.snapshot()["queued"] == [] and worker.snapshot()["running"] is None


def test_409_not_connected_or_disabled(papi):
    client, _, net = papi
    set_publish(dry_run=False)
    b = assert_error_shape(live(client), 409, "cannot publish")
    assert set(b["errors"]) == set(PLATFORMS)
    connect_all(dry_run=False, enable=False)
    b = assert_error_shape(live(client, key="k-2"), 409)
    assert set(b["errors"]) == set(PLATFORMS)
    assert net.requests == []


def test_409_same_account_for_both_languages(papi):
    client, _, _ = papi
    connect_all(dry_run=False)
    body = client.get("/api/publish/settings").json()
    body["hi"]["youtube"]["channel_id"] = CHANNEL["en"][0]
    client.put("/api/publish/settings", json=body)
    b = assert_error_shape(live(client, ["youtube"]), 409)
    assert "youtube" in b["errors"]


def test_409_no_final_render(papi):
    client, _, net = papi
    make_job("20261123-120002-nofinal", with_final=False)
    url = "/api/jobs/20261123-120002-nofinal/clips/1/hi/publish"
    assert_error_shape(client.post(url, json={"platforms": ["youtube"]}), 409, "no final render")
    connect_all(dry_run=False)
    r = client.post(url, json={"platforms": ["youtube"], "dry_run": False, "confirm": True},
                    headers={"Idempotency-Key": "nf"})
    assert_error_shape(r, 409, "no final render")
    make_job("20261123-120003-nometa", with_metadata=False)
    r = client.post("/api/jobs/20261123-120003-nometa/clips/1/hi/publish",
                    json={"platforms": ["youtube"], "dry_run": False, "confirm": True}, headers={"Idempotency-Key": "nm"})
    assert_error_shape(r, 409, "no final render")
    assert net.requests == []


def test_409_while_rerender_or_run_job_active(papi_idle):
    client, worker, net = papi_idle
    connect_all(dry_run=False)
    worker.submit_rerender(JOB, 1, "hi")
    assert_error_shape(client.post(URL, json={"platforms": ["youtube"], "dry_run": True}), 409, "rendered")
    assert_error_shape(live(client, ["youtube"]), 409)
    assert not pub_dir().exists()
    assert net.requests == []


def test_409_run_job_active(papi_idle):
    client, worker, _ = papi_idle
    connect_all(dry_run=False)
    worker.submit_resume(JOB)
    assert_error_shape(client.post(URL, json={"platforms": ["youtube"], "dry_run": True}), 409)
    assert_error_shape(live(client, ["youtube"]), 409)


def test_queued_publish_conflicts(papi_idle):
    client, worker, net = papi_idle
    connect_all(dry_run=False)
    r = live(client, ["youtube"], key="q1")
    assert r.status_code == 202, r.text
    t = r.json()["task"]
    assert_pub_task(t, ["youtube"])
    assert t["state"] == "queued" and t["position"] == 1
    # same platform set, new key -> the same queued task (200)
    r2 = live(client, ["youtube"], key="q2")
    assert r2.status_code == 200 and r2.json()["task"]["id"] == t["id"]
    # different set -> 409; dry run while queued -> 409; rerender -> 409
    assert_error_shape(live(client, ["facebook"], key="q3"), 409)
    assert_error_shape(client.post(URL, json={"platforms": ["youtube"], "dry_run": True}), 409, "published")
    assert_error_shape(client.post(f"/api/jobs/{JOB}/clips/1/hi/rerender"), 409)
    # the view shows the active task
    v = client.get(URL).json()
    assert_view(v)
    assert v["task"]["id"] == t["id"]
    assert client.get(f"/api/tasks/{t['id']}").json()["platforms"] == ["youtube"]
    assert net.requests == []


# =================================================================================================
# live publish through the worker
# =================================================================================================
def test_live_publish_all_platforms_then_already_published(papi, caplog):
    client, worker, net = papi
    caplog.set_level(logging.DEBUG)
    connect_all(dry_run=False)
    r = live(client, key="live-1")
    assert r.status_code == 202, r.text
    d = r.json()
    assert set(d) == {"dry_run", "task", "already_published"}
    assert d["dry_run"] is False and d["already_published"] == {}
    assert_pub_task(d["task"], PLATFORMS)
    assert worker.wait_idle(60)
    t = client.get(f"/api/tasks/{d['task']['id']}").json()
    assert t["state"] == "done", t

    v = client.get(URL).json()
    assert_view(v)
    assert v["task"] is None
    for p in PLATFORMS:
        st = v["platforms"][p]["status"]
        assert st["state"] == "published", st
        assert st["remote_id"] and st["url"].startswith("https://")
        assert st["idempotency_key"] == "live-1"
        assert st["video_sha256"] == v["final_sha256"]
        assert v["platforms"][p]["published_render_outdated"] is False
        raw = read_json(pub_dir() / f"{p}.status.json")
        assert raw["state"] == "published"
        assert read_json(pub_dir() / f"{p}.json")["dry_run"] is False
    assert v["platforms"]["youtube"]["status"]["url"].startswith("https://www.youtube.com/shorts/")
    assert v["platforms"]["facebook"]["status"]["url"].startswith("https://www.facebook.com/reel/")
    # exactly one upload per platform happened, on the right accounts, with no token in any URL
    assert len(net.google.videos) == 1
    assert all(grep_secrets(x.url) == [] for x in net.requests)
    idem = read_json(jobs_root().parent / "web" / "idempotency.json")
    assert "publish:live-1" in idem

    # a second publish (new key) sends nothing and returns the published records
    n = len(net.requests)
    r2 = live(client, key="live-2")
    assert r2.status_code == 200, r2.text
    d2 = r2.json()
    assert d2["task"] is None and set(d2["already_published"]) == set(PLATFORMS)
    for st in d2["already_published"].values():
        assert_status(st)
        assert st["state"] == "published"
    assert len(net.requests) == n

    # same key replay -> the same task, still nothing sent
    r3 = live(client, key="live-1")
    assert r3.status_code == 200 and r3.json()["task"]["id"] == d["task"]["id"]
    # same key, different body -> 409
    assert_error_shape(live(client, ["youtube"], key="live-1"), 409, "Idempotency-Key")
    assert len(net.requests) == n

    # no secret anywhere: responses, job files (plans, status, web.log), idempotency.json, logs
    texts = [r.text, r2.text, r3.text, client.get(URL).text, client.get(f"/api/jobs/{JOB}/log").text,
             client.get("/api/queue").text]
    assert grep_secrets(*texts, tree_text(), caplog.text) == []
    assert all(s not in tree_text() for s in ALL_SECRETS)


def test_live_force_publishes_again(papi):
    client, worker, net = papi
    connect_all(dry_run=False)
    assert live(client, ["youtube"], key="f1").status_code == 202
    assert worker.wait_idle(60)
    first = client.get(URL).json()["platforms"]["youtube"]["status"]["remote_id"]
    r = live(client, ["youtube"], key="f2", force=True)
    assert r.status_code == 202, r.text
    assert r.json()["already_published"] == {}
    assert worker.wait_idle(60)
    st = client.get(URL).json()["platforms"]["youtube"]["status"]
    assert st["state"] == "published" and st["remote_id"] != first
    assert len(net.google.videos) == 2


def test_live_one_platform_fails_others_publish(papi):
    client, worker, net = papi
    connect_all(dry_run=False)
    net.graph.inject["fb_start"] = [meta_error(4, "Application request limit reached", status=400)] * 10
    r = live(client, key="part-1")
    assert r.status_code == 202
    assert worker.wait_idle(60)
    v = client.get(URL).json()
    assert_view(v)
    assert v["platforms"]["youtube"]["status"]["state"] == "published"
    assert v["platforms"]["instagram"]["status"]["state"] == "published"
    fb = v["platforms"]["facebook"]["status"]
    assert fb["state"] == "failed"
    assert fb["error"]["kind"] == "rate_limit" and fb["error"]["message"]
    assert set(fb["error"]) == {"kind", "message", "code", "retry_after"}
    assert client.get(f"/api/tasks/{r.json()['task']['id']}").json()["state"] == "done"
    # retrying only re-sends the failed platform
    net.graph.inject.clear()
    r2 = live(client, key="part-2")
    assert r2.status_code == 202
    assert set(r2.json()["already_published"]) == {"youtube", "instagram"}
    assert r2.json()["task"]["platforms"] == ["facebook"]
    assert worker.wait_idle(60)
    assert client.get(URL).json()["platforms"]["facebook"]["status"]["state"] == "published"


def test_live_all_fail_quota_message(papi):
    client, worker, net = papi
    connect_all(dry_run=False)
    from publish_fakes import google_error
    net.google.inject["start"] = [google_error(403, "quotaExceeded", "quota")] * 10
    r = live(client, ["youtube"], key="q-1")
    assert r.status_code == 202
    assert worker.wait_idle(60)
    t = client.get(f"/api/tasks/{r.json()['task']['id']}").json()
    assert t["state"] == "failed" and t["error"]
    st = client.get(URL).json()["platforms"]["youtube"]["status"]
    assert st["state"] == "failed" and st["error"]["kind"] == "quota"
    assert "quota" in st["error"]["message"].lower()
    assert net.google.videos == []


def test_outdated_after_rerender(papi):
    client, worker, _ = papi
    connect_all(dry_run=False)
    live(client, ["youtube"], key="o1")
    assert worker.wait_idle(60)
    final = jobs_root() / JOB / "clips" / "01" / "hi" / "final.mp4"
    final.write_bytes(final.read_bytes() + b"\0")
    v = client.get(URL).json()
    assert v["platforms"]["youtube"]["published_render_outdated"] is True


def test_publish_task_cancel(papi_idle):
    client, worker, _ = papi_idle
    connect_all(dry_run=False)
    t = live(client, ["instagram"], key="c1").json()["task"]
    r = client.post(f"/api/tasks/{t['id']}/cancel")
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert client.get(URL).json()["task"] is None
    # a new key can publish again after the cancel
    assert live(client, ["instagram"], key="c2").status_code == 202


def test_idempotency_store_file_shape(papi_idle):
    client, _, _ = papi_idle
    connect_all(dry_run=False)
    live(client, ["youtube"], key="shape-1")
    store = read_json(jobs_root().parent / "web" / "idempotency.json")
    rec = store["publish:shape-1"]
    assert set(rec) >= {"hash", "task_id", "created_at"}
    assert json.dumps(rec).find("resume") == -1
