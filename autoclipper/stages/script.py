"""Per-clip, per-language narration script (new voice-over commentary) + title/description/hashtags."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from ..config import ScriptSettings
from ..interfaces import LLM, ShortenFn
from ..models import ClipScript, Moment, Transcript
from ._atomic import write_text_atomic
from ._optional import call_with_optional
from .moments import format_transcript
from .source_info import (SourceInfo, apply_source_credit, context_for_prompt, drop_misleading_hashtags,
                          ensure_game_hashtag)

WORDS_PER_SECOND = {"en": 2.6, "hi": 2.2}
FILL_RATIO = 0.9
MIN_TARGET_WORDS = 5
SHORTEN_THRESHOLD = 1.15
TITLE_MAX = 70
MAX_HASHTAGS = 8


class ScriptError(RuntimeError):
    """Script writing failed or script.json is unreadable."""


def count_words(text: str) -> int:
    return len(text.split())


def target_words(duration: float, lang: str, *, wps: float | None = None,
                 fill_ratio: float = FILL_RATIO) -> int:
    """Narration word count for a clip: duration x fill_ratio x words-per-second.

    Without ``wps`` the step-1 per-language constants are used (settings.script supplies the
    measured rates in the pipeline).
    """
    if wps is None:
        wps = WORDS_PER_SECOND.get(lang, WORDS_PER_SECOND["en"])
    return max(MIN_TARGET_WORDS, round(max(0.0, duration) * wps * fill_ratio))


_DQ = '"'
_CURLY = ("\u201c", "\u201d")  # left / right double quotation marks
_WRAPPERS = (('"', '"'), ("\u201c", "\u201d"), ("\u201e", "\u201c"), ("\u00ab", "\u00bb"))


_INCH_RE = re.compile(r'(?<=\d)"(?=[\s\-\u2013\u2014,.;:!?)]|$)')  # 5" screen, 27" monitor


def _drop_unpaired_curly(t: str) -> str:
    """Remove curly double quotes without a partner: a closing one with no open quote before it, and an
    opening one never closed. Paired quotes stay."""
    lq, rq = _CURLY
    keep = [True] * len(t)
    opened: list[int] = []
    for i, ch in enumerate(t):
        if ch == lq:
            opened.append(i)
        elif ch == rq:
            if opened:
                opened.pop()
            else:
                keep[i] = False
    for i in opened:
        keep[i] = False
    return "".join(ch for ch, k in zip(t, keep) if k)


def _fix_quotes(title: str) -> str:
    """Drop quotes that wrap the whole title, then any unpaired double quote.

    '"A title"' -> 'A title'; '"Quoted" - rest' stays as is (a balanced pair is kept);
    'Quoted" - rest' (an LLM or an old clean-up dropped the opening quote) -> 'Quoted - rest';
    '\u201cQuoted\u201d - rest\u201d' -> '\u201cQuoted\u201d - rest'. An inch mark after a digit ('5" screen')
    is not a quote and is kept. Apostrophes / single quotes are left alone ("Reagan's", "'Garbage'").
    """
    t = title.strip()
    changed = True
    while changed and len(t) >= 2:
        changed = False
        for left, right in _WRAPPERS:
            inner = t[len(left):-len(right)]
            if t.startswith(left) and t.endswith(right) and left not in inner and right not in inner:
                t, changed = inner.strip(), True
    inch = "\u0000"  # placeholder: inch marks take no part in the pairing
    t = _INCH_RE.sub(inch, t)
    if t.count(_DQ) % 2:  # unbalanced straight quotes: drop the last one that has no partner
        i = t.rfind(_DQ) if t.count(_DQ) > 1 else t.find(_DQ)
        t = t[:i] + t[i + 1:]
    t = _drop_unpaired_curly(t).replace(inch, _DQ)
    t = re.sub(r"\s+([,.;:!?])", r"\1", " ".join(t.split()))
    return t.strip()


def fix_title_quotes(title: str) -> str:
    """Public: the quote clean-up of _clean_title (also applied to titles of older scripts)."""
    return _fix_quotes(" ".join((title or "").split())) if title else title


def _clean_title(title: str) -> str:
    title = _fix_quotes(" ".join(title.split()).strip())
    if len(title) <= TITLE_MAX:
        return title
    cut = title[:TITLE_MAX]
    space = cut.rfind(" ")
    if space >= TITLE_MAX // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:-|")


def _tag_chars(text: str) -> str:
    """Keep letters, combining marks (Devanagari matras), digits and underscore.

    Note: re's \\w drops Devanagari vowel signs (category Mn/Mc), so filter by category instead.
    """
    return "".join(ch for ch in text
                   if ch == "_" or unicodedata.category(ch)[0] in ("L", "M", "N"))


def _clean_hashtags(tags: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        # a single entry may hold several tags ("#a #b") - split on whitespace/commas
        for piece in re.split(r"[\s,]+", str(raw)):
            body = piece.lstrip("#").strip()
            body = _tag_chars(body)
            if not body:
                continue
            key = body.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(f"#{body}")
    return out[:MAX_HASHTAGS]


def normalize_script(s: ClipScript) -> ClipScript:
    """Title <= 70 chars; hashtags "#x", no spaces, deduped, at most 8; whitespace tidied; the hook one clean
    line (G4); keywords cleaned and deduplicated, an emoji kept only when it is one emoji (G5)."""
    from .caption_style import clean_hook, clean_keywords

    return ClipScript(
        narration=" ".join(s.narration.split()),
        title=_clean_title(s.title),
        description=s.description.strip(),
        hashtags=_clean_hashtags(s.hashtags),
        hook=clean_hook(s.hook),
        keywords=clean_keywords(s.keywords),
    )


def save_script(s: ClipScript, path: Path) -> Path:
    text = json.dumps(s.model_dump(), ensure_ascii=False, indent=2)
    return write_text_atomic(Path(path), text)


def load_script(path: Path) -> ClipScript:
    try:
        return ClipScript.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ScriptError(f"script not found: {path}") from e
    except Exception as e:  # noqa: BLE001
        raise ScriptError(f"could not read {path}: {e}; fix the JSON or delete it to regenerate") from e


def apply_source(script: ClipScript, source: SourceInfo | None, lang: str, *, credit: bool = True) -> ClipScript:
    """Source-context fixes on LLM metadata: game footage loses hashtags that sell it as real life, news or a
    movie and gets the game's Latin hashtag (#GTA6) when missing, and (``credit``) the description credits the original creator by name with the source URL."""
    if source is None:
        return script
    tags = ensure_game_hashtag(drop_misleading_hashtags(script.hashtags, source), source)
    desc = apply_source_credit(script.description, source, lang) if credit else script.description
    if tags == script.hashtags and desc == script.description:
        return script
    return script.model_copy(update={"hashtags": tags, "description": desc})


def _generate_script(*, llm: LLM, transcript: Transcript, moment: Moment, lang: str,
                     script_settings: ScriptSettings | None = None, source: SourceInfo | None = None) -> ClipScript:
    duration = moment.end - moment.start
    clip_transcript = format_transcript(transcript, moment.start, moment.end)
    optional = {"context": context_for_prompt(source)}
    if script_settings is None:
        target = target_words(duration, lang)
        script = call_with_optional(llm.write_script, lang=lang, clip_transcript=clip_transcript, moment=moment,
                                    duration=duration, target_words=target, optional=optional)
    else:
        target = target_words(duration, lang, wps=script_settings.wps(lang),
                              fill_ratio=script_settings.fill_ratio)
        target_seconds = round(duration * script_settings.fill_ratio, 1)
        script = call_with_optional(llm.write_script, lang=lang, clip_transcript=clip_transcript, moment=moment,
                                    duration=duration, target_words=target, target_seconds=target_seconds,
                                    optional=optional)
    if not script.narration.strip():
        raise ScriptError(f"the LLM returned an empty {lang} narration")
    if count_words(script.narration) > target * SHORTEN_THRESHOLD:
        shorter = llm.shorten(lang=lang, narration=script.narration, max_words=target)
        if shorter.strip():
            script = script.model_copy(update={"narration": shorter})
    from .caption_style import check_llm_extras

    # keywords are checked against the FINAL narration (after any shortening), the hook against the language
    return check_llm_extras(normalize_script(script), lang, voiceover=True)


def write_script(*, llm: LLM, transcript: Transcript, moment: Moment, lang: str,
                 out_json: Path, script_settings: ScriptSettings | None = None,
                 source: SourceInfo | None = None, credit: bool = True) -> ClipScript:
    """Ask the LLM for the script, shorten once if far too long, normalize and write out_json.

    With ``script_settings`` the word target uses its per-language words/s and fill_ratio, and the
    LLM is also told the spoken duration to fill (target_seconds). Without it, step-1 constants.
    ``source`` (stages.source_info) is given to the LLM as delimited context and applied by apply_source.
    """
    script = _generate_script(llm=llm, transcript=transcript, moment=moment, lang=lang,
                              script_settings=script_settings, source=source)
    script = apply_source(script, source, lang, credit=credit)
    save_script(script, out_json)
    return script


def write_metadata_script(*, llm: LLM, transcript: Transcript, moment: Moment, lang: str,
                          out_json: Path, source: SourceInfo | None = None, credit: bool = True) -> ClipScript:
    """Voice-over off: script.json with title/description/hashtags in ``lang`` and an empty narration.

    Uses the LLM's optional ``write_publish_metadata``; an LLM without it writes a normal script, whose
    narration is then dropped (it would never be spoken).
    """
    duration = moment.end - moment.start
    clip_transcript = format_transcript(transcript, moment.start, moment.end)
    optional = {"context": context_for_prompt(source)}
    fn = getattr(llm, "write_publish_metadata", None)
    if callable(fn):
        script = call_with_optional(fn, lang=lang, clip_transcript=clip_transcript, moment=moment,
                                    duration=duration, optional=optional)
    else:
        script = call_with_optional(llm.write_script, lang=lang, clip_transcript=clip_transcript, moment=moment,
                                    duration=duration, target_words=target_words(duration, lang), optional=optional)
    from .caption_style import check_llm_extras

    script = check_llm_extras(normalize_script(script.model_copy(update={"narration": ""})), lang, voiceover=False)
    script = apply_source(script, source, lang, credit=credit)
    if not script.title:
        raise ScriptError(f"the LLM returned an empty {lang} title")
    save_script(script, out_json)
    return script


def add_narration(*, llm: LLM, transcript: Transcript, moment: Moment, lang: str, script: ClipScript,
                  out_json: Path, script_settings: ScriptSettings | None = None,
                  source: SourceInfo | None = None) -> ClipScript:
    """Voice-over turned on for a script written without narration: ask the LLM for a narration and keep
    the existing (possibly user-edited) title, description and hashtags."""
    fresh = _generate_script(llm=llm, transcript=transcript, moment=moment, lang=lang,
                             script_settings=script_settings, source=source)
    # the keywords must come from the new narration (the captions now show it); a hook already there is kept
    merged = script.model_copy(update={"narration": fresh.narration, "keywords": fresh.keywords,
                                       "hook": script.hook or fresh.hook})
    save_script(merged, out_json)
    return merged


def make_shorten_fn(llm: LLM, lang: str) -> ShortenFn:
    """ShortenFn for the voice stage: (narration, max_words) -> shorter narration in `lang`."""

    def shorten(narration: str, max_words: int) -> str:
        return llm.shorten(lang=lang, narration=narration, max_words=max_words)

    return shorten

