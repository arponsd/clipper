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
from .moments import format_transcript

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


def _clean_title(title: str) -> str:
    title = " ".join(title.split()).strip().strip('"').strip()
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
    """Title <= 70 chars; hashtags "#x", no spaces, deduped, at most 8; whitespace tidied."""
    return ClipScript(
        narration=" ".join(s.narration.split()),
        title=_clean_title(s.title),
        description=s.description.strip(),
        hashtags=_clean_hashtags(s.hashtags),
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


def write_script(*, llm: LLM, transcript: Transcript, moment: Moment, lang: str,
                 out_json: Path, script_settings: ScriptSettings | None = None) -> ClipScript:
    """Ask the LLM for the script, shorten once if far too long, normalize and write out_json.

    With ``script_settings`` the word target uses its per-language words/s and fill_ratio, and the
    LLM is also told the spoken duration to fill (target_seconds). Without it, step-1 constants.
    """
    duration = moment.end - moment.start
    clip_transcript = format_transcript(transcript, moment.start, moment.end)
    if script_settings is None:
        target = target_words(duration, lang)
        script = llm.write_script(lang=lang, clip_transcript=clip_transcript, moment=moment,
                                  duration=duration, target_words=target)
    else:
        target = target_words(duration, lang, wps=script_settings.wps(lang),
                              fill_ratio=script_settings.fill_ratio)
        target_seconds = round(duration * script_settings.fill_ratio, 1)
        script = llm.write_script(lang=lang, clip_transcript=clip_transcript, moment=moment,
                                  duration=duration, target_words=target, target_seconds=target_seconds)
    if not script.narration.strip():
        raise ScriptError(f"the LLM returned an empty {lang} narration")
    if count_words(script.narration) > target * SHORTEN_THRESHOLD:
        shorter = llm.shorten(lang=lang, narration=script.narration, max_words=target)
        if shorter.strip():
            script = script.model_copy(update={"narration": shorter})
    script = normalize_script(script)
    save_script(script, out_json)
    return script


def make_shorten_fn(llm: LLM, lang: str) -> ShortenFn:
    """ShortenFn for the voice stage: (narration, max_words) -> shorter narration in `lang`."""

    def shorten(narration: str, max_words: int) -> str:
        return llm.shorten(lang=lang, narration=narration, max_words=max_words)

    return shorten

