"""Captions from the ORIGINAL speech (voice-over off): no narration, the clip keeps its own audio.

Same language as the transcript: Whisper's word timings for the clip range, re-based to clip time.
Other language: the clip's transcript lines are translated by the LLM (one call per clip-language, ids
kept) and each translated line is timed over its original line's span, words spread by character length
over the spoken parts (long pauses skipped; very short lines held for a readable minimum).
The translation is cached in ``caption_translation.json`` so a re-render does not call the LLM again
(a cache missing some lines is completed by asking again).

Writes words.json and captions.ass exactly like the voice-over captions stage, so both caption engines
(ass, hyperframes) work unchanged, plus the ``captions.json`` stamp (see ``write_captions_stamp``).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import Settings
from ..models import Moment, Transcript, Word
from ._atomic import write_text_atomic
from .captions import CAPTIONS_STAMP, CaptionsError, build_ass, captions_voiceover, distribute_words, write_captions_stamp

__all__ = ["CAPTIONS_STAMP", "captions_voiceover", "make_original_captions", "clip_segments",
           "source_words", "translated_words", "translate_lines", "same_language"]

log = logging.getLogger(__name__)

TRANSLATION_CACHE = "caption_translation.json"
# A word or a word-less segment must overlap the clip by at least this much to be captioned.
_MIN_OVERLAP = 0.05
# A segment without word timings is captioned only when at least this share of it lies inside the clip.
_SEGMENT_SHARE = 0.5

# translate(lang=, source_lang=, segments=[{"id", "text"}]) -> {id: text}
TranslateFn = Callable[..., dict]


@dataclass
class ClipSegment:
    """One transcript segment inside the clip, in clip time (0 = clip start)."""
    id: int
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


def base_language(code: str | None) -> str:
    """'en', 'EN-us', 'en_GB' -> 'en'."""
    return (code or "").strip().lower().replace("_", "-").split("-")[0]


def same_language(transcript_language: str | None, lang: str) -> bool:
    return base_language(transcript_language) == base_language(lang)


def _clean(text: str) -> str:
    return " ".join(str(text).split())


def clip_segments(transcript: Transcript, start: float, end: float) -> list[ClipSegment]:
    """The transcript segments inside [start, end], re-based to clip time and clamped to the clip.

    With word timings, only the words that overlap the clip are kept, and the segment text is those
    words (so a segment cut by the clip edge is captioned and translated only for its heard part).
    Without word timings, a segment is kept when at least half of it lies inside the clip.
    """
    dur = max(0.0, end - start)
    out: list[ClipSegment] = []
    for i, seg in enumerate(transcript.segments):
        if seg.end <= start or seg.start >= end:
            continue
        if seg.words:
            words: list[Word] = []
            for w in seg.words:
                text = _clean(w.text)
                if not text:
                    continue
                s, e = w.start - start, w.end - start
                if min(e, dur) - max(s, 0.0) < _MIN_OVERLAP and not (s >= 0 and e <= dur):
                    continue
                s = min(max(0.0, s), dur)
                e = min(max(s, e), dur)
                words.append(Word(start=round(s, 3), end=round(e, 3), text=text))
            if not words:
                continue
            out.append(ClipSegment(id=i, start=words[0].start, end=words[-1].end,
                                   text=" ".join(w.text for w in words), words=words))
            continue
        text = _clean(seg.text)
        length = seg.end - seg.start
        inside = min(seg.end, end) - max(seg.start, start)
        if not text or inside < _MIN_OVERLAP or (length > 0 and inside / length < _SEGMENT_SHARE):
            continue
        s = max(0.0, seg.start - start)
        e = min(dur, seg.end - start)
        out.append(ClipSegment(id=i, start=round(s, 3), end=round(e, 3), text=text,
                               words=distribute_words(text.split(), e - s, start=s)))
    return out


def _monotonic(words: list[Word]) -> list[Word]:
    """Sorted by start, never overlapping the next word, never ending before they start."""
    words = sorted(words, key=lambda w: (w.start, w.end))
    out: list[Word] = []
    for k, w in enumerate(words):
        s = max(w.start, out[-1].end if out else 0.0)
        e = max(s, w.end)
        if k + 1 < len(words):
            e = min(e, max(s, words[k + 1].start))
        out.append(Word(start=round(s, 3), end=round(e, 3), text=w.text))
    return out


def source_words(segments: list[ClipSegment]) -> list[Word]:
    """Caption words in the transcript's own language: the (re-based) Whisper words."""
    return _monotonic([w for seg in segments for w in seg.words])


# A pause longer than this inside a line is not captioned over (translated words skip it).
_PAUSE = 0.6
# A translated line stays on screen at least this long (and _MIN_WORD_SECONDS per word), reaching into
# the silence after it, but never into the next line or past the clip end.
_MIN_LINE_SECONDS = 0.8
_MIN_WORD_SECONDS = 0.25


def _voiced_spans(seg: ClipSegment) -> list[tuple[float, float]]:
    """The parts of a line where words are spoken: its words merged across pauses up to _PAUSE."""
    spans: list[list[float]] = []
    for w in sorted(seg.words, key=lambda x: x.start):
        if spans and w.start - spans[-1][1] <= _PAUSE:
            spans[-1][1] = max(spans[-1][1], w.end)
        else:
            spans.append([w.start, max(w.start, w.end)])
    return [(a, b) for a, b in spans] or [(seg.start, max(seg.start, seg.end))]


def _spread(tokens: list[str], spans: list[tuple[float, float]]) -> list[Word]:
    """Spread tokens by character length over the spans (skipping the gaps between them)."""
    total = sum(b - a for a, b in spans)
    if total <= 0:
        return distribute_words(tokens, 0.0, start=spans[0][0])
    timed = distribute_words(tokens, total, start=0.0)

    offsets = []  # (span start, span end, spoken time before the span)
    acc = 0.0
    for a, b in spans:
        offsets.append((a, b, acc))
        acc += b - a

    out: list[Word] = []
    for w in timed:
        # each word belongs to the span holding its middle, so no word is shown across a pause
        mid = (w.start + w.end) / 2
        a, b, before = next((o for o in offsets if mid < o[2] + (o[1] - o[0])), offsets[-1])
        s = min(max(a + (w.start - before), a), b)
        e = min(max(a + (w.end - before), s), b)
        out.append(Word(start=round(s, 3), end=round(e, 3), text=w.text))
    return out


def translated_words(segments: list[ClipSegment], translations: dict[int, str],
                     clip_end: float | None = None) -> list[Word]:
    """Each translated line timed over its original line: words by character length over the parts
    where the line is spoken (long pauses skipped); a line too short to read is held a little longer,
    up to the next line or ``clip_end``."""
    words: list[Word] = []
    for k, seg in enumerate(segments):
        text = _clean(translations.get(seg.id, ""))
        if not text:
            log.warning("no translation for caption line %s; it is left out", seg.id)
            continue
        tokens = text.split()
        spans = _voiced_spans(seg)
        need = max(_MIN_LINE_SECONDS, _MIN_WORD_SECONDS * len(tokens))
        if sum(b - a for a, b in spans) < need:
            start = spans[0][0]
            limit = segments[k + 1].start if k + 1 < len(segments) else clip_end
            end = start + need if limit is None else min(start + need, max(limit, start))
            spans = [(start, max(end, spans[-1][1]))]
        words.extend(_spread(tokens, spans))
    return _monotonic(words)


def _source_hash(lang: str, source_lang: str, segments: list[ClipSegment]) -> str:
    payload = json.dumps({"lang": lang, "source": source_lang,
                          "lines": [[s.id, s.text] for s in segments]}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_cached_translation(path: Path, key: str) -> dict[int, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("source_hash") != key or not isinstance(data.get("segments"), list):
        return None
    out: dict[int, str] = {}
    for item in data["segments"]:
        if isinstance(item, dict) and isinstance(item.get("id"), int) and isinstance(item.get("text"), str):
            out[item["id"]] = item["text"]
    return out


def _parse_translation(result: object, wanted: set[int]) -> dict[int, str]:
    """{id: text} for the wanted ids; ids that are not whole numbers, and empty texts, are ignored."""
    out: dict[int, str] = {}
    for k, v in (result.items() if isinstance(result, dict) else ()):
        try:
            i = int(str(k).strip())
        except (TypeError, ValueError):
            continue
        if i in wanted and isinstance(v, str) and _clean(v):
            out[i] = _clean(v)
    return out


def translate_lines(segments: list[ClipSegment], *, lang: str, source_lang: str,
                    translate: TranslateFn | None, out_dir: Path) -> dict[int, str]:
    """{segment id: text in ``lang``}: from the cache when the source lines are unchanged, else the LLM."""
    if not segments:
        return {}
    cache = Path(out_dir) / TRANSLATION_CACHE
    key = _source_hash(lang, source_lang, segments)
    wanted = {s.id for s in segments}
    cached = _load_cached_translation(cache, key) or {}
    if cached and wanted <= set(cached):
        return cached
    # No cache, or a partial one (the LLM left lines out last time): ask again, keep what we had.
    if translate is None:
        raise CaptionsError(f"cannot translate the captions into {lang}: the language model has no "
                            "translate_segments")
    result = translate(lang=lang, source_lang=base_language(source_lang),
                       segments=[{"id": s.id, "text": s.text} for s in segments])
    out = {**{i: v for i, v in cached.items() if i in wanted}, **_parse_translation(result, wanted)}
    if not out:
        raise CaptionsError(f"the {lang} caption translation came back empty")
    missing = sorted(wanted - set(out))
    if missing:
        log.warning("caption translation into %s is missing line(s) %s (asked again on the next render)",
                    lang, missing)
    write_text_atomic(cache, json.dumps({
        "lang": lang, "source_language": base_language(source_lang), "source_hash": key,
        "segments": [{"id": s.id, "source": s.text, "text": out[s.id]} for s in segments if s.id in out],
    }, indent=2, ensure_ascii=False))
    return out


def make_original_captions(*, transcript: Transcript, moment: Moment, clip_duration: float, lang: str,
                           settings: Settings, out_dir: Path,
                           translate: TranslateFn | None = None) -> Path:
    """Write out_dir/words.json, captions.ass and captions.json from the original speech; return the .ass."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    end = moment.start + (clip_duration if clip_duration > 0 else moment.end - moment.start)
    segments = clip_segments(transcript, moment.start, end)
    if same_language(transcript.language, lang):
        words = source_words(segments)
        source = "transcript"
    else:
        translations = translate_lines(segments, lang=lang, source_lang=transcript.language,
                                       translate=translate, out_dir=out_dir)
        words = translated_words(segments, translations, clip_end=end - moment.start)
        source = "translation"
    write_text_atomic(out_dir / "words.json",
                      json.dumps([w.model_dump() for w in words], indent=2, ensure_ascii=False))
    ass = out_dir / "captions.ass"
    write_text_atomic(ass, build_ass(words, lang=lang, settings=settings.captions,
                                     width=settings.clips.width, height=settings.clips.height))
    write_captions_stamp(out_dir, voiceover=False, source=source,
                         transcript_language=base_language(transcript.language), lines=len(segments))
    return ass
