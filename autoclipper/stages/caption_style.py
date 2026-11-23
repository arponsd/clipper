"""Caption look shared by both caption engines: keyword emphasis (G5), the hook line (G4), clean-up of the
hook / keyword text the LLM or the user writes, and the style key that marks a render stale.

Both engines (captions.build_ass and hyperframes.build_composition) take their emphasis marks and hook text
from here, so the ASS burn-in, the HyperFrames overlay and the ASS fallback always agree.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from ..config import CaptionSettings, HookSettings, Settings
from ..models import ClipScript, Keyword, Word

HOOK_MAX_CHARS = 100
HOOK_MAX_WORDS = 12  # the LLM is asked for <= 8; a user may write a few more
KEYWORD_MAX_CHARS = 40
KEYWORD_MAX_WORDS = 3
MAX_KEYWORDS = 8
EMOJI_MAX_CODEPOINTS = 10
# a keyword (of at least _PREFIX_MIN letters) also matches the same word with a known inflection ending:
# "clutch" ~ "clutched", "steal" ~ "steals", Hindi stem + case/plural ending. Any other longer word does not
# match ("star" is not "start", "pain" is not "paint").
_PREFIX_MIN = 4
_LATIN_ENDINGS = frozenset({"s", "es", "ed", "d", "ing", "er", "ers", "est"})
_DEVANAGARI_ENDINGS = frozenset({"\u093e", "\u0940", "\u0947", "\u094b", "\u0902", "\u094b\u0902",
                                 "\u0947\u0902", "\u093e\u0902", "\u0913\u0902", "\u093e\u0913\u0902",
                                 "\u092f\u094b\u0902", "\u0928\u0947", "\u0915\u094b"})

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_WRAP_QUOTES = (('"', '"'), ("“", "”"), ("'", "'"), ("‘", "’"), ("«", "»"))
# characters that may appear inside an emoji sequence without being an emoji themselves
_EMOJI_JOINERS = {0x200D, 0xFE0F, 0xFE0E, 0x20E3} | set(range(0x1F3FB, 0x1F400)) | set(range(0xE0020, 0xE0080))


def _is_emoji_cp(cp: int) -> bool:
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF
            or 0x2190 <= cp <= 0x21FF or 0x2300 <= cp <= 0x23FF or 0x25A0 <= cp <= 0x25FF
            or cp in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299))


def is_emoji(text: str) -> bool:
    """True for exactly ONE emoji (e.g. "🔥", "👍🏽", "❤️", "🏳️‍🌈", "🇮🇳"); False for letters, digits, spaces
    and for several emoji in a row ("🔥🔥"). Skin tones, variation selectors and tags belong to the emoji
    before them; a zero-width joiner glues two emoji into one; two regional indicators make one flag."""
    if not text or len(text) > EMOJI_MAX_CODEPOINTS:
        return False
    units = 0
    joined = False
    flag_half = False
    for ch in text:
        cp = ord(ch)
        if cp == 0x200D:
            joined = True
            continue
        if cp in _EMOJI_JOINERS:
            continue
        if not _is_emoji_cp(cp):
            return False
        if 0x1F1E6 <= cp <= 0x1F1FF:  # regional indicator: a pair is one flag
            if flag_half:
                flag_half = False
                continue
            flag_half = True
        if not joined:
            units += 1
        joined = False
    return units == 1


def emoji_error(text: str) -> str | None:
    if text == "":
        return None
    return None if is_emoji(text) else "must be one single emoji (e.g. 🔥) or empty"


def _strip_wrapping_quotes(t: str) -> str:
    changed = True
    while changed and len(t) >= 2:
        changed = False
        for left, right in _WRAP_QUOTES:
            if t.startswith(left) and t.endswith(right) and len(t) > len(left) + len(right) - 1:
                inner = t[len(left):len(t) - len(right)]
                if left not in inner and right not in inner:
                    t, changed = inner.strip(), True
    return t


def clean_hook(text: str | None) -> str:
    """One line, no control characters, no wrapping quotes, at most HOOK_MAX_CHARS (cut at a word)."""
    if not text:
        return ""
    t = " ".join(_CONTROL.sub(" ", str(text)).split())
    t = _strip_wrapping_quotes(t)
    words = t.split()
    if len(words) > HOOK_MAX_WORDS:
        t = " ".join(words[:HOOK_MAX_WORDS])
    if len(t) > HOOK_MAX_CHARS:
        cut = t[:HOOK_MAX_CHARS]
        space = cut.rfind(" ")
        t = cut[:space] if space >= HOOK_MAX_CHARS // 2 else cut
    return t.strip()


def hook_error(text: str) -> str | None:
    """Why a hook typed by the user is not accepted (None = ok). Line breaks are simply joined."""
    t = " ".join(_CONTROL.sub(" ", text).split())
    if len(t) > HOOK_MAX_CHARS:
        return f"hook must be at most {HOOK_MAX_CHARS} characters"
    if len(t.split()) > HOOK_MAX_WORDS:
        return f"hook must be at most {HOOK_MAX_WORDS} words (about 8 reads best)"
    return None


def clean_keyword_word(text: str | None) -> str:
    return " ".join(_CONTROL.sub(" ", str(text or "")).split())


def keyword_error(word: str) -> str | None:
    w = clean_keyword_word(word)
    if not w:
        return "keyword must not be empty"
    if len(w) > KEYWORD_MAX_CHARS:
        return f"keyword must be at most {KEYWORD_MAX_CHARS} characters"
    if len(w.split()) > KEYWORD_MAX_WORDS:
        return f"keyword must be at most {KEYWORD_MAX_WORDS} words"
    if not any(_norm(t) for t in w.split()):
        return "keyword must contain a letter or digit"
    return None


def clean_keywords(items: Iterable[Any]) -> list[Keyword]:
    """Keywords from the LLM or the user: invalid ones dropped, emoji kept only when it is one emoji,
    duplicates (same matching key) removed, at most MAX_KEYWORDS."""
    out: list[Keyword] = []
    seen: set[tuple[str, ...]] = set()
    for item in items or []:
        if isinstance(item, Keyword):
            word, emoji = item.word, item.emoji
        elif isinstance(item, dict):
            word, emoji = item.get("word"), item.get("emoji")
        elif isinstance(item, str):
            word, emoji = item, ""
        else:
            continue
        word = clean_keyword_word(word)
        if keyword_error(word):
            continue
        emoji = str(emoji or "").strip()
        if not is_emoji(emoji):
            emoji = ""
        key = tuple(_norm(t) for t in word.split())
        if key in seen:
            continue
        seen.add(key)
        out.append(Keyword(word=word, emoji=emoji))
    return out[:MAX_KEYWORDS]


# ------------------------------------------------------------------------------------------------ matching
def _norm(token: str) -> str:
    from .captions import _norm as norm  # the same folding the caption aligner uses (Devanagari variants)

    return norm(token)


def _token_matches(token: str, key: str) -> bool:
    if not token or not key:
        return False
    if token == key:
        return True
    if len(key) < _PREFIX_MIN or not token.startswith(key):
        return False
    rest = token[len(key):]
    if rest in _LATIN_ENDINGS or rest in _DEVANAGARI_ENDINGS:
        return True
    # a doubled final consonant: "stop" ~ "stopped", "win" is too short anyway
    return rest[:1] == key[-1:] and rest[1:] in ("ed", "ing", "er", "ers")


def emphasis_marks(words: list[Word], keywords: list[Keyword], *, per_line: int, max_per_line: int = 2
                   ) -> dict[int, str]:
    """Which caption words to emphasise: {word index: emoji ("" = none)}.

    Words are chunked into lines exactly like the captions (``per_line``). A keyword (one to three words)
    matches a run of caption words, compared with the aligner's folding (case, punctuation, Devanagari
    spelling variants) and allowing a short inflection ending. At most ``max_per_line`` keyword matches per
    line; a keyword's emoji goes after its last word, on its first match in the clip only.
    """
    keys = [(tuple(k for k in (_norm(t) for t in kw.word.split()) if k), kw.emoji) for kw in keywords]
    keys = [(k, e) for k, e in keys if k]
    if not keys or not words:
        return {}
    per_line = max(1, int(per_line))
    norms = [_norm(w.text) for w in words]
    marks: dict[int, str] = {}
    emoji_used: set[int] = set()
    for start in range(0, len(words), per_line):
        end = min(len(words), start + per_line)
        found = 0
        i = start
        while i < end and found < max_per_line:
            hit = None
            for ki, (key, _emoji) in enumerate(keys):
                n = len(key)
                if i + n <= end and all(_token_matches(norms[i + j], key[j]) for j in range(n)):
                    hit = (ki, n)
                    break
            if hit is None:
                i += 1
                continue
            ki, n = hit
            for j in range(i, i + n):
                marks.setdefault(j, "")
            emoji = keys[ki][1]
            if emoji and ki not in emoji_used:
                marks[i + n - 1] = emoji
                emoji_used.add(ki)
            found += 1
            i += n
    return marks


def display_text(text: str, uppercase: bool) -> str:
    """The text as drawn: upper-cased when asked (scripts without case, like Devanagari, are unchanged)."""
    return text.upper() if uppercase else text


# ------------------------------------------------------------------------------------------------ hook
def hook_text(script: ClipScript | None, settings: Settings) -> str:
    """The hook line to draw for this clip-language ("" = none: hook off, or no hook in script.json)."""
    if script is None or not settings.hook.enabled:
        return ""
    t = clean_hook(script.hook)
    return display_text(t, settings.hook.uppercase) if t else ""


def hook_end(settings: Settings, clip_duration: float | None) -> float:
    """When the hook disappears (seconds): hook.duration, never past the end of the clip. Both engines."""
    d = float(settings.hook.duration)
    if clip_duration is not None and clip_duration > 0:
        d = min(d, float(clip_duration))
    return round(d, 3)


# Hook layout, shared by both engines. Sizes are ASS units = video pixels (PlayResY = the video height); an
# ASS font size N gives a line about N px tall (ascent + descent), and HyperFrames draws N x em_ratio CSS px.
HOOK_SIDE_MARGIN = 1 / 12  # of the width, each side: the hook wraps inside the middle 5/6 (900 px of 1080)
HOOK_CAPTION_GAP = 0.02  # of the height: the least gap between the hook's bottom and the captions' top
HOOK_MIN_SCALE = 0.55  # a long hook is drawn down to 55 % of hook.font_size to fit above the captions


@dataclass(frozen=True)
class HookLayout:
    top_px: int  # top edge of the box (box style) or of the outlined text (plain)
    font_size: int  # ASS font size actually used (<= hook.font_size)
    pad_px: int  # box padding (box style) or outline width (plain), around the text
    lines: int  # estimated number of lines
    height_px: int  # estimated height of the whole block (text + padding)
    wrap_px: int  # the text wraps inside this width (centred)


def hook_pad(style: str, font_size: int) -> int:
    return max(10, round(font_size * 0.18)) if style == "box" else max(4, round(font_size / 14))


def _char_em(ch: str, upper: bool) -> float:
    """Rough advance of one character in ASS font-size units (bold Noto Sans / Noto Sans Devanagari). Slightly
    generous, so the estimate errs towards one line too many (the hook is then a bit smaller, never cut)."""
    if ch == " ":
        return 0.21
    cat = unicodedata.category(ch)
    if cat == "Mn":
        return 0.0
    if "\u0900" <= ch <= "\u097f":
        return 0.22 if cat == "Mc" else 0.50
    if ch.isupper() or (upper and ch.isalpha()):
        return 0.56
    if ch.isdigit():
        return 0.44
    if cat.startswith("P"):
        return 0.24
    return 0.46


def estimate_lines(text: str, font_size: int, wrap_px: int, *, upper: bool = False) -> int:
    """How many lines ``text`` wraps to inside ``wrap_px`` (greedy word wrap, a long word on its own line)."""
    space = 0.21 * font_size
    lines, cur = 0, 0.0
    for word in text.split():
        w = sum(_char_em(c, upper) for c in word) * font_size
        if cur and cur + space + w <= wrap_px:
            cur += space + w
            continue
        if cur:
            lines += 1
        cur = w
        while cur > wrap_px:  # a word longer than the line breaks (libass / CSS overflow-wrap)
            lines += 1
            cur -= wrap_px
    return lines + (1 if cur else 0) or 1


def caption_top_px(c: CaptionSettings, height: int) -> float:
    """Where the caption line starts (its top edge, px from the top) - for keeping the hook above it.
    One line at its biggest (pop / emphasis scale, box padding) around position_pct, or above margin_v."""
    scale = max(1.0, c.emphasis_scale if c.emphasis else 1.0, 1.15 if c.highlight_style == "pop" else 1.0)
    line = c.font_size * scale + (2 * max(8, round(c.font_size * 0.18)) if c.box else 2 * c.outline_px)
    if c.position_pct is not None:
        return c.position_pct / 100 * height - line / 2
    return height - c.margin_v - line


def hook_layout(text: str, settings: Settings, *, width: int, height: int) -> HookLayout:
    """Place the hook: its top edge at hook.top_pct, growing downwards (so it never leaves the frame), at
    hook.font_size - or smaller (down to HOOK_MIN_SCALE) when the estimated block would reach the captions;
    if even that is too tall it moves up (not above 2 % of the height)."""
    h: HookSettings = settings.hook
    wrap = max(1, round(width * (1 - 2 * HOOK_SIDE_MARGIN)))
    top = round(h.top_pct / 100 * height)
    limit = caption_top_px(settings.captions, height) - HOOK_CAPTION_GAP * height
    smallest = max(30, round(h.font_size * HOOK_MIN_SCALE))
    size = h.font_size
    while True:
        pad = hook_pad(h.style, size)
        lines = estimate_lines(text, size, wrap, upper=h.uppercase)
        block = lines * size + 2 * pad
        if top + block <= limit or size <= smallest:
            break
        size = max(smallest, int(size * 0.92))
    if top + block > limit:  # still too big at the smallest size: move it up, but never above 2 % of the height
        top = max(round(0.02 * height), min(top, math.floor(limit - block)))
    return HookLayout(top_px=top, font_size=size, pad_px=pad, lines=lines, height_px=block, wrap_px=wrap)


# ------------------------------------------------------------------------------------------------ script stage
_DEVANAGARI = re.compile("[\u0900-\u097f]")


def has_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI.search(text or ""))


def keywords_in(keywords: list[Keyword], text: str) -> list[Keyword]:
    """The keywords that the text really contains (same matching as the captions)."""
    toks = [Word(start=0, end=0, text=t) for t in (text or "").split()]
    if not toks:
        return []
    return [k for k in keywords if emphasis_marks(toks, [k], per_line=len(toks), max_per_line=1)]


def check_llm_extras(script: ClipScript, lang: str, *, voiceover: bool) -> ClipScript:
    """The hook and keywords the LLM wrote, checked against the clip: with a voice-over a keyword must occur
    in the (final, possibly shortened) narration, else it would never be emphasised; a Hindi hook must be in
    Devanagari (else no hook) and, without a narration to check against, so must a Hindi keyword. Text the
    user types later in the clip editor is not checked this way."""
    hook, kws = script.hook, list(script.keywords)
    if lang == "hi" and hook and not has_devanagari(hook):
        hook = ""
    if voiceover and script.narration.strip():
        kws = keywords_in(kws, script.narration)
    elif lang == "hi":
        kws = [k for k in kws if has_devanagari(k.word)]
    if hook == script.hook and kws == script.keywords:
        return script
    return script.model_copy(update={"hook": hook, "keywords": kws})


# ------------------------------------------------------------------------------------------------ staleness
STYLE_KEY_VERSION = 1
# CaptionSettings fields added with G5 whose defaults are the step 1-4 look
_LOOK_FIELDS = ("bold", "uppercase", "outline_color", "outline_px", "shadow_px", "box", "highlight_style")


def emphasis_active(settings: Settings, script: ClipScript | None) -> bool:
    return bool(settings.captions.emphasis and script is not None and script.keywords)


def style_key(settings: Settings, script: ClipScript | None, lang: str = "en") -> dict:
    """What a final.mp4 looks like beyond its words: the caption style fields that matter for this clip-language
    (its own font only; emphasis fields only when emphasis is active; the emoji switch only for HyperFrames
    with an emoji to draw), the hook (text + settings, only when a hook is drawn) and the keywords (only with
    emphasis active). render.json "style_key"."""
    c = settings.captions
    drop = {"preset", "engine", "font_en" if lang == "hi" else "font_hi"}
    active = emphasis_active(settings, script)
    if not active:
        drop |= {"emphasis", "emphasis_color", "emphasis_scale", "emphasis_max_per_line", "emoji"}
    elif c.engine != "hyperframes" or not any(k.emoji for k in script.keywords):  # type: ignore[union-attr]
        drop.add("emoji")
    key: dict[str, Any] = {"v": STYLE_KEY_VERSION, "captions": c.model_dump(exclude=drop)}
    text = hook_text(script, settings)
    if text:
        key["hook"] = {"text": text, **settings.hook.model_dump(exclude={"enabled"})}
    if active:
        key["keywords"] = [k.model_dump() for k in script.keywords]  # type: ignore[union-attr]
    return key


def legacy_look(settings: Settings, script: ClipScript | None) -> bool:
    """True when the G4/G5 features change nothing: the step 1-4 look. A render stamped before style_key
    existed is current only in that case."""
    c, d = settings.captions, CaptionSettings()
    if any(getattr(c, f) != getattr(d, f) for f in _LOOK_FIELDS):
        return False
    return not hook_text(script, settings) and not emphasis_active(settings, script)
