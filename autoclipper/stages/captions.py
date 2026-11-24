"""Captions stage: time the exact script words against the voice, then write word-by-word ASS."""
from __future__ import annotations

import difflib
import json
import logging
import math
import re
import unicodedata
from pathlib import Path
from typing import Any

from .. import ffmpeg
from ..config import CaptionSettings, HookSettings, Settings
from ..interfaces import Transcriber
from ..models import ClipScript, Keyword, Word
from .caption_style import HookLayout, display_text, emphasis_marks, hook_end, hook_layout
from ._atomic import write_text_atomic
from .transcribe import transcribe_voice
from .voice import load_voice_result, looks_truncated, narration_hash, truncated_prefix

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


def distribute_segments(words: list[str], segments: list[tuple[float, float, int]] | None,
                        duration: float) -> list[Word]:
    """Spread ``words`` over known spans: ``segments`` = (start, end, word count) per generated voice chunk
    (M2), in order. Falls back to one span over ``duration`` when the counts do not add up."""
    if not segments or sum(n for _s, _e, n in segments) != len(words):
        return distribute_words(words, duration)
    out: list[Word] = []
    k = 0
    for start, end, n in segments:
        out.extend(distribute_words(words[k:k + n], max(0.0, end - start), start=start))
        k += n
    return out


def match_ratio(script_words: list[str], recognized: list[Word]) -> float:
    """Share of the script words align_words can time from ``recognized`` (same matching rule)."""
    if not script_words or not recognized:
        return 0.0
    a = [_norm(w) for w in script_words]
    b = [_norm(w.text) for w in recognized]
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    hits = sum(1 for blk in matcher.get_matching_blocks() for k in range(blk.size) if a[blk.a + k])
    return hits / len(script_words)


def clamp_to_segments(words: list[Word], segments: list[tuple[float, float, int]] | None) -> list[Word]:
    """Keep each sentence's words out of the neighbouring sentences (M2): a word never starts before its own
    voice chunk, and never runs into the next chunk (an unmatched word at a chunk edge is otherwise
    interpolated into the other sentence). No-op when the counts differ."""
    if not segments or sum(n for _s, _e, n in segments) != len(words):
        return words
    out: list[Word] = []
    k = 0
    for i, (start, _end, n) in enumerate(segments):
        limit = segments[i + 1][0] if i + 1 < len(segments) else math.inf
        for w in words[k:k + n]:
            s = min(max(w.start, start), limit)
            e = min(max(w.end, s), limit)
            out.append(w if (s, e) == (w.start, w.end) else Word(start=round(s, 3), end=round(e, 3), text=w.text))
        k += n
    return out


def transcribe_chunks(voice_wav: Path, lang: str, transcriber: Transcriber,
                      segments: list[tuple[float, float, int]], tmp_dir: Path) -> list[Word]:
    """Recognise each generated voice chunk (M2) on its own and shift its words onto the joined voice.
    Whisper on a whole Hindi voice sometimes drops long stretches; one short sentence at a time it does not."""
    out: list[Word] = []
    for i, (start, end, _n) in enumerate(segments):
        if end - start < 0.2:
            continue
        piece = Path(tmp_dir) / f".captions_chunk{i}.wav"
        try:
            ffmpeg.run(["-i", str(voice_wav), "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-c:a", "pcm_s16le",
                        str(piece)], desc=f"cut voice chunk {i + 1}")
            for w in transcribe_voice(piece, lang, transcriber):
                ws = min(max(start, start + w.start), end)
                out.append(Word(start=round(ws, 3), end=round(min(max(ws, start + w.end), end), 3), text=w.text))
        finally:
            piece.unlink(missing_ok=True)
    return out


def align_words(script_words: list[str], recognized: list[Word], duration: float, *,
                trimmed: bool = False, segments: list[tuple[float, float, int]] | None = None) -> list[Word]:
    """Map the exact script words onto recognized timings, monotonically.

    Matched words take the recognized timing; runs of unmatched words are spread by character
    length between their matched neighbours. Fewer than 30% matches -> plain distribution (over the
    voice chunks' spans when ``segments`` is given, see distribute_segments).
    With ``trimmed`` (the voice was cut before the end of the text), script words after the last
    matched word are dropped: they were never spoken.
    """
    if not script_words:
        return []
    if not recognized:
        return distribute_segments(script_words, segments, duration)

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
        return distribute_segments(script_words, segments, duration)
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


POP_SCALE_ASS = 115  # highlight_style "pop": the spoken word is drawn at 115 %
HOOK_FADE_IN_MS = 150
HOOK_FADE_OUT_MS = 250


def _hook_style_line(font: str, hook: HookSettings, lay: HookLayout, *, bold: bool, width: int) -> str:
    text = _style_color(hook.text_color)
    b = -1 if bold else 0
    margin = max(0, (width - lay.wrap_px) // 2)  # the text wraps inside lay.wrap_px, like the HyperFrames page
    if hook.style == "box":
        # BorderStyle 3 = opaque box: libass fills it with the OutlineColour; Outline = the padding
        return (f"Style: Hook,{font},{lay.font_size},{text},{text},{_style_color(hook.box_color)},&H80000000,"
                f"{b},0,0,0,100,100,0,0,3,{lay.pad_px},0,8,{margin},{margin},0,1")
    return (f"Style: Hook,{font},{lay.font_size},{text},{text},{_style_color(hook.outline_color)},&H80000000,"
            f"{b},0,0,0,100,100,0,0,1,{lay.pad_px},3,8,{margin},{margin},0,1")


def build_ass(words: list[Word], *, lang: str, settings: CaptionSettings, width: int = 1080,
              height: int = 1920, keywords: list[Keyword] | None = None, hook: str = "",
              hook_settings: HookSettings | None = None, duration: float | None = None) -> str:
    """captions.ass: one event per word (the spoken word highlighted, see captions.highlight_style), the
    keyword emphasis (``keywords``, captions.emphasis) and, with ``hook`` text, the hook line over the first
    hook_settings.duration seconds (fades in and out, layer 1). With the default settings and no keywords /
    hook this is exactly the step 1-4 output."""
    font = font_for(lang, settings)
    pct = settings.position_pct
    # position_pct: centre of the line at pct % from the top (Alignment 5 + \pos on every event).
    # None: legacy bottom-centre (Alignment 2) placed margin_v px above the bottom.
    alignment = 2 if pct is None else 5
    prefix = "" if pct is None else f"{{\\an5\\pos({width // 2},{round(height * pct / 100)})}}"
    primary = _style_color(settings.primary_color)
    hl, base = _inline_color(settings.highlight_color), _inline_color(settings.primary_color)
    emph = _inline_color(settings.emphasis_color)
    bold = -1 if settings.bold else 0
    if settings.box:
        pad = max(8, round(settings.font_size * 0.18))
        look = f"{_style_color(settings.box_color)},&H80000000,{bold},0,0,0,100,100,0,0,3,{pad},0"
    else:
        look = (f"{_style_color(settings.outline_color)},&H80000000,{bold},0,0,0,100,100,0,0,1,"
                f"{settings.outline_px},{settings.shadow_px}")
    hook_on = bool(hook and hook.strip())
    hs = hook_settings or HookSettings()
    lay = None
    styles = [f"Style: Default,{font},{settings.font_size},{primary},{primary},{look},"
              f"{alignment},60,60,{settings.margin_v},1"]
    if hook_on:
        lay = hook_layout(hook.strip(), Settings(captions=settings, hook=hs), width=width, height=height)
        styles.append(_hook_style_line(font, hs, lay, bold=settings.bold, width=width))
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
        *styles,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ])
    per_line = max(1, settings.words_per_line)
    marks = (emphasis_marks(words, keywords or [], per_line=per_line, max_per_line=settings.emphasis_max_per_line)
             if settings.emphasis and keywords else {})
    hstyle = settings.highlight_style or "color"
    escaped_all = [_escape(display_text(w.text, settings.uppercase)) for w in words]
    escale = round(settings.emphasis_scale * 100)

    def word_tag(k: int, t: str, state: str) -> str:
        """``state``: "active" (being spoken), "spoken" (karaoke, already said) or "" (plain).

        A keyword is drawn in the emphasis colour and emphasis_scale bigger; while it is lit (spoken, or
        already said in karaoke) the highlight colour wins, so the "current word" cue never disappears on the
        most important word. With a box (BorderStyle 3) nothing is scaled: libass would draw a taller box
        behind the scaled word."""
        lit = (state == "active" or (state == "spoken" and hstyle == "karaoke")) and hstyle != "none"
        pop = state == "active" and hstyle == "pop" and not settings.box
        if k in marks:
            sc = 100 if settings.box else escale
            if pop:
                sc = round(escale * POP_SCALE_ASS / 100)
            col = hl if lit else emph
            if sc == 100:
                return f"{{\\c{col}}}{t}{{\\c{base}}}"
            return f"{{\\c{col}\\fscx{sc}\\fscy{sc}}}{t}{{\\c{base}\\fscx100\\fscy100}}"
        if not lit:
            return t
        if pop:
            return (f"{{\\c{hl}\\fscx{POP_SCALE_ASS}\\fscy{POP_SCALE_ASS}}}{t}"
                    f"{{\\c{base}\\fscx100\\fscy100}}")
        return f"{{\\c{hl}}}{t}{{\\c{base}}}"

    lines = [list(range(k, min(k + per_line, len(words)))) for k in range(0, len(words), per_line)]
    events: list[str] = []
    if hook_on and lay is not None:
        end = hook_end(Settings(hook=hs), duration)
        # \an8: (x, y) is the top centre of the text; the box (or outline) reaches lay.pad_px above it
        pos = f"\\an8\\pos({width // 2},{lay.top_px + lay.pad_px})"
        events.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(end)},Hook,,0,0,0,,"
                      f"{{{pos}\\fad({HOOK_FADE_IN_MS},{HOOK_FADE_OUT_MS})}}{_escape(hook.strip())}")
    for li, line in enumerate(lines):
        next_line_start = words[lines[li + 1][0]].start if li + 1 < len(lines) else None
        escaped = [escaped_all[k] for k in line]

        def line_end(last: Word) -> float:
            end = last.end
            if next_line_start is not None and next_line_start - end <= _LINE_HOLD:
                end = next_line_start
            return end

        spans: list[tuple[float, float, int | None]] = []
        if hstyle == "none":
            spans.append((words[line[0]].start, line_end(words[line[-1]]), None))
        else:
            for wi, k in enumerate(line):
                end = words[line[wi + 1]].start if wi + 1 < len(line) else line_end(words[k])
                spans.append((words[k].start, end, wi))
        for start, end, wi in spans:
            # times are written in centiseconds: never let an event round to zero length
            start_cs = max(0, int(round(start * 100)))
            end_cs = max(int(round(end * 100)), start_cs + 1)
            parts = []
            for pos_i, (k, t) in enumerate(zip(line, escaped)):
                state = "" if wi is None else ("active" if pos_i == wi else ("spoken" if pos_i < wi else ""))
                parts.append(word_tag(k, t, state))
            events.append(f"Dialogue: 0,{_ass_time(start_cs / 100)},{_ass_time(end_cs / 100)},Default,,0,0,0,,"
                          + prefix + " ".join(parts))
    return header + "\n" + "\n".join(events) + ("\n" if events else "")


def rebuild_ass(out_dir: Path, *, lang: str, settings: Settings, script: ClipScript | None,
                duration: float | None) -> Path | None:
    """Rewrite out_dir/captions.ass from words.json with the current caption style, keywords and hook (the
    render does this every time, so a style / hook / keyword edit needs no new captions stage).
    None (and the file is left alone) when words.json is missing or unreadable."""
    from .caption_style import hook_text

    out_dir = Path(out_dir)
    try:
        raw = json.loads((out_dir / "words.json").read_text(encoding="utf-8"))
        words = [Word(**w) for w in raw]
    except (OSError, ValueError, TypeError):
        return None
    ass = out_dir / "captions.ass"
    write_text_atomic(ass, build_ass(words, lang=lang, settings=settings.captions, width=settings.clips.width,
                                     height=settings.clips.height,
                                     keywords=script.keywords if script is not None else None,
                                     hook=hook_text(script, settings), hook_settings=settings.hook,
                                     duration=duration))
    return ass


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
    if vr is not None and (vr.trimmed or vr.truncated) and vr.narration_hash == narration_hash(narration):
        trimmed = True
        if vr.spoken_narration:
            narration = vr.spoken_narration
    elif looks_truncated(narration, dur, settings.script.wps(lang)):
        # A take Voicebox cut short before voice.json recorded it (older voices): caption only the words
        # that fit in the voice, never the whole text squeezed into it.
        log.warning("%s is %.1fs, far shorter than its %d-word narration needs; captioning only the spoken part",
                    voice_wav.name, dur, len(narration.split()))
        narration = truncated_prefix(narration, dur, settings.script.wps(lang))
        trimmed = True
    script_words = split_words(narration)
    recognized: list[Word] = []
    if script_words:
        try:
            recognized = transcribe_voice(voice_wav, lang, transcriber)
        except Exception as e:  # noqa: BLE001 - incl. TranscribeError: timing falls back to distribution
            log.warning("voice transcription failed for %s (%s); spreading caption words evenly",
                        voice_wav.name, e)
            recognized = []
    segments = None
    if vr is not None and vr.chunks and not trimmed and vr.narration_hash == narration_hash(narration):
        # M2: the voice was generated sentence by sentence; without usable recognition, each sentence's
        # words are spread over its own chunk (sped up by the tempo) instead of over the whole voice
        tempo = max(vr.tempo, 1e-6)
        segments = [(c.start / tempo, (c.start + c.duration) / tempo, len(split_words(c.text))) for c in vr.chunks]
    if segments and len(segments) > 1 and match_ratio(script_words, recognized) < 0.6:
        # the whole-voice recognition missed a lot (Hindi): recognise the sentences one by one instead
        try:
            per_chunk = transcribe_chunks(voice_wav, lang, transcriber, segments, out_dir)
        except Exception as e:  # noqa: BLE001 - keep what the whole-voice pass gave
            log.warning("per-sentence transcription of %s failed (%s)", voice_wav.name, e)
            per_chunk = []
        if match_ratio(script_words, per_chunk) > match_ratio(script_words, recognized):
            recognized = per_chunk
    words = align_words(script_words, recognized, dur, trimmed=trimmed, segments=segments)
    words = clamp_to_segments(words, segments)
    write_text_atomic(out_dir / "words.json",
                      json.dumps([w.model_dump() for w in words], indent=2, ensure_ascii=False))
    ass = out_dir / "captions.ass"
    write_text_atomic(ass, build_ass(words, lang=lang, settings=settings.captions,
                                     width=settings.clips.width, height=settings.clips.height))
    # No stamp = voice-over captions (as in every job before captions-only mode existed).
    (out_dir / CAPTIONS_STAMP).unlink(missing_ok=True)
    return ass
