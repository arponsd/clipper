"""Shared helpers for the tests/test_web_*.py files (no tests here).

Everything runs in the conftest-isolated data dir; no real port is bound (TestClient), no real Voicebox,
LLM or doctor checks are used (they are injected).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from autoclipper.cli import Check
from autoclipper.config import Settings, save_settings
from autoclipper.jobstore import CLIP_STAGES, JOB_STAGES, JobStore
from autoclipper.models import ClipLangState, ClipState, Moment, StageState
from autoclipper.stages.voice import narration_hash
from autoclipper.web.auth import TOKEN_HEADER
from autoclipper.web.server import create_app
from autoclipper.web.worker import Worker

BASE = "http://127.0.0.1:8765"


class Stub:
    """Injectable run_job/rerender for the worker. `block` makes every call wait for `release`."""

    def __init__(self, block: bool = False, fail: str | None = None, loop_log: bool = False):
        self.calls: list[tuple] = []
        self.block = block
        self.fail = fail
        self.loop_log = loop_log
        self.release = threading.Event()
        self.started = threading.Event()
        self.lock = threading.Lock()

    def _body(self, log) -> None:
        self.started.set()
        if self.loop_log:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not self.release.is_set():
                log("tick")
                time.sleep(0.01)
        elif self.block:
            self.release.wait(20)
        if self.fail:
            raise RuntimeError(self.fail)

    def run_job(self, source, *, settings, deps, langs, count, min_seconds, max_seconds, job_id, log):
        with self.lock:
            self.calls.append(("run_job", job_id, dict(source=source, langs=langs, count=count,
                                                       min_seconds=min_seconds, max_seconds=max_seconds)))
        self._body(log)

    def rerender(self, job_id, clip, lang, *, settings, deps, log):
        with self.lock:
            self.calls.append(("rerender", job_id, clip, lang))
        self._body(log)


def make_settings() -> Settings:
    s = Settings()
    s.voicebox.english.profile_id = "fake-en"
    s.voicebox.hindi.profile_id = "fake-hi"
    return s



def fake_doctor(settings: Settings) -> list[Check]:
    return [Check("python", "ok", "3.12"), Check("ffmpeg", "warn", "old"), Check("voicebox", "fail", "down")]


class FakeProfilesClient:
    def __init__(self, profiles=None, fail: Exception | None = None):
        self.profiles = profiles if profiles is not None else [
            {"id": "p1", "name": "Narrator", "language": "en"},
            {"profile_id": "p2", "name": "Hindi"},
            {"name": "no id"},
        ]
        self.fail = fail
        self.closed = False

    def list_profiles(self):
        if self.fail:
            raise self.fail
        return self.profiles

    def close(self):
        self.closed = True


def make_client(*, worker: Worker | None = None, stub: Stub | None = None, start: bool = True,
                voicebox_factory=None, doctor=fake_doctor, **kw: Any) -> tuple[TestClient, Worker, Stub]:
    save_settings(make_settings())
    stub = stub or Stub()
    worker = worker or Worker(run_job=stub.run_job, rerender=stub.rerender)
    app = create_app(worker=worker, doctor=doctor,
                     voicebox_factory=voicebox_factory or (lambda s: FakeProfilesClient()),
                     start_worker=start, **kw)
    client = TestClient(app, base_url=BASE)
    client.headers[TOKEN_HEADER] = app.state.token
    return client, worker, stub


@pytest.fixture
def api():
    """(client, worker, stub) with the lifespan running (worker thread started, stopped at teardown)."""
    client, worker, stub = make_client()
    with client:
        yield client, worker, stub
        stub.release.set()


@pytest.fixture
def api_idle():
    """Like `api` but the worker thread is NOT started: tasks stay queued (positions/cancel tests)."""
    client, worker, stub = make_client(start=False)
    with client:
        yield client, worker, stub


def make_job_on_disk(*, clips: int = 2, langs=("en", "hi"), status: str = "done", final: bool = True,
                     script: bool = True, cut: bool = True, voice: bool = True, error: str | None = None,
                     job_id: str | None = None) -> JobStore:
    """A finished-looking job written by the test (the test thread is the writer, no worker involved)."""
    store = JobStore.create("https://example.com/v.mp4")
    if job_id:
        import shutil

        old = store.dir
        store.job.id = job_id
        store.dir = old.parent / job_id
        store.dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(old)
    job = store.job
    job.status = status  # type: ignore[assignment]
    job.error = error
    for s in JOB_STAGES:
        job.stages[s] = StageState(status="done", progress=1.0, message="ok")
    for i in range(clips):
        cs = ClipState(index=i, moment=Moment(start=10.0 * i, end=10.0 * i + 12.5, hook=f"hook {i}",
                                               reason="r", score=0.9))
        cdir = store.clip_dir(i)
        if cut:
            (cdir / "cut.mp4").write_bytes(b"cut" * 100)
            cs.cut = store.rel(cdir / "cut.mp4")
        for lang in langs:
            out = store.clip_dir(i, lang)
            st = ClipLangState(stages={s: StageState(status="done", progress=1.0) for s in CLIP_STAGES})
            narration = "यह एक परीक्षण है" if lang == "hi" else "this is a test narration"
            if script:
                (out / "script.json").write_text(json.dumps({
                    "narration": narration, "title": f"Title {lang}", "description": "desc",
                    "hashtags": ["#a", "#b"]}, ensure_ascii=False), encoding="utf-8")
            if voice:
                (out / "voice.wav").write_bytes(b"RIFF" + b"\0" * 100)
                (out / "voice.json").write_text(json.dumps({
                    "path": str(out / "voice.wav"), "narration": narration,
                    "narration_hash": narration_hash(narration), "raw_duration": 5.0, "final_duration": 5.0,
                    "clip_duration": 12.5, "tempo": 1.0, "shortened": False, "trimmed": False}), encoding="utf-8")
            if final:
                (out / "final.mp4").write_bytes(bytes(range(256)) * 40)  # 10240 bytes, known content
                st.final = store.rel(out / "final.mp4")
                (out / "metadata.json").write_text(json.dumps({
                    "language": lang, "title": f"Title {lang}", "description": "desc", "hashtags": ["#a"]}),
                    encoding="utf-8")
            cs.langs[lang] = st
        job.clips.append(cs)
    store.save()
    return store


def age_job_json(job_id: str, seconds: float = 120) -> None:
    import os

    from autoclipper.jobstore import jobs_root

    p = jobs_root() / job_id / "job.json"
    t = time.time() - seconds
    os.utime(p, (t, t))


def assert_error_shape(resp, status: int, message: str | None = None) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"message", "errors"}
    assert isinstance(body["message"], str) and body["message"]
    assert isinstance(body["errors"], dict)
    for k, v in body["errors"].items():
        assert isinstance(k, str) and isinstance(v, list) and all(isinstance(m, str) for m in v)
    assert "Traceback" not in resp.text
    if message is not None:
        assert message in body["message"], body
    return body


TASK_KEYS = {"id": str, "kind": str, "job_id": str, "clip": (int, type(None)), "lang": (str, type(None)),
             "state": str, "position": (int, type(None)), "error": (str, type(None)),
             "created_at": float, "started_at": (float, type(None)), "finished_at": (float, type(None)),
             "platforms": (list, type(None))}  # step 4: publish tasks list their platforms, else null

SUMMARY_KEYS = {"id": str, "source": str, "status": str, "error": (str, type(None)), "created_at": float,
                "clip_count": int, "langs": list, "rendered": int, "failed": int, "interrupted": bool,
                "task": (dict, type(None))}


def assert_keys(d: dict, spec: dict) -> None:
    assert set(d) == set(spec), (sorted(d), sorted(spec))
    for k, t in spec.items():
        assert isinstance(d[k], t), (k, d[k])
        if t is int or t == (int, type(None)):
            assert not isinstance(d[k], bool), k


def assert_task(d: dict) -> None:
    assert_keys(d, TASK_KEYS)
    assert len(d["id"]) == 32
    assert d["kind"] in ("run_job", "rerender", "publish")
    assert (d["platforms"] is None) == (d["kind"] != "publish")
    assert d["state"] in ("queued", "running", "done", "failed", "cancelled")


def assert_summary(d: dict) -> None:
    assert_keys(d, SUMMARY_KEYS)
    assert d["status"] in ("queued", "running", "done", "failed")
    if d["task"] is not None:
        assert_task(d["task"])


def wait_for(pred, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def read_json(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))
