"""G6/G7 web API: /api/schedules (list, create dry-run/live, get, edit, cancel) and the per-clip cover routes
(view, frame preview, image upload, PUT cover, cover.jpg, DELETE). Every route: happy path, validation
(422, never 500), auth (401 no token, 403 wrong token / foreign host / cross origin), 404, the exact JSON
structure and the side effects on disk. The worker runs the real runner against publish_fakes (no network).
"""
from __future__ import annotations

import functools
import json
import subprocess
import time

import pytest
from fastapi.testclient import TestClient
from publish_fakes import ALL_SECRETS, IG, FakeNet, connect_all, grep_secrets, make_job, set_publish
from test_web_support import Stub, assert_error_shape, assert_keys, make_client

from autoclipper.jobstore import jobs_root
from autoclipper.publish import records, runner, schedules, service
from autoclipper.web.auth import TOKEN_HEADER
from autoclipper.web.worker import Worker

JOB = "20261123-120000-pubtst"
ALL = ["youtube", "facebook", "instagram"]
COVER = f"/api/jobs/{JOB}/clips/1/hi/cover"

SCHEDULE_KEYS = {"id": (str, type(None)), "job_id": str, "clip": int, "lang": str, "title": str,
                 "publish_at": float, "publish_at_utc": str, "publish_at_local": str, "timezone": str,
                 "state": str, "platforms": list, "items": dict, "created_at": float, "updated_at": float,
                 "dry_run": bool}
ITEM_KEYS = {"platform": str, "mode": str, "mode_note": str, "handoff_at": float, "account": (dict, type(None)),
             "state": str, "message": str, "task_id": (str, type(None)), "fired_at": (float, type(None)),
             "finished_at": (float, type(None)), "remote_id": (str, type(None)), "url": (str, type(None)),
             "cancellable": bool}
PLAN_SCHEDULE_KEYS = {"id": (str, type(None)), "publish_at": float, "publish_at_utc": str, "publish_at_local": str,
                      "timezone": str, "mode": str, "mode_note": str, "handoff_at": float}
COVER_VIEW_KEYS = {"cover": (dict, type(None)), "final": bool, "duration": (float, type(None)), "has_upload": bool,
                   "max_title": int, "max_upload_bytes": int, "upload_types": list}
COVER_KEYS = {"source": str, "time": (float, type(None)), "title": str, "width": int, "height": int, "size": int,
              "sha256": str, "final_sha256": str, "created_at": float, "outdated": bool,
              "thumb_offset_ms": (int, type(None)), "url": str}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def net():
    return FakeNet()


@pytest.fixture
def sapi(net):
    stub = Stub()
    worker = Worker(run_job=stub.run_job, rerender=stub.rerender,
                    publish=functools.partial(runner.run_publish, http=net.client(), sleep=lambda s: None))
    client, worker, stub = make_client(worker=worker, stub=stub, publish_http=net.client)
    with client:
        make_job(JOB)
        yield client, worker, net
        stub.release.set()


def iso(hours=3.0, offset="+06:00"):
    from datetime import datetime, timedelta, timezone
    sign = 1 if offset[0] == "+" else -1
    tz = timezone(sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6])))
    return datetime.fromtimestamp(int(time.time() + hours * 3600), tz=tz).isoformat(timespec="seconds")


def body(**kw):
    b = {"job_id": JOB, "clip": 1, "lang": "hi", "platforms": ALL, "publish_at": iso(), "timezone": "Asia/Dhaka"}
    b.update(kw)
    return b


def live(client, key="sk-1", **kw):
    return client.post("/api/schedules", json=body(dry_run=False, confirm=True, **kw), headers={"Idempotency-Key": key})


def assert_schedule(s):
    assert_keys(s, SCHEDULE_KEYS)
    for p, it in s["items"].items():
        assert_keys(it, ITEM_KEYS)
        assert it["platform"] == p and it["mode"] in ("native", "app")
    assert s["platforms"] == list(s["items"])


def png(w=200, h=300):
    return subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=red:s={w}x{h}",
                           "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"], check=True,
                          capture_output=True).stdout


# =================================================================================================
# GET /api/schedules
# =================================================================================================
def test_list_empty_structure(sapi):
    client, _, _ = sapi
    r = client.get("/api/schedules")
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d) == {"schedules", "now", "timezone", "dry_run", "handed_off", "scheduler"}
    assert d["schedules"] == [] and d["dry_run"] is True and isinstance(d["now"], float)
    assert d["handed_off"] == 0
    assert set(d["scheduler"]) == {"running", "blocked", "grace_minutes", "native_lead_minutes", "prefer_native"}
    assert d["scheduler"]["running"] is True and d["scheduler"]["blocked"] is None
    assert d["timezone"].startswith("UTC") or "/" in d["timezone"]


@pytest.mark.parametrize("q", ["state=soon", "clip=-1", "clip=1000", "clip=x", "lang=fr", "job_id=../x"])
def test_list_bad_query_422(sapi, q):
    assert_error_shape(sapi[0].get("/api/schedules?" + q), 422)


def test_auth_401_403_on_every_route(sapi):
    client, _, _ = sapi
    routes = [("GET", "/api/schedules", None), ("POST", "/api/schedules", body()),
              ("GET", "/api/schedules/s_000000000000", None),
              ("PATCH", "/api/schedules/s_000000000000", {"publish_at": iso()}),
              ("POST", "/api/schedules/s_000000000000/cancel", None),
              ("GET", COVER, None), ("GET", COVER + "/frame?t=1", None), ("PUT", COVER, {"source": "frame", "time": 1}),
              ("POST", COVER + "/image", None), ("GET", COVER + ".jpg", None), ("DELETE", COVER, None)]
    anon = TestClient(client.app, base_url="http://127.0.0.1:8765")
    wrong = TestClient(client.app, base_url="http://127.0.0.1:8765", headers={TOKEN_HEADER: "x" * 43})
    for method, path, b in routes:
        assert_error_shape(anon.request(method, path, json=b), 401)
        assert_error_shape(wrong.request(method, path, json=b), 403)
    evil = TestClient(client.app, base_url="http://evil.example", headers={TOKEN_HEADER: client.headers[TOKEN_HEADER]})
    assert_error_shape(evil.get("/api/schedules"), 403)
    r = client.post("/api/schedules", json=body(), headers={"Origin": "https://evil.example"})
    assert_error_shape(r, 403)
    assert not schedules.store_path().exists()


# =================================================================================================
# POST /api/schedules - dry run
# =================================================================================================
def test_dry_run_preview_structure_and_plans(sapi):
    client, _, net = sapi
    connect_all(dry_run=True)
    r = client.post("/api/schedules", json=body())
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d) == {"dry_run", "schedule", "plans"} and d["dry_run"] is True
    s = d["schedule"]
    assert_keys(s, SCHEDULE_KEYS)
    assert s["id"] is None and s["state"] == "preview" and s["dry_run"] is True
    assert s["timezone"] == "Asia/Dhaka" and s["publish_at_local"].endswith("+06:00")
    assert all(it["state"] == "preview" and it["cancellable"] is False for it in s["items"].values())
    assert set(d["plans"]) == set(ALL)
    for p, plan in d["plans"].items():
        assert_keys(plan["schedule"], PLAN_SCHEDULE_KEYS)
        assert plan["dry_run"] is True and plan["schedule"]["publish_at"] == s["publish_at"]
        saved = json.loads((jobs_root() / JOB / "clips/01/hi/publish" / f"{p}.json").read_text(encoding="utf-8"))
        assert saved["schedule"]["publish_at"] == s["publish_at"]
    assert not schedules.store_path().exists()  # nothing stored live
    assert net.requests == []
    assert grep_secrets(r.text) == []


def test_dry_run_forced_even_when_live(sapi):
    client, _, net = sapi
    connect_all()
    r = client.post("/api/schedules", json=body(dry_run=True))
    assert r.status_code == 200 and r.json()["dry_run"] is True
    assert not schedules.store_path().exists() and net.requests == []


def test_dry_run_lists_problems_instead_of_refusing(sapi):
    client, _, _ = sapi
    r = client.post("/api/schedules", json=body())  # nothing connected, dry run on
    assert r.status_code == 200
    for plan in r.json()["plans"].values():
        assert "not connected" in plan["problems"]


# =================================================================================================
# POST /api/schedules - validation
# =================================================================================================
@pytest.mark.parametrize("change", [
    {"job_id": None}, {"clip": None}, {"lang": None}, {"platforms": None}, {"publish_at": None},
    {"clip": "1"}, {"clip": 1.5}, {"clip": -1}, {"clip": 1000}, {"clip": True},
    {"lang": "fr"}, {"lang": ""}, {"platforms": []}, {"platforms": ["youtube"] * 2},
    {"platforms": ALL + ["tiktok"]}, {"platforms": ["tiktok"]}, {"platforms": "youtube"},
    {"publish_at": ""}, {"publish_at": "x" * 41}, {"publish_at": "tomorrow 6pm"}, {"publish_at": 1795523400},
    {"publish_at": "2026-02-30T10:00"}, {"timezone": "x" * 65}, {"timezone": "Not A Zone"},
    {"dry_run": "yes"}, {"confirm": 1}, {"extra": 1}, {"job_id": "../etc"},
    {"job_id": "x" * 65},
])
def test_create_validation_422(sapi, change):
    client, _, _ = sapi
    b = body(**change)
    for k, v in change.items():
        if v is None:
            b.pop(k)
    r = client.post("/api/schedules", json=b)
    assert r.status_code == 422, (change, r.text)
    assert_error_shape(r, 422)
    assert not schedules.store_path().exists()


@pytest.mark.parametrize("hours,msg", [(-1, "at least 2 minutes"), (0.01, "at least 2 minutes"),
                                       (24 * 91, "within 90 days")])
def test_create_time_window_422(sapi, hours, msg):
    client, _, _ = sapi
    d = assert_error_shape(client.post("/api/schedules", json=body(publish_at=iso(hours))), 422)
    assert msg in d["errors"]["publish_at"][0]


def test_unknown_zone_with_explicit_offset_uses_the_offset(sapi):
    client, _, _ = sapi
    r = client.post("/api/schedules", json=body(timezone="Mars/Olympus"))  # e.g. Windows without tzdata
    assert r.status_code == 200 and r.json()["schedule"]["timezone"] == "UTC+06:00"
    d = assert_error_shape(client.post("/api/schedules", json=body(timezone="Mars/Olympus",
                                                                   publish_at=iso()[:16])), 422)
    assert "timezone" in d["errors"]


def test_create_not_json_422(sapi):
    client, _, _ = sapi
    assert_error_shape(client.post("/api/schedules", content=b"{bad", headers={"Content-Type": "application/json"}), 422)
    assert_error_shape(client.post("/api/schedules", json=[1, 2]), 422)


def test_create_404_unknown_job_clip_lang(sapi):
    client, _, _ = sapi
    assert_error_shape(client.post("/api/schedules", json=body(job_id="20990101-000000-nojob")), 404)
    assert_error_shape(client.post("/api/schedules", json=body(clip=9)), 404)
    make_job("20261123-120000-enonly", clips={1: ["en"]})
    assert_error_shape(client.post("/api/schedules", json=body(job_id="20261123-120000-enonly")), 404)


def test_live_needs_key_and_confirm_422(sapi):
    client, _, _ = sapi
    connect_all()
    d = assert_error_shape(client.post("/api/schedules", json=body(dry_run=False)), 422)
    assert set(d["errors"]) == {"Idempotency-Key", "confirm"}
    d = assert_error_shape(client.post("/api/schedules", json=body(dry_run=False, confirm=True),
                                       headers={"Idempotency-Key": "bad key!"}), 422)
    assert "Idempotency-Key" in d["errors"]
    assert not schedules.store_path().exists()


def test_live_refused_while_settings_dry_run_409(sapi):
    client, _, _ = sapi
    connect_all(dry_run=True)
    assert_error_shape(live(client), 409, "dry-run mode")
    assert not schedules.store_path().exists()


def test_live_not_connected_409_per_platform(sapi):
    client, _, _ = sapi
    set_publish(dry_run=False)
    d = assert_error_shape(live(client), 409, "cannot publish")
    assert set(d["errors"]) == set(ALL) and all("not connected" in v for v in d["errors"].values())


def test_live_no_final_render_409(sapi):
    client, _, _ = sapi
    connect_all()
    (jobs_root() / JOB / "clips/01/hi/final.mp4").unlink()
    assert_error_shape(live(client), 409, "no final render")


# =================================================================================================
# POST /api/schedules - live
# =================================================================================================
def test_live_create_structure_side_effects_and_idempotency(sapi):
    client, _, net = sapi
    connect_all()
    r = live(client)
    assert r.status_code == 201, r.text
    d = r.json()
    assert set(d) == {"dry_run", "schedule"} and d["dry_run"] is False
    s = d["schedule"]
    assert_schedule(s)
    assert schedules.ID_RE.match(s["id"]) and s["state"] == "upcoming"
    assert {p: it["state"] for p, it in s["items"].items()} == {p: "scheduled" for p in ALL}
    assert s["items"]["instagram"]["mode"] == "app" and s["items"]["facebook"]["mode"] == "native"
    assert s["items"]["youtube"]["mode"] == "app"  # privacy private by default
    stored = json.loads(schedules.store_path().read_text(encoding="utf-8"))["schedules"]
    assert [x["id"] for x in stored] == [s["id"]] and stored[0]["idempotency_key"] == "sk-1"
    idem = json.loads((jobs_root().parent / "web" / "idempotency.json").read_text(encoding="utf-8"))
    assert idem["schedule:sk-1"]["schedule_id"] == s["id"]
    # same key + same body -> same schedule, 200
    r2 = live(client, publish_at=None) if False else client.post(
        "/api/schedules", json={**json.loads(r.request.content)}, headers={"Idempotency-Key": "sk-1"})
    assert r2.status_code == 200 and r2.json()["schedule"]["id"] == s["id"]
    # same key, different body -> 409
    assert_error_shape(live(client, publish_at=iso(5)), 409, "Idempotency-Key")
    # a second schedule of the same clip-lang-platform -> 409
    d = assert_error_shape(live(client, key="sk-2", publish_at=iso(6), platforms=["instagram"]), 409)
    assert "already scheduled" in d["errors"]["instagram"][0]
    assert len(json.loads(schedules.store_path().read_text(encoding="utf-8"))["schedules"]) == 1
    assert net.requests == []  # scheduling sends nothing
    assert grep_secrets(r.text, schedules.store_path().read_text(encoding="utf-8")) == []


def test_live_daily_limit_409(sapi):
    client, _, _ = sapi
    connect_all()

    def fill(ps):
        ps.schedule.daily_limits.instagram = 1
    service._update_settings(fill)
    records.append_ledger("instagram", IG["hi"][0], "hi", time.time() + 3 * 3600)
    d = assert_error_shape(live(client, platforms=["instagram"], publish_at=iso(3)), 409, "cannot schedule")
    assert d["errors"]["instagram"][0].startswith("daily limit reached: 1 of 1 Instagram posts for hindi_ig")


# =================================================================================================
# GET / PATCH / cancel
# =================================================================================================
def test_get_schedule_and_404(sapi):
    client, _, _ = sapi
    connect_all()
    sid = live(client).json()["schedule"]["id"]
    r = client.get(f"/api/schedules/{sid}")
    assert r.status_code == 200
    assert_schedule(r.json())
    assert_error_shape(client.get("/api/schedules/s_ffffffffffff"), 404)
    assert_error_shape(client.get("/api/schedules/nope"), 422)
    assert_error_shape(client.get("/api/schedules/s_FFFFFFFFFFFF"), 422)


def test_list_filters(sapi):
    client, _, _ = sapi
    connect_all()
    sid = live(client).json()["schedule"]["id"]
    assert [s["id"] for s in client.get("/api/schedules?state=upcoming").json()["schedules"]] == [sid]
    assert client.get("/api/schedules?state=done").json()["schedules"] == []
    assert [s["id"] for s in client.get(f"/api/schedules?job_id={JOB}&clip=1&lang=hi").json()["schedules"]] == [sid]
    assert client.get(f"/api/schedules?job_id={JOB}&clip=1&lang=en").json()["schedules"] == []
    for s in client.get("/api/schedules").json()["schedules"]:
        assert_schedule(s)


def test_patch_moves_time(sapi):
    client, _, _ = sapi
    connect_all()
    sid = live(client).json()["schedule"]["id"]
    new = iso(8, "+05:30")
    r = client.patch(f"/api/schedules/{sid}", json={"publish_at": new, "timezone": "Asia/Kolkata"})
    assert r.status_code == 200, r.text
    s = r.json()
    assert_schedule(s)
    assert s["timezone"] == "Asia/Kolkata" and s["publish_at_local"].endswith("+05:30")
    stored = json.loads(schedules.store_path().read_text(encoding="utf-8"))["schedules"][0]
    assert stored["publish_at"] == s["publish_at"]


@pytest.mark.parametrize("b", [{}, {"publish_at": ""}, {"publish_at": "soon"}, {"publish_at": 5},
                               {"publish_at": iso(), "x": 1}, {"publish_at": iso(-2)}, {"publish_at": iso(), "timezone": 3}])
def test_patch_validation_422(sapi, b):
    client, _, _ = sapi
    connect_all()
    sid = live(client).json()["schedule"]["id"]
    assert_error_shape(client.patch(f"/api/schedules/{sid}", json=b), 422)


def test_patch_404_and_409(sapi):
    client, _, _ = sapi
    client.app.state.scheduler.stop()  # the test edits item states by hand
    connect_all()
    assert_error_shape(client.patch("/api/schedules/s_ffffffffffff", json={"publish_at": iso()}), 404)
    sid = live(client).json()["schedule"]["id"]

    def fn(rows):
        rows[0]["items"]["facebook"]["state"] = "running"
    schedules.update(fn)
    d = assert_error_shape(client.patch(f"/api/schedules/{sid}", json={"publish_at": iso(5)}), 409)
    assert "facebook" in d["errors"]


def test_cancel_all_partial_and_errors(sapi):
    client, _, _ = sapi
    connect_all()
    sid = live(client).json()["schedule"]["id"]
    r = client.post(f"/api/schedules/{sid}/cancel", json={"platforms": ["instagram"]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert_keys(d, {**SCHEDULE_KEYS, "refused": dict})
    assert d["items"]["instagram"]["state"] == "cancelled" and d["items"]["youtube"]["state"] == "scheduled"
    r = client.post(f"/api/schedules/{sid}/cancel")
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert client.post(f"/api/schedules/{sid}/cancel").status_code == 200  # idempotent
    stored = json.loads(schedules.store_path().read_text(encoding="utf-8"))["schedules"][0]
    assert all(it["state"] == "cancelled" for it in stored["items"].values())
    assert_error_shape(client.post("/api/schedules/s_ffffffffffff/cancel"), 404)
    for bad in ({"platforms": []}, {"platforms": ["x"]}, {"platforms": "youtube"}, {"other": 1}, [1]):
        assert_error_shape(client.post(f"/api/schedules/{sid}/cancel", json=bad), 422)
    assert_error_shape(client.post(f"/api/schedules/{sid}/cancel", content=b"{x"), 422)


def test_cancel_handed_off_409(sapi):
    client, _, _ = sapi
    client.app.state.scheduler.stop()  # the test edits item states by hand
    connect_all()
    sid = live(client, platforms=["facebook"]).json()["schedule"]["id"]

    def fn(rows):
        rows[0]["items"]["facebook"].update(state="handed_off", url="https://www.facebook.com/reel/1")
    schedules.update(fn)
    d = assert_error_shape(client.post(f"/api/schedules/{sid}/cancel"), 409, "cannot cancel")
    assert "already handed to Facebook" in d["errors"]["facebook"][0]


def test_cancel_removes_queued_task(sapi):
    client, worker, _ = sapi
    client.app.state.scheduler.stop()  # the test edits item states by hand
    connect_all()
    sid = live(client, platforms=["instagram"]).json()["schedule"]["id"]
    idle = Worker()  # never started: the task stays queued
    task, _ = idle.submit_publish(JOB, 1, "hi", ["instagram"])

    def fn(rows):
        rows[0]["items"]["instagram"].update(state="queued", task_id=task.id)
    schedules.update(fn)
    client.app.state.ctx.worker = idle  # the route cancels through state.worker
    try:
        r = client.post(f"/api/schedules/{sid}/cancel")
    finally:
        client.app.state.ctx.worker = worker
    assert r.status_code == 200 and r.json()["items"]["instagram"]["state"] == "cancelled"
    assert idle.status(task.id).state == "cancelled"


# =================================================================================================
# end to end: API schedule -> scheduler -> worker -> published
# =================================================================================================
def test_schedule_fires_through_worker_and_shows_done(sapi):
    client, worker, net = sapi
    connect_all()
    s = live(client, publish_at=iso(2)).json()["schedule"]
    sch = client.app.state.scheduler
    sch._clock = Clock(s["publish_at"] + 1)
    sch.tick()
    assert worker.wait_idle(15)
    sch.tick()
    d = client.get(f"/api/schedules/{s['id']}").json()
    assert d["state"] == "done", d
    assert {p: it["state"] for p, it in d["items"].items()} == {p: "done" for p in ALL}
    assert d["items"]["facebook"]["url"].startswith("https://www.facebook.com/reel/")
    view = client.get(f"/api/jobs/{JOB}/clips/1/hi/publish").json()
    assert all(view["platforms"][p]["status"]["state"] == "published" for p in ALL)
    assert view["platforms"]["youtube"]["status"]["scheduled_for"] is None
    assert grep_secrets(client.get("/api/schedules").text) == []


def test_missed_on_startup_tick(sapi):
    client, _, net = sapi
    connect_all()
    s = live(client, publish_at=iso(2), platforms=["instagram"]).json()["schedule"]
    sch = client.app.state.scheduler
    sch._clock = Clock(s["publish_at"] + 2 * 3600)
    sch.tick()
    d = client.get("/api/schedules?state=missed").json()["schedules"]
    assert [x["id"] for x in d] == [s["id"]]
    assert "missed" in d[0]["items"]["instagram"]["message"]
    assert net.requests == []


# =================================================================================================
# covers
# =================================================================================================
def test_cover_view_fresh(sapi):
    client, _, _ = sapi
    r = client.get(COVER)
    assert r.status_code == 200, r.text
    d = r.json()
    assert_keys(d, COVER_VIEW_KEYS)
    assert d["cover"] is None and d["final"] is True and 5.5 < d["duration"] < 6.5
    assert d["upload_types"] == ["image/jpeg", "image/png", "image/webp"] and d["max_title"] == 100


def test_cover_404s(sapi):
    client, _, _ = sapi
    for path in (f"/api/jobs/20990101-000000-nojob/clips/1/hi/cover", f"/api/jobs/{JOB}/clips/9/hi/cover"):
        for method, suffix in (("GET", ""), ("GET", "/frame?t=1"), ("PUT", ""), ("GET", ".jpg"), ("DELETE", ""),
                               ("POST", "/image")):
            b = {"source": "frame", "time": 1} if method == "PUT" else None
            assert_error_shape(client.request(method, path + suffix, json=b), 404)
    assert_error_shape(client.get(f"/api/jobs/{JOB}/clips/1/fr/cover"), 422)
    assert_error_shape(client.get(COVER + ".jpg"), 404)


def test_frame_preview(sapi):
    client, _, _ = sapi
    r = client.get(COVER + "/frame?t=2.5")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content[:3] == b"\xff\xd8\xff" and r.headers["cache-control"] == "no-store"
    assert not list((jobs_root() / JOB / "clips/01/hi").glob(".cover-preview-*"))


@pytest.mark.parametrize("q", ["", "?t=", "?t=abc", "?t=-1", "?t=6", "?t=1e9", "?t=nan"])
def test_frame_preview_422(sapi, q):
    d = assert_error_shape(sapi[0].get(COVER + "/frame" + q), 422)
    assert "t" in d["errors"] or "time" in d["errors"]


@pytest.mark.parametrize("q,msg", [("?t=nan", "finite"), ("?t=inf", "finite"), ("?t=-1", "0 or more"),
                                   ("?t=6", "outside the clip")])
def test_frame_preview_error_key_is_t(sapi, q, msg):
    d = assert_error_shape(sapi[0].get(COVER + "/frame" + q), 422)
    assert list(d["errors"]) == ["t"] and msg in d["errors"]["t"][0]


def test_list_counts_handed_off_items(sapi):
    client, _, _ = sapi
    client.app.state.scheduler.stop()  # the test edits item states by hand
    connect_all()
    live(client, platforms=["facebook"])

    def fn(rows):
        rows[0]["items"]["facebook"].update(state="handed_off")
    schedules.update(fn)
    assert client.get("/api/schedules").json()["handed_off"] == 1


def test_cancel_route_refuses_running_item_without_interrupting(sapi):
    client, worker, _ = sapi
    client.app.state.scheduler.stop()  # the test edits item states by hand
    connect_all()
    sid = live(client, platforms=["instagram"]).json()["schedule"]["id"]

    class Busy:
        def cancel_if_queued(self, task_id):
            return "running"

        def cancel_ex(self, task_id):  # pragma: no cover - must never be called
            raise AssertionError("cancel_ex would interrupt the running publish")

    def fn(rows):
        rows[0]["items"]["instagram"].update(state="queued", task_id="a" * 32)
    schedules.update(fn)
    client.app.state.ctx.worker = Busy()
    try:
        d = assert_error_shape(client.post(f"/api/schedules/{sid}/cancel"), 409, "cannot cancel")
    finally:
        client.app.state.ctx.worker = worker
    assert "being published right now" in d["errors"]["instagram"][0]


def test_frame_preview_409_without_final(sapi):
    client, _, _ = sapi
    (jobs_root() / JOB / "clips/01/hi/final.mp4").unlink()
    assert_error_shape(client.get(COVER + "/frame?t=1"), 409, "no final render")
    assert_error_shape(client.put(COVER, json={"source": "frame", "time": 1}), 409, "no final render")
    d = client.get(COVER).json()
    assert d["final"] is False and d["duration"] is None


def test_put_cover_frame_structure_and_files(sapi):
    client, _, _ = sapi
    r = client.put(COVER, json={"source": "frame", "time": 2, "title": "वो चार मिनट"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert_keys(d, COVER_VIEW_KEYS)
    c = d["cover"]
    assert_keys(c, COVER_KEYS)
    assert c["source"] == "frame" and c["time"] == 2.0 and c["thumb_offset_ms"] == 2000
    assert (c["width"], c["height"]) == (1080, 1920) and c["outdated"] is False
    assert c["url"] == COVER + ".jpg"
    d_dir = jobs_root() / JOB / "clips/01/hi"
    assert (d_dir / "cover.jpg").stat().st_size == c["size"]
    assert json.loads((d_dir / "cover.json").read_text(encoding="utf-8"))["title"] == "वो चार मिनट"
    img = client.get(COVER + ".jpg")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert img.content == (d_dir / "cover.jpg").read_bytes()
    # the dry-run plans now carry it
    set_publish(dry_run=True)
    plans = client.post(f"/api/jobs/{JOB}/clips/1/hi/publish", json={"platforms": ALL}).json()["plans"]
    assert all(p["cover"]["sha256"] == c["sha256"] for p in plans.values())


@pytest.mark.parametrize("b", [{}, {"source": "frame"}, {"source": "frame", "time": -1}, {"source": "frame", "time": 6},
                               {"source": "frame", "time": "1"}, {"source": "frame", "time": True},
                               {"source": "frame", "time": 1, "title": "x" * 101},
                               {"source": "frame", "time": 1, "title": 5}, {"source": "video", "time": 1},
                               {"source": "frame", "time": 1, "extra": 1}, {"source": "frame", "time": 1, "title": "a\u0007b"}])
def test_put_cover_422(sapi, b):
    client, _, _ = sapi
    assert_error_shape(client.put(COVER, json=b), 422)
    assert not (jobs_root() / JOB / "clips/01/hi/cover.jpg").exists()


def test_put_cover_upload_without_image_409(sapi):
    assert_error_shape(sapi[0].put(COVER, json={"source": "upload"}), 409, "upload an image first")


def test_upload_image_then_use_it(sapi):
    client, _, _ = sapi
    data = png()
    r = client.post(COVER + "/image", content=data, headers={"Content-Type": "image/png"})
    assert r.status_code == 201, r.text
    d = r.json()
    assert_keys(d, {**COVER_VIEW_KEYS, "image": dict})
    assert d["image"] == {"type": "image/png", "width": 200, "height": 300, "size": len(data)}
    assert d["has_upload"] is True and d["cover"] is None
    r = client.put(COVER, json={"source": "upload", "title": "Hello"})
    assert r.status_code == 200 and r.json()["cover"]["source"] == "upload"
    assert r.json()["cover"]["thumb_offset_ms"] is None


@pytest.mark.parametrize("ctype,data,status", [
    ("image/gif", b"GIF89a....", 415), ("text/plain", b"hello", 415), ("", b"\x89PNG\r\n\x1a\n", 415),
    ("image/png", b"", 422), ("image/jpeg", b"\x89PNG\r\n\x1a\nxxxx", 422), ("image/png", b"\x89PNG\r\n\x1a\njunk", 422),
])
def test_upload_image_rejects(sapi, ctype, data, status):
    client, _, _ = sapi
    headers = {"Content-Type": ctype} if ctype else {}
    r = client.post(COVER + "/image", content=data, headers=headers)
    assert_error_shape(r, status)
    assert not list((jobs_root() / JOB / "clips/01/hi").glob("cover_upload*"))


def test_upload_image_too_large_413(sapi):
    client, _, _ = sapi
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (10 * 1024 * 1024)
    assert_error_shape(client.post(COVER + "/image", content=big, headers={"Content-Type": "image/png"}), 413)


def test_delete_cover(sapi):
    client, _, _ = sapi
    client.put(COVER, json={"source": "frame", "time": 1})
    r = client.delete(COVER)
    assert r.status_code == 204 and r.content == b""
    assert not (jobs_root() / JOB / "clips/01/hi/cover.jpg").exists()
    assert client.get(COVER).json()["cover"] is None
    assert client.delete(COVER).status_code == 204  # idempotent


def test_cover_write_conflicts_with_active_publish(sapi):
    client, worker, _ = sapi
    idle = Worker()
    idle.submit_publish(JOB, 1, "hi", ["instagram"])
    client.app.state.ctx.worker = idle
    try:
        assert_error_shape(client.put(COVER, json={"source": "frame", "time": 1}), 409, "being published")
        assert_error_shape(client.delete(COVER), 409)
        assert_error_shape(client.post(COVER + "/image", content=png(), headers={"Content-Type": "image/png"}), 409)
    finally:
        client.app.state.ctx.worker = worker


def test_no_secret_in_any_response(sapi):
    client, _, _ = sapi
    connect_all()
    texts = [live(client).text, client.get("/api/schedules").text, client.get(COVER).text]
    assert grep_secrets(*texts) == []
    assert all(s not in schedules.store_path().read_text(encoding="utf-8") for s in ALL_SECRETS)


def test_settings_api_refuses_unknown_schedule_timezone(sapi):
    client, _, _ = sapi
    s = client.get("/api/publish/settings").json()
    s.setdefault("schedule", {})["timezone"] = "Mars/Base"
    d = assert_error_shape(client.put("/api/publish/settings", json=s), 422)
    assert "unknown time zone" in json.dumps(d["errors"])
    s["schedule"]["timezone"] = "Asia/Dhaka"
    assert client.put("/api/publish/settings", json=s).status_code == 200
    assert client.get("/api/schedules").json()["timezone"] == "Asia/Dhaka"
