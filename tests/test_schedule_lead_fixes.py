"""Team B lead: regression tests for the G6/G7 review findings (security SEC-1..8, UX-1..8).
All against publish_fakes (no network)."""
from __future__ import annotations

import functools
import threading
import time
from pathlib import Path

import pytest
from publish_fakes import FakeNet, IG, PAGE, connect_all, make_job, meta_error, set_publish
from pydantic import ValidationError

from autoclipper import cli
from autoclipper.config import load_settings
from autoclipper.jobstore import jobs_root
from autoclipper.publish import cover as covers
from autoclipper.publish import records, runner, schedules, service
from autoclipper.publish.base import PublishError
from autoclipper.publish.service import PublishConflict
from autoclipper.publish.settings import ScheduleSettings
from autoclipper.web.scheduler import Scheduler
from autoclipper.web.worker import Worker

JOB = "20261123-120000-pubtst"
NOOP = lambda s: None  # noqa: E731


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def job() -> str:
    return make_job(JOB)


@pytest.fixture
def net() -> FakeNet:
    return FakeNet()


@pytest.fixture
def worker(net):
    w = Worker(publish=functools.partial(runner.run_publish, http=net.client(), sleep=NOOP))
    w.start()
    yield w
    w.stop()


def at(hours: float = 3.0) -> float:
    return float(int(time.time() + hours * 3600))


def item(sid, p):
    return schedules.get(sid)["items"][p]


def set_privacy(lang, privacy):
    def fill(ps):
        ps.lang(lang).youtube.privacy = privacy
    service._update_settings(fill)


# ---- SEC-1: cancel never interrupts a running publish ----------------------------------------
def test_cancel_if_queued_never_signals_a_running_task(job):
    started, release = threading.Event(), threading.Event()

    def slow_publish(*a, **kw):
        started.set()
        release.wait(10)
        return {}

    w = Worker(publish=slow_publish)
    w.start()
    try:
        running, _ = w.submit_publish(JOB, 1, "hi", ["instagram"])
        assert started.wait(5)
        queued, _ = w.submit_publish(JOB, 1, "en", ["instagram"])
        assert w.cancel_if_queued(running.id) == "running"
        assert not w._cancel[running.id].is_set()  # not interrupted
        assert w.cancel_if_queued(queued.id) == "removed"
        assert w.status(queued.id).state == "cancelled"
        assert w.cancel_if_queued("f" * 32) == "unknown"
        release.set()
        assert w.wait_idle(10)
        assert w.status(running.id).state == "done"
        assert w.cancel_if_queued(running.id) == "finished"
    finally:
        release.set()
        w.stop()


def test_cancel_of_a_running_schedule_item_is_refused_and_keeps_running(job):
    connect_all()
    started, release = threading.Event(), threading.Event()

    def slow_publish(*a, **kw):
        started.set()
        release.wait(10)
        return {}

    w = Worker(publish=slow_publish)
    w.start()
    try:
        s = schedules.create(JOB, 1, "hi", ["instagram"], at(), "", load_settings())
        task, _ = w.submit_publish(JOB, 1, "hi", ["instagram"])
        assert started.wait(5)

        def fn(rows):  # the store still says queued (the scheduler has not ticked yet)
            rows[0]["items"]["instagram"].update(state="queued", task_id=task.id)
        schedules.update(fn)
        with pytest.raises(PublishConflict) as e:
            schedules.cancel(s["id"], cancel_task=w.cancel_if_queued)
        assert "being published right now" in e.value.errors["instagram"][0]
        assert not w._cancel[task.id].is_set()
        assert item(s["id"], "instagram")["state"] == "queued"
    finally:
        release.set()
        w.stop()


def test_cancel_after_restart_unknown_task(job):
    connect_all()
    s = schedules.create(JOB, 1, "hi", ["instagram"], at(), "", load_settings())

    def fn(rows):
        rows[0]["items"]["instagram"].update(state="queued", task_id="0" * 32)
    schedules.update(fn)
    out = schedules.cancel(s["id"], cancel_task=lambda tid: "unknown")  # nothing runs it in this server
    assert out["items"]["instagram"]["state"] == "cancelled"


def test_cancel_after_restart_already_published_is_refused(job):
    connect_all()
    s = schedules.create(JOB, 1, "hi", ["instagram"], at(), "", load_settings())

    def fn(rows):
        rows[0]["items"]["instagram"].update(state="queued", task_id="0" * 32)
    schedules.update(fn)
    sw = records.FileStatusWriter(JOB, 1, "hi", "instagram", {"platform": "instagram", "state": "published",
                                                              "url": "https://www.instagram.com/reel/x/"})
    sw.update(state="published", remote_id="178")
    with pytest.raises(PublishConflict):
        schedules.cancel(s["id"], cancel_task=lambda tid: "unknown")
    assert item(s["id"], "instagram")["state"] == "queued"


def test_cancel_route_uses_cancel_if_queued():
    src = (Path(__file__).parents[1] / "autoclipper/web/schedule_routes.py").read_text(encoding="utf-8")
    assert "cancel_if_queued(" in src and "cancel_ex(" not in src


# ---- SEC-2: one scheduler per data folder ---------------------------------------------------
def test_second_scheduler_on_same_data_folder_does_not_fire(job):
    a, b = Scheduler(Worker()), Scheduler(Worker())
    try:
        assert a._own() is True and a.owner
        assert b._own() is False and not b.owner
        assert "another Auto Clipper server" in b.blocked
        a.release()
        assert b._own() is True and b.blocked is None
    finally:
        a.release()
        b.release()


def test_blocked_scheduler_thread_never_ticks(job):
    owner = Scheduler(Worker())
    assert owner._own()
    try:
        connect_all()
        schedules.create(JOB, 1, "hi", ["instagram"], at(), "", load_settings())
        w = Worker()
        sch = Scheduler(w, interval=0.05)
        w.scheduler = sch
        w.start()
        try:
            time.sleep(0.3)
            assert sch.ticks == 0 and not sch.alive and sch.blocked
        finally:
            w.stop()
    finally:
        owner.release()


def test_worker_stop_releases_the_owner_lock(job):
    w = Worker()
    sch = Scheduler(w, interval=0.05)
    w.scheduler = sch
    w.start()
    deadline = time.monotonic() + 5
    while not sch.owner and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sch.owner
    w.stop()
    assert not sch.owner
    other = Scheduler(Worker())
    assert other._own()
    other.release()


# ---- SEC-3 / UX-1: dry run at the hand-off time ------------------------------------------------
def test_native_handoff_with_dry_run_on_waits_in_app_mode(job, net, worker):
    connect_all()
    t = at(3)
    s = schedules.create(JOB, 1, "hi", ["facebook"], t, "", load_settings())
    assert item(s["id"], "facebook")["mode"] == "native"
    set_publish(dry_run=True)
    sch = Scheduler(worker, clock=Clock(t - 3600 + 1))
    sch.tick()
    it = item(s["id"], "facebook")
    assert it["state"] == "scheduled" and it["mode"] == "app", it
    assert "dry run was on at the hand-off" in it["mode_note"] and it["handoff_at"] == t
    assert net.requests == []
    set_publish(dry_run=False)  # turned off again before the time: it still goes out
    sch._clock = Clock(t + 1)
    sch.tick()
    assert worker.wait_idle(10)
    sch.tick()
    assert item(s["id"], "facebook")["state"] == "done"
    assert next(iter(net.graph.reels.values()))["form"]["video_state"] == "PUBLISHED"


def test_native_handoff_dry_run_still_on_at_time_fails_then(job, net, worker):
    connect_all()
    t = at(3)
    s = schedules.create(JOB, 1, "hi", ["facebook"], t, "", load_settings())
    set_publish(dry_run=True)
    sch = Scheduler(worker, clock=Clock(t - 3600 + 1))
    sch.tick()
    assert item(s["id"], "facebook")["state"] == "scheduled"
    sch._clock = Clock(t + 1)
    sch.tick()
    it = item(s["id"], "facebook")
    assert it["state"] == "failed" and "dry run was on" in it["message"]
    assert net.requests == []


def test_native_plan_and_dialog_say_handoff_cannot_be_stopped(job):
    connect_all()
    set_privacy("hi", "public")
    t = at(3)
    out = schedules.preview(JOB, 1, "hi", ["youtube", "facebook"], t, "", load_settings())
    for p in ("youtube", "facebook"):
        assert any("does not stop it" in w for w in out["plans"][p]["warnings"]), out["plans"][p]["warnings"]
    js = (Path(__file__).parents[1] / "autoclipper/web/static/schedule.js").read_text(encoding="utf-8")
    assert "no longer stops it" in js


# ---- SEC-4: a read error never empties the store ---------------------------------------------
def test_store_read_error_retries_then_raises_and_keeps_the_file(job, monkeypatch):
    connect_all()
    s = schedules.create(JOB, 1, "hi", ["instagram"], at(), "", load_settings())
    monkeypatch.setattr(schedules, "LOAD_RETRY_SLEEP", 0)
    real = Path.read_text
    calls = {"n": 0}

    def locked_read(self, *a, **kw):
        if self.name == "schedules.json":
            calls["n"] += 1
            raise PermissionError(13, "sharing violation")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", locked_read)
    with pytest.raises(schedules.ScheduleStoreError):
        schedules.read_all()
    with pytest.raises(schedules.ScheduleStoreError):
        schedules.update(lambda rows: rows.clear())
    assert calls["n"] == 2 * schedules.LOAD_RETRIES
    monkeypatch.setattr(Path, "read_text", real)
    assert [x["id"] for x in schedules.list_schedules()] == [s["id"]]
    assert not list(schedules.store_path().parent.glob("schedules.broken-*.json"))


def test_store_brief_read_error_recovers(job, monkeypatch):
    connect_all()
    s = schedules.create(JOB, 1, "hi", ["instagram"], at(), "", load_settings())
    monkeypatch.setattr(schedules, "LOAD_RETRY_SLEEP", 0)
    real = Path.read_text
    left = {"n": 2}

    def flaky(self, *a, **kw):
        if self.name == "schedules.json" and left["n"]:
            left["n"] -= 1
            raise PermissionError(13, "sharing violation")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert [x["id"] for x in schedules.read_all()] == [s["id"]]


@pytest.mark.parametrize("text", ['["x"]', '{"schedules": "x"}', "\udcff"])
def test_wrong_shape_store_is_kept_aside(job, text):
    schedules.store_path().write_bytes(text.encode("utf-8", "surrogateescape"))
    assert schedules.read_all() == []
    assert list(schedules.store_path().parent.glob("schedules.broken-*.json"))


# ---- SEC-5: daily limits count a native post on its posting day, in the settings zone -----------
def test_native_ledger_counts_on_the_posting_day(job, net):
    connect_all()
    t = at(3)
    runner.run_publish(JOB, 1, "hi", platforms=["facebook"], force=False, settings=load_settings(), log=NOOP,
                       http=net.client(), sleep=NOOP,
                       schedule={"id": "s_000000000001", "publish_at": t, "native": True})
    rows = schedules._ledger_rows()
    assert len(rows) == 1 and rows[0]["day_t"] == t and rows[0]["t"] < t
    start, end, _ = schedules.day_bounds(t, "")
    assert schedules.day_count([], "facebook", PAGE["hi"][0], start, end) == 1
    # the rolling 24 h API window still counts it from the upload
    assert records.window_count("facebook", PAGE["hi"][0]) == 1


def test_limit_day_uses_settings_zone(job):
    def fill(ps):
        ps.schedule.timezone = "America/New_York"
    service._update_settings(fill)
    assert schedules.limit_label(load_settings()) == "America/New_York"
    service._update_settings(lambda ps: setattr(ps.schedule, "timezone", ""))
    assert schedules.limit_label(load_settings()) == ""


def test_daily_limit_counted_in_settings_zone_not_browser_zone(job):
    connect_all()
    base = float(int(time.time() // 86400 + 2) * 86400)  # a UTC midnight two days ahead
    # one post at 17:00 UTC (23:00 in Dhaka); the new one at 19:00 UTC (01:00 the next day in Dhaka)
    records.append_ledger("instagram", IG["hi"][0], "hi", time.time(), day_t=base + 17 * 3600)

    def zone(name):
        def fill(ps):
            ps.schedule.daily_limits.instagram = 1
            ps.schedule.timezone = name
        service._update_settings(fill)

    zone("UTC")  # same UTC day -> refused, although the browser sent a +06:00 time
    with pytest.raises(PublishConflict) as e:
        schedules.create(JOB, 1, "hi", ["instagram"], base + 19 * 3600, "UTC+06:00", load_settings())
    assert "daily limit reached" in str(e.value.errors)
    zone("Asia/Dhaka")  # another Dhaka day -> accepted, although the browser sent a UTC time
    s = schedules.create(JOB, 1, "hi", ["instagram"], base + 19 * 3600, "UTC", load_settings())
    assert s["items"]["instagram"]["state"] == "scheduled"


# ---- SEC-6/7: Facebook window re-check, hand-off failures fall back to app mode ---------------
def test_facebook_finish_rechecks_the_10_minute_window(job, net):
    connect_all()
    t = time.time() + 10 * 60 + 30  # passes preflight (> 10 min), fails the re-check before finish
    res = runner.run_publish(JOB, 1, "hi", platforms=["facebook"], force=False, settings=load_settings(),
                             log=NOOP, http=net.client(), sleep=NOOP,
                             schedule={"id": "s_000000000001", "publish_at": t, "native": True})
    st = res["facebook"]
    assert st["state"] == "failed" and "too close to the scheduled time" in st["message"]
    assert all(r["form"] is None for r in net.graph.reels.values())  # finish never sent
    assert not records.resume_sent(JOB, 1, "hi", "facebook")


def test_failed_native_handoff_falls_back_to_app_at_the_time(job, net, worker):
    connect_all()
    t = at(3)
    s = schedules.create(JOB, 1, "hi", ["facebook"], t, "", load_settings())
    net.graph.inject["fb_start"] = [meta_error(100, "Invalid parameter")]
    sch = Scheduler(worker, clock=Clock(t - 3600 + 1))
    sch.tick()
    assert worker.wait_idle(10)
    sch.tick()
    it = item(s["id"], "facebook")
    assert it["state"] == "scheduled" and it["mode"] == "app" and "hand-off to Facebook failed" in it["mode_note"]
    sch._clock = Clock(t + 1)
    sch.tick()
    assert worker.wait_idle(10)
    sch.tick()
    assert item(s["id"], "facebook")["state"] == "done"
    finished = [r for r in net.graph.reels.values() if r["form"]]
    assert len(finished) == 1 and finished[0]["form"]["video_state"] == "PUBLISHED"


def test_failed_handoff_after_publish_request_does_not_fall_back(job, net, worker):
    connect_all()
    t = at(3)
    s = schedules.create(JOB, 1, "hi", ["facebook"], t, "", load_settings())
    sch = Scheduler(worker, clock=Clock(t - 3600 + 1))

    def fn(rows):  # pretend the finish request may have gone out
        rows[0]["items"]["facebook"].update(state="queued", task_id="0" * 32)
    schedules.update(fn)
    sw = records.FileStatusWriter(JOB, 1, "hi", "facebook", {"platform": "facebook", "state": "failed",
                                                             "idempotency_key": f"schedule-{s['id']}-facebook",
                                                             "message": "lost answer"})
    sw.update(state="failed")
    sw.save_resume({"video_id": "1", "sha256": "x", "uploaded": True, "sent": True})
    sch.tick()
    it = item(s["id"], "facebook")
    assert it["state"] == "failed" and it["mode"] == "native"


def test_youtube_privacy_changed_after_scheduling_goes_app_side(job, net, worker):
    connect_all()
    set_privacy("hi", "public")
    t = at(3)
    s = schedules.create(JOB, 1, "hi", ["youtube"], t, "", load_settings())
    assert item(s["id"], "youtube")["mode"] == "native"
    set_privacy("hi", "unlisted")
    sch = Scheduler(worker, clock=Clock(t - 3600 + 1))
    sch.tick()
    it = item(s["id"], "youtube")
    assert it["state"] == "scheduled" and it["mode"] == "app" and "no longer public" in it["mode_note"]
    assert net.requests == []


def test_handed_off_done_message_is_honest(job, net, worker):
    connect_all()
    t = at(2)
    s = schedules.create(JOB, 1, "hi", ["facebook"], t, "", load_settings())

    def fn(rows):
        rows[0]["items"]["facebook"].update(state="handed_off")
    schedules.update(fn)
    Scheduler(worker, clock=Clock(t + 1)).tick()
    it = item(s["id"], "facebook")
    assert it["state"] == "done" and "does not re-check" in it["message"]


# ---- SEC-8c / UX-5: covers ------------------------------------------------------------------------
def _final(lang="hi"):
    return jobs_root() / JOB / "clips" / "01" / lang / "final.mp4"


def test_thumb_offset_past_the_end_of_a_shorter_render_is_dropped(job):
    f = _final()
    covers.make_cover(JOB, 1, "hi", source="frame", t=5.5, title="", final=f,
                      final_sha256=records.file_sha256(f), font="Noto Sans Devanagari")
    c = covers.item_cover(JOB, 1, "hi", "other-sha", duration=3.0)
    assert c["thumb_offset_ms"] is None and c["past_end"] is True and c["outdated"] is True
    assert covers.item_cover(JOB, 1, "hi", records.file_sha256(f), duration=6.0)["thumb_offset_ms"] == 5500


def test_uploaded_cover_is_never_outdated(job):
    import subprocess
    png = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                          "color=c=red:s=200x300", "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"],
                         check=True, capture_output=True).stdout
    covers.save_upload(JOB, 1, "hi", png, "image/png")
    f = _final()
    covers.make_cover(JOB, 1, "hi", source="upload", t=None, title="", final=f,
                      final_sha256=records.file_sha256(f), font="Noto Sans Devanagari")
    assert covers.public_info(JOB, 1, "hi", "a-new-render")["outdated"] is False
    assert covers.item_cover(JOB, 1, "hi", "a-new-render")["outdated"] is False


# ---- UX-3: plan warnings in local time ------------------------------------------------------------
def test_plan_warnings_use_local_time(job):
    connect_all()
    set_privacy("hi", "public")
    t = at(3)
    out = schedules.preview(JOB, 1, "hi", ["youtube", "facebook"], t, "", load_settings())
    for p in ("youtube", "facebook"):
        w = " ".join(out["plans"][p]["warnings"])
        assert "(UTC" in w and "Z;" not in w and " UTC;" not in w, w


# ---- UX-4: time zone setting validated ---------------------------------------------------------
@pytest.mark.parametrize("tz", ["Mars/Base", "Asia/Nowhere"])
def test_unknown_timezone_setting_is_refused(tz):
    with pytest.raises(ValidationError):
        ScheduleSettings(timezone=tz)


@pytest.mark.parametrize("tz", ["", "UTC", "Asia/Dhaka", "America/New_York"])
def test_known_timezone_setting_is_accepted(tz):
    assert ScheduleSettings(timezone=tz).timezone == tz


# ---- UX-8 small points ----------------------------------------------------------------------------
def test_past_time_message(job):
    connect_all()
    with pytest.raises(service.PublishInputError) as e:
        schedules.create(JOB, 1, "hi", ["instagram"], time.time() - 3 * 3600, "", load_settings())
    assert "in the past" in e.value.errors["publish_at"][0]


@pytest.mark.parametrize("t", [float("nan"), float("inf"), float("-inf")])
def test_check_time_non_finite_message(t):
    with pytest.raises(covers.CoverError) as e:
        covers.check_time(t, 10.0)
    assert "finite" in e.value.message


def test_cli_yes_in_dry_run_exits_nonzero(job, capsys):
    connect_all(dry_run=True)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 3 * 3600))
    code = cli.main(["schedule", "add", JOB, "--clip", "1", "--lang", "hi", "--platform", "instagram",
                     "--at", when, "--yes"])
    err = capsys.readouterr().err
    assert code == 2 and "dry-run mode" in err
    assert schedules.read_all() == []


def test_facebook_plan_status_note_for_scheduled_reel(job):
    connect_all()
    out = schedules.preview(JOB, 1, "hi", ["facebook"], at(3), "", load_settings())
    status = [r for r in out["plans"]["facebook"]["requests"] if r["step"] == "status"][0]
    assert status["note"] == "read once to confirm the reel is scheduled"


def test_facebook_schedule_error_type():
    e = PublishError("x", kind="invalid", platform="facebook", code="schedule_window")
    assert not e.transient
