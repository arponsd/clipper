"""Regression tests for the step-2 review fixes (API-1..4, UX-1, UX-5, static cache/HEAD, WORKER-1..4).

All in the conftest-isolated data dir, with stub run_job/rerender: no real LLM, Voicebox or port.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient
from test_web_support import (BASE, Stub, age_job_json, api, api_idle, assert_error_shape,  # noqa: F401
                              make_client, make_job_on_disk, make_settings, read_json, wait_for)

from autoclipper import cli
from autoclipper import joblock
from autoclipper.config import save_settings, settings_path
from autoclipper.jobstore import jobs_root
from autoclipper.models import Job
from autoclipper.web.auth import TOKEN_HEADER, TokenRedactFilter, allowed_hosts_for, install_access_log_filter
from autoclipper.web.server import install_stop_hook
from autoclipper.web.worker import JobOptions, Worker

INVALID_JSON = {"message": "validation failed", "errors": {"body": ["invalid JSON"]}}


def load(job_id: str) -> Job:
    return Job.model_validate_json((jobs_root() / job_id / "job.json").read_text(encoding="utf-8"))


def job_dirs() -> set[str]:
    return {p.name for p in jobs_root().iterdir()}


def write_lock(job_id: str, pid: int, host: str | None = None) -> None:
    (jobs_root() / job_id / "job.lock").write_text(json.dumps(
        {"pid": pid, "host": host if host is not None else socket.gethostname(), "started": time.time()}),
        encoding="utf-8")


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not joblock.pid_alive(p.pid)
    return p.pid


@pytest.fixture
def settings_saved():
    save_settings(make_settings())


# ---- API-1: the token only goes to loopback peers ----------------------------------------------------
def test_index_withholds_token_from_non_loopback_peer():
    client, _, _ = make_client(allowed_hosts=allowed_hosts_for("0.0.0.0"))
    token = client.app.state.token
    lan = TestClient(client.app, base_url=BASE, client=("192.168.1.99", 1))
    r = lan.get("/", headers={"Host": "127.0.0.1:8765"})  # spoofed Host passes the Host guard
    assert r.status_code == 200
    assert token not in r.text
    assert "__AUTOCLIPPER_TOKEN__" not in r.text
    assert 'name="autoclipper-token" content=""' in r.text  # app.js shows its "no access token" banner
    # and without the token the API stays closed for that peer
    assert_error_shape(lan.get("/api/settings", headers={"Host": "127.0.0.1:8765"}), 401)


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "127.0.0.2"])
def test_index_gives_token_to_loopback_peer(peer):
    client, _, _ = make_client(allowed_hosts=allowed_hosts_for("0.0.0.0"))
    r = TestClient(client.app, base_url=BASE, client=(peer, 50000)).get("/")
    assert r.status_code == 200
    assert f'content="{client.app.state.token}"' in r.text


def test_serve_warning_text_for_network_bind(monkeypatch, capsys):
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, **kw: None)
    assert cli.main(["serve", "--host", "0.0.0.0", "--no-browser"]) == 0
    err = capsys.readouterr().err
    assert "the UI token is only given to loopback clients" in err
    assert "reachable from the network" not in err


# ---- API-2: json.loads errors other than JSONDecodeError -> 422 ---------------------------------------
HUGE_INT = b'{"url": "https://a.b/x", "options": {"clips": ' + b"9" * 5000 + b"}}"
DEEP = b'{"url": ' + b"[" * 100000 + b"]" * 100000 + b"}"


@pytest.mark.parametrize("body", [HUGE_INT, DEEP], ids=["5000-digit-int", "100000-deep"])
def test_post_jobs_unparseable_json_is_422(api, body):
    client, _, _ = api
    before = job_dirs()
    r = client.post("/api/jobs", content=body, headers={"Content-Type": "application/json"})
    assert_error_shape(r, 422)
    assert r.json() == INVALID_JSON
    assert job_dirs() == before


@pytest.mark.parametrize("body", [b'{"captions": {"font_size": ' + b"7" * 5000 + b"}}",
                                  b'{"llm": ' + b"[" * 100000 + b"]" * 100000 + b"}"],
                         ids=["5000-digit-int", "100000-deep"])
def test_put_settings_unparseable_json_is_422(api, body):
    client, _, _ = api
    before = settings_path().read_bytes()
    r = client.put("/api/settings", content=body, headers={"Content-Type": "application/json"})
    assert_error_shape(r, 422)
    assert r.json() == INVALID_JSON
    assert settings_path().read_bytes() == before


# ---- API-3: settings values the model cannot express --------------------------------------------------
def _set(path: str, value):
    def mutate(s: dict) -> None:
        *parents, last = path.split(".")
        d = s
        for p in parents:
            d = d[p]
        d[last] = value
    return mutate


LONG = "x" * 201
BAD_SETTINGS = [
    ("voicebox.url", "file:///etc/passwd"),
    ("voicebox.url", "127.0.0.1:17493"),
    ("voicebox.url", "http://"),
    ("voicebox.url", "http://127.0.0.1:99999"),
    ("voicebox.url", "http://127.0.0.1:abc"),
    ("voicebox.url", "http://a.b/" + "x" * 2048),
    ("voicebox.url", ""),
    ("captions.margin_v", -5),
    ("captions.margin_v", 1921),
    ("captions.margin_v", 10 ** 30),
    ("captions.primary_color", ""),
    ("captions.primary_color", "&H00FFFFF"),
    ("captions.primary_color", "white"),
    ("captions.primary_color", "&h00FFFFFF"),
    ("captions.highlight_color", "&H0000E5FG"),
    ("captions.highlight_color", "&H0000E5FF0"),
    ("llm.model", LONG),
    ("llm.model", "x" * 100000),
    ("llm.api_key_env", LONG),
    ("captions.font_en", LONG),
    ("captions.font_hi", LONG),
    ("whisper.model", LONG),
    ("whisper.device", LONG),
    ("whisper.compute_type", LONG),
    ("whisper.language", LONG),
    ("voicebox.english.profile_id", LONG),
    ("voicebox.hindi.profile_id", LONG),
    ("voicebox.english.engine", LONG),
    ("voicebox.hindi.engine", LONG),
]


@pytest.mark.parametrize("key,value", BAD_SETTINGS, ids=[f"{k}={str(v)[:20]}" for k, v in BAD_SETTINGS])
def test_put_settings_rejects_unusable_values(api, key, value):
    client, _, _ = api
    s = client.get("/api/settings").json()
    before = settings_path().read_bytes()
    _set(key, value)(s)
    body = assert_error_shape(client.put("/api/settings", json=s), 422)
    assert key in body["errors"], body
    assert settings_path().read_bytes() == before


def test_defaults_pass_the_settings_checks(api):
    client, _, _ = api
    from autoclipper.config import Settings

    r = client.put("/api/settings", json=Settings().model_dump())
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("key,value", [
    ("voicebox.url", "https://voicebox.local:443/api"), ("voicebox.url", "http://[::1]:17493"),
    ("captions.margin_v", 0), ("captions.margin_v", 1920), ("captions.primary_color", "&H00ffffff"),
    ("llm.model", "m" * 200), ("whisper.language", None), ("voicebox.english.profile_id", "p" * 200),
])
def test_put_settings_accepts_boundary_values(api, key, value):
    client, _, _ = api
    s = client.get("/api/settings").json()
    _set(key, value)(s)
    r = client.put("/api/settings", json=s)
    assert r.status_code == 200, r.text
    assert read_json(settings_path()) == s


def test_margin_v_bound_follows_clip_height(api):
    client, _, _ = api
    s = client.get("/api/settings").json()
    s["clips"]["height"] = 1280
    s["captions"]["margin_v"] = 1300
    body = assert_error_shape(client.put("/api/settings", json=s), 422)
    assert "1280" in body["errors"]["captions.margin_v"][0]


# ---- API-4: job URL ports ---------------------------------------------------------------------------
@pytest.mark.parametrize("url", ["http://a.b:99999/x", "http://a.b:abc/x", "https://a.b:-1/x"])
def test_post_jobs_bad_port_is_422(api, url):
    client, worker, _ = api
    before = job_dirs()
    body = assert_error_shape(client.post("/api/jobs", json={"url": url}), 422)
    assert list(body["errors"]) == ["url"]
    assert job_dirs() == before
    assert worker.snapshot()["queued"] == [] and worker.snapshot()["running"] is None


def test_post_jobs_valid_port_is_201(api_idle):
    client, _, _ = api_idle
    r = client.post("/api/jobs", json={"url": "http://a.b:8080/x"})
    assert r.status_code == 201, r.text


# ---- UX-1: one hashtag limit (8) ---------------------------------------------------------------------
def test_put_script_more_than_8_hashtags_is_422(api):
    client, _, _ = api
    store = make_job_on_disk(clips=1, langs=("en",))
    path = jobs_root() / store.job.id / "clips" / "00" / "en" / "script.json"
    before = path.read_bytes()
    body = {"narration": "n", "title": "t", "description": "", "hashtags": [f"#t{i}" for i in range(9)]}
    r = client.put(f"/api/jobs/{store.job.id}/clips/0/en/script", json=body)
    assert "hashtags" in assert_error_shape(r, 422)["errors"]
    assert path.read_bytes() == before
    body["hashtags"] = body["hashtags"][:8]
    r = client.put(f"/api/jobs/{store.job.id}/clips/0/en/script", json=body)
    assert r.status_code == 200, r.text
    assert read_json(path)["hashtags"] == [f"#t{i}" for i in range(8)]


def test_server_limit_matches_normalizer_and_ui():
    from pathlib import Path

    from autoclipper.stages.script import MAX_HASHTAGS
    from autoclipper.web.server import ClipScriptIn

    assert MAX_HASHTAGS == 8
    assert ClipScriptIn.model_json_schema()["properties"]["hashtags"]["maxItems"] == MAX_HASHTAGS
    js = (Path(__file__).resolve().parents[1] / "autoclipper" / "web" / "static" / "app.js").read_text("utf-8")
    assert f"const MAX_HASHTAGS = {MAX_HASHTAGS};" in js and "up to 30" not in js


# ---- UX-5: no token in the access log ----------------------------------------------------------------
def test_access_log_filter_redacts_token():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("127.0.0.1:5000", "GET", "/api/jobs/j/clips/0/en/video?token=SECRET-tok_1&v=3",
                             "1.1", 206), None)
    assert TokenRedactFilter().filter(rec) is True
    text = rec.getMessage()
    assert "SECRET" not in text
    assert "token=***&v=3" in text and "206" in text
    rec2 = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                             'GET /x?token=abc"q', None, None)
    TokenRedactFilter().filter(rec2)
    assert rec2.getMessage() == 'GET /x?token=***"q'


def test_access_log_filter_installed_once(monkeypatch):
    logger = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(logger, "filters", [])
    f1 = install_access_log_filter()
    f2 = install_access_log_filter()
    assert f1 is f2 and logger.filters == [f1]


def test_serve_installs_access_log_filter(monkeypatch):
    import uvicorn

    logger = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(logger, "filters", [])
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, **kw: None)
    assert cli.main(["serve", "--no-browser"]) == 0
    assert any(isinstance(f, TokenRedactFilter) for f in logger.filters)


# ---- static: no-cache + HEAD --------------------------------------------------------------------------
def test_static_no_cache_and_head(api):
    client, _, _ = api
    bare = TestClient(client.app, base_url=BASE)
    r = bare.get("/static/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    h = bare.head("/static/app.js")
    assert h.status_code == 200
    assert h.headers["cache-control"] == "no-cache"
    assert h.content == b"" and int(h.headers["content-length"]) == len(r.content)
    assert bare.head("/static/missing.js").status_code == 404


# ---- WORKER-1: stop() leaves no job stuck in "queued" -------------------------------------------------
def test_stop_marks_still_queued_jobs_interrupted(settings_saved):
    stub = Stub(loop_log=True)
    w = Worker(run_job=stub.run_job, rerender=stub.rerender)
    a = w.submit_job("https://e.com/a", options=JobOptions())
    b = w.submit_job("https://e.com/b", options=JobOptions())
    w.start()
    assert stub.started.wait(5)
    w.stop(timeout=10)
    assert not w.alive
    jb = load(b.job_id)
    assert jb.status == "failed" and jb.error == "interrupted"
    ja = load(a.job_id)
    assert ja.status == "failed" and ja.error == "interrupted"
    assert w.status(b.id).state == "cancelled"
    assert w.active_task_for(b.job_id) is None
    assert w.snapshot()["queued"] == []
    assert len(stub.calls) == 1  # B never ran


def test_stop_drains_pending_finalize(settings_saved):
    w = Worker(run_job=Stub().run_job)  # never started
    t = w.submit_job("https://e.com/a", options=JobOptions())
    w.cancel(t.id)
    assert load(t.job_id).status == "queued"
    w.stop()
    assert load(t.job_id).error == "interrupted"


# ---- WORKER-2: job.lock ownership -----------------------------------------------------------------------
def test_live_lock_blocks_recovery_and_resume(api_idle):
    client, worker, _ = api_idle
    store = make_job_on_disk(status="running")
    age_job_json(store.job.id)  # old job.json: only the lock says a process owns it
    write_lock(store.job.id, os.getpid())
    assert worker.recover_interrupted() == []
    assert load(store.job.id).status == "running"
    body = assert_error_shape(client.post(f"/api/jobs/{store.job.id}/resume"), 409, "another process")
    assert body["errors"] == {}
    assert worker.snapshot()["queued"] == []
    assert load(store.job.id).status == "running"


def test_live_lock_blocks_rerender(api_idle):
    client, worker, _ = api_idle
    store = make_job_on_disk(status="done")
    write_lock(store.job.id, os.getpid())
    assert_error_shape(client.post(f"/api/jobs/{store.job.id}/clips/0/en/rerender"), 409, "another process")
    assert worker.snapshot()["queued"] == []


@pytest.mark.parametrize("kind", ["dead-pid", "other-host", "garbage"])
def test_stale_lock_does_not_block(api_idle, kind):
    client, worker, _ = api_idle
    store = make_job_on_disk(status="running")  # job.json is young: the lock decides, not the mtime
    if kind == "dead-pid":
        write_lock(store.job.id, dead_pid())
    elif kind == "other-host":
        write_lock(store.job.id, os.getpid(), host="some-other-machine")
    else:
        (jobs_root() / store.job.id / "job.lock").write_text("{not json", encoding="utf-8")
    assert worker.recover_interrupted() == [store.job.id]
    j = load(store.job.id)
    assert j.status == "failed" and j.error == "interrupted"
    r = client.post(f"/api/jobs/{store.job.id}/resume")
    assert r.status_code == 202, r.text


def test_dead_lock_running_job_can_be_resumed(api_idle):
    client, _, _ = api_idle
    store = make_job_on_disk(status="running")
    write_lock(store.job.id, dead_pid())
    r = client.post(f"/api/jobs/{store.job.id}/resume")
    assert r.status_code == 202, r.text


def test_no_lock_keeps_mtime_fallback(api_idle):
    client, worker, _ = api_idle
    store = make_job_on_disk(status="running")  # young, no lock: maybe an older CLI
    assert worker.recover_interrupted() == []
    assert_error_shape(client.post(f"/api/jobs/{store.job.id}/resume"), 409)


def test_worker_holds_lock_during_task_and_removes_it(settings_saved):
    seen = {}

    def run_job(source, *, job_id, log, **kw):
        seen["lock"] = joblock.read_lock(job_id)

    w = Worker(run_job=run_job)
    t = w.submit_job("https://e.com/a", options=JobOptions())
    w.start()
    try:
        assert w.wait_idle(10)
    finally:
        w.stop()
    assert seen["lock"]["pid"] == os.getpid() and seen["lock"]["host"] == socket.gethostname()
    assert isinstance(seen["lock"]["started"], float)
    assert not (jobs_root() / t.job_id / "job.lock").exists()


def test_worker_refuses_job_locked_by_other_live_process(settings_saved):
    stub = Stub()
    w = Worker(run_job=stub.run_job)
    store = make_job_on_disk(status="failed")
    write_lock(store.job.id, os.getppid())  # the parent process: alive, not us
    before = (jobs_root() / store.job.id / "job.json").read_bytes()
    t, _ = w.submit_resume(store.job.id)
    w.start()
    try:
        assert w.wait_idle(10)
    finally:
        w.stop()
    assert w.status(t.id).state == "failed" and "another process" in w.status(t.id).error
    assert stub.calls == []
    assert (jobs_root() / store.job.id / "job.json").read_bytes() == before
    assert joblock.read_lock(store.job.id)["pid"] == os.getppid()  # not removed by us


def test_cli_run_and_rerender_write_lock(monkeypatch, settings_saved):
    from autoclipper import pipeline
    from autoclipper.jobstore import JobStore

    seen = []

    def fake_run_job(source, *, job_id, **kw):
        seen.append(("run", source, job_id, joblock.read_lock(job_id)))
        return JobStore.load(job_id)

    def fake_rerender(job_id, clip, lang, **kw):
        seen.append(("rerender", job_id, joblock.read_lock(job_id)))
        return jobs_root() / job_id / "final.mp4"

    monkeypatch.setattr(pipeline, "run_job", fake_run_job)
    monkeypatch.setattr(pipeline, "rerender", fake_rerender)
    cli.main(["run", "https://e.com/v.mp4"])
    kind, source, job_id, lock = seen[-1]
    assert kind == "run" and source == "https://e.com/v.mp4" and job_id
    assert lock["pid"] == os.getpid()
    assert not (jobs_root() / job_id / "job.lock").exists()
    cli.main(["run", "--job", job_id])
    assert seen[-1][2] == job_id and seen[-1][3]["pid"] == os.getpid()
    assert cli.main(["rerender", job_id, "0", "en"]) == 0
    assert seen[-1][0] == "rerender" and seen[-1][2]["pid"] == os.getpid()
    assert not (jobs_root() / job_id / "job.lock").exists()


def test_cli_run_refuses_job_locked_by_other_process(monkeypatch, settings_saved, capsys):
    from autoclipper import pipeline

    called = []
    monkeypatch.setattr(pipeline, "run_job", lambda *a, **k: called.append(1))
    store = make_job_on_disk(status="running")
    write_lock(store.job.id, os.getppid())
    assert cli.main(["run", "--job", store.job.id]) == 1
    assert "another process" in capsys.readouterr().err
    assert called == []


def test_hold_job_lock_ignores_unknown_or_unsafe_ids(settings_saved):
    for bad in ("nope", "../x", ""):
        with joblock.hold_job_lock(bad):
            pass
    assert not list(jobs_root().parent.glob("**/job.lock"))


# ---- WORKER-3: a task that ends after stop() is cancelled, the job interrupted ---------------------------
def test_exception_after_stop_is_cancelled_not_failed(settings_saved):
    started = threading.Event()
    holder: dict = {}

    def run_job(source, *, job_id, log, **kw):
        started.set()
        deadline = time.monotonic() + 10
        while not holder["w"].stopping and time.monotonic() < deadline:
            time.sleep(0.01)
        raise RuntimeError("ffmpeg exited with code 255")  # Ctrl-C killed the child

    w = Worker(run_job=run_job)
    holder["w"] = w
    t = w.submit_job("https://e.com/a", options=JobOptions())
    w.start()
    assert started.wait(5)
    w.stop(timeout=10)
    assert w.status(t.id).state == "cancelled"
    j = load(t.job_id)
    assert j.status == "failed" and j.error == "interrupted"
    log_text = (jobs_root() / t.job_id / "web.log").read_text(encoding="utf-8")
    assert "task failed" not in log_text


def test_normal_return_after_stop_is_cancelled(settings_saved):
    started = threading.Event()
    holder: dict = {}

    def run_job(source, *, job_id, log, **kw):
        from autoclipper.jobstore import JobStore

        st = JobStore.load(job_id)
        st.job.status = "running"
        st.save()
        started.set()
        while not holder["w"].stopping:
            time.sleep(0.01)

    w = Worker(run_job=run_job)
    holder["w"] = w
    t = w.submit_job("https://e.com/a", options=JobOptions())
    w.start()
    assert started.wait(5)
    w.stop(timeout=10)
    assert w.status(t.id).state == "cancelled"
    assert load(t.job_id).error == "interrupted"


def test_stop_hook_sets_worker_flag_before_previous_handler(settings_saved):
    original = signal.getsignal(signal.SIGINT)
    calls = []
    w = Worker(run_job=Stub().run_job)
    try:
        signal.signal(signal.SIGINT, lambda s, f: calls.append(w.stopping))
        restore = install_stop_hook(w)
        assert restore is not None
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        assert calls == [True]  # the flag was set before uvicorn's handler ran
        restore()
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        assert calls == [True, True]
    finally:
        signal.signal(signal.SIGINT, original)


def test_stop_hook_is_noop_off_main_thread():
    out = []
    t = threading.Thread(target=lambda: out.append(install_stop_hook(Worker())))
    t.start()
    t.join()
    assert out == [None]


def test_serve_creates_app_with_stop_hook(monkeypatch):
    import uvicorn

    from autoclipper.web import server

    seen = {}
    real = server.create_app

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(server, "create_app", spy)
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, **kw: None)
    assert cli.main(["serve", "--no-browser"]) == 0
    assert seen.get("stop_on_signal") is True


# ---- WORKER-4: bounded task table -------------------------------------------------------------------
def test_queue_cancel_cycles_do_not_grow_tables(settings_saved):
    client, w, _ = make_client(start=False, worker=Worker(run_job=Stub().run_job, history=20))
    store = make_job_on_disk(status="failed")
    first = None
    with client:
        for _ in range(500):
            t, created = w.submit_resume(store.job.id)
            assert created
            first = first or t.id
            assert w.cancel_ex(t.id)[1] == "removed"
        live, _ = w.submit_resume(store.job.id)
        assert len(w._cancel) == 1  # only the live task
        assert len(w._tasks) <= 20 + 1
        assert w.status(live.id) is not None
        body = assert_error_shape(client.get(f"/api/tasks/{first}"), 404)
        assert body["errors"] == {}
        assert client.get(f"/api/tasks/{w.snapshot()['recent'][0]['id']}").status_code == 200
        w.cancel(live.id)
        assert len(w._cancel) == 0


def test_finished_tasks_evicted_after_running(settings_saved):
    w = Worker(run_job=Stub().run_job, history=3)
    tasks = [w.submit_job(f"https://e.com/{i}", options=JobOptions()) for i in range(6)]
    w.start()
    try:
        assert w.wait_idle(10)
    finally:
        w.stop()
    assert len(w._tasks) == 3 and len(w._cancel) == 0
    assert w.status(tasks[0].id) is None and w.status(tasks[-1].id).state == "done"
