"""Job manifests on disk: data/jobs/<id>/job.json. Thread-safe writes so the UI can read while a worker runs.

Single-writer rule (CONTRACT.md section 1a): exactly one JobStore owner writes a job's job.json. Saves are
atomic (unique tmp file + os.replace) so readers never see a torn file, but two writers would still lose
each other's updates; there is no cross-process lock.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

from .config import data_dir
from .models import Job, StageState

JOB_STAGES = ["ingest", "transcribe", "signals", "moments", "vision", "cut"]
# Job stages added later: a job made before one existed has no state for it, which counts as finished.
OPTIONAL_JOB_STAGES = ("signals", "vision")
CLIP_STAGES = ["script", "voice", "captions", "render"]

_lock = threading.RLock()

# os.replace onto job.json fails on Windows (WinError 5/32) while another process has it open
# (a status reader, antivirus, OneDrive). Retry for about 1 s before giving up.
REPLACE_RETRIES = 20
REPLACE_RETRY_DELAY = 0.05


def _replace_with_retry(src: Path, dst: Path) -> None:
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == REPLACE_RETRIES - 1:
                raise
            time.sleep(REPLACE_RETRY_DELAY)


def read_job_text(path: Path) -> str:
    """The text of a job.json. On Windows, opening it while a save's os.replace swaps it in fails with
    PermissionError (Errno 13) for a moment; retry for about 1 s, like the save does. Other errors
    (FileNotFoundError, decoding) are raised at once."""
    for attempt in range(REPLACE_RETRIES):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if attempt == REPLACE_RETRIES - 1:
                raise
            time.sleep(REPLACE_RETRY_DELAY)
    raise AssertionError("unreachable")  # pragma: no cover


def jobs_root() -> Path:
    p = data_dir() / "jobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


class JobStore:
    def __init__(self, job: Job):
        self.job = job
        self.dir = jobs_root() / job.id
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- lifecycle -------------------------------------------------------
    @classmethod
    def create(cls, source: str, *, voiceover: bool | None = None) -> "JobStore":
        """A brand-new job; ``voiceover`` is the job's fixed voice-over mode (None = decided by run_job)."""
        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        job = Job(id=job_id, source=source, created_at=time.time(), voiceover=voiceover,
                  stages={s: StageState() for s in JOB_STAGES})
        store = cls(job)
        store.save()
        return store

    @classmethod
    def load(cls, job_id: str) -> "JobStore":
        path = jobs_root() / job_id / "job.json"
        if not path.exists():
            raise FileNotFoundError(f"job {job_id!r} not found")
        return cls(Job.model_validate_json(read_job_text(path)))

    @staticmethod
    def list_ids() -> list[str]:
        return sorted((p.name for p in jobs_root().iterdir() if (p / "job.json").exists()), reverse=True)

    def save(self) -> None:
        with _lock:
            # Unique tmp name per save: two savers never share (and steal) one tmp file.
            tmp = self.dir / f"job.json.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
            try:
                tmp.write_text(self.job.model_dump_json(indent=2), encoding="utf-8")
                _replace_with_retry(tmp, self.dir / "job.json")
            finally:
                tmp.unlink(missing_ok=True)

    # ---- paths -----------------------------------------------------------
    def path(self, *parts: str) -> Path:
        p = self.dir.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def clip_dir(self, index: int, lang: str | None = None) -> Path:
        p = self.dir / "clips" / f"{index:02d}"
        if lang:
            p = p / lang
        p.mkdir(parents=True, exist_ok=True)
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.dir).as_posix()

    # ---- progress --------------------------------------------------------
    def set_stage(self, stage: str, status: str, progress: float | None = None, message: str = "",
                  clip: int | None = None, lang: str | None = None) -> None:
        with _lock:
            if clip is None:
                st = self.job.stages.setdefault(stage, StageState())
            else:
                st = self.job.clips[clip].langs[lang].stages.setdefault(stage, StageState())
            st.status = status  # type: ignore[assignment]
            if progress is not None:
                st.progress = progress
            elif status == "done":
                st.progress = 1.0
            st.message = message
            self.save()
