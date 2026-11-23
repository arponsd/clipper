"""Step 4 lead triage: regression tests for SEC-1..4 and F1..F6 (no network: MockTransport fakes only)."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from publish_fakes import FakeNet, LogSink, connect_all, google_error, make_job, meta_error, set_publish
from test_web_support import api  # noqa: F401 - fixture

from autoclipper.config import load_settings, settings_path
from autoclipper.publish import records, runner, secrets, service
from autoclipper.publish.base import classify_google
from autoclipper.publish.settings import PublishSettings

LANG = "hi"


@pytest.fixture
def net() -> FakeNet:
    return FakeNet()


@pytest.fixture
def job() -> str:
    job_id = make_job()
    connect_all()
    return job_id


def run(job_id, net, platforms, force=False, sleeps=None):
    return runner.run_publish(job_id, 1, LANG, platforms=list(platforms), force=force, settings=load_settings(),
                              log=LogSink(), http=net.client(),
                              sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
                              idempotency_key="key-lead")


def posts(net, suffix):
    return [r for r in net.graph.requests if r.method == "POST" and r.url.endswith(suffix)]


def resume(job_id, platform):
    return secrets.read_secret(records.resume_name(job_id, 1, LANG, platform))


# ---- F1 / SEC-1: Instagram media_publish answer lost ---------------------------------------------
def test_ig_lost_publish_response_recovers_post_without_duplicate(job, net):
    net.graph.lose["ig_publish"] = 1  # Instagram publishes, the answer never arrives
    res = run(job, net, ["instagram"])["instagram"]
    assert res["state"] == "published", res
    assert len(net.graph.containers) == 1 and len(net.graph.media) == 1
    assert res["remote_id"] in net.graph.media and res["url"].startswith("https://www.instagram.com/reel/")
    assert len(posts(net, "/media_publish")) == 1  # never auto-retried
    assert resume(job, "instagram") is None
    net.reset()
    again = run(job, net, ["instagram"])["instagram"]
    assert again["remote_id"] == res["remote_id"] and net.graph.requests == []


def test_ig_lost_publish_unidentified_refuses_until_force(job, net):
    net.graph.lose["ig_publish"] = 1
    net.graph.inject["ig_media_list"] = [meta_error(1, "unknown", status=500, transient=False)] * 10
    st = run(job, net, ["instagram"])["instagram"]
    assert st["state"] == "failed" and st["error"]["code"] == "maybe_published", st
    assert "already published" in st["message"] and "force" in st["message"]
    assert resume(job, "instagram")["sent"] is True  # kept: the next run must check first
    # a plain re-publish (fresh key, no force) passes check_live but posts nothing new
    to_run, _ = service.check_live(job, 1, LANG, ["instagram"], load_settings(), force=False)
    assert to_run == ["instagram"]
    net.reset()
    st2 = run(job, net, ["instagram"])["instagram"]
    assert st2["error"]["code"] == "maybe_published"
    assert posts(net, "/media") == [] and posts(net, "/media_publish") == []
    assert not any(r.url.startswith("https://rupload") for r in net.graph.requests)
    assert len(net.graph.media) == 1
    # explicit force = the user really wants a second post
    net.graph.inject.pop("ig_media_list", None)
    st3 = run(job, net, ["instagram"], force=True)["instagram"]
    assert st3["state"] == "published" and len(net.graph.media) == 2 and len(net.graph.containers) == 2


def test_ig_published_container_in_old_resume_record_is_never_reposted(job, net):
    """SEC-1 exact scenario: record {container_id, uploaded} (no media_id, no `sent`), container PUBLISHED,
    even when the record is older than the 23 h reuse window."""
    run(job, net, ["instagram"])
    (cid,) = list(net.graph.containers)
    name = records.resume_name(job, 1, LANG, "instagram")
    item, _ = service.load_item(job, 1, LANG)
    for age in (60.0, 30 * 3600.0):
        records.status_path(job, 1, LANG, "instagram").unlink()
        secrets.write_secret(name, {"container_id": cid, "sha256": item.sha256, "uploaded": True,
                                    "created": time.time() - age})
        net.reset()
        st = run(job, net, ["instagram"])["instagram"]
        assert posts(net, "/media") == [] and posts(net, "/media_publish") == [], age
        assert len(net.graph.media) == 1 and len(net.graph.containers) == 1
        # the live post is found by exact caption + posted after the container: recorded, not re-posted
        assert st["state"] == "published" and st["remote_id"] in net.graph.media
    # caption edited on Instagram since: cannot be matched safely -> refuses, never posts
    for m in net.graph.media.values():
        m["caption"] = "edited by hand"
    records.status_path(job, 1, LANG, "instagram").unlink()
    secrets.write_secret(name, {"container_id": cid, "sha256": item.sha256, "uploaded": True,
                                "created": time.time()})
    net.reset()
    st = run(job, net, ["instagram"])["instagram"]
    assert st["state"] == "failed" and st["error"]["code"] == "maybe_published"
    assert posts(net, "/media") == [] and posts(net, "/media_publish") == [] and len(net.graph.media) == 1


def test_ig_publish_rejected_but_container_unpublished_retries_same_container(job, net):
    net.graph.inject["ig_publish"] = [meta_error(9, "limit", subcode=2207042, status=403)]
    st = run(job, net, ["instagram"])["instagram"]
    assert st["state"] == "failed" and st["error"]["kind"] == "rate_limit"
    assert "may already be live" not in st["message"]  # verified FINISHED, not published
    net.reset()
    st2 = run(job, net, ["instagram"])["instagram"]
    assert st2["state"] == "published" and len(net.graph.media) == 1


def test_ig_publish_network_error_and_status_unknown_keeps_record(job, net):
    net.graph.inject["ig_publish"] = [httpx.ConnectError("down")]
    st = run(job, net, ["instagram"])["instagram"]
    assert st["state"] == "failed" and st["error"]["kind"] == "network"
    assert len(posts(net, "/media_publish")) == 1  # one attempt, no blind retry
    assert resume(job, "instagram")["uploaded"] is True  # transient: kept for the container reuse
    net.reset()
    st2 = run(job, net, ["instagram"])["instagram"]
    assert st2["state"] == "published" and len(net.graph.containers) == 1 and len(net.graph.media) == 1


# ---- F1 / SEC-2: Facebook finish answer lost, status poll failing after finish ------------------
def test_fb_lost_finish_response_continues_without_duplicate(job, net):
    net.graph.lose["fb_finish"] = 1
    st = run(job, net, ["facebook"])["facebook"]
    assert st["state"] == "published", st
    assert len(net.graph.reels) == 1 and len(posts(net, "/video_reels")) == 2  # start + one finish
    assert resume(job, "facebook") is None


def test_fb_status_poll_error_after_finish_keeps_record_and_does_not_repost(job, net):
    net.graph.inject["fb_status"] = [meta_error(4, "Application request limit reached")]
    st = run(job, net, ["facebook"])["facebook"]
    assert st["state"] == "failed" and st["error"]["kind"] == "rate_limit"
    assert "may already be live" in st["message"]
    rec = resume(job, "facebook")
    assert rec and rec["sent"] is True and rec["finished"] is True
    net.reset()
    st2 = run(job, net, ["facebook"])["facebook"]
    assert st2["state"] == "published" and len(net.graph.reels) == 1
    assert not any(r.form.get("upload_phase") == "start" for r in posts(net, "/video_reels"))


def test_fb_finish_lost_and_status_unreachable_next_run_checks_first(job, net):
    net.graph.lose["fb_finish"] = 1
    net.graph.inject["fb_status"] = [meta_error(190, "expired")]
    st = run(job, net, ["facebook"])["facebook"]
    assert st["state"] == "failed" and resume(job, "facebook")["sent"] is True
    net.reset()
    st2 = run(job, net, ["facebook"])["facebook"]
    assert st2["state"] == "published" and len(net.graph.reels) == 1
    assert posts(net, "/video_reels") == []  # publishing had started: no start, no second finish


def test_fb_finish_rejected_verified_unpublished_resends_on_same_video(job, net):
    net.graph.inject["fb_finish"] = [meta_error(100, "Invalid parameter")]
    st = run(job, net, ["facebook"])["facebook"]
    assert st["state"] == "failed" and st["error"]["kind"] == "invalid"
    assert len(net.graph.reels) == 1


# ---- F3: narration edited but not re-rendered -----------------------------------------------------
def test_voice_stale_blocks_publish(job, net):
    d = service.clip_dir(job, 1, LANG)
    script = json.loads((d / "script.json").read_text(encoding="utf-8"))
    from autoclipper.stages.voice import narration_hash
    (d / "voice.json").write_text(json.dumps({"narration_hash": narration_hash(script["narration"])}),
                                  encoding="utf-8")
    assert service.voice_stale(job, 1, LANG) is False
    script["narration"] = script["narration"] + " नया"
    (d / "script.json").write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    assert service.voice_stale(job, 1, LANG) is True
    with pytest.raises(service.PublishConflict) as ei:
        service.check_live(job, 1, LANG, ["youtube", "facebook", "instagram"], load_settings(), force=False)
    for p in ("youtube", "facebook", "instagram"):
        assert service.VOICE_STALE_PROBLEM in ei.value.errors[p]
    plans = service.dry_run(job, 1, LANG, ["youtube"], load_settings())["plans"]
    assert service.VOICE_STALE_PROBLEM in plans["youtube"]["problems"]
    res = run(job, net, ["facebook"])
    assert res["facebook"]["state"] == "failed" and net.requests == []


# ---- F4: Facebook AI disclosure -------------------------------------------------------------------
def test_fb_finish_sends_is_ai_generated(job, net):
    run(job, net, ["facebook"])
    fin = [r for r in posts(net, "/video_reels") if r.form.get("upload_phase") == "finish"]
    assert fin[0].form["is_ai_generated"] == "true"
    plan = service.dry_run(job, 1, LANG, ["facebook"], load_settings())["plans"]["facebook"]
    assert [r for r in plan["requests"] if r["step"] == "finish"][0]["body"]["is_ai_generated"] == "true"

    def off(ps):
        ps.hi.facebook.is_ai_generated = False
    service._update_settings(off)
    net2 = FakeNet()
    run(job, net2, ["facebook"], force=True)
    fin = [r for r in posts(net2, "/video_reels") if r.form.get("upload_phase") == "finish"]
    assert fin[0].form["is_ai_generated"] == "false"
    assert PublishSettings().hi.facebook.is_ai_generated is True


# ---- F5: channel upload limit is not the API quota ------------------------------------------------
def test_upload_limit_exceeded_message():
    e = classify_google(google_error(400, "uploadLimitExceeded"))
    assert e.kind == "quota" and "upload limit" in e.message and "midnight" not in e.message
    q = classify_google(google_error(403, "quotaExceeded"))
    assert "midnight Pacific" in q.message


# ---- F6: Instagram polling backs off to once a minute, 5 minutes by default ----------------------
def test_ig_poll_backoff_and_defaults(job, net):
    ps = PublishSettings()
    assert ps.poll_timeout_seconds == 300.0 and ps.poll_interval_seconds == 10.0
    net.graph.ig_status_seq = ["IN_PROGRESS"]
    set_publish(poll_interval_seconds=10.0, poll_timeout_seconds=300.0)
    sleeps: list[float] = []
    st = run(job, net, ["instagram"], sleeps=sleeps)["instagram"]
    assert st["error"]["kind"] == "processing"
    assert sleeps == [10.0, 20.0, 40.0, 60.0, 60.0, 60.0, 50.0] and sum(sleeps) == 300.0
    status_gets = [r for r in net.graph.requests if "fields=status_code" in r.url]
    assert len(status_gets) == 8  # was 31 at a flat 10 s


# ---- SEC-3: Windows owner-only ACL on data/secrets ------------------------------------------------
def test_windows_secrets_dir_gets_owner_only_acl(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(secrets, "_IS_WINDOWS", True)
    monkeypatch.setattr(secrets.subprocess, "run", fake_run)
    monkeypatch.setattr(secrets, "_acl_done", set())
    monkeypatch.setenv("USERNAME", "alice")
    monkeypatch.setenv("USERDOMAIN", "PC1")
    d = secrets.secrets_dir()
    secrets.secrets_dir()
    assert calls == [["icacls", str(d), "/inheritance:r", "/grant:r", "PC1\\alice:(OI)(CI)F"]]  # once


def test_windows_acl_failure_is_logged_and_retried(monkeypatch, caplog):
    def fail(args, **kw):
        return subprocess.CompletedProcess(args, 5, "", "Access is denied.")
    monkeypatch.setattr(secrets, "_IS_WINDOWS", True)
    monkeypatch.setattr(secrets.subprocess, "run", fail)
    monkeypatch.setattr(secrets, "_acl_done", set())
    monkeypatch.setenv("USERNAME", "alice")
    with caplog.at_level("WARNING", logger="autoclipper.publish.secrets"):
        secrets.secrets_dir()
    assert "owner-only ACL" in caplog.text and secrets._acl_done == set()


def test_posix_secrets_dir_is_0700():
    d = secrets.secrets_dir()
    if not secrets._IS_WINDOWS:
        assert (d.stat().st_mode & 0o777) == 0o700


# ---- SEC-4 / F2: generic settings save cannot revert the publish section -------------------------
def test_put_settings_without_publish_keeps_stored_publish(api):  # noqa: F811
    client, _, _ = api
    stale = client.get("/api/settings").json()
    assert stale["publish"]["dry_run"] is True
    connect_all(dry_run=False)  # Accounts page: dry run off, mapping filled
    set_publish(dry_run=True)  # ... and back ON in another tab
    stored = load_settings().publish.model_dump()
    body = {k: v for k, v in stale.items() if k != "publish"}  # what app.js now sends
    body["clips"]["max_seconds"] = stale["clips"]["max_seconds"] - 1
    r = client.put("/api/settings", json=body)
    assert r.status_code == 200, r.text
    on_disk = json.loads(settings_path().read_text(encoding="utf-8"))
    assert on_disk["publish"] == stored and on_disk["publish"]["dry_run"] is True
    assert on_disk["clips"]["max_seconds"] == body["clips"]["max_seconds"]
    assert r.json()["publish"] == stored


def test_settings_page_does_not_send_publish():
    js = (Path(__file__).resolve().parents[1] / "autoclipper/web/static/app.js").read_text(encoding="utf-8")
    i = js.index("const out = deepClone(loaded) || {};")
    assert "delete out.publish;" in js[i:i + 400]
