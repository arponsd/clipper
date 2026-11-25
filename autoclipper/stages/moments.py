"""Pick viral moments with the LLM, then post-validate them against the transcript."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import ClipSettings
from ..interfaces import LLM
from ..models import Moment, Transcript
from ._atomic import write_text_atomic
from ._optional import call_with_optional
from .signals import MomentSignals, Signals, legend, moment_signals, window_size, window_summary

# A boundary further away than this is not "near": keep the LLM's time instead of jumping.
SNAP_TOLERANCE = 5.0
# Low-speech videos: a sentence boundary must be this close (the LLM picks the action, not the sentence).
SNAP_TOLERANCE_LOW_SPEECH = 2.5
# No sentence boundary nearby: a scene cut or quiet point this close is used instead.
FALLBACK_TOLERANCE = 3.0
# Low-speech videos: the LLM picks on the signal table's window grid, so look a little further for a cut.
FALLBACK_TOLERANCE_LOW_SPEECH = 5.0
CUT_END_MARGIN = 0.25  # end this much before a detected cut (one 4 fps sample)
STATIC_PENALTY = 0.5  # low-speech videos: score x (1 - STATIC_PENALTY x static share)
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


def _near(value: float, candidates: list[float], tolerance: float) -> float | None:
    best = min(candidates, key=lambda c: abs(c - value), default=None)
    return None if best is None or abs(best - value) > tolerance else best


@dataclass
class _Bounds:
    """Boundary candidates: sentence boundaries first, then (with signals) scene cuts and quiet points."""
    starts: list[float]
    ends: list[float]
    fb_starts: list[float] = field(default_factory=list)
    fb_ends: list[float] = field(default_factory=list)
    tolerance: float = SNAP_TOLERANCE
    fb_tolerance: float = FALLBACK_TOLERANCE

    def snap_start(self, v: float) -> float:
        hit = _near(v, self.starts, self.tolerance)
        if hit is None:
            hit = _near(v, self.fb_starts, self.fb_tolerance)
        return v if hit is None else hit

    def snap_end(self, v: float) -> float:
        hit = _near(v, self.ends, self.tolerance)
        if hit is None:
            hit = _near(v, self.fb_ends, self.fb_tolerance)
        return v if hit is None else hit

    def ends_in(self, lo: float, hi: float) -> list[float]:
        c = [e for e in self.ends if lo - _EPS <= e <= hi + _EPS]
        return c or [e for e in self.fb_ends if lo - _EPS <= e <= hi + _EPS]

    def starts_in(self, lo: float, hi: float) -> list[float]:
        c = [s for s in self.starts if lo - _EPS <= s <= hi + _EPS]
        return c or [s for s in self.fb_starts if lo - _EPS <= s <= hi + _EPS]


def _fit_one(m: Moment, t: Transcript, starts: list[float] | _Bounds, ends: list[float] | None,
             min_s: float, max_s: float) -> Moment | None:
    b = starts if isinstance(starts, _Bounds) else _Bounds(list(starts), list(ends or []))
    dur_video = float(t.duration)
    if not (math.isfinite(m.start) and math.isfinite(m.end)):
        return None
    start = min(max(float(m.start), 0.0), dur_video)
    end = min(max(float(m.end), 0.0), dur_video)
    if end - start <= _EPS:
        return None
    if dur_video < min_s:  # whole video is shorter than one clip: use all of it
        return m.model_copy(update={"start": 0.0, "end": dur_video})

    s2, e2 = b.snap_start(start), b.snap_end(end)
    if e2 - s2 > _EPS:
        start, end = s2, e2

    if end - start < min_s - _EPS:
        # extend to the first boundary end that reaches min (within max), else exactly min
        cands = b.ends_in(start + min_s, start + max_s)
        end = min(cands) if cands else start + min_s
        if end > dur_video + _EPS:  # ran past the video: anchor at the end and move start back
            end = dur_video
            cands = b.starts_in(end - max_s, end - min_s)
            start = max(cands) if cands else max(0.0, end - min_s)
    elif end - start > max_s + _EPS:
        cands = b.ends_in(start + min_s, start + max_s)
        end = max(cands) if cands else start + max_s

    end = min(end, dur_video)
    start = max(0.0, start)
    dur = end - start
    if dur < min_s - _EPS or dur > max_s + _EPS:
        return None
    return m.model_copy(update={"start": round(start, 3), "end": round(end, 3)})


def _overlaps(a: Moment, b: Moment) -> bool:
    return a.start < b.end - _EPS and b.start < a.end - _EPS


def _trim_avoided_edges(m: Moment, sig: MomentSignals, min_s: float) -> Moment:
    """Low-speech videos: cut a static/menu stretch without speech off the start or end of a moment (an end
    screen, a map opened at the end), as long as the moment keeps its minimum length."""
    if sig.avoid is None:
        return m
    avoid, speech = sig.avoid, sig.speech
    start, end = m.start, m.end
    i = int(math.ceil(end)) - 1
    while i >= 0 and i < len(avoid) and avoid[i] and speech[i] < 0.2 and i - start >= min_s - _EPS:
        end = float(i)
        i -= 1
    i = int(math.floor(start))
    while 0 <= i < len(avoid) and avoid[i] and speech[i] < 0.2 and end - (i + 1) >= min_s - _EPS:
        start = float(i + 1)
        i += 1
    if (start, end) == (m.start, m.end):
        return m
    return m.model_copy(update={"start": round(start, 3), "end": round(end, 3)})


SPLIT_MIN_RUN = 3  # s: a static/menu/silent stretch this long INSIDE a low-speech moment splits it


def _split_at_avoided(m: Moment, sig: MomentSignals, min_s: float) -> Moment:
    """Low-speech videos: a map / menu / silent stretch without speech in the MIDDLE of a moment (a map opened
    between two drives) splits it; the longest part that still has the minimum length is kept, ranked down by
    the share that was cut. When no part is long enough the moment is left as it is (and ranked down later)."""
    if sig.avoid is None:
        return m
    avoid, speech = sig.avoid, sig.speech
    i0, i1 = max(0, int(math.floor(m.start))), min(len(avoid), int(math.ceil(m.end)))
    runs: list[tuple[int, int]] = []
    i = i0
    while i < i1:
        if avoid[i] and speech[i] < 0.2:
            j = i
            while j < i1 and avoid[j] and speech[j] < 0.2:
                j += 1
            if j - i >= SPLIT_MIN_RUN:
                runs.append((i, j))
            i = j
        else:
            i += 1
    if not runs:
        return m
    pieces, a = [], float(m.start)
    for r0, r1 in runs:
        pieces.append((a, max(a, float(r0))))
        a = max(a, float(r1))
    pieces.append((a, float(m.end)))
    best = max(pieces, key=lambda p: p[1] - p[0])
    if best[1] - best[0] < min_s - _EPS:
        return m
    cut_share = 1.0 - (best[1] - best[0]) / max(_EPS, m.end - m.start)
    return m.model_copy(update={"start": round(best[0], 3), "end": round(best[1], 3),
                                "score": round(m.score * (1.0 - STATIC_PENALTY * cut_share), 4)})


def _bounds(t: Transcript, signals: MomentSignals | None) -> _Bounds:
    starts = sorted({0.0, *(float(s.start) for s in t.segments)})
    ends = sorted({float(t.duration), *(float(s.end) for s in t.segments)})
    if signals is None:
        return _Bounds(starts, ends)
    # a cut is detected at the first sample of the new shot: start there, end one sample before it
    fb_starts = sorted({*signals.cuts, *signals.quiet})
    fb_ends = sorted({*(max(0.0, c - CUT_END_MARGIN) for c in signals.cuts), *signals.quiet})
    if signals.low_speech:
        return _Bounds(starts, ends, fb_starts, fb_ends, SNAP_TOLERANCE_LOW_SPEECH, FALLBACK_TOLERANCE_LOW_SPEECH)
    return _Bounds(starts, ends, fb_starts, fb_ends, SNAP_TOLERANCE, FALLBACK_TOLERANCE)


def validate_moments(raw: list[Moment], t: Transcript, *, count: int, min_seconds: float,
                     max_seconds: float, signals: MomentSignals | None = None,
                     rejected: list[tuple[Moment, str]] | None = None) -> list[Moment]:
    """Clamp, snap to segment boundaries, enforce min/max, drop overlaps, sort by score, top N.

    With ``signals`` (feature "moment picking for low-speech videos"): a moment with no sentence boundary
    nearby snaps to a scene cut or a quiet point instead. In a low-speech video only (speech-rich videos are
    picked from the transcript exactly as before): a static/menu/silent stretch at either end is trimmed off,
    one of 3 s or more in the middle splits the moment (the longest part that is long enough is kept), a partly
    static moment is ranked down, and a moment that is mostly static/menu/silent and mostly without
    speech is rejected (appended to ``rejected`` with the reason) - unless that would leave no moment at
    all, then the best rejected ones are kept (reason suffixed " - kept, nothing better").
    """
    min_s, max_s = float(min_seconds), float(max(max_seconds, min_seconds))
    b = _bounds(t, signals)
    fitted: list[Moment] = []
    dropped: list[tuple[Moment, str]] = []
    for m in raw:
        f = _fit_one(m, t, b, None, min_s, max_s)
        if f is None:
            continue
        if signals is not None and signals.low_speech:
            f = _split_at_avoided(_trim_avoided_edges(f, signals, min_s), signals, min_s)
            avoid = signals.static_fraction(f.start, f.end)
            speech = signals.speech_fraction(f.start, f.end)
            if avoid >= signals.reject_static_fraction - _EPS and speech < signals.static_speech_max:
                dropped.append((f, f"{avoid:.0%} static/menu/silent screen, {speech:.0%} speech"))
                continue
            if avoid > 0:
                f = f.model_copy(update={"score": round(f.score * (1.0 - STATIC_PENALTY * avoid), 4)})
        fitted.append(f)
    kept = _top(fitted, count)
    fallback: list[Moment] = []
    if not kept and dropped:  # never fail a job over the post-filter: keep the best of what was rejected
        fallback = _top([m for m, _ in dropped], count)
        kept = fallback
    if rejected is not None:
        for m, why in dropped:
            rejected.append((m, why + (" - kept, nothing better" if any(m is k for k in fallback) else "")))
    return kept


def _top(moments: list[Moment], count: int) -> list[Moment]:
    """Best first, without overlaps, at most ``count``."""
    kept: list[Moment] = []
    for m in sorted(moments, key=lambda m: m.score, reverse=True):
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


@dataclass
class MomentsPlan:
    """How the moments are picked for one video (feature "moment picking for low-speech videos")."""
    signals: MomentSignals | None
    signals_text: str = ""        # legend + per-window table for the LLM ("" = transcript only)
    ask: int = 0                  # how many candidates the LLM is asked for

    @property
    def low_speech(self) -> bool:
        return bool(self.signals_text)


EXTRA_CANDIDATES = 0.5  # low-speech videos: ask for 50 % more candidates (at least 2), the post-filter drops some


def plan_moments(t: Transcript, settings: ClipSettings, signals: Signals | None = None,
                 signal_settings: object | None = None) -> MomentsPlan:
    """Speech-rich video (or no signals): transcript only, exactly as before (plus the static post-filter).
    Low-speech video: the LLM also gets the signal table and is asked for extra candidates."""
    count = settings.count
    if signals is None or signal_settings is None:
        return MomentsPlan(None, "", count)
    ms = moment_signals(signals, t, speech_rich_coverage=float(signal_settings.speech_rich_coverage),
                        reject_static_fraction=float(signal_settings.reject_static_fraction))
    if not ms.low_speech:
        return MomentsPlan(ms, "", count)
    w = window_size(signals.duration, float(signal_settings.window_seconds))
    table = window_summary(signals, t, window=w)
    text = legend(w, signals.has_video) + "\n" + table
    extra = max(2, math.ceil(count * EXTRA_CANDIDATES))
    return MomentsPlan(ms, text, count + extra)


def pick_candidates(t: Transcript, llm: LLM, settings: ClipSettings, *, signals: Signals | None = None,
                    signal_settings: object | None = None, context: str = "", ask: int | None = None,
                    keep: int | None = None, plan: MomentsPlan | None = None,
                    log: Callable[[str], None] | None = None) -> list[Moment]:
    """Ask the LLM for ``ask`` moments (default: the plan's count) and validate them; at most ``keep``
    (default settings.count) are returned, best first. The vision check asks for and keeps extra candidates
    (stages.vision.candidate_count). Raises MomentsError when nothing usable comes back."""
    if plan is None:
        plan = plan_moments(t, settings, signals, signal_settings)
    text = format_transcript(t)
    if not text.strip():
        if not plan.low_speech:
            raise MomentsError("the transcript is empty (no speech found), so no moments can be picked")
        text = "(no speech was found in this video)"
    optional = {"context": context or None}
    if plan.low_speech and plan.signals is not None:
        optional.update(signals=plan.signals_text, speech_coverage=plan.signals.coverage)
    raw = call_with_optional(llm.pick_moments, text, count=ask or plan.ask or settings.count,
                             min_seconds=settings.min_seconds, max_seconds=settings.max_seconds,
                             video_duration=t.duration, optional=optional)
    rejected: list[tuple[Moment, str]] = []
    moments = validate_moments(raw, t, count=keep or settings.count, min_seconds=settings.min_seconds,
                               max_seconds=settings.max_seconds, signals=plan.signals, rejected=rejected)
    if log is not None:
        for m, why in rejected:
            log(f"moment {m.start:.1f}-{m.end:.1f}s rejected: {why}")
    if not moments:
        raise MomentsError(
            f"the LLM returned {len(raw)} moment(s) but none were valid "
            f"({settings.min_seconds:g}-{settings.max_seconds:g}s, inside the video, non-overlapping)"
        )
    return moments


def run_moments(t: Transcript, llm: LLM, settings: ClipSettings, out_json: Path, *,
                signals: Signals | None = None, signal_settings: object | None = None, context: str = "",
                log: Callable[[str], None] | None = None) -> list[Moment]:
    """Idempotent: reuse out_json if present; else ask the LLM, validate and write.

    ``signals`` + ``signal_settings`` (config.SignalSettings) turn on the low-speech path and the static/menu
    post-filter; ``context`` is the delimited source context block (stages.source_info). An LLM whose
    pick_moments predates these arguments gets the call it always got.
    """
    out_json = Path(out_json)
    if out_json.is_file() and out_json.stat().st_size > 0:
        return load_moments(out_json)
    moments = pick_candidates(t, llm, settings, signals=signals, signal_settings=signal_settings,
                              context=context, log=log)
    save_moments(moments, out_json)
    return moments
