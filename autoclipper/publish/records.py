"""Plan / status files under clips/NN/<lang>/publish/, the publish ledger and resume records.

- `<platform>.json`         request plan (dry-run output; rewritten with dry_run=false before a live send)
- `<platform>.status.json`  live state; single writer = the live runner
- `<platform>.history.jsonl` earlier published records replaced by a forced re-publish
- `data/secrets/publish_ledger.jsonl` one line per live publish (local rate windows)
- `data/secrets/resume_<job>_<NN>_<lang>_<platform>.json` INTERNAL resume state (session URI, ids); kept out
  of the job dir because a resumable session URI is a credential
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from ..jobstore import jobs_root
from ..stages._atomic import write_text_atomic
from . import secrets

LEDGER = "publish_ledger.jsonl"
ACTIVE_STATES = ("queued", "uploading", "processing", "publishing")
STATUS_KEYS = ("platform", "lang", "job_id", "clip", "state", "progress", "message", "attempts", "error",
               "remote_id", "url", "account", "video_sha256", "video_size", "idempotency_key", "started_at",
               "updated_at", "published_at")
_ledger_lock = threading.Lock()


def publish_dir(job_id: str, clip: int, lang: str) -> Path:
    return jobs_root() / job_id / "clips" / f"{clip:02d}" / lang / "publish"


def plan_path(job_id: str, clip: int, lang: str, platform: str) -> Path:
    return publish_dir(job_id, clip, lang) / f"{platform}.json"


def status_path(job_id: str, clip: int, lang: str, platform: str) -> Path:
    return publish_dir(job_id, clip, lang) / f"{platform}.status.json"


def history_path(job_id: str, clip: int, lang: str, platform: str) -> Path:
    return publish_dir(job_id, clip, lang) / f"{platform}.history.jsonl"


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _read(path: Path) -> dict | None:
    for attempt in range(3):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            if attempt == 2:
                return None
            time.sleep(0.03)
    return None  # pragma: no cover


def write_plan(job_id: str, clip: int, lang: str, platform: str, plan: dict) -> Path:
    return write_text_atomic(plan_path(job_id, clip, lang, platform), _dump(plan))


def read_plan(job_id: str, clip: int, lang: str, platform: str) -> dict | None:
    return _read(plan_path(job_id, clip, lang, platform))


def read_status_raw(job_id: str, clip: int, lang: str, platform: str) -> dict | None:
    return _read(status_path(job_id, clip, lang, platform))


def public_status(status: dict | None, *, active: bool = True) -> dict | None:
    """Status for API/CLI output: only the contract keys (never `resume`); an in-flight state without an
    active publish task is shown as `interrupted` (computed, the file is not rewritten)."""
    if status is None:
        return None
    out = {k: status.get(k) for k in STATUS_KEYS}
    if not active and out.get("state") in ACTIVE_STATES:
        out["state"] = "interrupted"
        out["message"] = "interrupted - publish again to resume"
    return out


def is_published(status: dict | None) -> bool:
    return bool(status) and status.get("state") == "published" and bool(status.get("remote_id"))


def append_history(job_id: str, clip: int, lang: str, platform: str, status: dict) -> None:
    p = history_path(job_id, clip, lang, platform)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(public_status(status), ensure_ascii=False) + "\n")


class FileStatusWriter:
    """Atomic writer of one `<platform>.status.json` (the live runner is the only writer)."""

    def __init__(self, job_id: str, clip: int, lang: str, platform: str, initial: dict):
        self.job_id, self.clip, self.lang, self.platform = job_id, clip, lang, platform
        self.path = status_path(job_id, clip, lang, platform)
        self.data = dict(initial)
        self.write()

    def write(self) -> None:
        self.data["updated_at"] = time.time()
        if isinstance(self.data.get("message"), str):
            self.data["message"] = secrets.redact(self.data["message"])[:500]
        write_text_atomic(self.path, _dump({k: self.data.get(k) for k in STATUS_KEYS}))

    def update(self, **fields: Any) -> dict:
        self.data.update(fields)
        self.write()
        return dict(self.data)

    # resume records live in data/secrets (a session URI is a credential)
    def _resume_name(self) -> str:
        return resume_name(self.job_id, self.clip, self.lang, self.platform)

    def save_resume(self, data: dict | None) -> None:
        if data is None:
            secrets.delete_secret(self._resume_name())
        else:
            secrets.write_secret(self._resume_name(), data)

    def load_resume(self) -> dict | None:
        return secrets.read_secret(self._resume_name())


def resume_name(job_id: str, clip: int, lang: str, platform: str) -> str:
    return f"resume_{job_id}_{clip:02d}_{lang}_{platform}"


def resume_sent(job_id: str, clip: int, lang: str, platform: str) -> bool:
    """True once the non-idempotent publishing request (IG media_publish / FB finish) may have been sent:
    the post may be live, so the record must survive errors (the next run checks before posting)."""
    try:
        data = secrets.read_secret(resume_name(job_id, clip, lang, platform))
    except ValueError:
        return False
    return bool(data and data.get("sent"))


def clear_resume(job_id: str, clip: int, lang: str, platform: str) -> None:
    try:
        secrets.delete_secret(resume_name(job_id, clip, lang, platform))
    except ValueError:
        pass


# ---------------------------------------------------------------------------------------------
# ledger (local rate windows)
# ---------------------------------------------------------------------------------------------
def ledger_path() -> Path:
    return secrets.secrets_dir() / LEDGER


def append_ledger(platform: str, account_id: str, lang: str, t: float | None = None) -> None:
    line = json.dumps({"t": time.time() if t is None else t, "platform": platform, "account_id": account_id,
                       "lang": lang})
    with _ledger_lock:
        p = ledger_path()
        fd = os.open(p, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def window_count(platform: str, account_id: str, *, window: float = 24 * 3600, now: float | None = None) -> int:
    now = time.time() if now is None else now
    p = ledger_path()
    n = 0
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("platform") != platform or e.get("account_id") != account_id:
            continue
        t = e.get("t")
        if isinstance(t, (int, float)) and now - window <= t <= now + 60:
            n += 1
    return n


def oldest_in_window(platform: str, account_id: str, *, window: float = 24 * 3600) -> float | None:
    now = time.time()
    best = None
    try:
        lines = ledger_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("platform") == platform and e.get("account_id") == account_id:
            t = e.get("t")
            if isinstance(t, (int, float)) and t >= now - window and (best is None or t < best):
                best = t
    return best


# ---------------------------------------------------------------------------------------------
# file facts (cached by path + size + mtime)
# ---------------------------------------------------------------------------------------------
_sha_cache: dict[tuple[str, int, int], str] = {}
_sha_lock = threading.Lock()


def file_sha256(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    with _sha_lock:
        hit = _sha_cache.get(key)
    if hit:
        return hit
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    digest = h.hexdigest()
    with _sha_lock:
        if len(_sha_cache) > 256:
            _sha_cache.clear()
        _sha_cache[key] = digest
    return digest
