"""Cut stage: one vertical clip (clips/NN/cut.mp4) per moment, original audio kept."""
from __future__ import annotations

from pathlib import Path

from .. import ffmpeg
from ..config import ClipSettings
from ..interfaces import ProgressFn
from ..models import Moment

__all__ = ["cut_clip"]


def cut_clip(
    source: Path,
    moment: Moment,
    dst: Path,
    settings: ClipSettings,
    on_progress: ProgressFn | None = None,
) -> Path:
    """Cut ``moment`` out of ``source`` into ``dst`` (``clip_dir(i)/"cut.mp4"``).

    Always re-cuts (the pipeline decides whether to skip an existing output). Raises
    :class:`autoclipper.ffmpeg.FFmpegError` on failure.
    """
    return ffmpeg.cut_vertical(
        Path(source), Path(dst), moment.start, moment.end,
        width=settings.width, height=settings.height, mode=settings.reframe,
        on_progress=on_progress,
    )
