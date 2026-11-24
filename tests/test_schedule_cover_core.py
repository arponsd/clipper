"""G6 scheduled publishing + G7 cover picker: core (publish.cover, publish.schedules, the clients'
native scheduling / cover requests, and the web.scheduler timer) - all against publish_fakes (no network).
"""
from __future__ import annotations

import functools
import json
import subprocess
import time
from pathlib import Path

import pytest
from publish_fakes import FakeNet, IG, PAGE, CHANNEL, connect_all, google_error, make_job, set_publish

from autoclipper import ffmpeg
from autoclipper.config import load_settings
from autoclipper.jobstore import jobs_root
from autoclipper.publish import cover as covers
from autoclipper.publish import records, runner, schedules, service
from autoclipper.publish.service import PublishConflict, PublishInputError, PublishNotFound
from autoclipper.web.scheduler import Scheduler
from autoclipper.web.worker import Worker

JOB = "20261123-120000-pubtst"
ALL = ["youtube", "facebook", "instagram"]
NOOP = lambda s: None  # noqa: E731


@pytest.fixture
def job() -> str:
    return make_job(JOB)


@pytest.fixture
def net() -> FakeNet:
    return FakeNet()


def clip_dir(lang="hi"):
    return jobs_root() / JOB / "clips" / "01" / lang


def final(lang="hi"):
    return clip_dir(lang) / "final.mp4"


def png_bytes(w=200, h=300) -> bytes:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                          f"color=c=red:s={w}x{h}", "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"],
                         check=True, capture_output=True).stdout
    return out


def make_frame_cover(lang="hi", t=1.0, title=""):
    return covers.make_cover(JOB, 1, lang, source="frame", t=t, title=title, final=final(lang),
                             final_sha256=records.file_sha256(final(lang)),
                             font="Noto Sans Devanagari" if lang == "hi" else "Noto Sans")


def set_privacy(lang, privacy):
    def fill(ps):
        ps.lang(lang).youtube.privacy = privacy
    service._update_settings(fill)


# =================================================================================================
# cover
# =================================================================================================
def test_make_cover_from_frame_with_devanagari_title(job):
    info = make_frame_cover("hi", 2.5, "वो चार मिनट")
    p = clip_dir() / "cover.jpg"
    assert p.read_bytes()[:3] == b"\xff\xd8\xff"
    mi = ffmpeg.probe(p)
    assert (mi.width, mi.height) == (1080, 1920)
    assert 0 < p.stat().st_size <= covers.MAX_COVER_BYTES
    saved = json.loads((clip_dir() / "cover.json").read_text(encoding="utf-8"))
    assert saved == info
    assert saved["source"] == "frame" and saved["time"] == 2.5 and saved["title"] == "वो चार मिनट"
    assert saved["sha256"] == records.file_sha256(p) and saved["final_sha256"] == records.file_sha256(final())
    assert not list(clip_dir().glob(".cover*"))  # no temp files left


@pytest.mark.system_font
def test_cover_title_changes_the_pixels(job):
    make_frame_cover("en", 1.0, "")
    plain = (clip_dir("en") / "cover.jpg").read_bytes()
    make_frame_cover("en", 1.0, "Big title")
    assert (clip_dir("en") / "cover.jpg").read_bytes() != plain


@pytest.mark.parametrize("t", [-0.1, 5.99, 6.0, 100, float("nan"), True, "1", None])
def test_check_time_rejects_outside_clip(t):
    with pytest.raises(covers.CoverError) as e:
        covers.check_time(t, 6.0)
    assert e.value.field == "time" and e.value.status == 422


@pytest.mark.parametrize("t", [0, 0.0, 3, 5.95])
def test_check_time_accepts_inside_clip(t):
    assert covers.check_time(t, 6.0) == float(t)


def test_check_title():
    assert covers.check_title(None) == ""
    assert covers.check_title("  two   words ") == "two words"
    assert covers.check_title("x" * 100) == "x" * 100
    for bad in ("x" * 101, 5, "bad\x07bell"):
        with pytest.raises(covers.CoverError):
            covers.check_title(bad)


def test_save_upload_validation(job):
    good = png_bytes()
    with pytest.raises(covers.CoverError) as e:
        covers.save_upload(JOB, 1, "hi", good, "image/gif")
    assert e.value.status == 415
    with pytest.raises(covers.CoverError) as e:
        covers.save_upload(JOB, 1, "hi", good, "image/jpeg")  # PNG bytes labelled JPEG
    assert e.value.status == 422
    with pytest.raises(covers.CoverError) as e:
        covers.save_upload(JOB, 1, "hi", b"", "image/png")
    assert e.value.status == 422
    with pytest.raises(covers.CoverError) as e:
        covers.save_upload(JOB, 1, "hi", b"\x89PNG\r\n\x1a\n" + b"0" * (covers.MAX_UPLOAD_BYTES), "image/png")
    assert e.value.status == 413
    with pytest.raises(covers.CoverError) as e:
        covers.save_upload(JOB, 1, "hi", b"\x89PNG\r\n\x1a\n" + b"garbage" * 10, "image/png")
    assert "could not be read" in e.value.message
    with pytest.raises(covers.CoverError) as e:
        covers.save_upload(JOB, 1, "hi", png_bytes(40, 40), "image/png")
    assert "64-8192" in e.value.message
    assert covers.upload_path(JOB, 1, "hi") is None
    info = covers.save_upload(JOB, 1, "hi", good, "image/png; charset=binary")
    assert info == {"type": "image/png", "width": 200, "height": 300, "size": len(good)}
    assert covers.upload_path(JOB, 1, "hi").name == "cover_upload.png"


def test_cover_from_upload_and_delete(job):
    covers.save_upload(JOB, 1, "hi", png_bytes(), "image/png")
    info = covers.make_cover(JOB, 1, "hi", source="upload", t=3.0, title="", final=final(),
                             final_sha256=records.file_sha256(final()), font="Noto Sans")
    assert info["source"] == "upload" and info["time"] is None
    assert ffmpeg.probe(clip_dir() / "cover.jpg").width == 1080
    assert covers.thumb_offset_ms(info) is None
    assert covers.delete_cover(JOB, 1, "hi") is True
    assert not any((clip_dir() / n).exists() for n in covers.COVER_FILES)
    assert covers.delete_cover(JOB, 1, "hi") is False


def test_upload_source_without_upload_is_409(job):
    with pytest.raises(covers.CoverError) as e:
        covers.make_cover(JOB, 1, "hi", source="upload", t=None, title="", final=final(),
                          final_sha256="x", font="Noto Sans")
    assert e.value.status == 409


def test_item_cover_marks_outdated_after_rerender(job):
    make_frame_cover("hi", 1.2)
    item, _ = service.load_item(JOB, 1, "hi")
    assert item.cover["outdated"] is False and item.cover["thumb_offset_ms"] == 1200
    assert item.cover["path"] == "clips/01/hi/cover.jpg"
    with open(final(), "ab") as f:  # a "re-render"
        f.write(b"\0" * 16)
    item, _ = service.load_item(JOB, 1, "hi")
    assert item.cover["outdated"] is True
    plan = service.dry_run(JOB, 1, "hi", ["youtube"], load_settings())["plans"]["youtube"]
    assert any("earlier render" in w for w in plan["warnings"])


# =================================================================================================
# clients: cover in plans and live
# =================================================================================================
def test_plans_with_cover_all_platforms_no_network(job, monkeypatch):
    import httpx

    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    make_frame_cover("hi", 2.0, "शीर्षक")
    plans = service.dry_run(JOB, 1, "hi", ALL, load_settings())["plans"]
    yt, fb, ig = plans["youtube"], plans["facebook"], plans["instagram"]
    for p in plans.values():
        assert p["cover"]["path"] == "clips/01/hi/cover.jpg" and p["cover"]["source"] == "frame"
        assert p["cover"]["title"] == "शीर्षक" and p["schedule"] is None
    steps = [r["step"] for r in yt["requests"]]
    assert steps[-2:] == ["processing", "thumbnail"]
    th = yt["requests"][-1]
    assert th["url"] == "https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}"
    assert th["headers"]["Content-Type"] == "image/jpeg" and th["headers"]["Authorization"] == "Bearer ***"
    fb_steps = [r["step"] for r in fb["requests"]]
    assert fb_steps.index("thumbnail") < fb_steps.index("finish")
    assert fb["requests"][fb_steps.index("thumbnail")]["url"].endswith("/{video_id}/thumbnails")
    container = next(r for r in ig["requests"] if r["step"] == "container")
    assert container["body"]["thumb_offset"] == "2000"
    assert any("title text is not shown" in w for w in ig["warnings"])


def test_plans_without_cover_unchanged(job):
    plans = service.dry_run(JOB, 1, "en", ALL, load_settings())["plans"]
    assert all(p["cover"] is None for p in plans.values())
    assert "thumbnail" not in [r["step"] for r in plans["youtube"]["requests"]]
    assert "thumb_offset" not in next(r for r in plans["instagram"]["requests"] if r["step"] == "container")["body"]


def test_live_publish_sets_cover_everywhere(job, net):
    connect_all()
    make_frame_cover("hi", 1.5)
    res = runner.run_publish(JOB, 1, "hi", platforms=ALL, force=False, settings=load_settings(), log=NOOP,
                             http=net.client(), sleep=NOOP)
    assert all(r["state"] == "published" for r in res.values()), res
    vid = net.google.videos[0]["id"]
    assert net.google.thumbnails[vid] == (clip_dir() / "cover.jpg").read_bytes()
    proc = [r for r in net.google.requests if r.url.startswith("https://www.googleapis.com/youtube/v3/videos?")]
    assert proc and "part=processingDetails" in proc[0].url
    reel = next(iter(net.graph.reels.values()))
    assert reel.get("thumbnail") is True
    container = next(iter(net.graph.containers.values()))
    assert container["form"]["thumb_offset"] == "1500"
    for p in ALL:
        assert res[p]["cover"]["state"] == "set", res[p]
        assert records.read_status_raw(JOB, 1, "hi", p)["cover"]["state"] == "set"
    assert res["youtube"]["message"] == "published"


def test_youtube_thumbnail_forbidden_does_not_fail_upload(job, net):
    connect_all()
    make_frame_cover("hi", 1.0)
    net.google.inject["thumbnail"] = [google_error(403, "forbidden", "The authenticated user doesnt have permissions")]
    res = runner.run_publish(JOB, 1, "hi", platforms=["youtube"], force=False, settings=load_settings(), log=NOOP,
                             http=net.client(), sleep=NOOP)
    st = res["youtube"]
    assert st["state"] == "published" and st["remote_id"]
    assert st["cover"]["state"] == "failed" and "youtube.com/verify" in st["cover"]["message"]
    assert "cover not set" in st["message"]


def test_youtube_cover_skipped_while_still_processing(job, net):
    connect_all()
    make_frame_cover("hi", 1.0)
    processing = lambda req: __import__("httpx").Response(  # noqa: E731
        200, json={"items": [{"id": "x", "processingDetails": {"processingStatus": "processing"}}]})
    net.google.inject["processing"] = [processing] * 50
    res = runner.run_publish(JOB, 1, "hi", platforms=["youtube"], force=False, settings=load_settings(), log=NOOP,
                             http=net.client(), sleep=NOOP)
    assert res["youtube"]["state"] == "published"
    assert res["youtube"]["cover"]["state"] == "skipped" and "YouTube Studio" in res["youtube"]["cover"]["message"]
    assert not net.google.thumbnails


def test_uploaded_cover_not_sent_to_instagram(job, net):
    connect_all()
    covers.save_upload(JOB, 1, "hi", png_bytes(), "image/png")
    covers.make_cover(JOB, 1, "hi", source="upload", t=None, title="", final=final(),
                      final_sha256=records.file_sha256(final()), font="Noto Sans")
    plan = service.dry_run(JOB, 1, "hi", ["instagram"], load_settings())["plans"]["instagram"]
    assert any("uploaded cover image is not used" in w for w in plan["warnings"])
    res = runner.run_publish(JOB, 1, "hi", platforms=["instagram"], force=False, settings=load_settings(),
                             log=NOOP, http=net.client(), sleep=NOOP)
    assert "thumb_offset" not in next(iter(net.graph.containers.values()))["form"]
    assert res["instagram"]["cover"]["state"] == "skipped"


# =================================================================================================
# clients: native scheduling
# =================================================================================================
def test_youtube_native_schedule_uploads_private_with_publish_at(job, net):
    connect_all()
    set_privacy("hi", "public")
    t = float(int(time.time()) + 3 * 3600)
    res = runner.run_publish(JOB, 1, "hi", platforms=["youtube"], force=False, settings=load_settings(), log=NOOP,
                             http=net.client(), sleep=NOOP,
                             schedule={"id": "s_000000000001", "publish_at": t, "native": True, "plan": {"mode": "native"}})
    st = res["youtube"]
    assert st["state"] == "published" and st["scheduled_for"] == t
    assert st["message"].startswith("scheduled on YouTube for ")
    meta = net.google.videos[0]["meta"]
    assert meta["status"]["privacyStatus"] == "private"
    assert meta["status"]["publishAt"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    plan = records.read_plan(JOB, 1, "hi", "youtube")
    assert plan["dry_run"] is False and plan["schedule"] == {"mode": "native"}


def test_facebook_native_schedule_finish_scheduled_no_publish_poll(job, net):
    connect_all()
    t = float(int(time.time()) + 3 * 3600)
    res = runner.run_publish(JOB, 1, "hi", platforms=["facebook"], force=False, settings=load_settings(), log=NOOP,
                             http=net.client(), sleep=NOOP,
                             schedule={"id": "s_000000000001", "publish_at": t, "native": True})
    assert res["facebook"]["state"] == "published" and res["facebook"]["scheduled_for"] == t
    reel = next(iter(net.graph.reels.values()))
    assert reel["form"]["video_state"] == "SCHEDULED"
    assert reel["form"]["scheduled_publish_time"] == str(int(t))
    assert reel["polls"] == 1  # one status read after finish, no wait for "complete"


def test_app_mode_schedule_publishes_normally(job, net):
    connect_all()
    res = runner.run_publish(JOB, 1, "hi", platforms=["instagram"], force=False, settings=load_settings(),
                             log=NOOP, http=net.client(), sleep=NOOP,
                             schedule={"id": "s_000000000001", "publish_at": time.time(), "native": False})
    assert res["instagram"]["state"] == "published" and res["instagram"]["scheduled_for"] is None


def test_native_preflight_problems(job):
    import dataclasses

    connect_all()
    ps = load_settings().publish
    item, _ = service.load_item(JOB, 1, "hi")
    soon = dataclasses.replace(item, publish_at=time.time() + 300)
    probs, _, _ = service.platform_problems(soon, ps, "youtube", [])
    assert any("set privacy to public" in p for p in probs)
    probs, _, _ = service.platform_problems(soon, ps, "facebook", [])
    assert any("more than 10 minutes" in p for p in probs)
    far = dataclasses.replace(item, publish_at=time.time() + 30 * 86400)
    probs, _, _ = service.platform_problems(far, ps, "facebook", [])
    assert any("29 days" in p for p in probs)
    probs, _, _ = service.platform_problems(soon, ps, "instagram", [])
    assert any("no scheduling" in p for p in probs)


# =================================================================================================
# schedules: time parsing
# =================================================================================================
def test_parse_time_offsets_and_zones():
    t, label = schedules.parse_time("2026-11-24T18:30:00+06:00", None)
    assert t == 1795523400.0 and label == "UTC+06:00"
    assert schedules.parse_time("2026-11-24T12:30Z", None)[0] == t
    assert schedules.parse_time("2026-11-24 12:30:00+0000", None)[0] == t
    t2, label2 = schedules.parse_time("2026-11-24T18:30", "Asia/Dhaka")
    assert t2 == t and label2 == "Asia/Dhaka"
    t3, label3 = schedules.parse_time("2026-11-24T18:30", None, "Asia/Dhaka")
    assert t3 == t and label3 == "Asia/Dhaka"
    assert schedules.parse_time("2026-11-24T18:30+06:00", "Asia/Kolkata")[0] == t  # explicit offset wins
    assert schedules.fmt_local(t, "Asia/Dhaka") == "2026-11-24 18:30 (UTC+06:00)"
    assert schedules.fmt_local(t, "UTC+06:00") == "2026-11-24 18:30 (UTC+06:00)"


@pytest.mark.parametrize("value", ["", "tomorrow", "2026-13-01T10:00", "2026-11-24", "2026-11-24T25:00",
                                   "2026-11-24T10:00+6", "x" * 41, 1763987400, None, "2026-11-24T10:00\n"])
def test_parse_time_rejects(value):
    with pytest.raises(PublishInputError) as e:
        schedules.parse_time(value, None)
    assert "publish_at" in e.value.errors


def test_parse_time_bad_zone():
    with pytest.raises(PublishInputError) as e:
        schedules.parse_time("2026-11-24T10:00", "Mars/Olympus_Mons")
    assert "timezone" in e.value.errors
    with pytest.raises(PublishInputError) as e:
        schedules.parse_time("2026-11-24T10:00", "../etc/passwd")
    assert "timezone" in e.value.errors


def test_day_bounds_use_the_schedule_zone():
    t = schedules.parse_time("2026-11-24T00:30+06:00", None)[0]
    start, end, day = schedules.day_bounds(t, "Asia/Dhaka")
    assert day == "Tue 2026-11-24" and end - start == 86400
    assert schedules.day_bounds(t, "UTC")[2] == "Mon 2026-11-23"


# =================================================================================================
# schedules: preview / create / edit / cancel
# =================================================================================================
def at(hours=3.0):
    return float(int(time.time() + hours * 3600))


def test_preview_dry_run_stores_nothing_live(job):
    connect_all(dry_run=True)
    set_privacy("hi", "public")
    t = at()
    out = schedules.preview(JOB, 1, "hi", ALL, t, "Asia/Dhaka", load_settings())
    assert out["dry_run"] is True
    assert not schedules.store_path().exists()
    s = out["schedule"]
    assert s["id"] is None and s["state"] == "preview" and s["publish_at"] == t
    modes = {p: it["mode"] for p, it in s["items"].items()}
    assert modes == {"youtube": "native", "facebook": "native", "instagram": "app"}
    for p, plan in out["plans"].items():
        assert plan["schedule"]["publish_at"] == t and plan["schedule"]["mode"] == modes[p]
        assert plan["schedule"]["timezone"] == "Asia/Dhaka"
        assert any(w.startswith("dry run: nothing was scheduled") for w in plan["warnings"])
        assert records.read_plan(JOB, 1, "hi", p)["schedule"]["publish_at"] == t  # stored plan
    yt_start = next(r for r in out["plans"]["youtube"]["requests"] if r["step"] == "start_session")
    assert yt_start["body"]["status"]["publishAt"] and yt_start["body"]["status"]["privacyStatus"] == "private"
    fb_finish = next(r for r in out["plans"]["facebook"]["requests"] if r["step"] == "finish")
    assert fb_finish["body"]["video_state"] == "SCHEDULED" and fb_finish["body"]["scheduled_publish_time"] == str(int(t))
    assert out["plans"]["youtube"]["problems"] == []


def test_preview_youtube_private_is_app_mode(job):
    connect_all(dry_run=True)
    out = schedules.preview(JOB, 1, "hi", ["youtube"], at(), "", load_settings())
    it = out["schedule"]["items"]["youtube"]
    assert it["mode"] == "app" and "privacy is private" in it["mode_note"]
    body = next(r for r in out["plans"]["youtube"]["requests"] if r["step"] == "start_session")["body"]
    assert "publishAt" not in body["status"]


def test_prefer_native_off_and_facebook_too_soon():
    ss = load_settings().publish.schedule
    now = time.time()
    tgt = load_settings().publish.target("hi", "facebook")
    assert schedules.plan_mode("facebook", tgt, ss, now + 600, now)[0] == "app"
    mode, _, handoff = schedules.plan_mode("facebook", tgt, ss, now + 7200, now)
    assert mode == "native" and handoff == now + 7200 - 3600
    ss2 = ss.model_copy(update={"prefer_native": False})
    assert schedules.plan_mode("facebook", tgt, ss2, now + 7200, now)[0] == "app"


def test_create_requires_dry_run_off(job):
    connect_all(dry_run=True)
    with pytest.raises(PublishConflict) as e:
        schedules.create(JOB, 1, "hi", ALL, at(), "", load_settings())
    assert "dry-run" in e.value.message
    assert not schedules.store_path().exists()


def test_create_needs_connected_and_enabled(job):
    set_publish(dry_run=False)
    with pytest.raises(PublishConflict) as e:
        schedules.create(JOB, 1, "hi", ["youtube"], at(), "", load_settings())
    assert "not connected" in " ".join(e.value.errors["youtube"])


@pytest.mark.parametrize("delta,msg", [(-60, "at least 2 minutes"), (60, "at least 2 minutes"),
                                       (91 * 86400, "within 90 days")])
def test_create_time_bounds(job, delta, msg):
    connect_all()
    with pytest.raises(PublishInputError) as e:
        schedules.create(JOB, 1, "hi", ["youtube"], time.time() + delta, "", load_settings())
    assert msg in e.value.errors["publish_at"][0]


def test_create_unknown_clip(job):
    connect_all()
    with pytest.raises(PublishNotFound):
        schedules.create(JOB, 7, "hi", ["youtube"], at(), "", load_settings())
    with pytest.raises(PublishNotFound):
        schedules.create("nope", 1, "hi", ["youtube"], at(), "", load_settings())


def test_create_persists_and_refuses_duplicates(job):
    connect_all()
    s = schedules.create(JOB, 1, "hi", ALL, at(), "Asia/Dhaka", load_settings(), idempotency_key="k1")
    assert schedules.ID_RE.match(s["id"]) and s["state"] == "upcoming" and s["dry_run"] is False
    assert s["title"] == "वो चार मिनट जिसने पूरे अमेरिका को रुला दिया"
    assert {p: it["account"]["id"] for p, it in s["items"].items()} == {
        "youtube": CHANNEL["hi"][0], "facebook": PAGE["hi"][0], "instagram": IG["hi"][0]}
    raw = json.loads(schedules.store_path().read_text(encoding="utf-8"))
    assert raw["version"] == 1 and raw["schedules"][0]["id"] == s["id"]
    assert schedules.get(s["id"]) == s  # survives a re-read from disk
    with pytest.raises(PublishConflict) as e:
        schedules.create(JOB, 1, "hi", ["instagram"], at(5), "", load_settings())
    assert "already scheduled" in e.value.errors["instagram"][0]
    other = schedules.create(JOB, 1, "en", ["instagram"], at(5), "", load_settings())  # other lang: fine
    assert other["id"] != s["id"]


def test_create_refuses_already_published(job, net):
    connect_all()
    runner.run_publish(JOB, 1, "hi", platforms=["instagram"], force=False, settings=load_settings(), log=NOOP,
                       http=net.client(), sleep=NOOP)
    with pytest.raises(PublishConflict) as e:
        schedules.create(JOB, 1, "hi", ["instagram"], at(), "", load_settings())
    assert "already published" in e.value.errors["instagram"][0]


def test_daily_limit_refusal_message(job):
    connect_all()

    def fill(ps):
        ps.schedule.daily_limits.instagram = 1
    service._update_settings(fill)
    make_job("20261123-120000-other2")
    t = schedules.parse_time("2026-12-01T10:00", "Asia/Dhaka")[0]
    now = t - 3 * 3600
    schedules.create(JOB, 1, "hi", ["instagram"], t, "Asia/Dhaka", load_settings(), now=now)
    with pytest.raises(PublishConflict) as e:
        schedules.create("20261123-120000-other2", 1, "hi", ["instagram"], t + 3600, "Asia/Dhaka", load_settings(),
                         now=now)
    msg = e.value.errors["instagram"][0]
    assert msg.startswith("daily limit reached: 1 of 1 Instagram posts for hindi_ig on Tue 2026-12-01")
    assert "daily_limits.instagram" in msg
    # the next local day is fine; a published post (ledger) also counts
    schedules.create("20261123-120000-other2", 1, "hi", ["instagram"], t + 86400, "Asia/Dhaka", load_settings(),
                     now=now)
    records.append_ledger("instagram", IG["en"][0], "en", t)
    with pytest.raises(PublishConflict):
        schedules.create(JOB, 1, "en", ["instagram"], t + 60, "Asia/Dhaka", load_settings(), now=now)


def test_edit_moves_time_and_recomputes_modes(job):
    connect_all()
    s = schedules.create(JOB, 1, "hi", ["facebook"], at(3), "", load_settings())
    assert s["items"]["facebook"]["mode"] == "native"
    t2 = time.time() + 600
    e = schedules.edit(s["id"], t2, "Asia/Dhaka", load_settings())
    assert e["publish_at"] == t2 and e["timezone"] == "Asia/Dhaka"
    assert e["items"]["facebook"]["mode"] == "app"  # less than 15 minutes ahead now
    with pytest.raises(PublishInputError):
        schedules.edit(s["id"], time.time() - 5, "", load_settings())
    with pytest.raises(PublishNotFound):
        schedules.edit("s_ffffffffffff", at(), "", load_settings())
    with pytest.raises(PublishNotFound):
        schedules.edit("../../x", at(), "", load_settings())


def test_edit_refused_once_started(job):
    connect_all()
    s = schedules.create(JOB, 1, "hi", ["facebook", "instagram"], at(), "", load_settings())

    def fn(rows):
        rows[0]["items"]["facebook"]["state"] = "handed_off"
    schedules.update(fn)
    with pytest.raises(PublishConflict) as e:
        schedules.edit(s["id"], at(4), "", load_settings())
    assert "handed off" in e.value.errors["facebook"][0]


def test_edit_requires_dry_run_off(job):
    connect_all()
    s = schedules.create(JOB, 1, "hi", ["instagram"], at(), "", load_settings())
    set_publish(dry_run=True)
    with pytest.raises(PublishConflict):
        schedules.edit(s["id"], at(4), "", load_settings())


def test_cancel_partial_idempotent_and_refusals(job):
    connect_all()
    s = schedules.create(JOB, 1, "hi", ALL, at(), "", load_settings())
    out = schedules.cancel(s["id"], ["instagram"])
    assert out["items"]["instagram"]["state"] == "cancelled" and out["state"] == "upcoming"
    assert out["refused"] == {}

    def fn(rows):
        rows[0]["items"]["youtube"]["state"] = "handed_off"
    schedules.update(fn)
    out = schedules.cancel(s["id"])
    assert out["items"]["facebook"]["state"] == "cancelled"
    assert "already handed to YouTube" in out["refused"]["youtube"][0]
    with pytest.raises(PublishConflict):
        schedules.cancel(s["id"], ["youtube"])
    # cancelling cancelled items again is a no-op
    assert schedules.cancel(s["id"], ["facebook", "instagram"])["items"]["facebook"]["state"] == "cancelled"
    with pytest.raises(PublishNotFound):
        schedules.cancel("s_000000000000")


def test_cancel_queued_needs_the_worker(job):
    connect_all()
    s = schedules.create(JOB, 1, "hi", ["instagram"], at(), "", load_settings())

    def fn(rows):
        rows[0]["items"]["instagram"].update(state="queued", task_id="t" * 32)
    schedules.update(fn)
    with pytest.raises(PublishConflict):
        schedules.cancel(s["id"])  # CLI: no worker
    out = schedules.cancel(s["id"], cancel_task=lambda tid: "removed")
    assert out["items"]["instagram"]["state"] == "cancelled"


def test_list_filters_and_order(job):
    connect_all()
    a = schedules.create(JOB, 1, "hi", ["instagram"], at(5), "", load_settings())
    b = schedules.create(JOB, 1, "en", ["instagram"], at(2), "", load_settings())
    schedules.cancel(a["id"])
    assert [s["id"] for s in schedules.list_schedules("upcoming")] == [b["id"]]
    assert [s["id"] for s in schedules.list_schedules("cancelled")] == [a["id"]]
    assert [s["id"] for s in schedules.list_schedules("all")] == [b["id"], a["id"]]
    assert [s["id"] for s in schedules.list_schedules("all", lang="hi")] == [a["id"]]
    assert schedules.list_schedules("all", job_id="other") == []


def test_broken_store_is_kept_aside(job):
    schedules.store_path().write_text("{not json", encoding="utf-8")
    assert schedules.read_all() == []
    assert list(schedules.store_path().parent.glob("schedules.broken-*.json"))


# =================================================================================================
# scheduler (web.scheduler) with a real worker + the real runner against the fakes
# =================================================================================================
class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def worker(net):
    w = Worker(publish=functools.partial(runner.run_publish, http=net.client(), sleep=NOOP))
    w.start()
    yield w
    w.stop()


def item(sid, p):
    return schedules.get(sid)["items"][p]


def test_scheduler_app_mode_fires_at_time_and_finishes(job, net, worker):
    connect_all()
    t = at(2)
    s = schedules.create(JOB, 1, "hi", ["instagram"], t, "", load_settings())
    clock = Clock(t - 60)
    sch = Scheduler(worker, clock=clock)
    sch.tick()
    assert item(s["id"], "instagram")["state"] == "scheduled" and net.requests == []
    clock.t = t + 1
    sch.tick()
    it = item(s["id"], "instagram")
    assert it["state"] in ("queued", "running") and it["task_id"]
    assert worker.wait_idle(10)
    sch.tick()
    it = item(s["id"], "instagram")
    assert it["state"] == "done" and it["url"].startswith("https://www.instagram.com/reel/")
    st = records.read_status_raw(JOB, 1, "hi", "instagram")
    assert st["idempotency_key"] == f"schedule-{s['id']}-instagram"
    assert records.read_plan(JOB, 1, "hi", "instagram")["schedule"]["id"] == s["id"]
    assert schedules.get(s["id"])["state"] == "done"
    assert records.window_count("instagram", IG["hi"][0]) == 1


def test_scheduler_native_youtube_hands_off_then_done(job, net, worker):
    connect_all()
    set_privacy("hi", "public")
    t = at(3)
    s = schedules.create(JOB, 1, "hi", ["youtube"], t, "", load_settings())
    it = item(s["id"], "youtube")
    assert it["mode"] == "native" and it["handoff_at"] == t - 3600
    clock = Clock(t - 3600 - 5)
    sch = Scheduler(worker, clock=clock)
    sch.tick()
    assert item(s["id"], "youtube")["state"] == "scheduled"
    clock.t = t - 3600 + 1
    sch.tick()
    assert worker.wait_idle(10)
    sch.tick()
    it = item(s["id"], "youtube")
    assert it["state"] == "handed_off" and it["remote_id"] and "scheduled on YouTube" in it["message"]
    assert net.google.videos[0]["meta"]["status"]["publishAt"]
    assert schedules.get(s["id"])["state"] == "upcoming"
    with pytest.raises(PublishConflict):
        schedules.cancel(s["id"])
    clock.t = t + 1
    sch.tick()
    assert item(s["id"], "youtube")["state"] == "done"
    assert len(net.google.videos) == 1


def test_scheduler_missed_after_grace(job, net, worker):
    connect_all()
    t = at(2)
    s = schedules.create(JOB, 1, "hi", ["instagram", "facebook"], t, "Asia/Dhaka", load_settings())
    sch = Scheduler(worker, clock=Clock(t + 31 * 60))
    sch.tick()
    for p in ("instagram", "facebook"):
        it = item(s["id"], p)
        assert it["state"] == "missed", it
        assert "late by 31 min" in it["message"] and "grace 30 min" in it["message"]
        assert "nothing was posted" in it["message"]
    assert schedules.get(s["id"])["state"] == "missed"
    assert net.requests == []


def test_scheduler_runs_late_schedule_within_grace(job, net, worker):
    connect_all()
    t = at(3)
    s = schedules.create(JOB, 1, "hi", ["facebook"], t, "", load_settings())
    # the server was off through the hand-off and the time; now 10 min late: posted by the app right away
    sch = Scheduler(worker, clock=Clock(t + 600))
    sch.tick()
    it = item(s["id"], "facebook")
    assert it["mode"] == "app" and "too close" in it["mode_note"]
    assert worker.wait_idle(10)
    sch.tick()
    assert item(s["id"], "facebook")["state"] == "done"
    reel = next(iter(net.graph.reels.values()))
    assert reel["form"]["video_state"] == "PUBLISHED"


def test_scheduler_refuses_when_dry_run_is_on_at_fire_time(job, net, worker):
    connect_all()
    t = at(2)
    s = schedules.create(JOB, 1, "hi", ["instagram"], t, "", load_settings())
    set_publish(dry_run=True)
    Scheduler(worker, clock=Clock(t + 1)).tick()
    it = item(s["id"], "instagram")
    assert it["state"] == "failed" and "dry run was on" in it["message"]
    assert net.requests == []


def test_scheduler_rechecks_accounts_at_fire_time(job, net, worker):
    connect_all()
    t = at(2)
    s = schedules.create(JOB, 1, "hi", ["instagram"], t, "", load_settings())
    service.disconnect("hi", "instagram", revoke=False)
    Scheduler(worker, clock=Clock(t + 1)).tick()
    it = item(s["id"], "instagram")
    assert it["state"] == "failed" and "not connected" in it["message"]


def test_scheduler_daily_limit_at_fire_time(job, net, worker):
    connect_all()
    t = at(2)
    s = schedules.create(JOB, 1, "hi", ["instagram"], t, "", load_settings())

    def fill(ps):
        ps.schedule.daily_limits.instagram = 1
    service._update_settings(fill)
    records.append_ledger("instagram", IG["hi"][0], "hi", t - 60)
    Scheduler(worker, clock=Clock(t + 1)).tick()
    it = item(s["id"], "instagram")
    assert it["state"] == "failed" and "daily limit reached" in it["message"]
    assert net.requests == []


def test_scheduler_fires_once_even_if_ticked_twice_or_refired(job, net, worker):
    connect_all()
    t = at(2)
    s = schedules.create(JOB, 1, "hi", ["instagram"], t, "", load_settings())
    sch = Scheduler(worker, clock=Clock(t + 1))
    sch.tick()
    sch.tick()
    assert worker.wait_idle(10)
    sch.tick()
    assert item(s["id"], "instagram")["state"] == "done"
    assert len(net.graph.media) == 1

    def again(rows):  # simulate a double fire (e.g. a store restored from a backup)
        rows[0]["items"]["instagram"].update(state="scheduled", task_id=None)
    schedules.update(again)
    sch.tick()
    assert worker.wait_idle(10)
    it = item(s["id"], "instagram")
    assert it["state"] == "done" and "not posted again" in it["message"]
    assert len(net.graph.media) == 1 and len(net.graph.containers) == 1


def test_scheduler_refires_task_lost_in_restart(job, net, worker):
    connect_all()
    t = at(2)
    s = schedules.create(JOB, 1, "hi", ["instagram"], t, "", load_settings())

    def lost(rows):  # queued before a restart: the new worker has never heard of this task
        rows[0]["items"]["instagram"].update(state="queued", task_id="0" * 32, fired_at=t)
    schedules.update(lost)
    sch = Scheduler(worker, clock=Clock(t + 120))
    sch.tick()
    it = item(s["id"], "instagram")
    assert it["state"] == "queued" and it["task_id"] != "0" * 32
    assert worker.wait_idle(10)
    sch.tick()
    assert item(s["id"], "instagram")["state"] == "done"
    assert len(net.graph.media) == 1


def test_scheduler_lost_task_past_grace_fails(job, net, worker):
    connect_all()
    t = at(2)
    s = schedules.create(JOB, 1, "hi", ["instagram"], t, "", load_settings())

    def lost(rows):
        rows[0]["items"]["instagram"].update(state="running", task_id="0" * 32, fired_at=t)
    schedules.update(lost)
    Scheduler(worker, clock=Clock(t + 3 * 3600)).tick()
    it = item(s["id"], "instagram")
    assert it["state"] == "failed" and "restart" in it["message"]


def test_scheduler_failed_publish_is_reported(job, net, worker):
    connect_all()
    from publish_fakes import meta_error
    net.graph.inject["ig_container"] = [meta_error(190, "expired")]
    t = at(2)
    s = schedules.create(JOB, 1, "hi", ["instagram"], t, "", load_settings())
    sch = Scheduler(worker, clock=Clock(t + 1))
    sch.tick()
    assert worker.wait_idle(10)
    sch.tick()
    it = item(s["id"], "instagram")
    assert it["state"] == "failed" and "reconnect" in it["message"]
    assert schedules.get(s["id"])["state"] == "failed"


def test_scheduler_waits_while_clip_busy_then_fires(job, net):
    connect_all()
    t = at(2)
    s = schedules.create(JOB, 1, "hi", ["instagram"], t, "", load_settings())
    w = Worker(publish=functools.partial(runner.run_publish, http=net.client(), sleep=NOOP))  # not started
    w.submit_rerender(JOB, 1, "hi")
    sch = Scheduler(w, clock=Clock(t + 1))
    sch.tick()
    it = item(s["id"], "instagram")
    assert it["state"] == "scheduled" and it["message"].startswith("waiting: clip is being rendered")


def test_scheduler_thread_start_stop_no_store(job):
    w = Worker()
    sch = Scheduler(w, interval=0.05)
    w.scheduler = sch
    w.start()
    try:
        deadline = time.monotonic() + 5
        while sch.ticks < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert sch.alive and sch.ticks >= 2
        assert not schedules.store_path().exists()
    finally:
        w.stop()
    assert not sch.alive
