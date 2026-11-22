"""Job manifests on disk: data/jobs/<id>/job.json. Thread-safe writes so the UI can read while a worker runs."""
from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

from .config import data_dir
from .models import Job, StageState

JOB_STAGES = ["ingest", "transcribe", "moments", "cut"]
CLIP_STAGES = ["script", "voice", "captions", "render"]

_lock = threading.RLock()


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
    def create(cls, source: str) -> "JobStore":
        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        job = Job(id=job_id, source=source, created_at=time.time(),
                  stages={s: StageState() for s in JOB_STAGES})
        store = cls(job)
        store.save()
        return store

    @classmethod
    def load(cls, job_id: str) -> "JobStore":
        path = jobs_root() / job_id / "job.json"
        if not path.exists():
            raise FileNotFoundError(f"job {job_id!r} not found")
        return cls(Job.model_validate_json(path.read_text(encoding="utf-8")))

    @staticmethod
    def list_ids() -> list[str]:
        return sorted((p.name for p in jobs_root().iterdir() if (p / "job.json").exists()), reverse=True)

    def save(self) -> None:
        with _lock:
            tmp = self.dir / "job.json.tmp"
            tmp.write_text(self.job.model_dump_json(indent=2), encoding="utf-8")
            os.replace(tmp, self.dir / "job.json")

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
