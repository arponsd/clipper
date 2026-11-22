"""Pick viral moments with the LLM, then post-validate them against the transcript."""
from __future__ import annotations

import json
import math
from pathlib import Path

from ..config import ClipSettings
from ..interfaces import LLM
from ..models import Moment, Transcript
from ._atomic import write_text_atomic

# A boundary further away than this is not "near": keep the LLM's time instead of jumping.
SNAP_TOLERANCE = 5.0
_EPS = 1e-6


class MomentsError(RuntimeError):
    """Moment picking produced nothing usable."""


def format_transcript(t: Transcript, start: float | None = None, end: float | None = None) -> str:
    """One line per segment: "[12.3-15.8] text". With start/end, only overlapping segments."""
    lines: list[str] = []
    for s in t.segments:
        if start is not None and s.end <= start:
            continue
        if end is not None and s.start >= end:
            continue
        text = " ".join(s.text.split())
        if text:
            lines.append(f"[{s.start:.1f}-{s.end:.1f}] {text}")
    return "\n".join(lines)


def _nearest(value: float, candidates: list[float], tolerance: float = SNAP_TOLERANCE) -> float:
    best = min(candidates, key=lambda c: abs(c - value), default=None)
    if best is None or abs(best - value) > tolerance:
        return value
    return best


def _fit_one(m: Moment, t: Transcript, starts: list[float], ends: list[float],
             min_s: float, max_s: float) -> Moment | None:
    dur_video = float(t.duration)
    if not (math.isfinite(m.start) and math.isfinite(m.end)):
        return None
    start = min(max(float(m.start), 0.0), dur_video)
    end = min(max(float(m.end), 0.0), dur_video)
    if end - start <= _EPS:
        return None
    if dur_video < min_s:  # whole video is shorter than one clip: use all of it
        return m.model_copy(update={"start": 0.0, "end": dur_video})

    s2, e2 = _nearest(start, starts), _nearest(end, ends)
    if e2 - s2 > _EPS:
        start, end = s2, e2

    if end - start < min_s - _EPS:
        # extend to the first segment end that reaches min (within max), else exactly min
        cands = [e for e in ends if start + min_s - _EPS <= e <= start + max_s + _EPS]
        end = min(cands) if cands else start + min_s
        if end > dur_video + _EPS:  # ran past the video: anchor at the end and move start back
            end = dur_video
            cands = [s for s in starts if end - max_s - _EPS <= s <= end - min_s + _EPS]
            start = max(cands) if cands else max(0.0, end - min_s)
    elif end - start > max_s + _EPS:
        cands = [e for e in ends if start + min_s - _EPS <= e <= start + max_s + _EPS]
        end = max(cands) if cands else start + max_s

    end = min(end, dur_video)
    start = max(0.0, start)
    dur = end - start
    if dur < min_s - _EPS or dur > max_s + _EPS:
        return None
    return m.model_copy(update={"start": round(start, 3), "end": round(end, 3)})


def _overlaps(a: Moment, b: Moment) -> bool:
    return a.start < b.end - _EPS and b.start < a.end - _EPS


def validate_moments(raw: list[Moment], t: Transcript, *, count: int, min_seconds: float,
                     max_seconds: float) -> list[Moment]:
    """Clamp, snap to segment boundaries, enforce min/max, drop overlaps, sort by score, top N."""
    min_s, max_s = float(min_seconds), float(max(max_seconds, min_seconds))
    starts = sorted({0.0, *(float(s.start) for s in t.segments)})
    ends = sorted({float(t.duration), *(float(s.end) for s in t.segments)})
    fitted = [f for m in raw if (f := _fit_one(m, t, starts, ends, min_s, max_s)) is not None]
    fitted.sort(key=lambda m: m.score, reverse=True)
    kept: list[Moment] = []
    for m in fitted:
        if any(_overlaps(m, k) for k in kept):
            continue
        kept.append(m)
        if len(kept) >= count:
            break
    return kept


def save_moments(moments: list[Moment], path: Path) -> Path:
    text = json.dumps([m.model_dump() for m in moments], ensure_ascii=False, indent=2)
    return write_text_atomic(Path(path), text)


def load_moments(path: Path) -> list[Moment]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return [Moment.model_validate(d) for d in data]
    except Exception as e:  # noqa: BLE001
        raise MomentsError(f"could not read {path}: {e}; delete it to pick moments again") from e


def run_moments(t: Transcript, llm: LLM, settings: ClipSettings, out_json: Path) -> list[Moment]:
    """Idempotent: reuse out_json if present; else ask the LLM, validate and write."""
    out_json = Path(out_json)
    if out_json.is_file() and out_json.stat().st_size > 0:
        return load_moments(out_json)
    text = format_transcript(t)
    if not text.strip():
        raise MomentsError("the transcript is empty (no speech found), so no moments can be picked")
    raw = llm.pick_moments(text, count=settings.count, min_seconds=settings.min_seconds,
                           max_seconds=settings.max_seconds, video_duration=t.duration)
    moments = validate_moments(raw, t, count=settings.count, min_seconds=settings.min_seconds,
                               max_seconds=settings.max_seconds)
    if not moments:
        raise MomentsError(
            f"the LLM returned {len(raw)} moment(s) but none were valid "
            f"({settings.min_seconds:g}-{settings.max_seconds:g}s, inside the video, non-overlapping)"
        )
    save_moments(moments, out_json)
    return moments
