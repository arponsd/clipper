"""Sentence-by-sentence narration (M2): split, per-chunk checks, edge trim, concatenation, chunk cache.

Chatterbox (multilingual) stops speaking after a sentence or two when it gets a long text in one call: a
115-word Hindi narration came back as 10 s of audio. The voice stage therefore sends one Voicebox call
per sentence-sized chunk and joins the takes with short gaps (see voice.make_voice).
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from .. import ffmpeg

# Sentence terminators: Devanagari danda / double danda, Latin . ? ! and the ellipsis; also the
# full-width / Arabic question marks a translation may produce.
_TERMINATORS = "।॥.?!…？！؟"
# Characters that may follow a terminator and still belong to the sentence: closing quotes/brackets.
_CLOSERS = "\"'”’»)]}›"
_OPENERS = "\"'“‘«([{‹"
# Clause boundaries used when one sentence is longer than the chunk cap.
_CLAUSE_END = ",;:–—،、"
# English abbreviations that end in "." without ending a sentence (lower case, without the final dot).
# Titles and "e.g."-style words are followed by a name / an example: they never end a sentence.
TITLE_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "st", "mt", "ft", "vs", "e.g", "i.e", "cf", "gen", "gov", "sen", "rep",
    "capt", "lt", "col", "sgt", "rev", "hon", "approx", "fig", "vol", "nos",
})
# The others can also close a sentence ("... in the U.S. It is big", "at 5 p.m. Tomorrow ..."): they end
# one only when a typical sentence opener follows (SENTENCE_STARTERS), so "U.S. Army" stays together.
ABBREVIATIONS = TITLE_ABBREVIATIONS | frozenset({
    "sr", "jr", "etc", "al", "u.s", "u.k", "u.s.a", "u.n", "e.u", "no", "dept", "est", "inc", "ltd", "co",
    "corp", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "a.m", "p.m",
    "ph.d", "mins", "hrs",
})
SENTENCE_STARTERS = frozenset({
    "It", "He", "She", "They", "We", "I", "You", "The", "This", "That", "These", "Those", "There", "Then",
    "But", "And", "So", "Now", "Today", "Tomorrow", "Yesterday", "Tonight", "His", "Her", "Their", "Our", "My",
    "Its", "Your", "What", "Why", "How", "When", "Where", "Who", "If", "Yet", "Still", "Also", "Meanwhile",
    "After", "Before", "Later", "Next", "Finally", "In", "On", "At", "Or", "Not", "No", "Yes",
})
# Devanagari abbreviations written with a full stop (डॉ. रमेश, प्रो. शर्मा): never the end of a sentence.
# Hindi ends sentences with । far more often, but LLM output also uses "." - so "है." still ends one.
HINDI_ABBREVIATIONS = frozenset({
    "डॉ", "डा", "प्रो", "श्री", "श्रीमती", "सुश्री", "कु", "स्व", "पं", "मि", "सं", "क्र", "ले", "जन", "मे",
})
_DEVANAGARI = re.compile(r"^[\u0900-\u097F]+$")
# A danda glued to the next word ("वाक्य।दूसरा"): space_after_danda inserts the missing space.
_GLUED_DANDA = re.compile("([।॥])(?=[^\\s।॥" + re.escape("\"'”’»)]}›") + "])")
_INITIALS = re.compile(r"^(?:[a-z]\.)*[a-z]$", re.I)  # "J", "U.S", "e.g"

# Per-chunk sanity checks (only with a known words-per-second rate).
CHUNK_TRUNCATED_BELOW = 0.5  # a take under half of what its words need was cut short
CHUNK_OVERLONG_ABOVE = 3.0  # a take over 3x (+ slack) what its words need is babbling / repeating
CHUNK_OVERLONG_SLACK = 1.5  # seconds
CHUNK_MIN_WORDS = 3  # fewer words: too little to judge a cut-short take (rambling is still checked)
EDGE_SILENCE_DB = -45  # edge trim threshold
EDGE_KEEP_SECONDS = 0.05  # silence kept at each edge after trimming


@dataclass(frozen=True)
class ChunkOptions:
    """How make_voice splits and generates a narration (from voicebox.chunk_* settings)."""

    max_chars: int = 150
    min_chars: int = 60
    gap_seconds: float = 0.2
    timeout: float | None = 240.0  # Voicebox wait per chunk
    retry_timeout: float | None = 120.0  # wait for the one retry of a cut-short chunk
    seed: int | None = None  # None: derived from the voice profile + language (same for every chunk)
    trim_edges: bool = True
    cache: bool = True

    @classmethod
    def from_settings(cls, vb) -> "ChunkOptions | None":
        """None when voicebox.chunking is off (one Voicebox call per narration, as before M2)."""
        if not getattr(vb, "chunking", False):
            return None
        cap = float(vb.timeout_seconds)
        return cls(max_chars=vb.chunk_max_chars, min_chars=vb.chunk_min_chars, gap_seconds=vb.chunk_gap_seconds,
                   timeout=min(vb.chunk_timeout_seconds, cap), retry_timeout=min(vb.chunk_retry_timeout_seconds, cap),
                   seed=vb.seed)


class ChunkInfo(BaseModel):
    """One generated chunk of the take, as stored in voice.json ``chunks``."""

    index: int
    text: str
    words: int
    start: float  # seconds in the joined take (before any tempo change)
    duration: float
    expected: float | None = None  # seconds its words need at the language's words per second
    attempts: int = 1
    cached: bool = False
    truncated: bool = False  # still cut short after the retry (kept, with a warning)
    overlong: bool = False  # still far too long after the retry (kept, with a warning)


# --------------------------------------------------------------------------------------- text
def spoken_words(text: str) -> int:
    """Words that are spoken: tokens with at least one letter or digit. A free-standing danda, dash or
    quote is not a word; Devanagari vowel signs (category M) belong to the word they sit in."""
    return sum(1 for tok in text.split() if any(unicodedata.category(ch)[0] in "LN" for ch in tok))


def _core(token: str) -> str:
    return token.strip(_CLOSERS).lstrip(_OPENERS)


def _ends_sentence(token: str, nxt: str | None) -> bool:
    core = _core(token)
    if not core or core[-1] not in _TERMINATORS:
        return False
    if core[-1] != "." and core[-1] != "…":
        if core[-1] in "?!" and nxt is not None and _continues(nxt):
            return False  # '"Really?" she asked.': a lower-case Latin word continues the sentence
        return True  # । ॥ ? ! always end a sentence
    if core.endswith("..") or core[-1] == "…":
        return not (nxt is not None and _continues(nxt))  # an ellipsis before a lower-case word pauses only
    stem = core[:-1].lower()
    if stem in HINDI_ABBREVIATIONS or _is_devanagari_initials(stem):
        return False  # "डॉ. रमेश", "ई.पू."
    if stem in TITLE_ABBREVIATIONS:
        return False
    if stem in ABBREVIATIONS or (_INITIALS.match(stem) and len(stem.replace(".", "")) <= 3):
        # "U.S." / "p.m." / "J." / "no.": a sentence ends here only before a typical sentence opener
        return nxt is not None and _core(nxt).rstrip(",;:") in SENTENCE_STARTERS
    if nxt is not None and _continues(nxt):
        return False  # "etc. and", "approx. ten": a lower-case Latin word continues the sentence
    return True


def _is_devanagari_initials(stem: str) -> bool:
    """"ई.पू" / "बी.ए": dotted Devanagari parts of at most 3 code points (a plain word like "है" is not)."""
    parts = stem.split(".")
    return len(parts) > 1 and all(p and len(p) <= 3 and _DEVANAGARI.match(p) for p in parts)


def space_after_danda(text: str) -> str:
    """Insert the missing space after a danda glued to the next word ("वाक्य।दूसरा" -> "वाक्य। दूसरा"),
    so the sentence split sees the boundary. Used on the narration before it is voiced chunk by chunk."""
    return _GLUED_DANDA.sub(r"\1 ", text)


def _continues(nxt: str) -> bool:
    first = _core(nxt)[:1]
    return first.isascii() and first.isalpha() and first.islower()


def split_sentences(text: str) -> list[str]:
    """Sentences of ``text`` (whitespace normalised). Splits after । ॥ . ? ! … (plus closing quotes);
    decimals ("3.5"), abbreviations ("Dr.", "U.S.", "e.g.") and initials do not end a sentence."""
    tokens = text.split()
    out: list[str] = []
    cur: list[str] = []
    for i, tok in enumerate(tokens):
        cur.append(tok)
        if _ends_sentence(tok, tokens[i + 1] if i + 1 < len(tokens) else None):
            out.append(" ".join(cur))
            cur = []
    if cur:
        # A free-standing "।" after the last sentence belongs to it.
        if out and spoken_words(" ".join(cur)) == 0:
            out[-1] = out[-1] + " " + " ".join(cur)
        else:
            out.append(" ".join(cur))
    # a sentence of only punctuation ("।" written as its own token) is glued to the one before it
    merged: list[str] = []
    for s in out:
        if merged and spoken_words(s) == 0:
            merged[-1] += " " + s
        else:
            merged.append(s)
    return merged


def _split_long(sentence: str, max_chars: int) -> list[str]:
    """Break a sentence longer than ``max_chars`` at clause marks (, ; : -), then between words."""
    if len(sentence) <= max_chars:
        return [sentence]
    clauses: list[str] = []
    cur: list[str] = []
    for tok in sentence.split():
        cur.append(tok)
        if _core(tok)[-1:] in _CLAUSE_END or tok in ("-", "–", "—"):
            clauses.append(" ".join(cur))
            cur = []
    if cur:
        clauses.append(" ".join(cur))
    pieces: list[str] = []
    for clause in clauses:
        if len(clause) <= max_chars:
            pieces.append(clause)
            continue
        line: list[str] = []
        for w in clause.split():
            if line and len(" ".join(line + [w])) > max_chars:
                pieces.append(" ".join(line))
                line = []
            line.append(w)
        if line:
            pieces.append(" ".join(line))
    # pack clauses back together greedily up to max_chars
    packed: list[str] = []
    for p in pieces:
        if packed and len(packed[-1]) + 1 + len(p) <= max_chars:
            packed[-1] += " " + p
        else:
            packed.append(p)
    return packed


def split_narration(text: str, *, max_chars: int = 150, min_chars: int = 60) -> list[str]:
    """Chunks for sentence-by-sentence generation.

    Sentences longer than ``max_chars`` are split at clause marks, then between words; pieces shorter
    than ``min_chars`` are merged with a neighbour while the result stays within ``max_chars``.
    Joining the chunks with spaces gives back the narration (whitespace normalised).
    """
    max_chars = max(20, int(max_chars))
    min_chars = max(0, min(int(min_chars), max_chars - 1))
    pieces: list[str] = []
    for s in split_sentences(text):
        pieces.extend(_split_long(s, max_chars))
    # A short chunk takes in the sentences after it until it reaches min_chars (a short question then
    # travels with its answer); only a short last chunk is joined to the one before it.
    merged: list[str] = []
    for p in pieces:
        if merged and len(merged[-1]) < min_chars and len(merged[-1]) + 1 + len(p) <= max_chars:
            merged[-1] += " " + p
        else:
            merged.append(p)
    if len(merged) > 1 and len(merged[-1]) < min_chars and len(merged[-2]) + 1 + len(merged[-1]) <= max_chars:
        last = merged.pop()
        merged[-1] += " " + last
    return merged


# --------------------------------------------------------------------------------------- checks
def expected_chunk_seconds(text: str, wps: float | None) -> float | None:
    if not wps:
        return None
    return spoken_words(text) / max(wps, 1e-6)


def chunk_truncated(text: str, duration: float, wps: float | None) -> bool:
    """True when a chunk take is far shorter than its words need (Voicebox stopped early)."""
    exp = expected_chunk_seconds(text, wps)
    if exp is None or spoken_words(text) < CHUNK_MIN_WORDS:
        return False
    return duration < CHUNK_TRUNCATED_BELOW * exp


def chunk_overlong(text: str, duration: float, wps: float | None) -> bool:
    """True when a chunk take is far longer than its words need (the engine babbled or repeated)."""
    exp = expected_chunk_seconds(text, wps)
    if exp is None or spoken_words(text) == 0:
        return False  # unlike chunk_truncated, a 1-2 word chunk is checked too: short input is where it babbles
    return duration > CHUNK_OVERLONG_ABOVE * exp + CHUNK_OVERLONG_SLACK


def derive_seed(profile_id: str, lang: str) -> int:
    """A stable seed per voice profile and language: every chunk (and every re-render) uses the same one."""
    return int(hashlib.sha256(f"{profile_id}\n{lang}".encode()).hexdigest()[:8], 16) % (2 ** 31)


def cache_key(text: str, *, profile_id: str, lang: str, engine: str, seed: int | None, trim_edges: bool) -> str:
    raw = json.dumps([" ".join(text.split()), profile_id, lang, engine, seed, trim_edges], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


# --------------------------------------------------------------------------------------- audio
def normalize_chunk(src: Path, dst: Path, *, sample_rate: int, trim_edges: bool = True) -> Path:
    """Decode ``src`` to mono s16 WAV at ``sample_rate``; with ``trim_edges`` leading and trailing silence
    is cut down to EDGE_KEEP_SECONDS (so the gaps between chunks are what the setting says). Falls back to
    the untrimmed audio when trimming would leave almost nothing (a quiet but real take)."""
    ffmpeg.to_wav(src, dst, sample_rate=sample_rate, channels=1)
    if not trim_edges:
        return dst
    trimmed = dst.with_name(dst.stem + ".trim.wav")
    edge = (f"silenceremove=start_periods=1:start_threshold={EDGE_SILENCE_DB}dB:"
            f"start_silence={EDGE_KEEP_SECONDS}:detection=peak")
    af = f"{edge},areverse,{edge},areverse"
    try:
        ffmpeg.run(["-i", str(dst), "-af", af, "-c:a", "pcm_s16le", str(trimmed)], desc="trim chunk silence")
        if wav_seconds(trimmed) >= 0.2 or wav_seconds(trimmed) >= 0.5 * wav_seconds(dst):
            trimmed.replace(dst)
    except (ffmpeg.FFmpegError, OSError, wave.Error, EOFError):
        pass  # keep the untrimmed chunk
    finally:
        trimmed.unlink(missing_ok=True)
    return dst


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate() or 1
        return w.getnframes() / rate


def concat_wavs(paths: list[Path], dst: Path, *, gap_seconds: float) -> list[tuple[float, float]]:
    """Join PCM WAVs (same rate / channels / width) with ``gap_seconds`` of silence between them.
    Returns the (start, duration) of each input in the joined file."""
    if not paths:
        raise ValueError("no chunks to join")
    dst = Path(dst)
    tmp = dst.with_name(dst.stem + ".part.wav")
    spans: list[tuple[float, float]] = []
    params = None
    try:
        with wave.open(str(tmp), "wb") as out:
            frames_written = 0
            for i, p in enumerate(paths):
                with wave.open(str(p), "rb") as w:
                    pr = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                    if params is None:
                        params = pr
                        out.setnchannels(pr[0])
                        out.setsampwidth(pr[1])
                        out.setframerate(pr[2])
                    elif pr != params:
                        raise ValueError(f"chunk {p.name} has format {pr}, expected {params}")
                    if i:
                        gap = int(round(max(0.0, gap_seconds) * params[2]))
                        out.writeframes(b"\x00" * gap * params[0] * params[1])
                        frames_written += gap
                    n = w.getnframes()
                    out.writeframes(w.readframes(n))
                    spans.append((frames_written / params[2], n / params[2]))
                    frames_written += n
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)
    return spans
