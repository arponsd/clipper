"""Step 4 core: platform clients through runner.run_publish with mocked Google/Graph (no network)."""
from __future__ import annotations

import json
import urllib.parse

import httpx
import pytest
from publish_fakes import (ACCESS_TOKEN, ALL_SECRETS, CHANNEL, CLIENT_ID, IG, PAGE, PAGE_TOKEN, REFRESH_TOKEN,
                           FakeNet, LogSink, connect_all, fake_client, google_error, grep_secrets, make_job,
                           meta_error, set_publish, stub_probe, tree_text)

from autoclipper.config import load_settings
from autoclipper.publish import records, runner, secrets, service
from autoclipper.web.worker import TaskCancelled

V = "v27.0"
MiB = 1024 * 1024


@pytest.fixture
def net() -> FakeNet:
    return FakeNet()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def job() -> str:
    job_id = make_job()
    connect_all()
    return job_id


def run(job_id, net, sleeps=None, platforms=("youtube", "facebook", "instagram"), lang="hi", force=False,
        log=None, http=None):
    sink = log or LogSink()
    res = runner.run_publish(job_id, 1, lang, platforms=list(platforms), force=force, settings=load_settings(),
                             log=sink, http=http or net.client(),
                             sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
                             idempotency_key="key-1")
    return res, sink


def assert_no_token_in_urls(net):
    for r in net.requests:
        for s in ALL_SECRETS:
            assert s not in r.url, r.url
        assert "access_token" not in r.url


# ---- plans (dry run) ---------------------------------------------------------------------------
PLAN_KEYS = {"platform", "lang", "job_id", "clip", "dry_run", "created_at", "api_version", "account", "video",
             "metadata", "requests", "limits", "problems", "warnings"}


def test_dry_run_plans_exact_shape_no_network_no_secrets(job, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network in dry run")
    monkeypatch.setattr(httpx.Client, "send", boom)
    out = service.dry_run(job, 1, "hi", ["youtube", "facebook", "instagram"], load_settings())
    assert out["dry_run"] is True and set(out["plans"]) == {"youtube", "facebook", "instagram"}
    for p, plan in out["plans"].items():
        assert set(plan) == PLAN_KEYS
        assert plan["dry_run"] is True and plan["platform"] == p and plan["lang"] == "hi" and plan["clip"] == 1
        assert plan["api_version"] == ("v3" if p == "youtube" else V)
        assert set(plan["video"]) == {"path", "size", "duration", "width", "height", "sha256"}
        assert plan["video"]["path"] == "clips/01/hi/final.mp4" and len(plan["video"]["sha256"]) == 64
        assert (plan["video"]["width"], plan["video"]["height"]) == (1080, 1920)
        assert plan["problems"] == [], plan["problems"]
        assert isinstance(plan["warnings"], list) and plan["requests"]
        for r in plan["requests"]:
            assert set(r) == {"step", "method", "url", "headers", "body", "note"}
            auth = r["headers"].get("Authorization")
            if auth is not None:
                assert auth in ("Bearer ***", "OAuth ***")
        disk = json.loads(records.plan_path(job, 1, "hi", p).read_text(encoding="utf-8"))
        assert disk == json.loads(json.dumps(plan, ensure_ascii=False))
    yt = out["plans"]["youtube"]
    assert yt["account"] == {"id": CHANNEL["hi"][0], "name": CHANNEL["hi"][1]}
    assert yt["requests"][1]["url"] == ("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable"
                                        "&part=snippet,status&notifySubscribers=false")
    assert yt["requests"][1]["headers"]["X-Upload-Content-Length"] == str(yt["video"]["size"])
    assert yt["metadata"]["status"]["privacyStatus"] == "private"
    assert "privacy is private: only you will see it" in yt["warnings"]
    fb = out["plans"]["facebook"]
    assert fb["requests"][0]["url"] == f"https://graph.facebook.com/{V}/{PAGE['hi'][0]}/video_reels"
    assert fb["requests"][1]["url"] == f"https://rupload.facebook.com/video-upload/{V}/{{video_id}}"
    ig = out["plans"]["instagram"]
    assert ig["requests"][1]["url"] == f"https://graph.facebook.com/{V}/{IG['hi'][0]}/media"
    assert ig["requests"][1]["body"]["media_type"] == "REELS"
    assert ig["requests"][2]["url"] == f"https://rupload.facebook.com/ig-api-upload/{V}/{{container_id}}"
    assert grep_secrets(tree_text(), json.dumps(out, ensure_ascii=False)) == []
    assert CLIENT_ID not in json.dumps(out)


def test_dry_run_unconnected_lists_problems(monkeypatch):
    job_id = make_job()
    out = service.dry_run(job_id, 1, "en", ["youtube"], load_settings())
    probs = out["plans"]["youtube"]["problems"]
    assert "not connected" in probs and any("disabled" in p for p in probs)
    assert out["plans"]["youtube"]["account"] is None


def test_dry_run_limits_landscape_and_long(monkeypatch):
    job_id = make_job(final_bytes=1000)
    connect_all()
    stub_probe(monkeypatch, duration=200.0, width=1920, height=1080)
    plans = service.dry_run(job_id, 1, "en", ["youtube", "facebook", "instagram"], load_settings())["plans"]
    assert any("180 s" in p for p in plans["youtube"]["problems"])
    assert any("landscape" in p for p in plans["youtube"]["problems"])
    assert any("90 s" in p for p in plans["facebook"]["problems"])
    assert plans["instagram"]["problems"] == []  # 200 s, width 1920 is fine for IG
    stub_probe(monkeypatch, duration=2.0, width=1080, height=1920)
    plans = service.dry_run(job_id, 1, "en", ["facebook", "instagram"], load_settings())["plans"]
    assert any("3-90 s" in p for p in plans["facebook"]["problems"])
    assert any("3 s - 15 min" in p for p in plans["instagram"]["problems"])


# ---- YouTube -----------------------------------------------------------------------------------
def test_youtube_happy_path(job, net, sleeps):
    res, sink = run(job, net, sleeps, platforms=["youtube"])
    st = res["youtube"]
    assert st["state"] == "published" and st["remote_id"] == "VID00000001"
    assert st["url"] == "https://www.youtube.com/shorts/VID00000001"
    assert st["account"] == {"id": CHANNEL["hi"][0], "name": CHANNEL["hi"][1]}
    assert st["idempotency_key"] == "key-1" and st["error"] is None and st["progress"] == 1.0
    reqs = net.google.requests
    assert reqs[0].method == "POST" and reqs[0].url == "https://oauth2.googleapis.com/token"
    assert reqs[0].form == {"client_id": CLIENT_ID, "client_secret": secrets.read_secret("google_client")["client_secret"],
                            "grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN["hi"]}
    start = reqs[1]
    assert start.url == ("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable"
                         "&part=snippet,status&notifySubscribers=false")
    assert start.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert start.headers["x-upload-content-type"] == "video/mp4"
    size = records.status_path(job, 1, "hi", "youtube")  # noqa: F841
    body = start.json
    assert body["status"]["privacyStatus"] == "private"
    assert body["snippet"]["defaultLanguage"] == "hi" and body["snippet"]["description"].endswith("#Shorts")
    assert [r.method for r in reqs[2:]] == ["PUT"]
    video = net.google.videos[0]
    final = (records.publish_dir(job, 1, "hi").parent / "final.mp4").read_bytes()
    assert video["data"] == final
    assert sleeps == []
    assert_no_token_in_urls(net)
    # side effects: status file, plan rewritten live, ledger, resume removed
    disk = json.loads(records.status_path(job, 1, "hi", "youtube").read_text(encoding="utf-8"))
    assert disk["state"] == "published" and "resume" not in disk
    assert json.loads(records.plan_path(job, 1, "hi", "youtube").read_text(encoding="utf-8"))["dry_run"] is False
    assert records.window_count("youtube", CHANNEL["hi"][0]) == 1
    assert secrets.read_secret(records.resume_name(job, 1, "hi", "youtube")) is None
    assert grep_secrets(tree_text(), sink.text) == []


def test_youtube_chunks_are_256k_multiples(net, sleeps, monkeypatch):
    size = int(2.5 * MiB) + 7
    job_id = make_job(final_bytes=size)
    stub_probe(monkeypatch)
    connect_all()  # chunk_mb = 1
    res, _ = run(job_id, net, sleeps, platforms=["youtube"])
    assert res["youtube"]["state"] == "published"
    puts = [r for r in net.google.requests if r.method == "PUT"]
    assert len(puts) == 3
    sizes = [len(r.body) for r in puts]
    assert sizes == [MiB, MiB, size - 2 * MiB]
    assert all(s % (256 * 1024) == 0 for s in sizes[:-1])
    assert [r.headers["content-range"] for r in puts] == [
        f"bytes 0-{MiB - 1}/{size}", f"bytes {MiB}-{2 * MiB - 1}/{size}", f"bytes {2 * MiB}-{size - 1}/{size}"]
    final = (records.publish_dir(job_id, 1, "hi").parent / "final.mp4").read_bytes()
    assert net.google.videos[0]["data"] == final


def test_youtube_transient_5xx_then_success(net, sleeps, monkeypatch):
    job_id = make_job(final_bytes=2 * MiB + 1)
    stub_probe(monkeypatch)
    connect_all()
    net.google.inject["chunk"] = [httpx.Response(503), httpx.Response(502)]
    res, _ = run(job_id, net, sleeps, platforms=["youtube"])
    assert res["youtube"]["state"] == "published"
    assert sleeps == [1.0, 2.0]
    assert len(net.google.videos) == 1 and len(net.google.sessions) == 1
    probes = [r for r in net.google.requests if r.method == "PUT" and r.headers["content-range"].startswith("bytes */")]
    assert len(probes) == 2


def test_youtube_transient_exhausts_retries(job, net, sleeps):
    set_publish(max_retries=2)
    net.google.inject["chunk"] = [httpx.Response(503)] * 5
    res, _ = run(job, net, sleeps, platforms=["youtube"])
    st = res["youtube"]
    assert st["state"] == "failed" and st["error"]["kind"] == "transient"
    assert "503" in st["error"]["message"]
    # transient: the resume record is kept for the next attempt
    assert secrets.read_secret(records.resume_name(job, 1, "hi", "youtube"))["session_uri"]


def test_youtube_network_error_then_success(job, net, sleeps):
    net.google.inject["chunk"] = [httpx.ConnectError("boom")]
    res, _ = run(job, net, sleeps, platforms=["youtube"])
    assert res["youtube"]["state"] == "published" and sleeps == [1.0]


def test_youtube_session_404_starts_new_session(net, sleeps, monkeypatch):
    job_id = make_job(final_bytes=2 * MiB + 5)
    stub_probe(monkeypatch)
    connect_all()
    calls = {"n": 0}
    real = net.google

    def expire_after_first(request):
        if request.method == "PUT":
            calls["n"] += 1
        if request.method == "PUT" and calls["n"] == 1:
            resp = real(request)  # first chunk accepted ...
            for s in real.sessions.values():
                s["expired"] = True  # ... then the session expires
            return resp
        return real(request)

    http = fake_client(lambda r: expire_after_first(r) if r.url.host.endswith("googleapis.com") else net(r))
    res, _ = run(job_id, net, sleeps, platforms=["youtube"], http=http)
    assert res["youtube"]["state"] == "published"
    assert len(real.sessions) == 2 and len(real.videos) == 1
    final = (records.publish_dir(job_id, 1, "hi").parent / "final.mp4").read_bytes()
    assert real.videos[0]["data"] == final


def test_youtube_resume_after_interrupt_continues_at_offset(net, monkeypatch):
    size = 3 * MiB + 11
    job_id = make_job(final_bytes=size)
    stub_probe(monkeypatch)
    connect_all()
    seen = {"n": 0}

    def log(line):
        if "YouTube" in line and "uploading" in line:
            pass
        if line == "" or "uploading" in line:
            seen["n"] += 1
            if seen["n"] == 3:  # before the 3rd chunk
                raise TaskCancelled()

    with pytest.raises(TaskCancelled):
        run(job_id, net, platforms=["youtube"], log=log)
    raw = records.read_status_raw(job_id, 1, "hi", "youtube")
    assert raw["state"] == "interrupted" and "resume" not in raw
    view = service.publish_view(job_id, 1, "hi", load_settings(), active=None)
    assert view["platforms"]["youtube"]["status"]["state"] == "interrupted"
    sent_before = sum(len(r.body) for r in net.google.requests if r.method == "PUT")
    assert sent_before == 2 * MiB
    net.reset()
    res, _ = run(job_id, net, platforms=["youtube"])
    assert res["youtube"]["state"] == "published" and res["youtube"]["attempts"] == 2
    puts = [r for r in net.google.requests if r.method == "PUT"]
    assert puts[0].headers["content-range"] == f"bytes */{size}"  # probe first
    assert sum(len(r.body) for r in puts) == size - 2 * MiB  # only the rest
    assert not any(r.method == "POST" and "upload" in r.url for r in net.google.requests)  # same session
    assert len(net.google.videos) == 1
    final = (records.publish_dir(job_id, 1, "hi").parent / "final.mp4").read_bytes()
    assert net.google.videos[0]["data"] == final


def test_youtube_crash_after_upload_recovers_id_without_duplicate(job, net):
    set_publish(max_retries=0)
    real = net.google

    def lose_answer(request):
        resp = real(request)
        if request.method == "PUT" and resp.status_code == 201:
            raise httpx.ReadTimeout("answer lost")
        return resp

    http = fake_client(lambda r: lose_answer(r) if r.url.host.endswith("googleapis.com") else net(r))
    res, _ = run(job, net, platforms=["youtube"], http=http)
    assert res["youtube"]["state"] == "failed" and res["youtube"]["error"]["kind"] == "network"
    assert len(real.videos) == 1
    res, _ = run(job, net, platforms=["youtube"])
    assert res["youtube"]["state"] == "published" and res["youtube"]["remote_id"] == real.videos[0]["id"]
    assert len(real.videos) == 1  # no duplicate upload


@pytest.mark.parametrize("resp,kind,text", [
    (google_error(403, "quotaExceeded"), "quota", "quota used up; it resets at midnight Pacific time"),
    (google_error(400, "uploadLimitExceeded"), "quota", "quota"),
    (google_error(403, "rateLimitExceeded"), "rate_limit", "rate limiting"),
    (google_error(429, "userRateLimitExceeded"), "rate_limit", "rate limiting"),
    (google_error(400, "invalidTitle", "bad title"), "invalid", "invalidTitle"),
    (google_error(403, "forbiddenPrivacySetting"), "invalid", "privacy"),
])
def test_youtube_start_errors(job, net, sleeps, resp, kind, text):
    net.google.inject["start"] = [resp]
    res, sink = run(job, net, sleeps, platforms=["youtube"])
    st = res["youtube"]
    assert st["state"] == "failed" and st["error"]["kind"] == kind
    assert text in st["error"]["message"]
    assert sleeps == []  # permanent errors are not retried
    assert st["error"]["message"] in sink.text


def test_youtube_refresh_invalid_grant_is_auth(job, net):
    net.google.inject["token_refresh_token"] = [httpx.Response(400, json={"error": "invalid_grant",
                                                                          "error_description": "expired"})]
    res, _ = run(job, net, platforms=["youtube"])
    st = res["youtube"]
    assert st["error"]["kind"] == "auth" and "reconnect on the Accounts page" in st["error"]["message"]
    assert len(net.google.requests) == 1


def test_youtube_retry_after_honoured(job, net, sleeps):
    set_publish(max_retries=1)
    net.google.inject["start"] = [httpx.Response(503, headers={"Retry-After": "30"})]
    res, _ = run(job, net, sleeps, platforms=["youtube"])
    assert res["youtube"]["state"] == "published" and sleeps == [30.0]


# ---- Facebook ----------------------------------------------------------------------------------
def test_facebook_happy_path(job, net, sleeps):
    res, sink = run(job, net, sleeps, platforms=["facebook"])
    st = res["facebook"]
    vid = st["remote_id"]
    assert st["state"] == "published" and st["url"] == f"https://www.facebook.com/reel/{vid}"
    page = PAGE["hi"][0]
    reqs = net.graph.requests
    assert reqs[0].url == f"https://graph.facebook.com/{V}/{page}/video_reels"
    assert reqs[0].form == {"upload_phase": "start"}
    assert reqs[0].headers["authorization"] == f"Bearer {PAGE_TOKEN['hi']}"
    up = reqs[1]
    assert up.url == f"https://rupload.facebook.com/video-upload/{V}/{vid}"
    assert up.headers["authorization"] == f"OAuth {PAGE_TOKEN['hi']}"
    assert up.headers["offset"] == "0" and up.headers["file_size"] == str(len(up.body))
    fin = reqs[2]
    assert fin.url == f"https://graph.facebook.com/{V}/{page}/video_reels"
    assert fin.form["upload_phase"] == "finish" and fin.form["video_state"] == "PUBLISHED"
    assert fin.form["video_id"] == vid and fin.form["title"].startswith("वो चार")
    assert fin.form["description"].endswith("#भाषण")
    assert all(r.url.startswith(f"https://graph.facebook.com/{V}/{vid}?fields=status") for r in reqs[3:])
    assert sleeps == [1.0]  # one "processing" poll
    assert records.window_count("facebook", page) == 1
    assert_no_token_in_urls(net)
    assert grep_secrets(tree_text(), sink.text) == []


def test_facebook_upload_resumes_at_offset(job, net, sleeps):
    final = (records.publish_dir(job, 1, "hi").parent / "final.mp4").read_bytes()
    half = len(final) // 2

    def partial(request):
        vid = request.url.path.rsplit("/", 1)[-1]
        r = net.graph.reels[vid]
        r["size"] = len(final)
        r["data"] = bytearray(final[:half])
        return httpx.Response(503)

    net.graph.inject["fb_upload"] = [partial]
    res, _ = run(job, net, sleeps, platforms=["facebook"])
    assert res["facebook"]["state"] == "published"
    ups = [r for r in net.graph.requests if r.url.startswith("https://rupload")]
    assert [u.headers["offset"] for u in ups] == ["0", str(half)]
    assert ups[1].body == final[half:]
    vid = res["facebook"]["remote_id"]
    assert bytes(net.graph.reels[vid]["data"]) == final


@pytest.mark.parametrize("step,resp,kind", [
    ("fb_start", meta_error(190, "token expired"), "auth"),
    ("fb_start", meta_error(4, "Application request limit reached"), "rate_limit"),
    ("fb_start", meta_error(200, "Permissions error", status=403), "auth"),
    ("fb_finish", meta_error(100, "Invalid parameter"), "invalid"),
])
def test_facebook_errors(job, net, sleeps, step, resp, kind):
    net.graph.inject[step] = [resp]
    res, _ = run(job, net, sleeps, platforms=["facebook"])
    st = res["facebook"]
    assert st["state"] == "failed" and st["error"]["kind"] == kind, st
    assert sleeps == []
    if kind == "auth" and "190" in (st["error"]["code"] or ""):
        assert "reconnect" in st["error"]["message"]


def test_facebook_transient_error_retried(job, net, sleeps):
    net.graph.inject["fb_start"] = [meta_error(2, "Service temporarily unavailable", transient=True, status=500)]
    res, _ = run(job, net, sleeps, platforms=["facebook"])
    assert res["facebook"]["state"] == "published" and sleeps[0] == 1.0


def test_facebook_processing_error_and_timeout(job, net, sleeps):
    net.graph.fb_status_seq = ["error"]
    res, _ = run(job, net, sleeps, platforms=["facebook"])
    assert res["facebook"]["error"]["kind"] == "processing"
    net.graph.fb_status_seq = ["processing"]
    res, _ = run(job, net, sleeps, platforms=["facebook"])
    st = res["facebook"]
    assert st["state"] == "failed" and st["error"]["kind"] == "processing"
    assert "still processing" in st["error"]["message"]


def test_facebook_local_rate_guard(job, net):
    page = PAGE["hi"][0]
    for _ in range(30):
        records.append_ledger("facebook", page, "hi")
    with pytest.raises(service.PublishConflict) as ei:
        service.check_live(job, 1, "hi", ["facebook"], load_settings(), force=False)
    assert any("30 API-published reels in 24 h" in p for p in ei.value.errors["facebook"])
    res, _ = run(job, net, platforms=["facebook"])
    assert res["facebook"]["state"] == "failed" and net.graph.requests == []


# ---- Instagram ---------------------------------------------------------------------------------
def test_instagram_happy_path(job, net, sleeps):
    res, sink = run(job, net, sleeps, platforms=["instagram"])
    st = res["instagram"]
    assert st["state"] == "published" and st["url"].startswith("https://www.instagram.com/reel/")
    ig = IG["hi"][0]
    reqs = net.graph.requests
    assert reqs[0].url == f"https://graph.facebook.com/{V}/{ig}/content_publishing_limit?fields=quota_usage%2Cconfig"
    assert reqs[1].url == f"https://graph.facebook.com/{V}/{ig}/media"
    form = reqs[1].form
    assert form["media_type"] == "REELS" and form["upload_type"] == "resumable" and form["share_to_feed"] == "true"
    assert form["caption"].startswith("वो चार मिनट") and form["caption"].endswith("#भाषण")
    cid = json.loads(json.dumps(net.graph.containers and list(net.graph.containers)[0]))
    assert reqs[2].url == f"https://rupload.facebook.com/ig-api-upload/{V}/{cid}"
    assert reqs[2].headers["authorization"] == f"OAuth {PAGE_TOKEN['hi']}"
    assert reqs[2].headers["offset"] == "0" and reqs[2].headers["file_size"] == str(len(reqs[2].body))
    polls = [r for r in reqs if r.url.startswith(f"https://graph.facebook.com/{V}/{cid}?")]
    assert len(polls) == 2 and sleeps == [1.0]
    pub = [r for r in reqs if r.url.endswith("/media_publish")][0]
    assert pub.form == {"creation_id": cid}
    assert reqs[-1].url.endswith("?fields=permalink")
    assert st["remote_id"] in net.graph.media
    assert_no_token_in_urls(net)
    assert grep_secrets(tree_text(), sink.text) == []


def test_instagram_uses_separate_ig_token(net):
    from publish_fakes import IG_TOKEN, connect_meta
    job_id = make_job()
    connect_all()
    connect_meta("hi", separate_ig_token=True)
    res, _ = run(job_id, net, platforms=["instagram"])
    assert res["instagram"]["state"] == "published"
    assert all(IG_TOKEN["hi"] in r.headers["authorization"] for r in net.graph.requests)


def test_instagram_quota_full_sends_nothing_else(job, net):
    net.graph.ig_quota_usage = 100
    res, _ = run(job, net, platforms=["instagram"])
    st = res["instagram"]
    assert st["error"]["kind"] == "rate_limit" and "100 of 100" in st["error"]["message"]
    assert len(net.graph.requests) == 1 and net.graph.containers == {}


@pytest.mark.parametrize("step,resp,kind,needle", [
    ("ig_publish", meta_error(9, "limit", subcode=2207042, status=403), "rate_limit", "limit of API-published posts"),
    ("ig_container", meta_error(352, "unsupported", subcode=2207026), "unsupported_video", "format"),
    ("ig_container", meta_error(190, "expired"), "auth", "reconnect"),
    ("ig_container", meta_error(32, "Page request limit reached"), "rate_limit", "rate limiting"),
])
def test_instagram_errors(job, net, sleeps, step, resp, kind, needle):
    net.graph.inject[step] = [resp]
    res, _ = run(job, net, sleeps, platforms=["instagram"])
    st = res["instagram"]
    assert st["state"] == "failed" and st["error"]["kind"] == kind and needle in st["error"]["message"]


def test_instagram_container_error_and_timeout(job, net, sleeps):
    net.graph.ig_status_seq = ["IN_PROGRESS", "ERROR"]
    res, _ = run(job, net, sleeps, platforms=["instagram"])
    assert res["instagram"]["error"]["kind"] == "processing" and "ERROR" in res["instagram"]["error"]["message"]
    net.graph.ig_status_seq = ["IN_PROGRESS"]
    sleeps.clear()
    res, _ = run(job, net, sleeps, platforms=["instagram"])
    st = res["instagram"]
    assert st["error"]["kind"] == "processing" and "after 60 s" in st["error"]["message"]
    assert sum(sleeps) == 60.0
    assert not any(r.url.endswith("/media_publish") for r in net.graph.requests)


def test_instagram_resume_reuses_finished_container(job, net):
    seen = {"n": 0}

    def log(line):
        if "Instagram is processing" in line:
            raise TaskCancelled()

    with pytest.raises(TaskCancelled):
        run(job, net, platforms=["instagram"], log=log)
    assert len(net.graph.containers) == 1
    net.reset()
    res, _ = run(job, net, platforms=["instagram"])
    assert res["instagram"]["state"] == "published"
    assert len(net.graph.containers) == 1  # reused, not created again
    assert not any(r.url.startswith("https://rupload") for r in net.graph.requests)


# ---- runner ------------------------------------------------------------------------------------
def test_all_three_published_and_one_failure_does_not_stop_others(job, net, sleeps):
    net.google.inject["start"] = [google_error(403, "quotaExceeded")]
    res, sink = run(job, net, sleeps)
    assert [res[p]["state"] for p in ("youtube", "facebook", "instagram")] == ["failed", "published", "published"]
    assert runner.all_failed(res) is None
    assert "YouTube failed (quota)" in sink.text


def test_all_failed_message():
    assert runner.all_failed({"youtube": {"state": "failed", "message": "m1"},
                              "facebook": {"state": "failed", "message": "m2"}}) == "m1"
    assert runner.all_failed({"youtube": {"state": "published"}}) is None
    assert runner.all_failed({}) is None


def test_runner_refuses_while_dry_run_on(net):
    job_id = make_job()
    connect_all(dry_run=True)
    res, _ = run(job_id, net)
    for p in ("youtube", "facebook", "instagram"):
        assert res[p]["state"] == "failed" and "dry-run mode" in res[p]["message"]
    assert net.requests == []


def test_idempotent_second_run_sends_nothing_and_force_posts_again(job, net):
    res, _ = run(job, net)
    first = {p: res[p]["remote_id"] for p in res}
    net.reset()
    res2, sink = run(job, net)
    assert net.requests == []
    assert {p: res2[p]["remote_id"] for p in res2} == first
    assert "already published" in sink.text
    to_run, already = service.check_live(job, 1, "hi", ["youtube", "facebook", "instagram"], load_settings(),
                                         force=False)
    assert to_run == [] and set(already) == {"youtube", "facebook", "instagram"}
    res3, _ = run(job, net, platforms=["youtube"], force=True)
    assert res3["youtube"]["remote_id"] != first["youtube"]
    hist = records.history_path(job, 1, "hi", "youtube").read_text(encoding="utf-8").splitlines()
    assert json.loads(hist[0])["remote_id"] == first["youtube"]


def test_status_files_never_hold_secrets_or_resume(job, net):
    run(job, net)
    for p in ("youtube", "facebook", "instagram"):
        raw = json.loads(records.status_path(job, 1, "hi", p).read_text(encoding="utf-8"))
        assert set(raw) == set(records.STATUS_KEYS)
    text = tree_text()
    assert "upload_id" not in text and grep_secrets(text) == []


def test_unexpected_exception_in_client_fails_platform_only(job, net, monkeypatch):
    from autoclipper.publish import youtube

    def broken(self, *a, **k):
        raise KeyError("weird " + PAGE_TOKEN["hi"])
    monkeypatch.setattr(youtube.YouTubePublisher, "publish", broken)
    res, sink = run(job, net)
    assert res["youtube"]["state"] == "failed" and res["youtube"]["error"]["kind"] == "unknown"
    assert PAGE_TOKEN["hi"] not in json.dumps(res) and PAGE_TOKEN["hi"] not in sink.text
    assert res["facebook"]["state"] == "published"


def test_english_goes_to_english_accounts(job, net):
    res, _ = run(job, net, lang="en")
    assert res["youtube"]["account"]["id"] == CHANNEL["en"][0]
    assert res["facebook"]["account"]["id"] == PAGE["en"][0]
    assert res["instagram"]["account"]["id"] == IG["en"][0]
    forms = [r.form for r in net.google.requests if r.url.endswith("/token")]
    assert forms[0]["refresh_token"] == REFRESH_TOKEN["en"]
    assert all(PAGE_TOKEN["en"] in r.headers["authorization"] for r in net.graph.requests)
    start = [r for r in net.google.requests if r.method == "POST" and "upload" in r.url][0]
    assert start.json["snippet"]["defaultLanguage"] == "en"
    _ = urllib.parse  # keep import used
