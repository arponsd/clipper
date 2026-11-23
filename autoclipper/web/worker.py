"""The single background worker (CONTRACT_WEB section 5).

One daemon thread takes tasks from an in-process FIFO deque and runs them one at a time. It is the only
writer of job.json for every job (CONTRACT 1a); the only other writer is `submit_job`, which creates a
brand-new job before it is enqueued. Request threads only read job.json.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from .. import pipeline
from ..config import Settings, data_dir, load_settings
from ..joblock import JobLocked, has_lock_file, hold_job_lock, live_lock
from ..jobstore import JobStore, jobs_root
from ..models import Job
from ..pipeline import Deps
from ..publish import runner as publish_runner
from ..stages._atomic import write_text_atomic

log = logging.getLogger("autoclipper.web.worker")

TaskKind = Literal["run_job", "rerender", "publish"]
TaskState = Literal["queued", "running", "done", "failed", "cancelled"]
FINISHED: tuple[str, ...] = ("done", "failed", "cancelled")
# seconds: with NO job.lock (an older CLI), a younger job.json may belong to a running CLI process
RECOVER_MIN_AGE = 30.0
INTERRUPTED = "interrupted"


@dataclass
class Task:
    id: str
    kind: TaskKind
    job_id: str
    clip: int | None = None
    lang: str | None = None
    state: TaskState = "queued"
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    platforms: list[str] | None = None  # publish tasks only
    force: bool = False  # publish tasks only
    idempotency_key: str | None = field(default=None, repr=False)  # publish tasks only (not in to_dict)
    # run_job only: switch the job's voice-over mode to this (PUT /api/jobs/{id}/mode); None = keep it
    voiceover: bool | None = field(default=None, repr=False)
    # publish only (G6): the schedule this task fires, passed to run_publish as `schedule=`
    schedule: dict | None = field(default=None, repr=False)

    def to_dict(self, position: int | None) -> dict:
        d = asdict(self)
        d["position"] = position
        d["platforms"] = list(self.platforms) if self.platforms is not None else None
        return {k: d[k] for k in ("id", "kind", "job_id", "clip", "lang", "state", "position", "error",
                                  "created_at", "started_at", "finished_at", "platforms")}


@dataclass
class JobOptions:
    clips: int | None = None
    min_seconds: float | None = None
    max_seconds: float | None = None
    langs: list[str] | None = None
    voiceover: bool | None = None  # the job's mode (the API fills in the setting); None = keep job.json's

    def to_dict(self) -> dict:
        d = {"clips": self.clips, "min_seconds": self.min_seconds, "max_seconds": self.max_seconds,
             "langs": list(self.langs) if self.langs is not None else None}
        if self.voiceover is not None:
            d["voiceover"] = self.voiceover
        return d


class TaskCancelled(BaseException):
    """Raised from `log` when the task's cancel flag is set. BaseException: the pipeline records it as
    "interrupted" and re-raises it."""


class WorkerConflict(Exception):
    """A request conflicts with a queued/running task (the API maps it to 409)."""


def uploads_root() -> Path:
    return data_dir() / "web" / "uploads"


def options_path(job_id: str) -> Path:
    return jobs_root() / job_id / "options.json"


def read_options(job_id: str) -> JobOptions | None:
    p = options_path(job_id)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    langs = data.get("langs")
    vo = data.get("voiceover")
    return JobOptions(clips=data.get("clips"), min_seconds=data.get("min_seconds"),
                      max_seconds=data.get("max_seconds"),
                      langs=list(langs) if isinstance(langs, list) else None,
                      voiceover=vo if isinstance(vo, bool) else None)


def read_job(job_id: str) -> Job | None:
    """Read-only view of job.json (never a JobStore: request threads must not write)."""
    p = jobs_root() / job_id / "job.json"
    for attempt in range(3):
        try:
            return Job.model_validate_json(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(0.05)
    return None  # pragma: no cover


def _first_line(e: BaseException) -> str:
    text = str(e).strip() or type(e).__name__
    return text.splitlines()[0][:500]


class Worker:
    def __init__(self, *,
                 settings_loader: Callable[[], Settings] = load_settings,
                 deps_factory: Callable[[Settings], Deps | None] = lambda s: None,
                 run_job: Callable[..., Any] = pipeline.run_job,
                 rerender: Callable[..., Any] = pipeline.rerender,
                 publish: Callable[..., Any] | None = None,
                 history: int = 20):
        self._settings_loader = settings_loader
        self._deps_factory = deps_factory
        self._run_job = run_job
        self._rerender = rerender
        self._publish = publish if publish is not None else publish_runner.run_publish
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._tasks: dict[str, Task] = {}
        self._queue: deque[Task] = deque()
        self._running: Task | None = None
        self._recent: deque[Task] = deque(maxlen=max(1, history))
        self._cancel: dict[str, threading.Event] = {}
        self._finalize: list[str] = []  # job ids whose queued run_job was cancelled (job.json -> interrupted)
        self._finalizing = False
        self._thread: threading.Thread | None = None
        self._stopping = False
        self.scheduler: Any = None  # G6: web.scheduler.Scheduler, started/stopped with the worker

    # ---- lifecycle ------------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
        self.recover_interrupted()
        self._clean_partial_uploads()
        t = threading.Thread(target=self._loop, name="autoclipper-worker", daemon=True)
        with self._lock:
            self._thread = t
        t.start()
        if self.scheduler is not None:  # G6
            self.scheduler.start()

    def begin_stop(self) -> None:
        """Set the stopping flag and cancel the running task, without waiting (safe from a signal handler:
        it only takes the worker lock briefly). The running task then ends as cancelled/interrupted."""
        with self._cond:
            self._stopping = True
            if self._running is not None:
                ev = self._cancel.get(self._running.id)
                if ev is not None:
                    ev.set()
            self._cond.notify_all()

    @property
    def stopping(self) -> bool:
        return self._stopping

    def stop(self, timeout: float = 10.0) -> None:
        if self.scheduler is not None:  # G6
            self.scheduler.stop()
        self.begin_stop()
        with self._lock:
            t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        if t is not None and t.is_alive():
            return  # the thread still owns job.json writes; it was told to stop
        # The worker thread is gone, so this thread is now the only writer (WORKER-1): finish cancelled
        # queued jobs and mark every still-queued run_job interrupted, so the UI offers Resume.
        with self._cond:
            finalize, self._finalize = self._finalize, []
            dropped = list(self._queue)
            self._queue.clear()
            now = time.time()
            for task in dropped:
                task.state = "cancelled"
                task.error = "server stopped"
                task.finished_at = now
                self._cancel.pop(task.id, None)
                self._remember(task)
            self._cond.notify_all()
        for job_id in finalize:
            self._safe_finalize(job_id)
        for task in dropped:
            if task.kind == "run_job":
                try:
                    self._mark_interrupted(task.job_id, only_if=("queued",))
                except Exception:  # noqa: BLE001
                    log.exception("could not mark job %s interrupted", task.job_id)
        if self.scheduler is not None and hasattr(self.scheduler, "release"):  # G6: after the last publish
            self.scheduler.release()

    @property
    def alive(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def recover_interrupted(self) -> list[str]:
        marked: list[str] = []
        now = time.time()
        for job_id in JobStore.list_ids():
            path = jobs_root() / job_id / "job.json"
            try:
                job = Job.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if job.status not in ("running", "queued"):
                continue
            with self._lock:
                if any(t.job_id == job_id and t.state not in FINISHED for t in self._tasks.values()):
                    continue
            lock = live_lock(job_id)
            if lock is not None:
                log.warning("job %s is %s and process %s holds its job.lock; not marking it interrupted",
                            job_id, job.status, lock.get("pid"))
                continue
            if not has_lock_file(job_id):  # no lock at all (older CLI): fall back to the mtime heuristic
                try:
                    age = now - path.stat().st_mtime
                except OSError:
                    continue
                if age < RECOVER_MIN_AGE:
                    log.warning("job %s is %s and job.json changed %.0f s ago; a CLI process may be running "
                                "it, not marking it interrupted", job_id, job.status, age)
                    continue
            if self._mark_interrupted(job_id, only_if=("running", "queued")):
                marked.append(job_id)
        if marked:
            log.warning("marked interrupted jobs: %s", ", ".join(marked))
        return marked

    def _mark_interrupted(self, job_id: str, *, only_if: tuple[str, ...], error: str = INTERRUPTED) -> bool:
        try:
            store = JobStore.load(job_id)
        except (FileNotFoundError, OSError, ValueError):
            return False
        job = store.job
        if job.status not in only_if:
            return False
        states = list(job.stages.values())
        for c in job.clips:
            for ls in c.langs.values():
                states.extend(ls.stages.values())
        for st in states:
            if st.status == "running":
                st.status = "failed"
                st.message = INTERRUPTED
        job.status = "failed"
        job.error = error
        store.save()
        return True

    def _clean_partial_uploads(self) -> None:
        root = uploads_root()
        if not root.is_dir():
            return
        for d in root.iterdir():
            if not d.is_dir():
                continue
            for p in d.glob("*.part*"):
                try:
                    p.unlink()
                except OSError:
                    pass
            try:
                next(d.iterdir())
            except StopIteration:
                try:
                    d.rmdir()
                except OSError:
                    pass
            except OSError:
                pass

    # ---- submit ---------------------------------------------------------------------------
    def _enqueue(self, task: Task) -> None:
        # caller holds self._lock
        self._tasks[task.id] = task
        self._cancel[task.id] = threading.Event()
        self._queue.append(task)
        self._cond.notify_all()

    def submit_job(self, source: str, *, options: JobOptions, upload: Path | None = None,
                   voiceover_default: bool = True) -> Task:
        """``voiceover_default`` (the `voiceover` setting) is stored as the job's mode when the options
        have none, so a later Settings change never switches this job."""
        vo = options.voiceover if options.voiceover is not None else bool(voiceover_default)
        # the one request-thread write: a brand-new job (CONTRACT 1a), its voice-over mode fixed in it
        store = JobStore.create(source, voiceover=vo)
        job_id = store.job.id
        try:
            if upload is not None:
                upload = Path(upload)
                os.replace(upload, store.dir / f"source{upload.suffix.lower()}")
                updir = upload.parent
                (updir / "upload.json").unlink(missing_ok=True)
                try:
                    updir.rmdir()
                except OSError:
                    pass
            write_text_atomic(options_path(job_id), json.dumps(options.to_dict(), ensure_ascii=False, indent=2))
            task = Task(id=uuid.uuid4().hex, kind="run_job", job_id=job_id)
            with self._lock:
                self._enqueue(task)
            return task
        except BaseException as e:
            store.job.status = "failed"
            store.job.error = f"could not queue the job: {_first_line(e)}"
            store.save()
            raise

    def submit_resume(self, job_id: str, *, voiceover: bool | None = None,
                      current: bool | None = None) -> tuple[Task, bool]:
        """Queue a run_job (resume). ``voiceover`` switches the job's mode first (run_job stores it in
        job.json). ``current`` = the job's mode now: a run already queued/running ends in its own switch, or
        in ``current`` when it switches nothing. Asking for the mode that run ends in returns it (no new
        task); asking for the other mode is a WorkerConflict."""
        with self._lock:
            existing = self._active(job_id, kind="run_job")
            if existing is not None:
                ends_in = existing.voiceover if existing.voiceover is not None else current
                asked = voiceover if voiceover is not None else current
                if asked is not None and ends_in is not None and asked != ends_in:
                    raise WorkerConflict("the job is queued or running in the other mode; change the mode when "
                                         "it finishes")
                if voiceover is not None and ends_in is None and existing.voiceover != voiceover:
                    raise WorkerConflict("the job is queued or running; change the mode when it finishes")
                return existing, False
            task = Task(id=uuid.uuid4().hex, kind="run_job", job_id=job_id, voiceover=voiceover)
            self._enqueue(task)
            return task, True

    def submit_rerender(self, job_id: str, clip: int, lang: str) -> tuple[Task, bool]:
        with self._lock:
            if self._active(job_id, kind="run_job") is not None:
                raise WorkerConflict("the job is queued or running; re-render when it finishes")
            if self._active(job_id, kind="publish", clip=clip, lang=lang) is not None:
                raise WorkerConflict("clip is being published; re-render when it finishes")
            for t in self._queue:
                if t.kind == "rerender" and t.job_id == job_id and t.clip == clip and t.lang == lang:
                    return t, False
            task = Task(id=uuid.uuid4().hex, kind="rerender", job_id=job_id, clip=clip, lang=lang)
            self._enqueue(task)
            return task, True

    def submit_publish(self, job_id: str, clip: int, lang: str, platforms: list[str], *, force: bool = False,
                       idempotency_key: str | None = None, schedule: dict | None = None) -> tuple[Task, bool]:
        """Queue a live publish of one clip-lang. Same active clip-lang + same platform set -> (it, False);
        a different set, a run_job of the job or a rerender of the clip-lang -> WorkerConflict."""
        wanted = list(platforms)
        with self._lock:
            if self._active(job_id, kind="run_job") is not None:
                raise WorkerConflict("the job is queued or running; publish when it finishes")
            if self._active(job_id, kind="rerender", clip=clip, lang=lang) is not None:
                raise WorkerConflict("clip is being rendered; publish when it finishes")
            existing = self._active(job_id, kind="publish", clip=clip, lang=lang)
            if existing is not None:
                if sorted(existing.platforms or []) == sorted(wanted) and existing.force == force:
                    return existing, False
                raise WorkerConflict("clip is already being published to "
                                     f"{', '.join(existing.platforms or [])}; wait until it finishes")
            task = Task(id=uuid.uuid4().hex, kind="publish", job_id=job_id, clip=clip, lang=lang,
                        platforms=wanted, force=force, idempotency_key=idempotency_key, schedule=schedule)
            self._enqueue(task)
            return task, True

    def active_publish(self, job_id: str, clip: int, lang: str) -> Task | None:
        with self._lock:
            return self._active(job_id, kind="publish", clip=clip, lang=lang)

    @contextmanager
    def exclusive_publish(self, job_id: str, clip: int, lang: str) -> Iterator[None]:
        """Hold the worker lock while a request thread writes a clip-lang's dry-run plan files. Raises
        WorkerConflict when a run_job of the job, or a publish/rerender of the clip-lang, is queued/running."""
        with self._lock:
            if self._active(job_id, kind="run_job") is not None:
                raise WorkerConflict("the job is queued or running; try again when it finishes")
            if self._active(job_id, kind="publish", clip=clip, lang=lang) is not None:
                raise WorkerConflict("clip is being published; try again when it finishes")
            if self._active(job_id, kind="rerender", clip=clip, lang=lang) is not None:
                raise WorkerConflict("clip is being rendered; try again when it finishes")
            yield

    # ---- cancel / query -------------------------------------------------------------------
    def cancel_ex(self, task_id: str) -> tuple[Task | None, str]:
        """-> (task, outcome) with outcome "removed" (was queued), "requested" (running) or "finished"."""
        with self._cond:
            task = self._tasks.get(task_id)
            if task is None:
                return None, "unknown"
            if task.state == "queued":
                try:
                    self._queue.remove(task)
                except ValueError:
                    pass
                task.state = "cancelled"
                task.finished_at = time.time()
                self._cancel.pop(task.id, None)
                self._remember(task)
                if task.kind == "run_job":
                    self._finalize.append(task.job_id)
                self._cond.notify_all()
                return task, "removed"
            if task.state == "running":
                self._cancel[task.id].set()
                return task, "requested"
            return task, "finished"

    def cancel(self, task_id: str) -> Task | None:
        return self.cancel_ex(task_id)[0]

    def cancel_if_queued(self, task_id: str) -> str:
        """G6: remove a task only while it still waits in the queue; never signals a running task.
        -> "removed", "running", "finished" or "unknown" (not a task of this server)."""
        with self._cond:
            task = self._tasks.get(task_id)
            if task is None:
                return "unknown"
            if task.state != "queued":
                return "running" if task.state == "running" else "finished"
            # same removal as cancel_ex, under the same lock hold, so it cannot start in between
            try:
                self._queue.remove(task)
            except ValueError:
                pass
            task.state = "cancelled"
            task.finished_at = time.time()
            self._cancel.pop(task.id, None)
            self._remember(task)
            if task.kind == "run_job":
                self._finalize.append(task.job_id)
            self._cond.notify_all()
            return "removed"

    def status(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def _active(self, job_id: str, *, kind: str | None = None, clip: int | None = None,
                lang: str | None = None) -> Task | None:
        # caller holds self._lock; running first, then the earliest queued
        candidates = ([self._running] if self._running is not None else []) + list(self._queue)
        for t in candidates:
            if t.job_id != job_id or (kind is not None and t.kind != kind):
                continue
            if clip is not None and t.clip != clip:
                continue
            if lang is not None and t.lang != lang:
                continue
            return t
        return None

    def active_task_for(self, job_id: str, clip: int | None = None, lang: str | None = None) -> Task | None:
        with self._lock:
            if clip is None and lang is None:
                return self._active(job_id, kind="run_job") or self._active(job_id)
            return self._active(job_id, kind="rerender", clip=clip, lang=lang)

    def active_run_job(self, job_id: str) -> Task | None:
        with self._lock:
            return self._active(job_id, kind="run_job")

    def _position(self, task: Task) -> int | None:
        # caller holds self._lock
        if task is self._running:
            return 0
        if task.state == "queued":
            try:
                return self._queue.index(task) + 1
            except ValueError:
                return None
        return None

    def position(self, task_id: str) -> int | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return None if task is None else self._position(task)

    def task_dict(self, task: Task | None) -> dict | None:
        if task is None:
            return None
        with self._lock:
            return task.to_dict(self._position(task))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self._running.to_dict(0) if self._running is not None else None,
                "queued": [t.to_dict(i + 1) for i, t in enumerate(self._queue)],
                "recent": [t.to_dict(None) for t in self._recent],
            }

    def wait_idle(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._queue or self._running is not None or self._finalize or self._finalizing:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(min(left, 0.1))
            return True

    @contextmanager
    def exclusive(self, job_id: str, clip: int, lang: str) -> Iterator[None]:
        """Hold the worker lock while a request thread writes a clip-lang's script files: no task can start
        meanwhile. Raises WorkerConflict when run_job for the job or a rerender of the clip-lang is running."""
        with self._lock:
            r = self._running
            if r is not None and r.job_id == job_id and r.kind != "publish" and (
                    r.kind == "run_job" or (r.clip == clip and r.lang == lang)):
                raise WorkerConflict("clip is being rendered; try again when it finishes")
            yield

    # ---- the thread -----------------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._finalize and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
                finalize, self._finalize = self._finalize, []
                self._finalizing = bool(finalize)
                task = None
                if not finalize:
                    task = self._queue.popleft()
                    task.state = "running"
                    task.started_at = time.time()
                    self._running = task
            for job_id in finalize:
                self._safe_finalize(job_id)
            if finalize:
                with self._cond:
                    self._finalizing = False
                    self._cond.notify_all()
                continue
            assert task is not None
            try:
                self._execute(task)
            except BaseException as e:  # never let anything kill the thread
                log.exception("worker task %s crashed", task.id)
                task.state = "failed"
                task.error = _first_line(e)
            finally:
                with self._cond:
                    task.finished_at = time.time()
                    if task.state not in FINISHED:
                        task.state = "failed"
                    self._running = None
                    self._cancel.pop(task.id, None)
                    self._remember(task)
                    self._cond.notify_all()

    def _remember(self, task: Task) -> None:
        """Put a finished task in the history; a finished task that falls out of it is forgotten (WORKER-4).
        Caller holds self._lock."""
        dropped = self._recent[-1] if len(self._recent) == self._recent.maxlen else None
        self._recent.appendleft(task)
        if dropped is None or dropped is task or dropped in self._recent:
            return
        if dropped.state in FINISHED and dropped is not self._running and dropped not in self._queue:
            self._tasks.pop(dropped.id, None)
            self._cancel.pop(dropped.id, None)

    def live_owner(self, job_id: str) -> dict | None:
        """The job.lock of ANOTHER live owner of the job (a CLI run), else None. While this worker has a
        task for the job, the lock (if any) is the worker's own."""
        with self._lock:
            if self._active(job_id) is not None:
                return None
        return live_lock(job_id)

    def _safe_finalize(self, job_id: str) -> None:
        try:
            # A cancelled queued job never ran: mark it so the UI offers Resume (worker thread = writer).
            with self._lock:
                if self._active(job_id, kind="run_job") is not None:
                    return
            self._mark_interrupted(job_id, only_if=("queued",))
        except Exception:  # noqa: BLE001
            log.exception("could not finalize cancelled job %s", job_id)

    def _execute(self, task: Task) -> None:
        flag = self._cancel.get(task.id) or threading.Event()
        job_dir = jobs_root() / task.job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        with open(job_dir / "web.log", "a", encoding="utf-8") as logf:
            def log_line(line: str) -> None:
                text = str(line)
                if text:  # an empty line is only a cancel point (publish between chunks/polls)
                    try:
                        logf.write(text.rstrip("\n") + "\n")
                        logf.flush()
                    except (OSError, ValueError):
                        pass
                    log.info("%s", text)
                # Raise once; while the pipeline handles that TaskCancelled (its except BaseException
                # blocks log too), further calls must not raise again or job.json would not be saved.
                if flag.is_set() and not isinstance(sys.exc_info()[1], TaskCancelled):
                    raise TaskCancelled()

            log_line.cancel_point = True  # type: ignore[attr-defined]  # pipeline._Reporter.cancel_point

            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            if task.kind == "run_job":
                what = "run" if task.voiceover is None else (
                    "run (switch to voice-over)" if task.voiceover else "run (switch to captions only)")
            elif task.kind == "publish":
                what = f"publish {task.clip:02d}/{task.lang} {','.join(task.platforms or [])}"
            else:
                what = f"rerender {task.clip:02d}/{task.lang}"
            try:
                logf.write(f"--- {stamp} task {task.id} {what}\n")
                logf.flush()
            except OSError:
                pass
            try:
                if flag.is_set():
                    raise TaskCancelled()
                publish_failure: str | None = None
                with hold_job_lock(task.job_id):
                    settings = self._settings_loader()
                    deps = self._deps_factory(settings) if task.kind != "publish" else None
                    if task.kind == "publish":
                        results = self._publish(task.job_id, task.clip, task.lang,
                                                platforms=list(task.platforms or []), force=task.force,
                                                settings=settings, log=log_line,
                                                idempotency_key=task.idempotency_key,
                                                **({"schedule": task.schedule} if task.schedule else {}))
                        publish_failure = publish_runner.all_failed(results if isinstance(results, dict) else {})
                    elif task.kind == "run_job":
                        job = read_job(task.job_id)
                        o = read_options(task.job_id) or JobOptions()
                        langs = o.langs
                        if langs is None and job is not None and job.clips and \
                                not options_path(task.job_id).exists():
                            langs = sorted({lang for c in job.clips for lang in c.langs}) or None
                        # the voice-over mode is only passed when the task switches it (PUT .../mode):
                        # job.json holds it (fixed at creation, or switched), and run_job keeps it.
                        extra = {"voiceover": task.voiceover} if task.voiceover is not None else {}
                        self._run_job(job.source if job is not None else "", settings=settings, deps=deps,
                                      langs=langs, count=o.clips, min_seconds=o.min_seconds,
                                      max_seconds=o.max_seconds, job_id=task.job_id, log=log_line, **extra)
                    else:
                        self._rerender(task.job_id, task.clip, task.lang, settings=settings, deps=deps,
                                       log=log_line)
                if self._stop_requested(flag):
                    self._cancelled_by_stop(task, logf)
                elif publish_failure is not None:
                    task.state = "failed"
                    task.error = _first_line(Exception(publish_failure))
                else:
                    task.state = "done"
            except JobLocked as e:  # another live process owns the job: do not touch its job.json
                task.state = "failed"
                task.error = _first_line(e)
                log_line_safe(logf, f"[{task.job_id}] task failed: {task.error}")
            except TaskCancelled:
                task.state = "cancelled"
                task.error = "cancelled"
                self._after_run_job(task, INTERRUPTED)
            except Exception as e:  # PipelineError or anything else: the task fails, the thread lives
                if self._stop_requested(flag):
                    # e.g. Ctrl-C killed the ffmpeg child: report the stop, not an ffmpeg error (WORKER-3)
                    log_line_safe(logf, f"[{task.job_id}] stopped ({_first_line(e)})")
                    self._cancelled_by_stop(task, logf)
                    return
                task.state = "failed"
                task.error = _first_line(e)
                log_line_safe(logf, f"[{task.job_id}] task failed: {task.error}")
                self._after_run_job(task, task.error)

    def _stop_requested(self, flag: threading.Event) -> bool:
        return self._stopping or flag.is_set()

    def _cancelled_by_stop(self, task: Task, logf: Any) -> None:
        task.state = "cancelled"
        task.error = "cancelled"
        if task.kind == "run_job":
            try:
                # queued too: a run that never reached the pipeline's own job.json writes
                self._mark_interrupted(task.job_id, only_if=("queued", "running"))
            except Exception:  # noqa: BLE001
                log.exception("could not mark job %s interrupted", task.job_id)

    def _after_run_job(self, task: Task, error: str) -> None:
        """If a run_job ended before the pipeline could record it (settings/deps failure, a stub, a
        cancel before start), job.json would stay queued/running: mark it failed (worker = writer)."""
        if task.kind != "run_job":
            return
        try:
            self._mark_interrupted(task.job_id, only_if=("queued", "running"),
                                   error=INTERRUPTED if error == INTERRUPTED else error)
        except Exception:  # noqa: BLE001
            log.exception("could not mark job %s failed", task.job_id)


def log_line_safe(logf: Any, line: str) -> None:
    try:
        logf.write(line + "\n")
        logf.flush()
    except (OSError, ValueError):
        pass


def remove_tree_quietly(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
