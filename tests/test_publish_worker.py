"""Step 4 core: the worker's `publish` task kind."""
from __future__ import annotations

import functools
import json
import threading
import time

import pytest
from publish_fakes import FakeNet, connect_all, grep_secrets, make_job, stub_probe
from test_web_support import Stub, wait_for

from autoclipper.config import load_settings
from autoclipper.jobstore import jobs_root
from autoclipper.joblock import lock_path
from autoclipper.publish import records, runner
from autoclipper.web.worker import Worker, WorkerConflict


class PubStub:
    def __init__(self, block: bool = False, fail: str | None = None, results: dict | None = None):
        self.calls: list[tuple] = []
        self.block, self.fail, self.results = block, fail, results
        self.release, self.started = threading.Event(), threading.Event()

    def __call__(self, job_id, clip, lang, *, platforms, force, settings, log, idempotency_key=None):
        self.calls.append((job_id, clip, lang, tuple(platforms), force, idempotency_key,
                           lock_path(job_id).exists()))
        self.started.set()
        if self.block:
            deadline = time.monotonic() + 20
            while not self.release.is_set() and time.monotonic() < deadline:
                log("")  # cancel point
                time.sleep(0.01)
        if self.fail:
            raise RuntimeError(self.fail)
        return self.results if self.results is not None else {p: {"state": "published"} for p in platforms}


@pytest.fixture
def job_id() -> str:
    return make_job()


def test_submit_publish_task_dict_and_run(job_id):
    stub = PubStub()
    w = Worker(publish=stub)
    t, created = w.submit_publish(job_id, 1, "hi", ["youtube", "facebook"], idempotency_key="k")
    assert created and t.kind == "publish" and t.platforms == ["youtube", "facebook"]
    d = w.task_dict(t)
    assert d["platforms"] == ["youtube", "facebook"] and d["kind"] == "publish" and d["position"] == 1
    assert "idempotency_key" not in d and "force" not in d
    assert w.active_publish(job_id, 1, "hi") is t
    w.start()
    try:
        assert w.wait_idle(10)
    finally:
        w.stop()
    assert stub.calls == [(job_id, 1, "hi", ("youtube", "facebook"), False, "k", True)]  # job.lock held
    assert w.status(t.id).state == "done"
    assert not lock_path(job_id).exists()
    log = (jobs_root() / job_id / "web.log").read_text(encoding="utf-8")
    assert f"publish 01/hi youtube,facebook" in log


def test_non_publish_tasks_have_null_platforms(job_id):
    w = Worker(rerender=Stub().rerender, publish=PubStub())
    t, _ = w.submit_rerender(job_id, 1, "hi")
    assert w.task_dict(t)["platforms"] is None


def test_same_set_reused_different_set_conflicts(job_id):
    w = Worker(publish=PubStub())
    t1, c1 = w.submit_publish(job_id, 1, "hi", ["youtube", "facebook"])
    t2, c2 = w.submit_publish(job_id, 1, "hi", ["facebook", "youtube"])
    assert c1 and not c2 and t2 is t1
    with pytest.raises(WorkerConflict):
        w.submit_publish(job_id, 1, "hi", ["youtube"])
    with pytest.raises(WorkerConflict):
        w.submit_publish(job_id, 1, "hi", ["youtube", "facebook"], force=True)
    t3, c3 = w.submit_publish(job_id, 1, "en", ["youtube"])  # other lang: independent
    assert c3 and t3 is not t1


def test_conflicts_with_run_job_and_rerender(job_id):
    stub = Stub()
    w = Worker(run_job=stub.run_job, rerender=stub.rerender, publish=PubStub())
    w.submit_rerender(job_id, 1, "hi")
    with pytest.raises(WorkerConflict, match="being rendered"):
        w.submit_publish(job_id, 1, "hi", ["youtube"])
    w.submit_publish(job_id, 1, "en", ["youtube"])  # other clip-lang is fine
    with pytest.raises(WorkerConflict, match="being published"):
        w.submit_rerender(job_id, 1, "en")
    w2 = Worker(run_job=stub.run_job, publish=PubStub())
    w2.submit_resume(job_id)
    with pytest.raises(WorkerConflict):
        w2.submit_publish(job_id, 1, "hi", ["youtube"])


def test_exclusive_publish_and_script_exclusive(job_id):
    stub = PubStub(block=True)
    w = Worker(publish=stub)
    with w.exclusive_publish(job_id, 1, "hi"):
        pass
    w.submit_publish(job_id, 1, "hi", ["youtube"])
    with pytest.raises(WorkerConflict, match="being published"):
        with w.exclusive_publish(job_id, 1, "hi"):
            pass
    with w.exclusive_publish(job_id, 1, "en"):
        pass
    w.start()
    try:
        assert stub.started.wait(5)
        with w.exclusive(job_id, 1, "hi"):  # a running publish does not block script edits
            pass
        stub.release.set()
        assert w.wait_idle(10)
    finally:
        w.stop()


def test_fifo_with_other_tasks(job_id):
    order: list[str] = []
    stub = Stub()

    def rr(job, clip, lang, *, settings, deps, log):
        order.append(f"rerender {lang}")

    def pub(job, clip, lang, *, platforms, force, settings, log, idempotency_key=None):
        order.append(f"publish {lang}")
        return {p: {"state": "published"} for p in platforms}

    w = Worker(run_job=stub.run_job, rerender=rr, publish=pub)
    w.submit_publish(job_id, 1, "hi", ["youtube"])
    w.submit_rerender(job_id, 1, "en")
    w.start()
    try:
        assert w.wait_idle(10)
    finally:
        w.stop()
    assert order == ["publish hi", "rerender en"]


def test_cancel_running_publish(job_id):
    stub = PubStub(block=True)
    w = Worker(publish=stub)
    t, _ = w.submit_publish(job_id, 1, "hi", ["youtube"])
    w.start()
    try:
        assert stub.started.wait(5)
        _, outcome = w.cancel_ex(t.id)
        assert outcome == "requested"
        assert w.wait_idle(10)
    finally:
        w.stop()
    assert w.status(t.id).state == "cancelled"


def test_cancel_queued_publish(job_id):
    w = Worker(publish=PubStub())
    t, _ = w.submit_publish(job_id, 1, "hi", ["youtube"])
    _, outcome = w.cancel_ex(t.id)
    assert outcome == "removed" and w.active_publish(job_id, 1, "hi") is None


def test_all_failed_marks_task_failed_and_thread_survives(job_id):
    results = {"youtube": {"state": "failed", "message": "YouTube daily upload quota used up"},
               "facebook": {"state": "failed", "message": "other"}}
    w = Worker(publish=PubStub(results=results))
    t1, _ = w.submit_publish(job_id, 1, "hi", ["youtube", "facebook"])
    crash = PubStub(fail="boom")
    w2 = Worker(publish=crash)
    t2, _ = w2.submit_publish(job_id, 1, "hi", ["youtube"])
    t3, _ = w2.submit_publish(job_id, 1, "en", ["youtube"])
    for wk in (w, w2):  # one after the other: two workers in one process would race on job.lock's tmp name
        wk.start()
        try:
            assert wk.wait_idle(10)
        finally:
            wk.stop()
    assert w.status(t1.id).state == "failed" and w.status(t1.id).error == "YouTube daily upload quota used up"
    assert w2.status(t2.id).state == "failed" and w2.status(t2.id).error == "boom"
    assert w2.status(t3.id).state == "failed"  # ran after the crash: the thread is alive
    partial = Worker(publish=PubStub(results={"youtube": {"state": "failed", "message": "x"},
                                              "facebook": {"state": "published"}}))
    t4, _ = partial.submit_publish(job_id, 1, "hi", ["youtube", "facebook"])
    partial.start()
    try:
        assert partial.wait_idle(10)
    finally:
        partial.stop()
    assert partial.status(t4.id).state == "done"


def test_real_runner_through_worker_with_fakes(job_id):
    connect_all()
    net = FakeNet()
    pub = functools.partial(runner.run_publish, http=net.client(), sleep=lambda s: None)
    w = Worker(settings_loader=load_settings, publish=pub)
    t, _ = w.submit_publish(job_id, 1, "hi", ["youtube", "facebook", "instagram"], idempotency_key="abc")
    w.start()
    try:
        assert w.wait_idle(20)
    finally:
        w.stop()
    assert w.status(t.id).state == "done", w.status(t.id).error
    for p in ("youtube", "facebook", "instagram"):
        st = records.read_status_raw(job_id, 1, "hi", p)
        assert st["state"] == "published" and st["idempotency_key"] == "abc"
    log = (jobs_root() / job_id / "web.log").read_text(encoding="utf-8")
    assert "YouTube published: https://www.youtube.com/shorts/" in log
    assert "\n\n" not in log  # cancel points do not write empty lines
    assert grep_secrets(log) == []


def test_real_runner_cancel_between_chunks_leaves_interrupted(monkeypatch):
    job_id = make_job(final_bytes=6 * 1024 * 1024)
    stub_probe(monkeypatch)
    connect_all()
    net = FakeNet()
    gate = threading.Event()
    real = net.google

    def slow(request):
        if request.method == "PUT":
            gate.set()
            time.sleep(0.05)
        return real(request)

    from publish_fakes import fake_client
    http = fake_client(lambda r: slow(r) if r.url.host.endswith("googleapis.com") else net(r))
    pub = functools.partial(runner.run_publish, http=http, sleep=lambda s: None)
    w = Worker(settings_loader=load_settings, publish=pub)
    t, _ = w.submit_publish(job_id, 1, "hi", ["youtube", "facebook"])
    w.start()
    try:
        assert gate.wait(10)
        w.cancel(t.id)
        assert w.wait_idle(10)
    finally:
        w.stop()
    assert w.status(t.id).state == "cancelled"
    yt = records.read_status_raw(job_id, 1, "hi", "youtube")
    assert yt["state"] == "interrupted"
    fb = records.read_status_raw(job_id, 1, "hi", "facebook")
    assert fb["state"] == "queued"  # never started; shown as interrupted when no task is active
    assert records.public_status(fb, active=False)["state"] == "interrupted"
    assert len(real.videos) == 0 and json.dumps(yt).count("upload_id") == 0
    _ = wait_for  # imported helper kept for parity with other worker tests
