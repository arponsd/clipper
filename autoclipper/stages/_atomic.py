"""Private helper: atomic text writes for stage outputs (JSON, ASS).

The text is fully built before any file is opened, written to a unique sibling tmp file and moved into
place with os.replace, so a crash or a serialization error never leaves a truncated output behind.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path
