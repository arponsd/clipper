"""G6 app-side scheduler: a small thread next to the worker that fires due schedule items as normal
`publish` tasks (Worker.submit_publish) and follows them to the end.

Every tick (and once right at start-up, which is how missed schedules are handled after a restart):
- `scheduled` item due (native: its hand-off time; app: the publish time) -> the same checks as Publish now
  (dry run off, connected, enabled, limits, final.mp4, account mapping) plus the daily limit, then one
  publish task per schedule and mode with a stable Idempotency-Key `schedule-<id>-<platforms>`.
- a due time more than `grace_minutes` in the past -> `missed`, with the time and how late it is.
- `queued`/`running` -> follows the task; when it ends, the platform's status file decides
  (published -> `done`, or `handed_off` for native scheduling until the time; else `failed`). A task lost
  in a restart is fired again within the grace window: the published record and the resume records of
  the publish runner make that safe (never two posts).
The store is changed only under `schedules.update` (thread + file lock).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from ..config import Settings, load_settings
from ..publish import PLATFORM_NAMES, accounts, records, schedules
from ..publish.service import PublishConflict, PublishInputError, PublishNotFound, check_live
from .worker import FINISHED, Worker, WorkerConflict

log = logging.getLogger("autoclipper.web.scheduler")


def _key(sid: str, platform: str) -> str:
    return f"schedule-{sid}-{platform}"


class Scheduler:
    def __init__(self, worker: Worker, *, settings_loader: Callable[[], Settings] = load_settings,
                 clock: Callable[[], float] = time.time, interval: float | None = None):
        self.worker = worker
        self._settings_loader = settings_loader
        self._clock = clock
        self._interval = interval
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._owner_fd: int | None = None
        self._owner_lock = threading.Lock()
        self.ticks = 0
        self.blocked: str | None = None  # why this server's scheduler does not fire (another one owns the data)

    # ---- thread ---------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        t = threading.Thread(target=self._loop, name="autoclipper-scheduler", daemon=True)
        self._thread = t
        t.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        if not getattr(self.worker, "alive", False):
            self.release()  # else Worker.stop releases it once its last publish task has ended

    # ---- one scheduler per data folder (two `serve` on one data dir must never fire one item twice) ----
    def _own(self) -> bool:
        with self._owner_lock:
            if self._owner_fd is None:
                self._owner_fd = schedules.try_owner_lock()
                if self._owner_fd is None:
                    if self.blocked is None:
                        log.warning("another Auto Clipper server owns the schedules of this data folder; "
                                    "this one does not fire them")
                    self.blocked = ("another Auto Clipper server on the same data folder runs the schedules; "
                                    "this one does not fire them")
                    return False
                self.blocked = None
            return True

    def release(self) -> None:
        with self._owner_lock:
            fd, self._owner_fd = self._owner_fd, None
        schedules.release_owner_lock(fd)

    @property
    def owner(self) -> bool:
        return self._owner_fd is not None

    def kick(self) -> None:
        """Run a tick soon (after a create/edit/cancel)."""
        self._wake.set()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self.owner

    def _loop(self) -> None:
        while not self._stop.is_set():
            interval = 15.0
            try:
                settings = self._settings_loader()
                interval = float(self._interval or settings.publish.schedule.check_interval_seconds)
                if self._own():
                    self.tick(settings)
            except Exception:  # noqa: BLE001 - the scheduler must never die
                log.exception("scheduler tick failed")
            self._wake.wait(interval)
            self._wake.clear()

    # ---- one pass -------------------------------------------------------------------------
    def tick(self, settings: Settings | None = None) -> None:
        if not schedules.store_path().exists():
            self.ticks += 1
            return  # nothing was ever scheduled
        settings = settings or self._settings_loader()
        now = self._clock()

        def fn(rows: list[dict]) -> None:
            for s in rows:
                due: dict[bool, list[str]] = {True: [], False: []}  # native? -> platforms to fire now
                for p in list(s.get("items") or {}):
                    try:
                        native = self._step(rows, s, p, settings, now)
                        if native is not None and self._ready(rows, s, p, settings, now):
                            due[native].append(p)
                    except Exception as e:  # noqa: BLE001 - one bad item never blocks the others
                        log.exception("schedule %s %s failed", s.get("id"), p)
                        self._end(s, p, "failed", f"scheduler error: {type(e).__name__}", now)
                # one publish task per schedule and mode, so the platforms of a schedule go out together
                for native, platforms in due.items():
                    if platforms:
                        self._submit(s, platforms, settings, now, native=native)

        schedules.update(fn)
        self.ticks += 1

    def _end(self, s: dict, p: str, state: str, message: str, now: float, **extra: Any) -> None:
        it = s["items"][p]
        it.update(state=state, message=message[:500], finished_at=now, **extra)
        s["updated_at"] = now
        log.info("schedule %s %s/%s %s: %s", s.get("id"), s.get("lang"), p, state, message)

    def _step(self, rows: list[dict], s: dict, p: str, settings: Settings, now: float) -> bool | None:
        """Advance one item. -> None, or whether a due item fires natively (True) or app-side (False)."""
        it = s["items"][p]
        state = it.get("state")
        t = float(s["publish_at"])
        label = s.get("timezone") or ""
        if state in ("queued", "running"):
            return self._follow(rows, s, p, settings, now)
        if state == "handed_off":
            if now >= t:
                self._end(s, p, "done", f"handed to {PLATFORM_NAMES[p]}, which publishes it at "
                                        f"{schedules.fmt_local(t, label)} (native scheduling; Auto Clipper does "
                                        "not re-check the platform - open the link to see it)", now)
            return None
        if state != "scheduled":
            return None
        grace = int(settings.publish.schedule.grace_minutes) * 60
        if now > t + grace:
            self._end(s, p, "missed", f"missed: it was due at {schedules.fmt_local(t, label)} but Auto Clipper "
                                      f"was not running (late by {schedules._fmt_dur(now - t)}, grace "
                                      f"{grace // 60} min); nothing was posted - publish it now or schedule "
                                      "it again", now)
            return None
        if it.get("mode") == "native":
            if now < float(it.get("handoff_at") or t):
                return None
            reason = None
            if not schedules.native_window_ok(p, t - now):
                reason = "too close to the time for native scheduling"
            elif settings.publish.dry_run:
                # not a failure yet: the time is still ahead and dry run is checked again then
                reason = "dry run was on at the hand-off time"
            elif p == "youtube" and getattr(settings.publish.target(s["lang"], p), "privacy", "private") != "public":
                reason = "the YouTube privacy is no longer public (YouTube only schedules public videos)"
            if reason:
                it.update(mode="app", handoff_at=t,
                          mode_note=f"{reason}: Auto Clipper posts it itself at the time (if dry run is off "
                                    "then; keep `autoclipper serve` running)",
                          message="waiting for the time")
                s["updated_at"] = now
        if it.get("mode") == "app" and now < t:
            return None
        return it.get("mode") == "native"

    def _ready(self, rows: list[dict], s: dict, p: str, settings: Settings, now: float) -> bool:
        """The same checks as Publish now, at fire time; a failing item ends here with the reason."""
        t = float(s["publish_at"])
        label = s.get("timezone") or ""
        job_id, clip, lang = s["job_id"], int(s["clip"]), s["lang"]
        if settings.publish.dry_run:
            self._end(s, p, "failed", "dry run was on when the schedule was due; nothing was posted - turn "
                                      "dry run off and schedule it again, or publish from the clip page", now)
            return False
        try:
            _, already = check_live(job_id, clip, lang, [p], settings, force=False)
        except PublishConflict as e:
            msgs = (e.errors or {}).get(p) or [e.message]
            self._end(s, p, "failed", "not posted: " + "; ".join(str(m) for m in msgs), now)
            return False
        except (PublishNotFound, PublishInputError) as e:
            self._end(s, p, "failed", f"not posted: {getattr(e, 'message', str(e))}", now)
            return False
        if already:
            st = already.get(p) or {}
            self._end(s, p, "done", "already published earlier; not posted again", now,
                      remote_id=st.get("remote_id"), url=st.get("url"))
            return False
        acc = accounts.account(lang, p)
        limit = int(getattr(settings.publish.schedule.daily_limits, p))
        msg = schedules.limit_problem(rows, p, {"id": acc.get("id"), "name": acc.get("name")} if acc else None,
                                      limit, t, schedules.limit_label(settings), exclude=s.get("id"),
                                      states=("queued", "running"))
        if msg:
            self._end(s, p, "failed", "not posted: " + msg, now)
            return False
        return True

    def _submit(self, s: dict, platforms: list[str], settings: Settings, now: float, *, native: bool) -> None:
        t = float(s["publish_at"])
        grace = int(settings.publish.schedule.grace_minutes) * 60
        sched = {"id": s.get("id"), "publish_at": t, "native": native,
                 "plans": {p: schedules.schedule_plan_info(s, p) for p in platforms}}
        try:
            task, _created = self.worker.submit_publish(s["job_id"], int(s["clip"]), s["lang"], list(platforms),
                                                        idempotency_key=_key(s["id"], "-".join(platforms)),
                                                        schedule=sched)
        except WorkerConflict as e:
            for p in platforms:
                if now > t + grace:
                    self._end(s, p, "failed", f"not posted: the clip stayed busy ({e}) past the grace window", now)
                else:
                    s["items"][p]["message"] = f"waiting: {e}"
            return
        for p in platforms:
            s["items"][p].update(state="queued", task_id=task.id, fired_at=now,
                                 message=("handing over to " + PLATFORM_NAMES[p]) if native else "publishing")
        s["updated_at"] = now

    def _follow(self, rows: list[dict], s: dict, p: str, settings: Settings, now: float) -> bool | None:
        it = s["items"][p]
        t = float(s["publish_at"])
        task = self.worker.status(it["task_id"]) if it.get("task_id") else None
        if task is not None and task.state not in FINISHED:
            if it.get("state") != task.state and task.state in ("queued", "running"):
                it["state"] = task.state
                s["updated_at"] = now
            return None
        raw = records.read_status_raw(s["job_id"], int(s["clip"]), s["lang"], p)
        mine = bool(raw) and str(raw.get("idempotency_key") or "").startswith(_key(s["id"], ""))
        if records.is_published(raw):
            extra = {"remote_id": raw.get("remote_id"), "url": raw.get("url")}
            if raw.get("scheduled_for") and now < t:
                it.update(state="handed_off", message=str(raw.get("message") or "scheduled on the platform"),
                          **extra)
                s["updated_at"] = now
            else:
                self._end(s, p, "done", str(raw.get("message") or "published"), now, **extra)
            return None
        if task is None:  # lost in a restart (the worker forgets its tasks)
            grace = int(settings.publish.schedule.grace_minutes) * 60
            if mine and raw.get("state") == "failed":
                if not self._fallback_to_app(s, p, raw, str(raw.get("message") or "publish failed"), now):
                    self._end(s, p, "failed", str(raw.get("message") or "publish failed"), now)
            elif now <= t + grace:
                it.update(state="scheduled", task_id=None, message="fired again after a restart")
                return self._step(rows, s, p, settings, now)
            else:
                self._end(s, p, "failed", "interrupted by a restart and past the grace window; publish it "
                                          "again from the clip page (it resumes, never posts twice)", now)
            return None
        msg = (raw.get("message") if mine and raw else None) or task.error or task.state
        if self._fallback_to_app(s, p, raw if mine else None, str(msg), now):
            return None
        self._end(s, p, "failed", str(msg), now)
        return None

    def _fallback_to_app(self, s: dict, p: str, raw: dict | None, msg: str, now: float) -> bool:
        """A native hand-off that failed before anything can be live (no publishing request sent, and for
        YouTube no finished upload) goes back to waiting and is posted by Auto Clipper at the time."""
        it = s["items"][p]
        t = float(s["publish_at"])
        if it.get("mode") != "native" or now >= t:
            return False
        if records.resume_sent(s["job_id"], int(s["clip"]), s["lang"], p):
            return False
        err = (raw or {}).get("error") or {}
        if err.get("code") == "maybe_published" or (p == "youtube" and (raw or {}).get("remote_id")):
            return False
        it.update(state="scheduled", mode="app", handoff_at=t, task_id=None, fired_at=None,
                  mode_note=f"the hand-off to {PLATFORM_NAMES[p]} failed ({msg[:200]}): Auto Clipper posts it "
                            "itself at the time (keep `autoclipper serve` running)",
                  message="waiting for the time")
        s["updated_at"] = now
        log.info("schedule %s %s/%s: native hand-off failed, posting app-side at the time", s.get("id"),
                 s.get("lang"), p)
        return True
