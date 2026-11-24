"""Voice stage: narration text -> Voicebox -> fitted voice.wav (never longer than the clip)."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
import time
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ValidationError

from .. import ffmpeg
from ..config import VoiceProfile
from ..interfaces import LengthenFn, ProgressFn, ShortenFn, VoiceboxAPI
from ..voicebox import VoiceboxError, VoiceboxTimeout
from ._atomic import write_text_atomic
from .voice_chunks import (ChunkInfo, ChunkOptions, cache_key, chunk_overlong, chunk_truncated, concat_wavs,
                           derive_seed, expected_chunk_seconds, normalize_chunk, space_after_danda, split_narration,
                           spoken_words, wav_seconds)

log = logging.getLogger(__name__)

VOICE_SAMPLE_RATE = 44100
FADE_SECONDS = 0.3
# A take shorter than this is treated as "Voicebox returned nothing" (wrong profile/engine, server bug).
MIN_VOICE_SECONDS = 0.2
# Tolerance so rounding in ffprobe durations does not trigger a needless atempo pass.
_EPS = 0.02
# A take shorter than this fraction of what its word count needs (words / words-per-second) was cut short
# by the TTS engine (Chatterbox sometimes stops after a few sentences): it is not a "too short script".
TRUNCATED_BELOW = 0.5
# Clock for the lengthen retry's time budget (a module attribute so tests can drive it).
_clock = time.monotonic
# Marker in voice_chunks/ while a chunked take is being made: holds the narration hash, so a resume after a
# failed first run (no voice.json yet) reuses the chunks that were already generated.
_PENDING = ".pending"


class VoiceError(RuntimeError):
    """Voice stage failure that is not a Voicebox transport error."""


class VoiceResult(BaseModel):
    path: str  # absolute path of the fitted voice.wav
    narration: str  # text actually spoken (after any shortening)
    narration_hash: str
    raw_duration: float  # duration of the final generated take before fitting
    final_duration: float
    clip_duration: float
    tempo: float
    shortened: bool
    trimmed: bool
    lengthened: bool = False  # a too-short take was replaced by a lengthened narration (step 2)
    # When trimmed: the estimated prefix of `narration` that is actually heard before the cut.
    # Captions use it instead of `narration`. None for untrimmed takes and older voice.json files.
    spoken_narration: str | None = None
    # Things the user should know about this take (e.g. the lengthen retry timed out); shown in the UI.
    warnings: list[str] = []
    # Voicebox stopped early: the take is far shorter than the text needs (see looks_truncated). Captions
    # then show only `spoken_narration` (the estimated spoken prefix), as for a trimmed take.
    truncated: bool = False
    # M2: the take was generated sentence by sentence; one entry per Voicebox call (empty: one call).
    chunks: list[ChunkInfo] = []
    chunk_gap: float | None = None  # seconds of silence between chunks


def narration_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def engine_for(lang: str, voice: VoiceProfile) -> str:
    """Hindi always uses Chatterbox (multilingual); other languages use the configured engine."""
    if lang == "hi":
        return "chatterbox"
    return voice.engine or "chatterbox"


def _count_words(text: str) -> int:
    return len(text.split())


def estimate_spoken_prefix(text: str, raw_duration: float, tempo: float, clip_duration: float) -> str:
    """Words of ``text`` that end before ``clip_duration`` in a take of ``raw_duration`` played at ``tempo``.

    The sped-up take (raw_duration / tempo) is spread over the words by character length, the same
    way captions.distribute_words does, and words whose estimated end is <= clip_duration are kept.
    At least the first word is kept.
    """
    words = text.split()
    if not words:
        return ""
    spoken = raw_duration / max(tempo, 1e-6)
    total = sum(max(1, len(w)) for w in words)
    kept: list[str] = []
    t = 0.0
    for w in words:
        t += spoken * max(1, len(w)) / total
        if t > clip_duration + 1e-6:
            break
        kept.append(w)
    return " ".join(kept or words[:1])


def expected_seconds(text: str, wps: float) -> float:
    """Seconds a narration of ``text`` needs at ``wps`` words per second."""
    return _count_words(text) / max(wps, 1e-6)


def looks_truncated(text: str, duration: float, wps: float | None) -> bool:
    """True when a take of ``duration`` seconds is far shorter than ``text`` needs (TTS stopped early)."""
    if not wps or _count_words(text) < 8:
        return False
    return duration < TRUNCATED_BELOW * expected_seconds(text, wps)


def truncated_prefix(text: str, duration: float, wps: float) -> str:
    """The words of ``text`` a cut-short take of ``duration`` seconds probably says (at ``wps``)."""
    return estimate_spoken_prefix(text, expected_seconds(text, wps), 1.0, duration)


def _settings_key(lang: str) -> str:
    return "hindi" if lang == "hi" else "english"


def make_voice(*, client: VoiceboxAPI, narration: str, lang: str, voice: VoiceProfile,
               clip_duration: float, out_dir: Path, max_speedup: float,
               shorten: ShortenFn | None = None, on_progress: ProgressFn | None = None,
               lengthen: LengthenFn | None = None, lengthen_below: float = 0.75,
               fill_ratio: float = 0.88, lengthen_timeout: float | None = None,
               wps: float | None = None, chunking: ChunkOptions | None = None) -> VoiceResult:
    """Generate the narration voice and fit it to ``clip_duration``.

    Fitting order: as-is if it fits -> atempo (<= max_speedup) -> shorten once via ``shorten`` and
    regenerate -> atempo max_speedup + trim to clip with a 0.3 s fade-out.

    Too short: when ``lengthen`` is given and the take is shorter than clip x ``lengthen_below``,
    the narration is lengthened once (to fill clip x ``fill_ratio``) and regenerated. The longer
    take is kept only if it fits within max_speedup; otherwise (or on any error) the original take
    is kept, with a warning in voice.json. A lengthen never leads to a shorten or a trim.
    ``lengthen_timeout`` limits the Voicebox wait of that retry (voicebox.lengthen_timeout_seconds); with
    chunking it is the budget of the whole lengthened take, shared by its chunks.

    Cut short: with ``wps`` (the language's speaking rate), a take under TRUNCATED_BELOW x the time its
    words need was stopped early by the TTS engine. The same text is generated once more and the longer
    take kept; if that is still cut short the take is marked ``truncated`` (captions show only the spoken
    prefix), a warning is added, and it is never lengthened (a longer text would be cut short too).

    Chunking (M2, ``chunking`` given): every take is generated sentence by sentence (voice_chunks.
    split_narration), one Voicebox call per chunk with the same profile and seed, each chunk's edge silence
    trimmed, joined with ``chunking.gap_seconds`` of silence. Each chunk is checked on its own: a chunk far
    shorter (or far longer) than its words need is generated once more (``chunking.retry_timeout``) and the
    better take kept; a chunk still bad is kept with a warning (and marks the take ``truncated`` when cut
    short, whichever take it is in: a lengthened take with a cut-short chunk is not used, a shortened one is
    kept as ``truncated``). Chunks that passed are cached in out_dir/voice_chunks, so an edited narration (or
    a resume after a run that failed part-way) regenerates only the sentences still missing. The whole-take
    retry above is not used with chunking. A danda glued to the next word gets a space first
    (space_after_danda); the returned ``narration`` then has it too.
    """
    progress = on_progress or (lambda _f, _m="": None)
    text = narration.strip()
    if chunking is not None:
        text = space_after_danda(text)
    if not text:
        raise VoiceError("narration is empty - edit script.json and add narration text")
    if not voice.profile_id:
        key = _settings_key(lang)
        raise VoiceboxError(
            f"no Voicebox profile set for {lang}: run `autoclipper profiles` then "
            f"`autoclipper settings set voicebox.{key}.profile_id=<id>`")
    if clip_duration <= 0:
        raise VoiceError(f"clip duration must be positive, got {clip_duration}")
    max_speedup = max(1.0, max_speedup)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / "voice_raw.wav"
    norm = out_dir / "voice_norm.wav"
    work = out_dir / "voice_work.wav"
    final = out_dir / "voice.wav"
    engine = engine_for(lang, voice)
    chunk_dir = out_dir / "voice_chunks"
    # normalised text -> (chunks, warnings, chunk file names) of the takes generated in this call
    chunk_runs: dict[str, tuple[list[ChunkInfo], list[str], set[str]]] = {}
    seed: int | None = None
    if chunking is not None:
        seed = chunking.seed if chunking.seed is not None else derive_seed(voice.profile_id, lang)
        prev = _previous_hash(out_dir)
        h = narration_hash(text)
        resuming = prev is None and _pending_hash(chunk_dir) == h  # the last run of this text failed part-way
        if not chunking.cache or (prev is None and not resuming) or prev == h:
            # Cached chunks are reused for an edited narration (voice.json of another text) and after a run
            # that failed part-way. The same narration voiced again, or no voice.json (re-cut clip, deleted
            # voice): a fresh take.
            shutil.rmtree(chunk_dir, ignore_errors=True)
        if chunking.cache:
            chunk_dir.mkdir(parents=True, exist_ok=True)
            (chunk_dir / _PENDING).write_text(h, encoding="utf-8")

    def generate(t: str, lo: float, hi: float, timeout: float | None = None) -> float:
        if chunking is not None:
            return _generate_chunked(
                t, lo, hi, timeout, client=client, voice=voice, lang=lang, engine=engine, opts=chunking,
                seed=seed, wps=wps, chunk_dir=chunk_dir, raw=raw, norm=norm, progress=progress,
                runs=chunk_runs)

        def sub(f: float, msg: str) -> None:
            progress(lo + (hi - lo) * min(max(f, 0.0), 1.0), msg)

        kw = {"timeout": timeout} if timeout is not None and _accepts_timeout(client) else {}
        client.synthesize(t, profile_id=voice.profile_id, language=lang, engine=engine,
                          dst=raw, on_progress=sub, **kw)
        ffmpeg.to_wav(raw, norm, sample_rate=VOICE_SAMPLE_RATE, channels=1)
        return ffmpeg.duration(norm)

    def check_not_empty(d: float) -> None:
        if d < MIN_VOICE_SECONDS:
            for tmp in (norm, raw, work):
                tmp.unlink(missing_ok=True)
            raise VoiceError(f"Voicebox returned empty audio ({d:.1f}s) for {lang}; check the profile/engine")

    progress(0.0, "generating voice")
    d = generate(text, 0.0, 0.6)
    check_not_empty(d)
    shortened = False

    lengthened = False
    warnings: list[str] = []
    truncated = False
    if chunking is not None:
        truncated = any(c.truncated for c in chunk_runs.get(_norm_text(text), ([], [], set()))[0])
    elif looks_truncated(text, d, wps):
        d, truncated = _retry_truncated(text=text, d=d, wps=wps, generate=generate, raw=raw, norm=norm,
                                        out_dir=out_dir, progress=progress, warnings=warnings)

    def cut_short_chunks(t: str) -> list[int]:
        return [c.index + 1 for c in chunk_runs.get(_norm_text(t), ([], [], set()))[0] if c.truncated]

    def reject_lengthened(t: str) -> str | None:
        if chunking is None:
            return None
        bad = cut_short_chunks(t)
        if not bad:
            return None
        n = len(chunk_runs[_norm_text(t)][0])
        return (f"Voicebox cut sentence {', '.join(f'{i}/{n}' for i in bad)} of the lengthened voice short "
                "twice, so it was not used")

    if truncated:
        pass  # never lengthen a cut-short take: the longer text would be cut short as well
    elif lengthen is not None and d < clip_duration * lengthen_below:
        text, d, lengthened = _try_lengthen(
            text=text, d=d, clip_duration=clip_duration, max_speedup=max_speedup, fill_ratio=fill_ratio,
            lengthen=lengthen, generate=generate, raw=raw, norm=norm, out_dir=out_dir, progress=progress,
            timeout=lengthen_timeout, warnings=warnings, reject=reject_lengthened)
    elif d > clip_duration * max_speedup + _EPS and shorten is not None:
        max_words = math.floor(_count_words(text) * clip_duration * max_speedup / d * 0.95)
        max_words = max(1, max_words)
        progress(0.6, f"voice {d:.1f}s > clip {clip_duration:.1f}s, shortening to {max_words} words")
        try:
            candidate = (shorten(text, max_words) or "").strip()
        except Exception as e:  # noqa: BLE001 - refusal, network, bad output: fall back to tempo + trim
            msg = f"shortening failed: {e}; speeding up and trimming instead"
            log.warning(msg)
            progress(0.6, msg)
            candidate = ""
        if candidate and candidate != text:
            text = candidate
            shortened = True
            d = generate(text, 0.6, 0.85)
            check_not_empty(d)

    progress(0.85, "fitting voice to clip")
    tempo = 1.0
    trimmed = False
    if d <= clip_duration + _EPS:
        shutil.copyfile(norm, final)
    elif d / clip_duration <= max_speedup:
        tempo = d / clip_duration
        ffmpeg.change_tempo(norm, work, tempo)
        work.replace(final)  # os.replace: overwrites on Windows too
    else:
        tempo = max_speedup
        if tempo > 1.0:
            ffmpeg.change_tempo(norm, work, tempo)
            src = work
        else:
            src = norm
        ffmpeg.trim_with_fade(src, final, clip_duration, FADE_SECONDS)
        trimmed = True

    for tmp in (norm, work):
        tmp.unlink(missing_ok=True)

    chunks: list[ChunkInfo] = []
    if chunking is not None:
        chunks, chunk_warnings, names = chunk_runs.get(_norm_text(text), ([], [], set()))
        warnings = chunk_warnings + warnings
        # the final take decides (a shortened take regenerated every chunk): a cut-short chunk in it marks
        # the take truncated, so captions show only what is spoken and the UI says "cut short"
        truncated = any(c.truncated for c in chunks)
        _prune_chunk_cache(chunk_dir, names, keep=chunking.cache)
        spoken = _chunked_spoken_text(chunks, wps) if truncated else text
        spoken_narration = (estimate_spoken_prefix(spoken, d, tempo, clip_duration) if trimmed
                            else spoken if truncated else None)
    else:
        spoken_narration = (estimate_spoken_prefix(text, d, tempo, clip_duration) if trimmed
                            else truncated_prefix(text, d, wps) if truncated and wps else None)

    result = VoiceResult(
        path=str(final.resolve()), narration=text, narration_hash=narration_hash(text),
        raw_duration=round(d, 3), final_duration=round(ffmpeg.duration(final), 3),
        clip_duration=round(clip_duration, 3), tempo=round(tempo, 4),
        shortened=shortened, trimmed=trimmed, lengthened=lengthened,
        spoken_narration=spoken_narration, warnings=warnings, truncated=truncated,
        chunks=chunks, chunk_gap=chunking.gap_seconds if chunking is not None else None)
    write_text_atomic(out_dir / "voice.json",
                      json.dumps(result.model_dump(), indent=2, ensure_ascii=False))
    (chunk_dir / _PENDING).unlink(missing_ok=True)
    progress(1.0, "voice ready")
    return result


def _norm_text(t: str) -> str:
    return " ".join(t.split())


def _pending_hash(chunk_dir: Path) -> str | None:
    try:
        return (chunk_dir / _PENDING).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _previous_hash(out_dir: Path) -> str | None:
    try:
        return json.loads((out_dir / "voice.json").read_text(encoding="utf-8")).get("narration_hash")
    except (OSError, ValueError, AttributeError):
        return None


def _snippet(t: str, limit: int = 60) -> str:
    t = _norm_text(t)
    return t if len(t) <= limit else t[: limit - 3] + "..."


def _generate_chunked(t: str, lo: float, hi: float, timeout_cap: float | None, *, client: VoiceboxAPI,
                      voice: VoiceProfile, lang: str, engine: str, opts: ChunkOptions, seed: int | None,
                      wps: float | None, chunk_dir: Path, raw: Path, norm: Path, progress: ProgressFn,
                      runs: dict[str, tuple[list[ChunkInfo], list[str], set[str]]]) -> float:
    """Generate ``t`` chunk by chunk into ``norm`` (and a copy in ``raw``); returns its duration.
    The chunk list and chunk warnings are recorded in ``runs`` under the normalised text.

    ``timeout_cap`` (the lengthen retry) is a time budget for the whole take: each chunk waits at most what
    is left of it, and a VoiceboxTimeout is raised once it is used up."""
    pieces = split_narration(t, max_chars=opts.max_chars, min_chars=opts.min_chars) or [_norm_text(t)]
    n = len(pieces)
    chunk_dir.mkdir(parents=True, exist_ok=True)
    infos: list[ChunkInfo] = []
    files: list[Path] = []
    warns: list[str] = []

    deadline = None if timeout_cap is None else _clock() + timeout_cap

    def cap(x: float | None) -> float | None:
        if deadline is None:
            return x
        left = round(deadline - _clock(), 1)  # 0.1 s steps: the first chunk waits the whole budget
        if left < 1.0:
            raise VoiceboxTimeout(f"the {timeout_cap:.0f}s budget for this voice was used up")
        return left if x is None else min(x, left)

    try:
        for i, piece in enumerate(pieces):
            a = lo + (hi - lo) * i / n
            b = lo + (hi - lo) * (i + 1) / n
            label = f"chunk {i + 1}/{n}"
            words = spoken_words(piece)
            exp = expected_chunk_seconds(piece, wps)
            key = cache_key(piece, profile_id=voice.profile_id, lang=lang, engine=engine, seed=seed,
                            trim_edges=opts.trim_edges)
            good = chunk_dir / f"{key}.wav"
            if opts.cache and good.is_file():
                try:
                    d = wav_seconds(good)
                except Exception:  # noqa: BLE001 - a broken cache file (wave.Error, EOFError, ...) is regenerated
                    d = 0.0
                if d >= MIN_VOICE_SECONDS:
                    progress(b, f"voice {label}: reused unchanged sentence ({d:.1f}s)")
                    infos.append(ChunkInfo(index=i, text=piece, words=words, start=0.0, duration=round(d, 3),
                                           expected=_r(exp), cached=True))
                    files.append(good)
                    continue
            best: tuple[Path, float, bool, bool] | None = None  # (file, seconds, cut short, too long)
            err: Exception | None = None
            attempts = 0
            for attempt in range(2):
                attempts += 1
                try:
                    if attempt == 0 or best is None:
                        tmo = cap(opts.timeout)  # first try, or a retry after an error / timeout
                    else:
                        tmo = cap(opts.retry_timeout)  # retry of a cut-short / too-long take
                except VoiceboxTimeout as e:
                    if best is not None:
                        break  # no time left for the retry: keep the take we have
                    raise VoiceboxTimeout(f"voice {label}: {e}") from e
                lo_a, hi_a = (a, a + (b - a) * 0.8) if attempt == 0 else (a + (b - a) * 0.8, b)

                def sub(f: float, msg: str, lo_a=lo_a, hi_a=hi_a) -> None:
                    progress(lo_a + (hi_a - lo_a) * min(max(f, 0.0), 1.0), f"voice {label}: {msg}")

                raw_c = chunk_dir / f"{key}.try{attempt}.raw.wav"
                out_c = chunk_dir / f"{key}.try{attempt}.wav"
                kw: dict = {}
                if tmo is not None and _accepts_kw(client, "timeout", var_kw=False):
                    kw["timeout"] = tmo
                if seed is not None and _accepts_kw(client, "seed", var_kw=False):
                    kw["seed"] = (seed + attempt) % (2 ** 31)  # a retry samples a different take
                try:
                    client.synthesize(piece, profile_id=voice.profile_id, language=lang, engine=engine,
                                      dst=raw_c, on_progress=sub, **kw)
                    normalize_chunk(raw_c, out_c, sample_rate=VOICE_SAMPLE_RATE, trim_edges=opts.trim_edges)
                    d = wav_seconds(out_c)
                except VoiceboxError as e:  # incl. VoiceboxTimeout (the generation was cancelled)
                    err = e
                    log.warning("voice %s for %s failed (attempt %d): %s", label, lang, attempt + 1, e)
                    if isinstance(e, VoiceboxTimeout) and timeout_cap is not None and best is None:
                        break  # an optional retry with its own time budget (lengthen): no second wait
                    if attempt == 0:
                        progress(a, f"voice {label} failed ({_one_line(e, 120)}), trying it once more")
                    continue
                finally:
                    raw_c.unlink(missing_ok=True)
                short = d < MIN_VOICE_SECONDS or chunk_truncated(piece, d, wps)
                long_ = chunk_overlong(piece, d, wps)
                cand = (out_c, d, short, long_)
                if best is None or _chunk_score(cand, exp) < _chunk_score(best, exp):
                    if best is not None:
                        best[0].unlink(missing_ok=True)
                    best = cand
                else:
                    out_c.unlink(missing_ok=True)
                if not (short or long_):
                    break
                if attempt == 0:
                    what = "cut short" if short else "far too long"
                    log.warning("voice %s for %s: %.1fs for %d words (~%.1fs expected), %s; retrying once",
                                label, lang, d, words, exp or 0.0, what)
                    progress(a + (b - a) * 0.8, f"voice {label}: {d:.1f}s for {words} words "
                                              f"(~{exp or 0:.1f}s expected) was {what}, generating it again")
            if best is None:
                kind = VoiceboxTimeout if isinstance(err, VoiceboxTimeout) else VoiceboxError
                if attempts == 1:
                    raise kind(f"voice {label} failed: {_one_line(err) if err else 'no audio'}") from err
                raise kind(f"voice {label} failed twice ({_one_line(err) if err else 'no audio'}); "
                                    f"sentence: \"{_snippet(piece)}\"") from err
            path, d, short, long_ = best
            dst = good if not (short or long_) else chunk_dir / f"{key}.kept.wav"
            path.replace(dst)
            files.append(dst)
            infos.append(ChunkInfo(index=i, text=piece, words=words, start=0.0, duration=round(d, 3),
                                   expected=_r(exp), attempts=attempts, truncated=short, overlong=long_))
            if short:
                warns.append(f"Voicebox cut sentence {i + 1}/{n} short twice ({d:.1f}s for {words} words, about "
                             f"{exp or 0:.0f}s expected): \"{_snippet(piece)}\" is partly missing. Edit the "
                             "narration (Review & edit) or re-render to try again")
            elif long_:
                warns.append(f"sentence {i + 1}/{n} came out far too long twice ({d:.1f}s for {words} words, "
                             f"about {exp or 0:.0f}s expected): \"{_snippet(piece)}\" may repeat or babble. "
                             "Listen to it; re-render to try again")
            progress(b, f"voice {label} ready ({d:.1f}s)")
        spans = concat_wavs(files, norm, gap_seconds=opts.gap_seconds)
    finally:
        for p in chunk_dir.glob("*.try*"):
            p.unlink(missing_ok=True)
    for info, (start, dur) in zip(infos, spans):
        info.start = round(start, 3)
        info.duration = round(dur, 3)
    shutil.copyfile(norm, raw)
    runs[_norm_text(t)] = (infos, warns, {f.name for f in files})
    return wav_seconds(norm)


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 2)


def _chunk_score(c: tuple[Path, float, bool, bool], exp: float | None) -> float:
    """Lower is better: a take that passed both checks first, then the one closest to the expected length."""
    _p, d, short, long_ = c
    bad = 1.0 if (short or long_) else 0.0
    if not exp:
        return bad * 1000 - d  # no rate: prefer the longer take
    return bad * 1000 + abs(math.log(max(d, 0.01) / exp))


def _chunked_spoken_text(chunks: list[ChunkInfo], wps: float | None) -> str:
    """The words a chunked take probably says: whole chunks, and only the spoken prefix of a cut-short one."""
    parts = []
    for c in chunks:
        if c.truncated and wps:
            parts.append(truncated_prefix(c.text, c.duration, wps))
        else:
            parts.append(c.text)
    return " ".join(p for p in parts if p)


def _prune_chunk_cache(chunk_dir: Path, names: set[str], *, keep: bool) -> None:
    """Drop cached chunk files the final take does not use (or the whole folder when caching is off)."""
    if not keep:
        shutil.rmtree(chunk_dir, ignore_errors=True)
        return
    if not chunk_dir.is_dir():
        return
    for p in list(chunk_dir.iterdir()):
        if p.is_file() and p.name not in names and p.name != _PENDING:
            p.unlink(missing_ok=True)


def _retry_truncated(*, text: str, d: float, wps: float, generate, raw: Path, norm: Path, out_dir: Path,
                     progress: ProgressFn, warnings: list[str]) -> tuple[float, bool]:
    """Generate a cut-short take once more; keep the longer one. Returns (duration, still truncated)."""
    need = expected_seconds(text, wps)
    progress(0.6, f"voice {d:.1f}s is far shorter than the {_count_words(text)}-word text needs "
                  f"(~{need:.0f}s): Voicebox stopped early, generating it again")
    log.warning("voice take %.1fs for a %d-word text (~%.0fs expected): cut short, retrying once",
                d, _count_words(text), need)
    backup_raw = out_dir / "voice_raw.first.wav"
    backup_norm = out_dir / "voice_norm.first.wav"
    shutil.copyfile(raw, backup_raw)
    shutil.copyfile(norm, backup_norm)
    try:
        try:
            d2 = generate(text, 0.6, 0.8)
        except Exception as e:  # noqa: BLE001 - the first take is still usable
            log.warning("regenerating the cut-short voice failed: %s", e)
            d2 = 0.0
        if d2 > d:
            d = d2
        else:
            backup_raw.replace(raw)
            backup_norm.replace(norm)
    finally:
        backup_raw.unlink(missing_ok=True)
        backup_norm.unlink(missing_ok=True)
    if not looks_truncated(text, d, wps):
        return d, False
    warnings.append(f"Voicebox cut the narration short twice: the voice is {d:.1f}s but the "
                    f"{_count_words(text)}-word text needs about {need:.0f}s. Captions show only the part that "
                    "is spoken; shorten the narration (Review & edit) or re-render to try again")
    return d, True


def _try_lengthen(*, text: str, d: float, clip_duration: float, max_speedup: float, fill_ratio: float,
                  lengthen: LengthenFn, generate, raw: Path, norm: Path, out_dir: Path,
                  progress: ProgressFn, timeout: float | None = None,
                  warnings: list[str] | None = None,
                  reject: Callable[[str], str | None] | None = None) -> tuple[str, float, bool]:
    """One lengthen retry. Returns (text, duration, lengthened); the original take's files are
    restored whenever the longer take is not used (and a warning is added to ``warnings``)."""
    warnings = warnings if warnings is not None else []
    kept = f"kept the original {d:.1f}s voice ({d / clip_duration:.0%} of the {clip_duration:.1f}s clip)"
    words = _count_words(text)
    target = max(words + 1, math.ceil(words * clip_duration * fill_ratio / max(d, 1e-6)))
    progress(0.6, f"voice {d:.1f}s fills only {d / clip_duration:.0%} of the {clip_duration:.1f}s clip, "
                  f"lengthening to {target} words")
    try:
        candidate = " ".join((lengthen(text, target) or "").split())
    except Exception as e:  # noqa: BLE001 - refusal, network, bad output: keep the original take
        msg = f"lengthening failed: {e}; keeping the original voice"
        log.warning(msg)
        progress(0.6, msg)
        warnings.append(f"narration could not be lengthened ({_one_line(e)}); {kept}")
        return text, d, False
    if not candidate or candidate == " ".join(text.split()) or _count_words(candidate) <= words:
        log.warning("lengthen returned no longer narration; keeping the original voice")
        warnings.append(f"narration could not be lengthened (no longer text); {kept}")
        return text, d, False

    backup_raw = out_dir / "voice_raw.orig.wav"
    backup_norm = out_dir / "voice_norm.orig.wav"
    shutil.copyfile(raw, backup_raw)
    shutil.copyfile(norm, backup_norm)

    def restore(reason: str) -> tuple[str, float, bool]:
        log.warning("%s; keeping the original voice", reason)
        progress(0.85, f"{reason}; keeping the original voice")
        warnings.append(f"{reason}; {kept}")
        backup_raw.replace(raw)
        backup_norm.replace(norm)
        return text, d, False

    try:
        try:
            d2 = generate(candidate, 0.6, 0.85, timeout)
        except Exception as e:  # noqa: BLE001 - the original take is still good
            if isinstance(e, VoiceboxTimeout):
                return restore(f"the lengthened voice took longer than {timeout:.0f}s "
                               "(voicebox.lengthen_timeout_seconds) and was cancelled")
            return restore(f"generating the lengthened voice failed: {_one_line(e)}")
        if d2 < MIN_VOICE_SECONDS:
            return restore(f"the lengthened voice was empty ({d2:.1f}s)")
        if d2 > clip_duration * max_speedup + _EPS:
            return restore(f"the lengthened voice ({d2:.1f}s) does not fit the {clip_duration:.1f}s clip "
                           f"even at x{max_speedup:g}")
        reason = reject(candidate) if reject is not None else None
        if reason:
            return restore(reason)
        return candidate, d2, True
    finally:
        backup_raw.unlink(missing_ok=True)
        backup_norm.unlink(missing_ok=True)


def _one_line(e: BaseException, limit: int = 200) -> str:
    text = " ".join(str(e).split()) or type(e).__name__
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _accepts_kw(client: object, name: str, var_kw: bool = True) -> bool:
    """Whether client.synthesize takes ``name=`` (VoiceboxClient takes timeout and seed; older fakes may not).
    ``var_kw=False``: only a parameter of that name counts, not ``**kwargs`` (a wrapper may pass them on
    to a client that does not take them)."""
    import inspect

    try:
        params = inspect.signature(client.synthesize).parameters  # type: ignore[attr-defined]
    except (TypeError, ValueError, AttributeError):
        return False
    return name in params or (var_kw and any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()))


def _accepts_timeout(client: object) -> bool:
    return _accepts_kw(client, "timeout")


def load_voice_result(out_dir: Path) -> VoiceResult | None:
    """voice.json if present, valid and its voice.wav still exists; else None."""
    p = Path(out_dir) / "voice.json"
    if not p.is_file():
        return None
    try:
        result = VoiceResult.model_validate_json(p.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError):
        return None
    wav = Path(out_dir) / "voice.wav"
    if not wav.is_file() or wav.stat().st_size == 0:
        return None
    return result
