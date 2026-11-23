"""G6 scheduled publishing: the schedule store and every check that is not the timer itself.

A schedule = one clip-lang, one time, one or more platforms. Each platform is an *item* with its own mode:
- `native`: handed to the platform `native_lead_minutes` before the time (YouTube `status.publishAt` with
  privacy private, Facebook `video_state=SCHEDULED` + `scheduled_publish_time`); the platform publishes it.
- `app`: Auto Clipper's scheduler (web/scheduler.py, in `autoclipper serve`) runs the normal publish task at
  the time (always for Instagram: its content publishing API has no scheduling).

Store: `data/schedules.json` (atomic writes) guarded by a thread lock + an OS file lock on
`data/schedules.lock`, so the CLI and the server can both edit it. Scheduling counts as the explicit
confirm: it needs dry run off when it is created AND when it fires, connected accounts and the same
pre-publish checks as Publish now. In dry-run, `preview` only returns (and writes) the plans.
Contract: docs/CONTRACT_STEP4.md section 15.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, Iterator

from ..config import Settings, data_dir
from ..stages._atomic import write_text_atomic
from . import LANG_NAMES, PLATFORM_NAMES, accounts, records
from .service import (PublishConflict, PublishInputError, PublishNotFound, build_plans, check_live,
                      check_platforms, load_item, read_metadata, _check_clip)

ITEM_STATES = ("scheduled", "queued", "running", "handed_off", "done", "failed", "missed", "cancelled")
WAITING = ("scheduled",)
IN_FLIGHT = ("queued", "running")
ACTIVE = ("scheduled", "queued", "running", "handed_off")
SCHEDULE_STATES = ("upcoming", "done", "missed", "failed", "cancelled")
MIN_AHEAD_SECONDS = 120
MAX_AHEAD_DAYS = 90
YT_NATIVE_MIN_SECONDS = 120
FB_NATIVE_MIN_SECONDS = 15 * 60  # Facebook needs > 10 minutes; 5 minutes of margin for the upload
FB_NATIVE_MAX_SECONDS = 29 * 24 * 3600 - 3600
ID_RE = re.compile(r"\As_[0-9a-f]{12}\Z")
TIME_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:?\d{2})?\Z")
TZ_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_+-]{0,40}(/[A-Za-z0-9_+-]{1,40}){0,2}\Z")
PUBLIC_ITEM_KEYS = ("platform", "mode", "mode_note", "handoff_at", "account", "state", "message", "task_id",
                    "fired_at", "finished_at", "remote_id", "url", "cancellable")

_lock = threading.RLock()


# ---------------------------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------------------------
def store_path() -> Path:
    return data_dir() / "schedules.json"


def _lock_path() -> Path:
    return data_dir() / "schedules.lock"


@contextmanager
def _file_lock() -> Iterator[None]:
    fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":  # pragma: no cover - Windows
            import msvcrt
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                yield
            finally:
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def try_owner_lock() -> int | None:
    """G6: one scheduler per data folder. Takes a non-blocking exclusive OS lock on
    `data/scheduler.lock` and returns its fd (keep it open while the scheduler may fire), or None when
    another `autoclipper serve` on the same data folder already holds it."""
    fd = os.open(data_dir() / "scheduler.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":  # pragma: no cover - Windows
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    return fd


def release_owner_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        if os.name == "nt":  # pragma: no cover - Windows
            import msvcrt
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


@contextmanager
def locked() -> Iterator[None]:
    with _lock, _file_lock():
        yield


class ScheduleStoreError(OSError):
    """schedules.json could not be read (e.g. a Windows sharing violation that outlived the retries).
    Never treated as "no schedules": the caller fails instead of saving an empty store over it."""


LOAD_RETRIES = 5
LOAD_RETRY_SLEEP = 0.1


def _move_aside(p: Path) -> None:
    try:
        os.replace(p, p.with_name(f"schedules.broken-{int(time.time())}.json"))
    except OSError:
        pass


def _load() -> list[dict]:
    p = store_path()
    text = None
    for attempt in range(LOAD_RETRIES):
        try:
            text = p.read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            return []
        except UnicodeDecodeError:
            text = None
            break
        except OSError as e:
            # a brief lock by antivirus / the search indexer (PermissionError on Windows): retry, then fail
            # loudly - never continue with an empty list, the next write would wipe every schedule
            if attempt == LOAD_RETRIES - 1:
                raise ScheduleStoreError(f"cannot read {p.name}: {type(e).__name__}") from e
            time.sleep(LOAD_RETRY_SLEEP)
    try:
        data = json.loads(text) if text is not None else None
    except ValueError:
        data = None
    rows = data.get("schedules") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        # really corrupt (not JSON / wrong shape): keep it aside for the user and start empty
        _move_aside(p)
        return []
    return [s for s in rows if isinstance(s, dict) and isinstance(s.get("items"), dict)]


def _save(rows: list[dict]) -> None:
    write_text_atomic(store_path(), json.dumps({"version": 1, "schedules": rows}, ensure_ascii=False, indent=2))


def update(fn: Callable[[list[dict]], Any]) -> Any:
    """Read, change and write the store under the lock. `fn` mutates the list in place."""
    with locked():
        rows = _load()
        before = json.dumps(rows, sort_keys=True)
        out = fn(rows)
        if json.dumps(rows, sort_keys=True) != before:
            _save(rows)
        return out


def read_all() -> list[dict]:
    with locked():
        return _load()


# ---------------------------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------------------------
def local_tz() -> tzinfo:
    return datetime.now().astimezone().tzinfo or timezone.utc


def resolve_tz(name: str | None) -> tzinfo | None:
    """IANA name -> tzinfo; "" / None -> this computer's zone; unknown -> None."""
    if not name:
        return local_tz()
    if not TZ_RE.match(name):
        return None
    if name.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, no tzdata on Windows, ...
        return None


def _offset_label(dt: datetime) -> str:
    off = dt.utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    mins = abs(mins)
    return f"UTC{sign}{mins // 60:02d}:{mins % 60:02d}"


def parse_time(value: Any, tz_name: Any, default_tz: str = "") -> tuple[float, str]:
    """ISO 8601 date-time -> (epoch seconds, time zone label). A value without an offset is read in
    `tz_name` (else the settings zone, else this computer's). Raises PublishInputError."""
    if not isinstance(value, str) or len(value) > 40 or not TIME_RE.match(value):
        raise PublishInputError({"publish_at": ["use an ISO date-time like 2026-11-24T18:30 or "
                                                "2026-11-24T18:30:00+06:00"]})
    if tz_name is not None and (not isinstance(tz_name, str) or (tz_name and not TZ_RE.match(tz_name))):
        raise PublishInputError({"timezone": ["use a time zone name like Asia/Dhaka"]})
    text = value.replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    m = re.search(r"([+-])(\d{2}):?(\d{2})\Z", text)
    if m and ":" not in text[m.start():]:
        text = text[:m.start()] + f"{m.group(1)}{m.group(2)}:{m.group(3)}"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise PublishInputError({"publish_at": ["not a valid date or time"]}) from None
    name = tz_name or default_tz or ""
    tz = resolve_tz(name)
    if dt.tzinfo is None:
        if tz is None:
            raise PublishInputError({"timezone": [f"unknown time zone {name!r} on this computer; send the time "
                                                  "with its UTC offset instead"]})
        dt = dt.replace(tzinfo=tz)
    label = name if (name and tz is not None) else _offset_label(dt)
    return dt.timestamp(), label


def tz_for(label: str) -> tzinfo:
    """The zone of a stored schedule (an IANA name or a "UTC+06:00" label)."""
    m = re.match(r"\AUTC([+-])(\d{2}):(\d{2})\Z", label or "")
    if m:
        mins = int(m.group(2)) * 60 + int(m.group(3))
        return timezone(timedelta(minutes=mins if m.group(1) == "+" else -mins))
    return resolve_tz(label) or local_tz()


def iso_local(t: float, label: str) -> str:
    return datetime.fromtimestamp(t, tz=tz_for(label)).isoformat(timespec="seconds")


def iso_utc(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_local(t: float, label: str) -> str:
    dt = datetime.fromtimestamp(t, tz=tz_for(label))
    return dt.strftime("%Y-%m-%d %H:%M") + f" ({_offset_label(dt)})"


def day_bounds(t: float, label: str) -> tuple[float, float, str]:
    tz = tz_for(label)
    dt = datetime.fromtimestamp(t, tz=tz)
    start = datetime(dt.year, dt.month, dt.day, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp(), start.strftime("%a %Y-%m-%d")


def _fmt_dur(seconds: float) -> str:
    s = int(max(0, seconds))
    h, m = divmod(s // 60, 60)
    return f"{h} h {m} min" if h else f"{m} min"


# ---------------------------------------------------------------------------------------------
# modes, limits
# ---------------------------------------------------------------------------------------------
def plan_mode(platform: str, target: Any, ss: Any, publish_at: float, now: float) -> tuple[str, str, float]:
    """-> (mode, note, handoff_at)."""
    lead = int(ss.native_lead_minutes) * 60
    if platform == "instagram":
        return ("app", "Instagram's API has no scheduling: Auto Clipper posts it at the time (keep "
                       "`autoclipper serve` running)", publish_at)
    if not ss.prefer_native:
        return "app", "native scheduling is off in settings: Auto Clipper posts it at the time", publish_at
    handoff = max(now, publish_at - lead)
    if platform == "youtube":
        privacy = getattr(target, "privacy", "private")
        if privacy != "public":
            return ("app", f"YouTube only schedules public videos; privacy is {privacy}, so Auto Clipper "
                           "uploads it at the time", publish_at)
        return ("native", f"uploaded as private up to {ss.native_lead_minutes} min before; YouTube makes it public "
                          "at the time (status.publishAt)", handoff)
    if publish_at - now < FB_NATIVE_MIN_SECONDS:
        return "app", "less than 15 minutes ahead: Auto Clipper posts it at the time", publish_at
    return ("native", f"uploaded up to {ss.native_lead_minutes} min before; Facebook publishes it at the time "
                      "(video_state=SCHEDULED)", handoff)


def native_window_ok(platform: str, ahead: float) -> bool:
    if platform == "youtube":
        return ahead >= YT_NATIVE_MIN_SECONDS
    if platform == "facebook":
        return FB_NATIVE_MIN_SECONDS <= ahead <= FB_NATIVE_MAX_SECONDS
    return False


def _ledger_rows() -> list[dict]:
    try:
        lines = records.ledger_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and isinstance(e.get("t"), (int, float)):
            out.append(e)
    return out


def _ledger_day_t(e: dict) -> float:
    """The moment a ledger entry goes public: `day_t` for a native schedule (handed over earlier, the
    platform publishes at the scheduled time), else the publish time `t`."""
    d = e.get("day_t")
    return float(d) if isinstance(d, (int, float)) and not isinstance(d, bool) else float(e["t"])


def limit_label(settings: Settings) -> str:
    """The zone daily limits are counted in: publish.schedule.timezone, else this computer's zone."""
    name = settings.publish.schedule.timezone or ""
    return name if name and resolve_tz(name) is not None else ""


def day_count(rows: list[dict], platform: str, account_id: str, start: float, end: float, *,
              exclude: str | None = None, states: tuple[str, ...] = ("scheduled", "queued", "running")) -> int:
    """Published (ledger) + still pending schedule items for one account on one local day."""
    n = sum(1 for e in _ledger_rows() if e.get("platform") == platform and e.get("account_id") == account_id
            and start <= _ledger_day_t(e) < end)
    for s in rows:
        if s.get("id") == exclude:
            continue
        it = (s.get("items") or {}).get(platform)
        if it and it.get("account_id") == account_id and it.get("state") in states \
                and start <= float(s.get("publish_at") or 0) < end:
            n += 1
    return n


def limit_problem(rows: list[dict], platform: str, account: dict | None, limit: int, publish_at: float,
                  label: str, *, exclude: str | None = None,
                  states: tuple[str, ...] = ("scheduled", "queued", "running")) -> str | None:
    if not account or not account.get("id"):
        return None
    start, end, day = day_bounds(publish_at, label)
    n = day_count(rows, platform, str(account["id"]), start, end, exclude=exclude, states=states)
    if n >= limit:
        return (f"daily limit reached: {n} of {limit} {PLATFORM_NAMES[platform]} posts for "
                f"{account.get('name') or account['id']} on {day} (published or scheduled); pick another day or "
                f"raise publish.schedule.daily_limits.{platform} on the Accounts page")
    return None


# ---------------------------------------------------------------------------------------------
# public shapes
# ---------------------------------------------------------------------------------------------
def aggregate_state(items: dict) -> str:
    states = [it.get("state") for it in items.values()]
    if any(s in ACTIVE for s in states):
        return "upcoming"
    if states and all(s == "cancelled" for s in states):
        return "cancelled"
    if any(s == "failed" for s in states):
        return "failed"
    if any(s == "missed" for s in states):
        return "missed"
    return "done"


def public(s: dict) -> dict:
    items = {}
    for p, it in (s.get("items") or {}).items():
        row = {k: it.get(k) for k in PUBLIC_ITEM_KEYS}
        row["cancellable"] = it.get("state") == "scheduled" or it.get("state") == "queued"
        items[p] = row
    return {"id": s.get("id"), "job_id": s.get("job_id"), "clip": s.get("clip"), "lang": s.get("lang"),
            "title": s.get("title") or "", "publish_at": s.get("publish_at"),
            "publish_at_utc": iso_utc(float(s.get("publish_at") or 0)),
            "publish_at_local": iso_local(float(s.get("publish_at") or 0), s.get("timezone") or ""),
            "timezone": s.get("timezone") or "", "state": aggregate_state(s.get("items") or {}),
            "platforms": list(items), "items": items, "created_at": s.get("created_at"),
            "updated_at": s.get("updated_at"), "dry_run": False}


def schedule_plan_info(s: dict, platform: str) -> dict:
    it = s["items"][platform]
    return {"id": s.get("id"), "publish_at": s["publish_at"], "publish_at_utc": iso_utc(s["publish_at"]),
            "publish_at_local": iso_local(s["publish_at"], s.get("timezone") or ""),
            "timezone": s.get("timezone") or "", "mode": it["mode"], "mode_note": it["mode_note"],
            "handoff_at": it["handoff_at"]}


def _find(rows: list[dict], sid: str) -> dict:
    if not isinstance(sid, str) or not ID_RE.match(sid):
        raise PublishNotFound(f"schedule {sid!r} not found")
    for s in rows:
        if s.get("id") == sid:
            return s
    raise PublishNotFound(f"schedule {sid} not found")


def get(sid: str) -> dict:
    return public(_find(read_all(), sid))


def list_schedules(state: str | None = None, *, job_id: str | None = None, clip: int | None = None,
                   lang: str | None = None) -> list[dict]:
    out = [public(s) for s in read_all()]
    if state and state != "all":
        out = [s for s in out if s["state"] == state]
    if job_id is not None:
        out = [s for s in out if s["job_id"] == job_id]
    if clip is not None:
        out = [s for s in out if s["clip"] == clip]
    if lang is not None:
        out = [s for s in out if s["lang"] == lang]
    out.sort(key=lambda s: (s["state"] != "upcoming", s["publish_at"] if s["state"] == "upcoming"
                            else -float(s["publish_at"] or 0)))
    return out


# ---------------------------------------------------------------------------------------------
# create / preview / edit / cancel
# ---------------------------------------------------------------------------------------------
def _check_when(publish_at: float, now: float) -> None:
    if publish_at <= now:
        raise PublishInputError({"publish_at": ["is in the past; pick a time at least 2 minutes from now"]})
    if publish_at < now + MIN_AHEAD_SECONDS:
        raise PublishInputError({"publish_at": ["must be at least 2 minutes from now"]})
    if publish_at > now + MAX_AHEAD_DAYS * 86400:
        raise PublishInputError({"publish_at": [f"must be within {MAX_AHEAD_DAYS} days"]})


def _items(job_id: str, clip: int, lang: str, platforms: list[str], publish_at: float, settings: Settings,
           now: float) -> dict[str, dict]:
    ps = settings.publish
    out = {}
    for p in platforms:
        mode, note, handoff = plan_mode(p, ps.target(lang, p), ps.schedule, publish_at, now)
        acc = accounts.account(lang, p)
        out[p] = {"platform": p, "mode": mode, "mode_note": note, "handoff_at": handoff,
                  "account_id": str(acc.get("id")) if acc else None,
                  "account": acc.public if acc is not None else None, "state": "scheduled",
                  "message": f"waiting for {'the hand-off' if mode == 'native' else 'the time'}",
                  "task_id": None, "fired_at": None, "finished_at": None, "remote_id": None, "url": None}
    return out


def _schedule_problems(rows: list[dict], job_id: str, clip: int, lang: str, items: dict[str, dict],
                       publish_at: float, label: str, settings: Settings, *, exclude: str | None) -> dict:
    ss = settings.publish.schedule
    errors: dict[str, list[str]] = {}
    for p, it in items.items():
        for s in rows:
            if s.get("id") == exclude or s.get("job_id") != job_id or s.get("clip") != clip \
                    or s.get("lang") != lang:
                continue
            other = (s.get("items") or {}).get(p)
            if other and other.get("state") in ACTIVE:
                errors.setdefault(p, []).append(
                    f"already scheduled for {fmt_local(s['publish_at'], s.get('timezone') or '')} "
                    f"(schedule {s['id']}); edit or cancel that one")
        acc = accounts.account(lang, p)
        msg = limit_problem(rows, p, {"id": acc.get("id"), "name": acc.get("name")} if acc else None,
                            int(getattr(ss.daily_limits, p)), publish_at, limit_label(settings), exclude=exclude)
        if msg:
            errors.setdefault(p, []).append(msg)
    return errors


def preview(job_id: str, clip: int, lang: str, platforms: list[str], publish_at: float, label: str,
            settings: Settings, *, now: float | None = None) -> dict:
    """Dry run: the plans (with the schedule) and the would-be schedule. Writes only the plan files."""
    now = time.time() if now is None else now
    platforms = check_platforms(platforms)
    _check_when(publish_at, now)
    item, extra = load_item(job_id, clip, lang)
    items = _items(job_id, clip, lang, platforms, publish_at, settings, now)
    meta, _ = read_metadata(job_id, clip, lang)
    s = {"id": None, "job_id": job_id, "clip": clip, "lang": lang, "title": meta.get("title") or "",
         "publish_at": publish_at, "timezone": label, "items": items, "created_at": now, "updated_at": now}
    problems = _schedule_problems(read_all(), job_id, clip, lang, items, publish_at, label, settings, exclude=None)
    plans = {}
    for p in platforms:
        native = items[p]["mode"] == "native"
        it = dataclasses.replace(item, publish_at=publish_at) if native else item
        plan = build_plans(it, extra, [p], settings.publish)[p]
        if native:
            # the native-window checks are made at hand-off time, not now
            plan["problems"] = [x for x in plan["problems"] if "scheduled time" not in x
                                and "schedules a reel" not in x]
        for x in problems.get(p, []):
            if x not in plan["problems"]:
                plan["problems"].append(x)
        plan["schedule"] = schedule_plan_info(s, p)
        plan["warnings"].append(f"dry run: nothing was scheduled (would be {items[p]['mode']} at "
                                f"{fmt_local(publish_at, label)})")
        records.write_plan(job_id, clip, lang, p, plan)
        plans[p] = plan
    view = public(s)
    view["dry_run"] = True
    for p, row in view["items"].items():
        row["state"] = "preview"
        row["cancellable"] = False
        row["message"] = "dry run: not scheduled"
    view["state"] = "preview"
    return {"dry_run": True, "schedule": view, "plans": plans}


def create(job_id: str, clip: int, lang: str, platforms: list[str], publish_at: float, label: str,
           settings: Settings, *, idempotency_key: str | None = None, now: float | None = None) -> dict:
    """Live schedule (dry run must be off). Raises PublishInputError / PublishConflict / PublishNotFound."""
    now = time.time() if now is None else now
    platforms = check_platforms(platforms)
    _check_when(publish_at, now)
    _check_clip(job_id, clip, lang)
    if settings.publish.dry_run:
        raise PublishConflict("publishing is in dry-run mode - turn dry run off on the Accounts page to schedule")
    _, already = check_live(job_id, clip, lang, platforms, settings, force=False)
    if already:
        raise PublishConflict("already published", {
            p: [f"already published{(' at ' + st['url']) if st and st.get('url') else ''}; use Publish with "
                "\"publish again\" on the clip page to post it twice"] for p, st in already.items()})
    meta, _ = read_metadata(job_id, clip, lang)

    def fn(rows: list[dict]) -> dict:
        items = _items(job_id, clip, lang, platforms, publish_at, settings, now)
        errors = _schedule_problems(rows, job_id, clip, lang, items, publish_at, label, settings, exclude=None)
        if errors:
            raise PublishConflict("cannot schedule", errors)
        s = {"id": "s_" + uuid.uuid4().hex[:12], "job_id": job_id, "clip": clip, "lang": lang,
             "title": meta.get("title") or "", "publish_at": publish_at, "timezone": label, "items": items,
             "created_at": now, "updated_at": now, "idempotency_key": idempotency_key}
        rows.append(s)
        return public(s)

    return update(fn)


def edit(sid: str, publish_at: float, label: str, settings: Settings, *, now: float | None = None) -> dict:
    """New time for a schedule that has not started (every item still `scheduled` or cancelled)."""
    now = time.time() if now is None else now
    _check_when(publish_at, now)
    if settings.publish.dry_run:
        raise PublishConflict("publishing is in dry-run mode - turn dry run off on the Accounts page to change "
                              "a schedule")

    def fn(rows: list[dict]) -> dict:
        s = _find(rows, sid)
        live = {p: it for p, it in s["items"].items() if it.get("state") != "cancelled"}
        busy = {p: [f"already {it['state'].replace('_', ' ')}; it can no longer be changed"]
                for p, it in live.items() if it.get("state") != "scheduled"}
        if busy or not live:
            raise PublishConflict("this schedule has already started or finished", busy)
        platforms = list(live)
        check_live(s["job_id"], s["clip"], s["lang"], platforms, settings, force=False)
        items = _items(s["job_id"], s["clip"], s["lang"], platforms, publish_at, settings, now)
        errors = _schedule_problems(rows, s["job_id"], s["clip"], s["lang"], items, publish_at, label, settings,
                                    exclude=sid)
        if errors:
            raise PublishConflict("cannot reschedule", errors)
        for p, it in s["items"].items():
            if p in items:
                s["items"][p] = items[p]
        s["publish_at"], s["timezone"], s["updated_at"] = publish_at, label, now
        return public(s)

    return update(fn)


def cancel(sid: str, platforms: list[str] | None = None, *,
           cancel_task: Callable[[str], str] | None = None) -> dict:
    """Cancel the items that have not started (all, or `platforms`). A queued task is removed through
    `cancel_task(task_id)` = the server's `Worker.cancel_if_queued`, which only ever removes a task that is
    still waiting in the queue ("removed"), never signals a running one ("running"/"finished"), and says
    "unknown" for a task this server does not know (fired before a restart: nothing runs it, the scheduler
    would fire it again) - that item is cancelled too. Without `cancel_task` (CLI) a queued item stays.
    Idempotent: cancelling a finished schedule returns it unchanged. PublishConflict when nothing requested
    could be cancelled."""
    def fn(rows: list[dict]) -> dict:
        s = _find(rows, sid)
        wanted = list(s["items"]) if platforms is None else check_platforms(platforms)
        refused: dict[str, list[str]] = {}
        changed = False
        for p in wanted:
            it = s["items"].get(p)
            if it is None:
                refused[p] = ["not part of this schedule"]
                continue
            st = it.get("state")
            outcome = None
            if st == "queued" and cancel_task is not None and it.get("task_id"):
                outcome = cancel_task(it["task_id"])
                if outcome == "unknown" and records.is_published(
                        records.read_status_raw(s["job_id"], int(s["clip"]), s["lang"], p)):
                    outcome = "finished"  # it went out before the restart; the next tick marks it done
            if st == "scheduled" or outcome in ("removed", "unknown"):
                it.update(state="cancelled", message="cancelled", finished_at=time.time())
                changed = True
            elif st == "queued":
                refused[p] = ["it is being published right now"]
            elif st == "running":
                refused[p] = ["it is being published right now"]
            elif st == "handed_off":
                refused[p] = [f"already handed to {PLATFORM_NAMES[p]}; change or delete it there "
                              f"({it.get('url') or 'the platform'})"]
        if changed:
            s["updated_at"] = time.time()
        elif refused and any(s["items"].get(p, {}).get("state") != "cancelled" for p in refused):
            raise PublishConflict("cannot cancel", refused)
        out = public(s)
        out["refused"] = refused
        return out

    return update(fn)


def lang_name(lang: str) -> str:
    return LANG_NAMES.get(lang, lang)


__all__ = ["create", "preview", "edit", "cancel", "get", "list_schedules", "update", "read_all", "public",
           "parse_time", "plan_mode", "native_window_ok", "limit_problem", "fmt_local", "schedule_plan_info",
           "PublishConflict", "PublishInputError", "PublishNotFound", "SCHEDULE_STATES", "ITEM_STATES",
           "ScheduleStoreError", "try_owner_lock", "release_owner_lock", "limit_label"]
