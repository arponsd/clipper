"""Per-job ownership marker: `data/jobs/<id>/job.lock` = {"pid", "host", "started"}.

Written around run_job / rerender by both the web worker and the CLI (`run`, `rerender`), removed in
`finally`. A lock is *live* when it was written on this host and its pid still exists; anything else
(dead pid, other host, unreadable file) is stale and may be overwritten. The web server uses it instead
of a job.json-mtime heuristic to decide whether another process owns a job (WORKER-2).
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .jobstore import jobs_root

LOCK_NAME = "job.lock"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class JobLocked(RuntimeError):
    """Another live process holds the job's lock."""


def lock_path(job_id: str) -> Path:
    return jobs_root() / job_id / LOCK_NAME


def _host() -> str:
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover
        return ""


def pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if sys.platform == "win32":  # pragma: no cover - os.kill(pid, 0) would send CTRL_C_EVENT on Windows
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def read_lock(job_id: str) -> dict | None:
    try:
        data = json.loads(lock_path(job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def lock_is_live(lock: dict | None) -> bool:
    if not lock:
        return False
    return lock.get("host") == _host() and pid_alive(lock.get("pid"))  # type: ignore[arg-type]


def live_lock(job_id: str) -> dict | None:
    """The job's lock when a live process holds it, else None."""
    lock = read_lock(job_id)
    return lock if lock_is_live(lock) else None


def has_lock_file(job_id: str) -> bool:
    return lock_path(job_id).exists()


@contextmanager
def hold_job_lock(job_id: str) -> Iterator[None]:
    """Write job.lock for this process while the block runs; remove it afterwards.

    Raises JobLocked when another live process holds it. A stale lock is overwritten. When the job dir
    does not exist (unknown or unsafe job id) nothing is written, so the caller's own "not found" error surfaces.
    """
    path = lock_path(job_id)
    if not _SAFE_ID.match(job_id or "") or not (path.parent / "job.json").is_file():
        yield
        return
    current = read_lock(job_id)
    if lock_is_live(current) and current.get("pid") != os.getpid():  # type: ignore[union-attr]
        raise JobLocked(f"job {job_id} is being run by another process (pid {current.get('pid')})")  # type: ignore[union-attr]
    mine = {"pid": os.getpid(), "host": _host(), "started": time.time()}
    tmp = path.with_name(f"{LOCK_NAME}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(mine), encoding="utf-8")
    os.replace(tmp, path)
    try:
        yield
    finally:
        try:
            if read_lock(job_id) == mine:
                path.unlink()
        except OSError:
            pass
