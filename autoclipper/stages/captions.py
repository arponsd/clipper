"""Captions stage: time the exact script words against the voice, then write word-by-word ASS."""
from __future__ import annotations

import difflib
import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

from .. import ffmpeg
from ..config import CaptionSettings, Settings
from ..interfaces import Transcriber
from ..models import Word
from ._atomic import write_text_atomic
from .transcribe import transcribe_voice
from .voice import load_voice_result, narration_hash

log = logging.getLogger(__name__)
MIN_MATCH_RATIO = 0.3
# Keep a line on screen across short pauses between lines so captions do not flicker.
_LINE_HOLD = 0.5
# How words.json was made (voice-over or the original speech); see write_captions_stamp.
CAPTIONS_STAMP = "captions.json"


class CaptionsError(RuntimeError):
    """Caption generation failed."""


def split_words(text: str) -> list[str]:
    return text.split()


def _weight(word: str) -> int:
    return max(1, len(word))


def distribute_words(words: list[str], duration: float, start: float = 0.0) -> list[Word]:
    """Spread ``words`` over [start, start+duration] proportionally to their character length."""
    if not words:
        return []
    duration = max(0.0, duration)
    total = sum(_weight(w) for w in words)
    out: list[Word] = []
    t = start
    for w in words:
        span = duration * _weight(w) / total
        out.append(Word(start=round(t, 3), end=round(t + span, 3), text=w))
        t += span
    return out


_NUKTA = "\u093c"
_CHANDRABINDU = "\u0901"
_ANUSVARA = "\u0902"
_ZW = dict.fromkeys(map(ord, "\u200c\u200d"))  # ZWNJ, ZWJ
# nasal consonant (न म ङ ञ ण) + virama before a consonant -> anusvara: हिन्दी ~ हिंदी
_NASAL_CLUSTER = re.compile("[\u0928\u092e\u0919\u091e\u0923]\u094d(?=[\u0915-\u0939])")


def _norm(token: str) -> str:
    """Matching key used only for alignment (the displayed text never changes).

    Folds Devanagari spelling variants that Whisper often emits differently from the script:
    nukta (ज़/ज, फ़/फ, क़/क), chandrabindu vs anusvara (हँ/हं), nasal+virama vs anusvara
    (हिन्दी/हिंदी), and ZWJ/ZWNJ. Then lowercase and keep letters/marks/numbers.
    """
    token = unicodedata.normalize("NFD", token).translate(_ZW)
    token = token.replace(_NUKTA, "").replace(_CHANDRABINDU, _ANUSVARA)
    token = _NASAL_CLUSTER.sub(_ANUSVARA, token)
    token = unicodedata.normalize("NFC", token).casefold()
    return "".join(ch for ch in token if unicodedata.category(ch)[0] in "LMN")


def align_words(script_words: list[str], recognized: list[Word], duration: float, *,
                trimmed: bool = False) -> list[Word]:
    """Map the exact script words onto recognized timings, monotonically.

    Matched words take the recognized timing; runs of unmatched words are spread by character
    length between their matched neighbours. Fewer than 30% matches -> plain distribution.
    With ``trimmed`` (the voice was cut before the end of the text), script words after the last
    matched word are dropped: they were never spoken.
    """
    if not script_words:
        return []
    if not recognized:
        return distribute_words(script_words, duration)

    a = [_norm(w) for w in script_words]
    b = [_norm(w.text) for w in recognized]
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    timing: dict[int, Word] = {}
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            i, j = block.a + k, block.b + k
            if a[i]:  # pure punctuation tokens carry no timing evidence
                timing[i] = recognized[j]
    if len(timing) < MIN_MATCH_RATIO * len(script_words):
        return distribute_words(script_words, duration)
    if trimmed:
        script_words = script_words[:max(timing) + 1]

    end_limit = max(duration, max(w.end for w in recognized))
    out: list[Word] = []
    cursor = 0.0
    i = 0
    n = len(script_words)
    while i < n:
        if i in timing:
            r = timing[i]
            s = max(cursor, r.start)
            e = max(s, r.end)
            out.append(Word(start=round(s, 3), end=round(e, 3), text=script_words[i]))
            cursor = e
            i += 1
            continue
        j = i
        while j < n and j not in timing:
            j += 1
        gap_end = timing[j].start if j < n else end_limit
        gap_end = max(cursor, gap_end)
        out.extend(distribute_words(script_words[i:j], gap_end - cursor, start=cursor))
        cursor = gap_end
        i = j
    return out


def _ass_time(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    # Backslash first; a word joiner after it stops libass reading \N, \n or \h.
    return text.replace("\\", "\\⁠").replace("{", "\\{").replace("}", "\\}")


def _inline_color(color: str) -> str:
    """Style colour &HAABBGGRR -> inline override &HBBGGRR& (alpha is not part of \\c)."""
    hexpart = color.strip().upper().removeprefix("&H").rstrip("&")
    hexpart = hexpart[-6:].rjust(6, "0")
    return f"&H{hexpart}&"


def _style_color(color: str) -> str:
    hexpart = color.strip().upper().removeprefix("&H").rstrip("&")
    return f"&H{hexpart.rjust(8, '0')}"


def font_for(lang: str, settings: CaptionSettings) -> str:
    return settings.font_hi if lang == "hi" else settings.font_en


def build_ass(words: list[Word], *, lang: str, settings: CaptionSettings, width: int = 1080,
              height: int = 1920) -> str:
    font = font_for(lang, settings)
    pct = settings.position_pct
    # position_pct: centre of the line at pct % from the top (Alignment 5 + \pos on every event).
    # None: legacy bottom-centre (Alignment 2) placed margin_v px above the bottom.
    alignment = 2 if pct is None else 5
    prefix = "" if pct is None else f"{{\\an5\\pos({width // 2},{round(height * pct / 100)})}}"
    primary = _style_color(settings.primary_color)
    hl, base = _inline_color(settings.highlight_color), _inline_color(settings.primary_color)
    header = "\n".join([
        "[Script Info]",
        "; Generated by Auto Clipper",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",  # smart wrapping inside MarginL/MarginR
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font},{settings.font_size},{primary},{primary},&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,5,2,{alignment},60,60,{settings.margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ])
    per_line = max(1, settings.words_per_line)
    lines = [words[k:k + per_line] for k in range(0, len(words), per_line)]
    events: list[str] = []
    for li, line in enumerate(lines):
        next_line_start = lines[li + 1][0].start if li + 1 < len(lines) else None
        escaped = [_escape(w.text) for w in line]
        for wi, w in enumerate(line):
            start = w.start
            if wi + 1 < len(line):
                end = line[wi + 1].start
            else:
                end = w.end
                if next_line_start is not None and next_line_start - end <= _LINE_HOLD:
                    end = next_line_start
            # times are written in centiseconds: never let an event round to zero length
            start_cs = max(0, int(round(start * 100)))
            end_cs = max(int(round(end * 100)), start_cs + 1)
            parts = [f"{{\\c{hl}}}{t}{{\\c{base}}}" if k == wi else t
                     for k, t in enumerate(escaped)]
            events.append(f"Dialogue: 0,{_ass_time(start_cs / 100)},{_ass_time(end_cs / 100)},Default,,0,0,0,,"
                          + prefix + " ".join(parts))
    return header + "\n" + "\n".join(events) + ("\n" if events else "")


def write_captions_stamp(out_dir: Path, *, voiceover: bool, source: str, **extra: Any) -> Path:
    """captions.json: how words.json was made. ``voiceover`` False = from the original speech."""
    data: dict[str, Any] = {"voiceover": voiceover, "source": source}
    data.update(extra)
    return write_text_atomic(Path(out_dir) / CAPTIONS_STAMP, json.dumps(data, indent=2, ensure_ascii=False))


def captions_voiceover(out_dir: Path) -> bool:
    """The voice-over mode words.json was made for. A missing/unreadable stamp = voice-over (step 1-4)."""
    try:
        data = json.loads((Path(out_dir) / CAPTIONS_STAMP).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if isinstance(data, dict) and isinstance(data.get("voiceover"), bool):
        return data["voiceover"]
    return True


def make_captions(*, narration: str, voice_wav: Path, lang: str, transcriber: Transcriber,
                  settings: Settings, out_dir: Path) -> Path:
    """Write out_dir/words.json and out_dir/captions.ass; return the .ass path.

    If out_dir/voice.json says the take for this narration was trimmed, only the estimated spoken
    prefix (VoiceResult.spoken_narration) is captioned. An ASR failure on the voice falls back to
    spreading the words over the voice duration.
    """
    voice_wav = Path(voice_wav)
    if not voice_wav.is_file():
        raise CaptionsError(f"voice file not found: {voice_wav} (run the voice stage first)")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = ffmpeg.duration(voice_wav)
    trimmed = False
    vr = load_voice_result(out_dir)
    if vr is not None and vr.trimmed and vr.narration_hash == narration_hash(narration):
        trimmed = True
        if vr.spoken_narration:
            narration = vr.spoken_narration
    script_words = split_words(narration)
    recognized: list[Word] = []
    if script_words:
        try:
            recognized = transcribe_voice(voice_wav, lang, transcriber)
        except Exception as e:  # noqa: BLE001 - incl. TranscribeError: timing falls back to distribution
            log.warning("voice transcription failed for %s (%s); spreading caption words evenly",
                        voice_wav.name, e)
            recognized = []
    words = align_words(script_words, recognized, dur, trimmed=trimmed)
    write_text_atomic(out_dir / "words.json",
                      json.dumps([w.model_dump() for w in words], indent=2, ensure_ascii=False))
    ass = out_dir / "captions.ass"
    write_text_atomic(ass, build_ass(words, lang=lang, settings=settings.captions,
                                     width=settings.clips.width, height=settings.clips.height))
    # No stamp = voice-over captions (as in every job before captions-only mode existed).
    (out_dir / CAPTIONS_STAMP).unlink(missing_ok=True)
    return ass
