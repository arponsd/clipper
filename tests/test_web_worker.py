"""Worker: FIFO, positions, idempotency, cancel, crash recovery, resilience, single writer; one real
end-to-end task with the fakes; `autoclipper serve` argument handling (no server is started)."""
from __future__ import annotations

import json
import threading
import time

import pytest
from fakes import FakeLLM, FakeTranscriber, FakeVoicebox, make_video
from test_web_support import Stub, age_job_json, make_client, make_job_on_disk, make_settings, read_json, wait_for

from autoclipper import cli
from autoclipper.config import save_settings
from autoclipper.jobstore import JobStore, jobs_root
from autoclipper.models import Job, StageState
from autoclipper.pipeline import Deps
from autoclipper.web import worker as worker_mod
from autoclipper.web.worker import JobOptions, TaskCancelled, Worker, WorkerConflict


def load(job_id: str) -> Job:
    return Job.model_validate_json((jobs_root() / job_id / "job.json").read_text(encoding="utf-8"))


@pytest.fixture
def settings_saved():
    save_settings(make_settings())


def test_fifo_order_and_positions(settings_saved):
    stub = Stub(block=True)
    w = Worker(run_job=stub.run_job, rerender=stub.rerender)
    tasks = [w.submit_job(f"https://e.com/{i}", options=JobOptions(clips=i + 1)) for i in range(3)]
    assert [w.position(t.id) for t in tasks] == [1, 2, 3]
    w.start()
    try:
        assert stub.started.wait(5)
        assert w.position(tasks[0].id) == 0 and w.position(tasks[1].id) == 1 and w.position(tasks[2].id) == 2
        stub.release.set()
        assert w.wait_idle(10)
    finally:
        w.stop()
    assert [c[1] for c in stub.calls] == [t.job_id for t in tasks]
    assert [c[2]["count"] for c in stub.calls] == [1, 2, 3]
    assert all(w.position(t.id) is None and w.status(t.id).state == "done" for t in tasks)
    assert [t["id"] for t in w.snapshot()["recent"]] == [t.id for t in reversed(tasks)]


def test_submit_job_creates_queued_job_and_options(settings_saved, tmp_path):
    w = Worker(run_job=Stub().run_job)
    up = tmp_path / "data" / "web" / "uploads" / ("c" * 32)
    up.mkdir(parents=True)
    (up / "source.webm").write_bytes(b"v" * 10)
    (up / "upload.json").write_text("{}", encoding="utf-8")
    t = w.submit_job("upload:a.webm", options=JobOptions(langs=["en"]), upload=up / "source.webm")
    job = load(t.job_id)
    assert job.status == "queued" and job.source == "upload:a.webm"
    assert (jobs_root() / t.job_id / "source.webm").read_bytes() == b"v" * 10
    assert not up.exists()
    assert read_json(jobs_root() / t.job_id / "options.json")["langs"] == ["en"]


def test_submit_job_failure_marks_job_failed(settings_saved, tmp_path):
    w = Worker(run_job=Stub().run_job)
    with pytest.raises(OSError):
        w.submit_job("upload:x.mp4", options=JobOptions(), upload=tmp_path / "missing" / "source.mp4")
    (job_id,) = JobStore.list_ids()
    job = load(job_id)
    assert job.status == "failed" and "could not queue" in job.error
    assert w.snapshot()["queued"] == []


def test_rerender_and_resume_idempotency(settings_saved):
    w = Worker(run_job=Stub().run_job, rerender=Stub().rerender)
    store = make_job_on_disk(status="failed", error="interrupted")
    t1, c1 = w.submit_rerender(store.job.id, 0, "en")
    t2, c2 = w.submit_rerender(store.job.id, 0, "en")
    t3, c3 = w.submit_rerender(store.job.id, 0, "hi")
    assert c1 and not c2 and c3 and t1 is t2 and t3 is not t1
    r1, d1 = w.submit_resume(store.job.id)
    r2, d2 = w.submit_resume(store.job.id)
    assert d1 and not d2 and r1 is r2
    with pytest.raises(WorkerConflict):
        w.submit_rerender(store.job.id, 1, "en")  # run_job queued for this job
    assert w.active_task_for(store.job.id) is r1
    assert w.active_task_for(store.job.id, 0, "hi") is t3
    assert w.active_task_for("other") is None


def test_cancel_queued_and_finished(settings_saved):
    stub = Stub()
    w = Worker(run_job=stub.run_job)
    t = w.submit_job("https://e.com/a", options=JobOptions())
    task, outcome = w.cancel_ex(t.id)
    assert outcome == "removed" and task.state == "cancelled"
    assert w.cancel_ex(t.id)[1] == "finished"
    assert w.cancel_ex("f" * 32) == (None, "unknown")
    w.start()
    try:
        assert w.wait_idle(10)
        assert stub.calls == []  # never ran
        job = load(t.job_id)  # the worker thread finalized the job (not the request thread)
        assert job.status == "failed" and job.error == "interrupted"
    finally:
        w.stop()


def test_cancel_running_stub_logging_in_a_loop(settings_saved):
    events = []

    def run_job(source, *, log, job_id, **_):
        try:
            while True:
                log("tick")
                time.sleep(0.01)
        except BaseException as e:
            events.append(type(e).__name__)
            log("handler logs again without raising")  # pipeline's except-BaseException path
            events.append("handler done")
            raise

    w = Worker(run_job=run_job)
    t = w.submit_job("https://e.com/a", options=JobOptions())
    w.start()
    try:
        assert wait_for(lambda: w.status(t.id).state == "running")
        time.sleep(0.05)
        assert w.cancel_ex(t.id)[1] == "requested"
        assert w.wait_idle(10)
    finally:
        w.stop()
    assert events == ["TaskCancelled", "handler done"]
    assert w.status(t.id).state == "cancelled"
    job = load(t.job_id)
    assert job.status == "failed" and job.error == "interrupted"
    log_text = (jobs_root() / t.job_id / "web.log").read_text(encoding="utf-8")
    assert "tick" in log_text and "handler logs again" in log_text


def test_failing_task_does_not_kill_thread(settings_saved):
    calls = []

    def run_job(source, *, job_id, **_):
        calls.append(job_id)
        if len(calls) == 1:
            raise RuntimeError("first line of failure\nsecond line")
        if len(calls) == 2:
            raise SystemExit(3)  # even a BaseException must not stop the worker

    w = Worker(run_job=run_job)
    a = w.submit_job("https://e.com/a", options=JobOptions())
    b = w.submit_job("https://e.com/b", options=JobOptions())
    c = w.submit_job("https://e.com/c", options=JobOptions())
    w.start()
    try:
        assert w.wait_idle(10)
        assert w.alive
    finally:
        w.stop()
    assert w.status(a.id).state == "failed" and w.status(a.id).error == "first line of failure"
    assert w.status(b.id).state == "failed"
    assert w.status(c.id).state == "done"
    assert load(a.job_id).status == "failed" and load(a.job_id).error == "first line of failure"


def test_bad_settings_fails_task_and_job(settings_saved):
    def loader():
        raise ValueError("settings.json broken")

    w = Worker(settings_loader=loader, run_job=Stub().run_job)
    t = w.submit_job("https://e.com/a", options=JobOptions())
    w.start()
    try:
        assert w.wait_idle(10)
    finally:
        w.stop()
    assert w.status(t.id).state == "failed" and "settings.json broken" in w.status(t.id).error
    assert load(t.job_id).status == "failed"


def test_each_task_loads_settings_and_deps(settings_saved):
    seen = []
    settings_calls = []

    def loader():
        settings_calls.append(1)
        return make_settings()

    def deps_factory(s):
        seen.append(s.voicebox.english.profile_id)
        return "DEPS"

    got = []

    def run_job(source, *, deps, settings, **_):
        got.append(deps)

    w = Worker(settings_loader=loader, deps_factory=deps_factory, run_job=run_job)
    for i in range(2):
        w.submit_job(f"https://e.com/{i}", options=JobOptions())
    w.start()
    try:
        assert w.wait_idle(10)
    finally:
        w.stop()
    assert len(settings_calls) == 2 and seen == ["fake-en", "fake-en"] and got == ["DEPS", "DEPS"]


# ---- crash recovery -------------------------------------------------------------------------------
def test_recover_interrupted_marks_old_running_jobs(settings_saved):
    old = make_job_on_disk(status="running")
    old.job.stages["cut"] = StageState(status="running", progress=0.4)
    old.job.clips[0].langs["en"].stages["voice"] = StageState(status="running", progress=0.2)
    old.save()
    age_job_json(old.job.id)
    queued = make_job_on_disk(status="queued")
    age_job_json(queued.job.id)
    young = make_job_on_disk(status="running")
    done = make_job_on_disk(status="done")
    age_job_json(done.job.id)
    w = Worker(run_job=Stub().run_job)
    w.start()
    try:
        j = load(old.job.id)
        assert j.status == "failed" and j.error == "interrupted"
        assert j.stages["cut"].status == "failed" and j.stages["cut"].message == "interrupted"
        assert j.clips[0].langs["en"].stages["voice"].status == "failed"
        assert load(queued.job.id).status == "failed"
        assert load(young.job.id).status == "running"  # < 30 s old: maybe a CLI process
        assert load(done.job.id).status == "done"
    finally:
        w.stop()
    assert sorted(Worker(run_job=Stub().run_job).recover_interrupted()) == []  # nothing left to mark


def test_recover_returns_ids(settings_saved):
    a = make_job_on_disk(status="running")
    age_job_json(a.job.id)
    assert Worker().recover_interrupted() == [a.job.id]


def test_stop_cancels_running_task(settings_saved):
    stub = Stub(loop_log=True)
    w = Worker(run_job=stub.run_job)
    t = w.submit_job("https://e.com/a", options=JobOptions())
    w.start()
    assert stub.started.wait(5)
    w.stop(timeout=10)
    assert not w.alive
    assert w.status(t.id).state == "cancelled"
    assert load(t.job_id).error == "interrupted"


# ---- single writer ---------------------------------------------------------------------------------
def test_request_threads_never_save_existing_job_json(monkeypatch):
    saves: list[tuple[str, str, str]] = []
    real_save = JobStore.save

    def recording_save(self):
        saves.append((threading.current_thread().name, self.job.id, self.job.status))
        return real_save(self)

    monkeypatch.setattr(JobStore, "save", recording_save)
    stub = Stub()
    client, worker, _ = make_client(stub=stub)
    with client:
        store = make_job_on_disk(status="failed", error="interrupted")
        saves.clear()
        jid = store.job.id
        new = client.post("/api/jobs", json={"url": "https://e.com/x"}).json()
        assert client.get(f"/api/jobs/{jid}").status_code == 200
        assert client.get("/api/jobs").status_code == 200
        assert client.put(f"/api/jobs/{jid}/clips/0/en/script",
                          json={"narration": "n", "title": "t", "description": "", "hashtags": []}).status_code == 200
        assert client.post(f"/api/jobs/{jid}/clips/0/en/rerender").status_code == 202
        assert worker.wait_idle(10)
        assert client.post(f"/api/jobs/{jid}/resume").status_code == 202
        assert worker.wait_idle(10)
    non_worker = [s for s in saves if s[0] != "autoclipper-worker"]
    # the only request-thread save: JobStore.create of the brand-new job (status queued)
    assert non_worker == [(non_worker[0][0], new["id"], "queued")]
    assert all(s[1] != jid for s in non_worker)


# ---- real end-to-end task with fakes ---------------------------------------------------------------
def test_end_to_end_upload_job_review_rerender(tmp_path):
    save_settings(make_settings())
    src = make_video(tmp_path / "src.mp4", seconds=20)
    deps = Deps(llm=FakeLLM(), voicebox=FakeVoicebox(), transcriber=FakeTranscriber(duration=20))
    w = Worker(deps_factory=lambda s: deps)
    client, worker, _ = make_client(worker=w)
    with client:
        up = client.post("/api/uploads?filename=src.mp4", content=src.read_bytes(),
                         headers={"Content-Type": "video/mp4"})
        assert up.status_code == 201
        r = client.post("/api/jobs", json={"upload_id": up.json()["upload_id"],
                                           "options": {"clips": 1, "min_seconds": 10, "max_seconds": 12,
                                                       "langs": ["en"]}},
                        headers={"Idempotency-Key": "e2e"})
        assert r.status_code == 201, r.text
        job_id = r.json()["id"]
        assert worker.wait_idle(180)
        d = client.get(f"/api/jobs/{job_id}").json()
        assert d["status"] == "done", d
        assert d["rendered"] == 1 and d["failed"] == 0 and d["langs"] == ["en"]
        assert all(st["status"] in ("done", "skipped") for st in d["stages"].values())
        en = d["clips"][0]["langs"]["en"]
        assert en["final"] is True and en["voice"] is not None and en["voice_stale"] is False
        v = client.get(en["video_url"], headers={"Range": "bytes=0-99"})
        assert v.status_code == 206 and len(v.content) == 100
        log = client.get(f"/api/jobs/{job_id}/log").json()["lines"]
        assert any("render" in ln for ln in log)
        # edit narration -> stale -> rerender regenerates the voice
        r = client.put(f"/api/jobs/{job_id}/clips/0/en/script",
                       json={"narration": "a completely different narration for this clip",
                             "title": "Edited", "description": "d", "hashtags": ["#x"]})
        assert r.status_code == 200 and r.json()["voice_stale"] is True
        n_voice = len(deps.voicebox.calls)
        r = client.post(f"/api/jobs/{job_id}/clips/0/en/rerender")
        assert r.status_code == 202
        assert worker.wait_idle(120)
        t = client.get(f"/api/tasks/{r.json()['task']['id']}").json()
        assert t["state"] == "done", t
        assert len(deps.voicebox.calls) == n_voice + 1
        en = client.get(f"/api/jobs/{job_id}").json()["clips"][0]["langs"]["en"]
        assert en["voice_stale"] is False and en["metadata"]["title"] == "Edited"


# ---- CLI serve ---------------------------------------------------------------------------------------
def test_serve_help_parses(capsys):
    assert cli.main(["serve", "--help"]) == 0
    out = capsys.readouterr().out
    assert "--host" in out and "--port" in out and "--no-browser" in out


@pytest.mark.parametrize("argv", [["serve", "--port", "0"], ["serve", "--port", "70000"], ["serve", "--port", "x"]])
def test_serve_bad_port(argv):
    assert cli.main(argv) == 2


def test_serve_runs_uvicorn_with_defaults(monkeypatch, capsys):
    import uvicorn

    seen = {}

    def fake_run(app, host, port, **kw):
        seen.update(host=host, port=port, app=app)

    opened = []
    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    assert cli.main(["serve", "--no-browser"]) == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8765
    out = capsys.readouterr()
    assert "http://127.0.0.1:8765/" in out.out and "warning" not in out.err
    assert opened == []
    assert not seen["app"].state.worker.alive


def test_serve_warns_for_network_host(monkeypatch, capsys):
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, **kw: None)
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "9000", "--no-browser"]) == 0
    err = capsys.readouterr().err
    assert "warning" in err and "only given to loopback clients" in err
