"""Pipeline orchestrator: source video -> per-clip, per-language finished shorts.

Job stages (once per job):      ingest -> transcribe -> signals -> moments -> cut
Clip stages (per clip x lang):  script -> voice -> captions -> render (+ metadata)

Progress lives in data/jobs/<id>/job.json through JobStore, so a later web UI can read it while a
worker thread runs this module. Every stage is idempotent: a stage whose output file already exists
(and is not empty) is skipped. Staleness is persistent: before a stage regenerates, the outputs of the
stages after it are deleted, so a resume after a failed/interrupted later stage rebuilds them.

External services (Claude, Voicebox, faster-whisper) are injected through `Deps`, so tests use fakes.
When no deps are passed, each real client is built lazily, only when a stage needs it.
"""
from __future__ import annotations

import json
import os
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError

from .config import Settings, load_settings
from .interfaces import LLM, LogFn, ProgressFn, Transcriber, VoiceboxAPI
from .jobstore import CLIP_STAGES, JOB_STAGES, OPTIONAL_JOB_STAGES, JobStore
from .models import ClipLangState, ClipScript, ClipState, Moment, StageState, Transcript

SUPPORTED_LANGS = ("en", "hi")


class PipelineError(RuntimeError):
    """A job-level failure (or a rerender failure) with an actionable message."""


# ---------------------------------------------------------------------------------------------
# dependencies
# ---------------------------------------------------------------------------------------------
@dataclass
class Deps:
    llm: LLM
    voicebox: VoiceboxAPI
    transcriber: Transcriber


def _make_llm(settings: Settings) -> LLM:
    from .llm import AnthropicLLM

    return AnthropicLLM(settings.llm)


def _make_voicebox(settings: Settings) -> VoiceboxAPI:
    from .voicebox import VoiceboxClient

    return VoiceboxClient.from_settings(settings.voicebox)


def _make_transcriber(settings: Settings) -> Transcriber:
    from .stages.transcribe import WhisperTranscriber

    return WhisperTranscriber(settings.whisper)


def default_deps(settings: Settings) -> Deps:
    """Build all real clients now. The pipeline itself prefers the lazy builder below."""
    return Deps(llm=_make_llm(settings), voicebox=_make_voicebox(settings),
                transcriber=_make_transcriber(settings))


class _LazyDeps:
    """Gives out injected deps, or builds each real client on first use (only what is needed)."""

    def __init__(self, settings: Settings, deps: Deps | None):
        self._settings = settings
        self._deps = deps
        self._llm: LLM | None = None
        self._voicebox: VoiceboxAPI | None = None
        self._transcriber: Transcriber | None = None

    @property
    def llm(self) -> LLM:
        if self._deps is not None:
            return self._deps.llm
        if self._llm is None:
            self._llm = _make_llm(self._settings)
        return self._llm

    @property
    def voicebox(self) -> VoiceboxAPI:
        if self._deps is not None:
            return self._deps.voicebox
        if self._voicebox is None:
            self._voicebox = _make_voicebox(self._settings)
        return self._voicebox

    @property
    def transcriber(self) -> Transcriber:
        if self._deps is not None:
            return self._deps.transcriber
        if self._transcriber is None:
            self._transcriber = _make_transcriber(self._settings)
        return self._transcriber


# ---------------------------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------------------------
def parse_langs(value: str | list[str] | None) -> list[str] | None:
    """'en,hi' -> ['en', 'hi'] (deduplicated, validated). None stays None."""
    if value is None:
        return None
    items = value.split(",") if isinstance(value, str) else list(value)
    langs = list(dict.fromkeys(x.strip().lower() for x in items if x.strip()))
    if not langs:
        raise PipelineError("no languages given (use e.g. --langs en,hi)")
    bad = [x for x in langs if x not in SUPPORTED_LANGS]
    if bad:
        raise PipelineError(f"unsupported language(s): {', '.join(bad)} (supported: {', '.join(SUPPORTED_LANGS)})")
    return langs


def effective_settings(settings: Settings, *, langs: list[str] | None = None, count: int | None = None,
                       min_seconds: float | None = None, max_seconds: float | None = None,
                       voiceover: bool | None = None) -> Settings:
    """Copy of settings with CLI overrides applied and re-validated (bounds from config.py)."""
    data = settings.model_dump()
    if voiceover is not None:
        data["voiceover"] = bool(voiceover)
    if langs is not None:
        data["languages"] = parse_langs(langs)
    if count is not None:
        data["clips"]["count"] = count
    if min_seconds is not None:
        data["clips"]["min_seconds"] = min_seconds
    if max_seconds is not None:
        data["clips"]["max_seconds"] = max_seconds
    try:
        out = Settings.model_validate(data)
    except ValidationError as e:
        raise PipelineError(f"invalid options: {_validation_summary(e)}") from e
    if not out.languages:
        raise PipelineError("settings.languages is empty - set at least one of en, hi")
    if out.clips.min_seconds > out.clips.max_seconds:
        raise PipelineError(f"min clip length ({out.clips.min_seconds}s) is greater than max "
                            f"({out.clips.max_seconds}s)")
    return out


def job_voiceover(job: Any, settings: Settings | None = None) -> bool:
    """The voice-over mode of a job. It is fixed in job.json when the job is created (the `voiceover`
    setting at that moment, or run --no-voiceover / --voiceover, web options.voiceover), so a later
    Settings change never switches an existing job. A job without one was made before the option
    existed, i.e. with the voice-over: on. ``settings`` is unused (kept for call-site symmetry)."""
    own = getattr(job, "voiceover", None)
    return own if isinstance(own, bool) else True


def settings_for_job(job: Any, settings: Settings) -> Settings:
    """settings with `voiceover` set to the job's effective mode (a copy when it differs)."""
    vo = job_voiceover(job, settings)
    return settings if vo == settings.voiceover else settings.model_copy(update={"voiceover": vo})


def _validation_summary(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg', 'invalid')}" if loc else str(err.get("msg", "invalid")))
    return "; ".join(parts)


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def output_exists(path: Path) -> bool:
    """Idempotency rule from CONTRACT.md: the file exists and is not empty."""
    return path.is_file() and path.stat().st_size > 0


def _write_script(path: Path, script: ClipScript) -> None:
    from .stages.script import save_script

    save_script(script, path)


def _error_text(e: BaseException) -> str:
    msg = str(e).strip()
    return msg if msg else type(e).__name__


INTERRUPTED = "interrupted"


def _unlink_outputs(out_dir: Path, *names: str) -> None:
    """Delete downstream outputs before an upstream stage regenerates (persistent staleness)."""
    for name in names:
        (out_dir / name).unlink(missing_ok=True)


# Per clip-lang files that depend on cut.mp4 (script.json does not: it comes from the transcript).
_CUT_DEPENDENT_OUTPUTS = ("voice.wav", "voice.json", "words.json", "captions.ass", "captions.json", "final.mp4",
                          "render.json", "captions_overlay.webm", "captions_overlay.mov")


def _invalidate(store: JobStore, clip: ClipState, lang: str, stages: Sequence[str]) -> None:
    """Reset the given clip-lang stages to pending (progress 0) and clear `final`, then save.

    Called whenever their outputs are unlinked, so job.json never claims a deleted output is done.
    """
    state = clip.langs.get(lang)
    if state is None:
        return
    for name in stages:
        st = state.stages.setdefault(name, StageState())
        st.status = "pending"
        st.progress = 0.0
        st.message = ""
    state.final = None
    store.save()


def _move_corrupt(path: Path, log: LogFn, job_id: str, error: BaseException) -> None:
    """A JSON output that does not parse is renamed to <name>.corrupt (kept for inspection) and redone."""
    dst = path.with_name(path.name + ".corrupt")
    os.replace(path, dst)
    log(f"[{job_id}] warning: {path.name} is corrupt ({_error_text(error).splitlines()[0]}); "
        f"moved to {dst.name} and regenerating it")


def _sweep_running(store: JobStore) -> None:
    """Any stage still marked 'running' becomes 'failed' / 'interrupted' (nothing stays running)."""
    job = store.job
    states = list(job.stages.values())
    for c in job.clips:
        for ls in c.langs.values():
            states.extend(ls.stages.values())
    for st in states:
        if st.status == "running":
            st.status = "failed"
            st.message = INTERRUPTED


def _mark_job_interrupted(store: JobStore, log: LogFn) -> None:
    job = store.job
    _sweep_running(store)
    job.status = "failed"
    job.error = INTERRUPTED
    try:
        store.save()
    finally:
        log(f"[{job.id}] interrupted; resume with `autoclipper run --job {job.id}`")


class _Reporter:
    """Writes stage state into job.json and prints progress lines.

    Line format: `[<job_id>] <stage> <clip>/<lang> <pct>% <message>` (clip/lang omitted for job stages).
    Progress callbacks are throttled: a line every 10%, a job.json write every 5% or 1 s.
    """

    def __init__(self, store: JobStore, log: LogFn, debug: bool = False):
        self.store = store
        self.log = log
        self.debug = debug

    def _where(self, stage: str, clip: int | None, lang: str | None) -> str:
        if clip is None:
            return stage
        return f"{stage} {clip:02d}/{lang}"

    @property
    def has_cancel_point(self) -> bool:
        return bool(getattr(self.log, "cancel_point", False))

    def cancel_point(self) -> None:
        """Lets a cancel/stop requested in the web worker interrupt a long subprocess (Demucs). The worker's
        log function treats an empty line as a cancel point only; other log functions are not called."""
        if getattr(self.log, "cancel_point", False):
            self.log("")

    def line(self, stage: str, pct: float, message: str, clip: int | None = None,
             lang: str | None = None) -> None:
        text = f"[{self.store.job.id}] {self._where(stage, clip, lang)} {round(pct * 100):d}%"
        self.log(f"{text} {message}".rstrip())

    def start(self, stage: str, clip: int | None = None, lang: str | None = None, message: str = "") -> None:
        self.store.set_stage(stage, "running", 0.0, message, clip=clip, lang=lang)
        self.line(stage, 0.0, message or "started", clip, lang)

    def done(self, stage: str, clip: int | None = None, lang: str | None = None, message: str = "") -> None:
        self.store.set_stage(stage, "done", 1.0, message, clip=clip, lang=lang)
        self.line(stage, 1.0, message or "done", clip, lang)

    def skip(self, stage: str, clip: int | None = None, lang: str | None = None,
             message: str = "output exists") -> None:
        st = self._state(stage, clip, lang)
        # "skipped" only when this stage never ran; a stage that finished before stays "done".
        if st.status != "done":
            self.store.set_stage(stage, "skipped", 1.0, message, clip=clip, lang=lang)
        self.line(stage, 1.0, f"skipped ({message})", clip, lang)

    def fail(self, stage: str, error: BaseException, clip: int | None = None, lang: str | None = None) -> None:
        msg = _error_text(error)
        st = self._state(stage, clip, lang)
        self.store.set_stage(stage, "failed", st.progress, msg, clip=clip, lang=lang)
        self.line(stage, st.progress, f"FAILED: {msg}", clip, lang)
        if self.debug:
            self.log("".join(traceback.format_exception(type(error), error, error.__traceback__)).rstrip())

    def interrupted(self, stage: str, clip: int | None = None, lang: str | None = None) -> None:
        st = self._state(stage, clip, lang)
        st.status = "failed"
        st.message = INTERRUPTED
        self.line(stage, st.progress, f"FAILED: {INTERRUPTED}", clip, lang)

    def progress(self, stage: str, clip: int | None = None, lang: str | None = None) -> ProgressFn:
        last_line = [-1]
        last_save = [-1, 0.0]

        def on_progress(fraction: float, message: str = "") -> None:
            f = min(1.0, max(0.0, float(fraction)))
            pct = int(f * 100)
            now = time.monotonic()
            if pct // 5 != last_save[0] // 5 or now - last_save[1] >= 1.0:
                last_save[0], last_save[1] = pct, now
                try:
                    self.store.set_stage(stage, "running", f, message, clip=clip, lang=lang)
                except OSError as e:
                    # A failed progress save (e.g. job.json locked by a reader on Windows) must never
                    # abort the running stage; the next save brings job.json up to date.
                    self.log(f"[{self.store.job.id}] warning: could not save progress to job.json: "
                             f"{_error_text(e)}")
            if pct // 10 != last_line[0] // 10:
                last_line[0] = pct
                self.line(stage, f, message, clip, lang)

        return on_progress

    def _state(self, stage: str, clip: int | None, lang: str | None) -> StageState:
        job = self.store.job
        if clip is None:
            return job.stages.setdefault(stage, StageState())
        return job.clips[clip].langs[lang].stages.setdefault(stage, StageState())  # type: ignore[index]


# ---------------------------------------------------------------------------------------------
# job-level stages
# ---------------------------------------------------------------------------------------------
def _record_source_info(store: JobStore, log: LogFn, settings: Settings | None = None,
                        info: dict | None = None) -> None:
    """source_info.json: the yt-dlp page info of a fresh download (``info``), else what the source string
    tells (backfill for older jobs, no network). The user's notes are kept. job.json points at the file."""
    from .stages import source_info as si

    try:
        if info is not None:
            chars = settings.context.description_chars if settings is not None else si.DESCRIPTION_MAX
            fresh = si.from_ytdlp(info, store.job.source, description_chars=chars)
            si.write_source_info(store.dir, si.merge(si.read_source_info(store.dir), fresh))
        else:
            si.ensure_source_info(store.dir, store.job.source)
    except (OSError, ValueError) as e:  # the context is optional: never fail the job for it
        log(f"[{store.job.id}] warning: could not write {si.SOURCE_INFO_FILE}: {_error_text(e)}")
        return
    if store.job.source_info != si.SOURCE_INFO_FILE:
        store.job.source_info = si.SOURCE_INFO_FILE
        store.save()


def _job_stage_ingest(store: JobStore, rep: _Reporter, settings: Settings | None = None) -> Path:
    from .stages.ingest import extract_audio_for_asr, find_source, ingest

    existing = find_source(store.dir)
    if existing is not None and output_exists(existing) and output_exists(store.dir / "audio.wav"):
        _record_source_info(store, rep.log, settings)
        rep.skip("ingest", message=existing.name)
        return existing
    rep.start("ingest", message=store.job.source)
    got: dict = {}
    src = ingest(store.job.source, store.dir, on_progress=rep.progress("ingest"), on_info=got.update)
    _record_source_info(store, rep.log, settings, got or None)
    extract_audio_for_asr(src, store.dir)
    rep.done("ingest", message=src.name)
    return src


def job_source_info(store: JobStore, settings: Settings):
    """The job's SourceInfo for the LLM calls, or None (context turned off, or no source_info.json)."""
    if not settings.context.enabled:
        return None
    from .stages.source_info import read_source_info

    return read_source_info(store.dir)


def _job_stage_signals(store: JobStore, rep: _Reporter, settings: Settings, source: Path):
    """Compute (or reuse) signals.json. Only needed while moments are still to be picked; a failure never
    fails the job (moments are then picked from the transcript only). Returns Signals or None."""
    out = store.dir / "moments.json"
    if not settings.signals.enabled:
        rep.skip("signals", message="turned off in settings (signals.enabled)")
        return None
    if output_exists(out):
        rep.skip("signals", message="moments already picked")
        return None
    from .stages.signals import SignalsError, compute_signals, signals_current

    sig_path = store.dir / "signals.json"
    audio = store.dir / "audio.wav"
    cached = signals_current(sig_path, source, audio if audio.is_file() else None, settings.signals)
    if cached is not None:
        rep.skip("signals", message="signals.json")
        return cached
    rep.start("signals", message="loudness" + (", scene cuts, motion" if settings.signals.video else ""))
    try:
        sig = compute_signals(source, sig_path, settings.signals, audio=audio,
                              on_progress=rep.progress("signals"))
    except (SignalsError, OSError, ValueError) as e:
        msg = f"unavailable ({_error_text(e).splitlines()[0][:200]}); moments from the transcript only"
        rep.line("signals", 1.0, "WARNING " + msg)
        store.set_stage("signals", "skipped", 1.0, msg)
        return None
    except Exception as e:  # noqa: BLE001 - e.g. FFmpegError: the signals are optional
        msg = f"unavailable ({_error_text(e).splitlines()[0][:200]}); moments from the transcript only"
        rep.line("signals", 1.0, "WARNING " + msg)
        store.set_stage("signals", "skipped", 1.0, msg)
        return None
    statics = sum(sig.static or []) + sum(sig.menu or [])
    rep.done("signals", message=f"{sig.compute_seconds:.0f}s: {len(sig.cuts)} scene cuts, "
                                f"{statics} static/menu seconds" if sig.has_video
             else f"{sig.compute_seconds:.0f}s: audio only")
    return sig


def _job_stage_transcribe(store: JobStore, rep: _Reporter, settings: Settings, deps: _LazyDeps) -> Transcript:
    out = store.dir / "transcript.json"
    if output_exists(out):
        try:
            transcript = Transcript.model_validate_json(out.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as e:  # pydantic ValidationError is a ValueError
            _move_corrupt(out, rep.log, store.job.id, e)
        else:
            rep.skip("transcribe", message="transcript.json")
            return transcript
    from .stages.transcribe import run_transcribe

    rep.start("transcribe", message=f"whisper {settings.whisper.model}")
    transcript = run_transcribe(store.dir / "audio.wav", out, deps.transcriber,
                                language=settings.whisper.language, on_progress=rep.progress("transcribe"))
    n_words = sum(len(s.words) for s in transcript.segments)
    rep.done("transcribe", message=f"{len(transcript.segments)} segments, {n_words} words, "
                                   f"language {transcript.language}")
    return transcript


def _job_stage_moments(store: JobStore, rep: _Reporter, settings: Settings, deps: _LazyDeps,
                       transcript: Transcript, signals: Any = None) -> tuple[list[Moment], bool]:
    """Returns (moments, fresh): fresh is True when moments were (re)generated in this run."""
    out = store.dir / "moments.json"
    fresh = False
    moments: list[Moment] | None = None
    if output_exists(out):
        try:
            raw = json.loads(out.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("expected a JSON list of moments")
            moments = [Moment.model_validate(m) for m in raw]
        except (ValueError, TypeError, UnicodeDecodeError) as e:  # JSONDecodeError/ValidationError too
            _move_corrupt(out, rep.log, store.job.id, e)
            moments = None
        else:
            rep.skip("moments", message=f"{len(moments)} moments in moments.json")
    if moments is None:
        from .stages.moments import plan_moments, run_moments
        from .stages.source_info import context_for_prompt

        plan = plan_moments(transcript, settings.clips, signals, settings.signals)
        how = (f"; low speech ({plan.signals.coverage:.0%}): using audio/video signals"
               if plan.low_speech and plan.signals is not None else "")
        rep.start("moments", message=f"asking {settings.llm.model} for {settings.clips.count} moments{how}")
        context = context_for_prompt(job_source_info(store, settings))
        moments = run_moments(transcript, deps.llm, settings.clips, out, signals=signals,
                              signal_settings=settings.signals if signals is not None else None, context=context,
                              log=lambda line: rep.line("moments", 0.9, line))
        fresh = True
        rep.done("moments", message=f"{len(moments)} moments")
    if not moments:
        raise PipelineError("no usable moments were found in this video (try a lower --min or a longer video)")
    return moments, fresh


_OVERRIDE_TOLERANCE = 0.5  # seconds


def _warn_ignored_overrides(store: JobStore, log: LogFn, moments: list[Moment], count: int | None,
                            min_seconds: float | None, max_seconds: float | None) -> None:
    """moments.json was reused: tell the user when --clips/--min/--max ask for something else."""
    differs: list[str] = []
    if count is not None and count != len(moments):
        differs.append(f"--clips {count} (moments.json has {len(moments)})")
    if min_seconds is not None and any(m.duration < min_seconds - _OVERRIDE_TOLERANCE for m in moments):
        differs.append(f"--min {min_seconds:g}")
    if max_seconds is not None and any(m.duration > max_seconds + _OVERRIDE_TOLERANCE for m in moments):
        differs.append(f"--max {max_seconds:g}")
    if differs:
        log(f"[{store.job.id}] warning: {', '.join(differs)} ignored for an existing job (moments.json is "
            f"reused); delete {store.rel(store.dir / 'moments.json')} to pick moments again")


def _stale_name(clips_root: Path, index: int) -> Path:
    n = 1
    while (clips_root / f"{index:02d}.stale-{n}").exists():
        n += 1
    return clips_root / f"{index:02d}.stale-{n}"


def _rebuild_clips(store: JobStore, moments: list[Moment], log: LogFn) -> None:
    """Moments were re-picked: make job.clips follow them. Changed or dropped clip dirs are renamed
    aside to clips/NN.stale-<n> (never deleted) and their state is reset."""
    job = store.job
    clips_root = store.dir / "clips"
    old = {c.index: c for c in job.clips}
    new_clips: list[ClipState] = []
    for i, m in enumerate(moments):
        prev = old.get(i)
        if prev is not None and prev.moment == m:
            new_clips.append(prev)
        else:
            new_clips.append(ClipState(index=i, moment=m))
    keep = {c.index for c in new_clips if old.get(c.index) is c}
    for idx in sorted(old):
        if idx in keep:
            continue
        d = clips_root / f"{idx:02d}"
        if d.exists():
            dst = _stale_name(clips_root, idx)
            d.rename(dst)
            log(f"[{job.id}] clip {idx:02d}: moment changed, old files moved to {store.rel(dst)}")
    job.clips = new_clips


def _populate_clips(store: JobStore, moments: list[Moment], langs: list[str], *, fresh: bool = False,
                    log: LogFn = print) -> None:
    """job.clips[i] <-> moments[i]; add missing languages to existing clips (resume with new --langs).

    When the moments were freshly (re)picked and job.clips already exists, job.clips is rebuilt from them.
    """
    job = store.job
    if not job.clips:
        job.clips = [ClipState(index=i, moment=m) for i, m in enumerate(moments)]
    elif fresh:
        _rebuild_clips(store, moments, log)
    for clip in job.clips:
        for lang in langs:
            state = clip.langs.setdefault(lang, ClipLangState())
            for s in CLIP_STAGES:
                state.stages.setdefault(s, StageState())
    store.save()


def _invalidate_after_cut(store: JobStore, clip: ClipState) -> None:
    """cut.mp4 was (re)made: everything built from the old cut is stale (script.json is kept)."""
    clip_root = store.clip_dir(clip.index)
    langs = set(clip.langs)
    for d in clip_root.iterdir():
        if d.is_dir():
            _unlink_outputs(d, *_CUT_DEPENDENT_OUTPUTS)
            langs.add(d.name)
    for lang in sorted(langs):
        _invalidate(store, clip, lang, ("voice", "captions", "render"))


def _job_stage_cut(store: JobStore, rep: _Reporter, settings: Settings, source: Path,
                   langs: list[str]) -> list[int]:
    """Cut every clip. Returns indexes that have a cut.mp4. A failed cut fails only that clip."""
    from .stages.cut import cut_clip

    job = store.job
    todo = [c for c in job.clips if not output_exists(store.clip_dir(c.index) / "cut.mp4")]
    if not todo:
        for c in job.clips:
            c.cut = store.rel(store.clip_dir(c.index) / "cut.mp4")
        store.save()
        rep.skip("cut", message=f"{len(job.clips)} clips already cut")
        return [c.index for c in job.clips]

    rep.start("cut", message=f"{len(todo)} of {len(job.clips)} clips to cut")
    ok: list[int] = []
    errors: list[str] = []
    total = len(job.clips)
    for n, clip in enumerate(job.clips):
        dst = store.clip_dir(clip.index) / "cut.mp4"
        if not output_exists(dst):
            base, span = n / total, 1.0 / total
            inner = rep.progress("cut")

            def clip_progress(f: float, msg: str = "", _b: float = base, _s: float = span,
                              _i: int = clip.index) -> None:
                inner(_b + _s * f, f"clip {_i:02d} {msg}".strip())

            try:
                cut_clip(source, clip.moment, dst, settings.clips, on_progress=clip_progress)
            except Exception as e:  # one bad cut must not kill the other clips
                msg = f"cut failed: {_error_text(e)}"
                errors.append(f"clip {clip.index:02d}: {msg}")
                rep.line("cut", base, f"clip {clip.index:02d} FAILED: {_error_text(e)}")
                for lang in langs:
                    state = clip.langs[lang]
                    state.error = msg
                    state.stages["script"].status = "failed"
                    state.stages["script"].message = msg
                store.save()
                continue
            _invalidate_after_cut(store, clip)  # re-cut: voice/captions/render must follow the new cut
        clip.cut = store.rel(dst)
        ok.append(clip.index)
    store.save()
    if not ok:
        raise PipelineError("every clip failed to cut: " + "; ".join(errors))
    rep.done("cut", message=f"{len(ok)} clips" + (f", {len(errors)} failed" if errors else ""))
    return ok


# ---------------------------------------------------------------------------------------------
# clip x language stages
# ---------------------------------------------------------------------------------------------
def _shorten_fn(deps: _LazyDeps, lang: str):
    """ShortenFn that resolves the LLM only if the voice stage actually needs to shorten."""

    def shorten(narration: str, max_words: int) -> str:
        from .stages.script import make_shorten_fn

        return make_shorten_fn(deps.llm, lang)(narration, max_words)

    return shorten


def _lengthen_fn(deps: _LazyDeps, lang: str):
    """LengthenFn resolved lazily; None-safe when the LLM has no optional `lengthen`."""

    def lengthen(narration: str, target_words: int) -> str:
        fn = getattr(deps.llm, "lengthen", None)
        if not callable(fn):
            return narration  # unchanged -> the voice stage keeps the original take
        return fn(lang=lang, narration=narration, target_words=target_words)

    return lengthen


def _voice_is_current(out_dir: Path, script: ClipScript, settings: Settings | None = None,
                      lang: str = "en") -> bool:
    """voice.wav matches the narration. M2: with voicebox.chunking on, a take made in one Voicebox call
    that came out far shorter than its words need (Chatterbox stopped early) is not current either, so
    the next resume / re-render regenerates it sentence by sentence."""
    from .stages.voice import load_voice_result, looks_truncated, narration_hash

    if not output_exists(out_dir / "voice.wav"):
        return False
    result = load_voice_result(out_dir)
    if result is None or result.narration_hash != narration_hash(script.narration):
        return False
    if (settings is not None and settings.voicebox.chunking and not result.chunks
            and looks_truncated(result.narration, result.raw_duration, settings.script.wps(lang))):
        return False
    return True


def _run_voice(store: JobStore, rep: _Reporter, settings: Settings, deps: _LazyDeps, clip: ClipState,
               lang: str, script: ClipScript, script_path: Path, allow_lengthen: bool = False) -> ClipScript:
    from . import ffmpeg
    from .stages.voice import make_voice
    from .stages.voice_chunks import ChunkOptions

    out_dir = store.clip_dir(clip.index, lang)
    cut = store.clip_dir(clip.index) / "cut.mp4"
    _unlink_outputs(out_dir, "words.json", "captions.ass", "captions.json", "final.mp4", "render.json")
    _invalidate(store, clip, lang, ("captions", "render"))
    rep.start("voice", clip.index, lang, f"engine {_engine_label(settings, lang)}")
    result = make_voice(client=deps.voicebox, narration=script.narration, lang=lang,
                        voice=settings.voice_for(lang), clip_duration=ffmpeg.duration(cut), out_dir=out_dir,
                        max_speedup=settings.audio.max_speedup, shorten=_shorten_fn(deps, lang),
                        on_progress=rep.progress("voice", clip.index, lang),
                        lengthen=_lengthen_fn(deps, lang) if allow_lengthen else None,
                        lengthen_below=settings.script.lengthen_below,
                        fill_ratio=settings.script.fill_ratio,
                        lengthen_timeout=min(settings.voicebox.lengthen_timeout_seconds,
                                             settings.voicebox.timeout_seconds),
                        wps=settings.script.wps(lang),
                        chunking=ChunkOptions.from_settings(settings.voicebox))
    if result.narration.strip() != script.narration.strip():
        # Keep script.json in sync with what was actually spoken (captions + rerender rely on it).
        script = script.model_copy(update={"narration": result.narration})
        _write_script(script_path, script)
        how = ("lengthened to fill the clip" if result.lengthened else "shortened to fit" if result.shortened
               else "given a space after each glued । (sentence split)")
        rep.line("voice", 1.0, f"narration was {how}; script.json updated", clip.index, lang)
    notes = [f"{result.final_duration:.1f}s"]
    if len(result.chunks) > 1:
        notes.append(f"{len(result.chunks)} sentences")
    if result.tempo != 1.0:
        notes.append(f"tempo x{result.tempo:.2f}")
    if result.trimmed:
        notes.append("trimmed")
    if result.lengthened:
        notes.append("lengthened")
    if result.truncated:
        notes.append("cut short by Voicebox")
    for w in result.warnings:
        rep.line("voice", 1.0, "WARNING " + w, clip.index, lang)
    rep.done("voice", clip.index, lang, ", ".join(notes))
    return script


def _engine_label(settings: Settings, lang: str) -> str:
    return "chatterbox" if lang == "hi" else settings.voice_for(lang).engine


VOICEOVER_OFF = "voice-over off"


def _skip_voice(store: JobStore, rep: _Reporter, clip: ClipState, lang: str) -> None:
    """Voice-over off: the voice stage is skipped (no Voicebox call). voice.wav/voice.json of an earlier
    voice-over render are kept, so turning the voice-over back on reuses a voice that still matches."""
    store.set_stage("voice", "skipped", 1.0, VOICEOVER_OFF, clip=clip.index, lang=lang)
    rep.line("voice", 1.0, f"skipped ({VOICEOVER_OFF})", clip.index, lang)


def _translate_fn(deps: _LazyDeps):
    """translate(lang=, source_lang=, segments=) resolving the LLM only when a translation is needed."""

    def translate(**kwargs):
        fn = getattr(deps.llm, "translate_segments", None)
        if not callable(fn):
            from .stages.captions import CaptionsError

            raise CaptionsError("the language model cannot translate captions (no translate_segments)")
        return fn(**kwargs)

    return translate


def _load_transcript(store: JobStore) -> Transcript:
    p = store.dir / "transcript.json"
    try:
        return Transcript.model_validate_json(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise PipelineError(f"{store.rel(p)} not found - resume the job first") from e
    except (ValueError, UnicodeDecodeError) as e:
        raise PipelineError(f"{store.rel(p)} is unreadable: {_error_text(e).splitlines()[0]}") from e


def _captions_current(out_dir: Path, settings: Settings) -> bool:
    from .stages.captions import captions_voiceover

    return (output_exists(out_dir / "captions.ass") and output_exists(out_dir / "words.json")
            and captions_voiceover(out_dir) == settings.voiceover)


def _run_captions(store: JobStore, rep: _Reporter, settings: Settings, deps: _LazyDeps, clip: ClipState,
                  lang: str, script: ClipScript, transcript: Transcript | None = None) -> Path:
    from .stages.captions import make_captions

    out_dir = store.clip_dir(clip.index, lang)
    _unlink_outputs(out_dir, "final.mp4", "render.json")
    _invalidate(store, clip, lang, ("render",))
    if not settings.voiceover:
        from . import ffmpeg
        from .stages.original_captions import make_original_captions, same_language

        if transcript is None:
            transcript = _load_transcript(store)
        how = ("from the original speech" if same_language(transcript.language, lang)
               else f"translated from {transcript.language}")
        rep.start("captions", clip.index, lang, how)
        ass = make_original_captions(transcript=transcript, moment=clip.moment,
                                     clip_duration=ffmpeg.duration(store.clip_dir(clip.index) / "cut.mp4"),
                                     lang=lang, settings=settings, out_dir=out_dir, translate=_translate_fn(deps))
        rep.done("captions", clip.index, lang, f"{ass.name} ({how})")
        return ass
    rep.start("captions", clip.index, lang)
    ass = make_captions(narration=script.narration, voice_wav=out_dir / "voice.wav", lang=lang,
                        transcriber=deps.transcriber, settings=settings, out_dir=out_dir)
    rep.done("captions", clip.index, lang, ass.name)
    return ass


def _run_render(store: JobStore, rep: _Reporter, settings: Settings, clip: ClipState, lang: str,
                script: ClipScript, hf_env_cache: dict | None = None) -> Path:
    from .stages.render import render_clip, write_metadata, write_render_stamp

    out_dir = store.clip_dir(clip.index, lang)
    final = out_dir / "final.mp4"
    state = clip.langs.get(lang)
    if state is not None:
        state.warning = None
    # Drop the old final together with its stamp: a failed or cancelled render must not leave a stamp-less
    # final behind, which read_render_stamp would take for a current legacy ASS render.
    _unlink_outputs(out_dir, "final.mp4", "render.json")
    _invalidate(store, clip, lang, ("render",))
    engine = settings.captions.engine
    notes = ["captions: HyperFrames"] if engine == "hyperframes" else []
    if not settings.voiceover:
        notes.append("original audio, no voice-over")
    elif settings.separation.background == "no_vocals":
        notes.append("background: removing vocals (Demucs)")
    rep.start("render", clip.index, lang, "; ".join(notes))
    outcome = render_clip(cut=store.clip_dir(clip.index) / "cut.mp4", out_dir=out_dir, settings=settings,
                          on_progress=rep.progress("render", clip.index, lang), hf_env_cache=hf_env_cache,
                          cancel_check=rep.cancel_point if rep.has_cancel_point else None)
    write_render_stamp(out_dir, settings, outcome)
    write_metadata(script, lang, out_dir / "metadata.json")
    message = store.rel(final)
    warnings = []
    if outcome.warning:
        warnings.append(outcome.warning)
        message = f"{final.name} (ASS fallback: {outcome.reason or outcome.warning})"
    elif outcome.engine_used == "hyperframes":
        message = f"{store.rel(final)} (HyperFrames {settings.hyperframes.style} captions)"
    if outcome.audio_warning:
        warnings.append(outcome.audio_warning)
    if settings.voiceover:
        warnings += _voice_warnings(out_dir)
    for w in warnings:
        rep.line("render", 1.0, "WARNING " + w, clip.index, lang)
    if state is not None and warnings:
        state.warning = "; ".join(warnings)
    audio = outcome.audio or {}
    if audio.get("background") == "no_vocals":
        secs = audio.get("separation_seconds")
        how = "cached stem" if audio.get("separation_cached") else (
            f"separated in {secs:.0f}s" if isinstance(secs, (int, float)) else "separated")
        rep.line("render", 1.0, f"background: vocals removed (Demucs, {how})", clip.index, lang)
    if audio.get("background_note"):
        rep.line("render", 1.0, f"background {audio['background_note']}", clip.index, lang)
    if audio.get("trimmed_to"):
        message += (f" - trimmed to {audio['trimmed_to']:.1f}s (narration ends at "
                     f"{audio.get('voice_end') or 0:.1f}s of {audio.get('clip_duration') or 0:.1f}s)")
    if not settings.voiceover:
        message += " - voice-over off"
    rep.done("render", clip.index, lang, message)
    return final


def _voice_warnings(out_dir: Path) -> list[str]:
    from .stages.voice import load_voice_result

    result = load_voice_result(out_dir)
    return list(result.warnings) if result is not None else []


def _add_narration(store: JobStore, rep: _Reporter, settings: Settings, deps: _LazyDeps,
                   transcript: Transcript, clip: ClipState, lang: str, script: ClipScript,
                   script_path: Path) -> ClipScript:
    """Voice-over is on but script.json has no narration (it was written with the voice-over off): write
    one, keeping the title, description and hashtags."""
    from .stages.script import add_narration

    rep.start("script", clip.index, lang, "writing the narration (voice-over turned on)")
    script = add_narration(llm=deps.llm, transcript=transcript, moment=clip.moment, lang=lang, script=script,
                           out_json=script_path, script_settings=settings.script,
                           source=job_source_info(store, settings))
    rep.done("script", clip.index, lang, "narration written; title kept")
    return script


def _process_clip_lang(store: JobStore, rep: _Reporter, settings: Settings, deps: _LazyDeps,
                       transcript: Transcript, clip: ClipState, lang: str,
                       hf_env_cache: dict | None = None) -> None:
    from .stages.render import render_is_current, write_metadata
    from .stages.script import load_script, write_metadata_script, write_script

    state = clip.langs[lang]
    state.error = None
    out_dir = store.clip_dir(clip.index, lang)
    script_path = out_dir / "script.json"
    vo = settings.voiceover
    stage = "script"
    try:
        # script
        regenerated = False
        if output_exists(script_path):
            script = load_script(script_path)
            if vo and not script.narration.strip():
                script = _add_narration(store, rep, settings, deps, transcript, clip, lang, script, script_path)
                regenerated = True
            else:
                rep.skip("script", clip.index, lang, "script.json")
        elif vo:
            rep.start("script", clip.index, lang, f"{clip.moment.duration:.1f}s clip")
            script = write_script(llm=deps.llm, transcript=transcript, moment=clip.moment, lang=lang,
                                  out_json=script_path, script_settings=settings.script,
                                  source=job_source_info(store, settings), credit=settings.context.credit_source)
            regenerated = True
            rep.done("script", clip.index, lang, script.title)
        else:
            rep.start("script", clip.index, lang, "title, description, hashtags (voice-over off)")
            script = write_metadata_script(llm=deps.llm, transcript=transcript, moment=clip.moment, lang=lang,
                                           out_json=script_path, source=job_source_info(store, settings),
                                           credit=settings.context.credit_source)
            regenerated = True
            rep.done("script", clip.index, lang, script.title)

        # voice (skipped only when voice.json matches the current narration, or the voice-over is off)
        stage = "voice"
        if not vo:
            _skip_voice(store, rep, clip, lang)
        elif not regenerated and _voice_is_current(out_dir, script, settings, lang):
            rep.skip("voice", clip.index, lang, "voice.wav matches script")
        else:
            # Lengthen only a narration the LLM wrote, never one the user edited before a resume:
            # the script was generated in this run, or it has never been voiced yet.
            allow_lengthen = regenerated or not (out_dir / "voice.json").exists()
            script = _run_voice(store, rep, settings, deps, clip, lang, script, script_path,
                                allow_lengthen=allow_lengthen)
            regenerated = True

        # captions (redone when they were made for the other voice-over mode)
        stage = "captions"
        if not regenerated and _captions_current(out_dir, settings):
            rep.skip("captions", clip.index, lang, "captions.ass")
        else:
            _run_captions(store, rep, settings, deps, clip, lang, script, transcript)
            regenerated = True

        # render (+ metadata, always refreshed because it is cheap and follows script.json edits)
        stage = "render"
        final = out_dir / "final.mp4"
        if not regenerated and render_is_current(out_dir, settings):
            write_metadata(script, lang, out_dir / "metadata.json")
            rep.skip("render", clip.index, lang, "final.mp4")
        else:
            _run_render(store, rep, settings, clip, lang, script, hf_env_cache)
        state.final = store.rel(final)
        store.save()
    except Exception as e:  # recorded per clip-language; other clips keep going
        state.error = f"{stage}: {_error_text(e)}"
        rep.fail(stage, e, clip.index, lang)
        store.save()
    except BaseException:  # Ctrl-C etc.: record it, then let it propagate
        state.error = f"{stage}: {INTERRUPTED}"
        rep.interrupted(stage, clip.index, lang)
        store.save()
        raise


# ---------------------------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------------------------
def run_job(source: str, *, settings: Settings | None = None, deps: Deps | None = None,
            langs: list[str] | None = None, count: int | None = None, min_seconds: float | None = None,
            max_seconds: float | None = None, job_id: str | None = None, log: LogFn = print,
            debug: bool = False, voiceover: bool | None = None, notes: str | None = None) -> JobStore:
    """Run (or resume, with job_id) a whole job. Returns the JobStore with the final job state.

    ``voiceover`` (run --no-voiceover / --voiceover, web options.voiceover) is stored in job.json as the
    job's mode. None keeps the job's stored mode; a new job stores the `voiceover` setting, so later
    Settings changes do not switch it (a job without a stored mode predates the option: on).

    ``notes`` (run --notes, web "Context / notes") replaces the user's notes in source_info.json; None keeps
    them. They are given to the LLM with the video's page info (title, creator, tags).

    Raises PipelineError when a job-level stage fails. Per clip-language failures are recorded in
    job.json (ClipLangState.error) and do not raise.
    """
    base = settings if settings is not None else load_settings()
    s = effective_settings(base, langs=langs, count=count, min_seconds=min_seconds, max_seconds=max_seconds,
                           voiceover=voiceover)

    if job_id:
        try:
            store = JobStore.load(job_id)
        except FileNotFoundError as e:
            raise PipelineError(f"job {job_id!r} not found (see `autoclipper jobs`)") from e
    else:
        if not source:
            raise PipelineError("no source given: pass a video file path or a URL")
        store = JobStore.create(source, voiceover=s.voiceover)  # fixed: later Settings changes don't switch it
    rep = _Reporter(store, log, debug)
    job = store.job
    if voiceover is not None:
        job.voiceover = bool(voiceover)
    s = settings_for_job(job, s)
    lazy = _LazyDeps(s, deps)
    if notes is not None:
        from .stages.source_info import SOURCE_INFO_FILE, set_notes

        set_notes(store.dir, notes, source=job.source)
        job.source_info = SOURCE_INFO_FILE
    job.status = "running"
    job.error = None
    store.save()
    log(f"[{job.id}] job dir {store.dir}")
    if not s.voiceover:
        log(f"[{job.id}] voice-over off: captions from the original speech, original audio kept")

    stage = "ingest"
    try:
        src = _job_stage_ingest(store, rep, s)
        stage = "transcribe"
        transcript = _job_stage_transcribe(store, rep, s, lazy)
        stage = "signals"
        signals = _job_stage_signals(store, rep, s, src)
        stage = "moments"
        moments, fresh = _job_stage_moments(store, rep, s, lazy, transcript, signals)
        if not fresh:
            _warn_ignored_overrides(store, log, moments, count, min_seconds, max_seconds)
        _populate_clips(store, moments, s.languages, fresh=fresh, log=log)
        stage = "cut"
        cut_ok = _job_stage_cut(store, rep, s, src, s.languages)
    except Exception as e:
        rep.fail(stage, e)
        job.status = "failed"
        job.error = f"{stage}: {_error_text(e)}"
        store.save()
        if isinstance(e, PipelineError):
            raise
        raise PipelineError(job.error) from e
    except BaseException:
        if job.stages.get(stage) is not None:
            rep.interrupted(stage)
        _mark_job_interrupted(store, log)
        raise

    hf_env_cache: dict = {}  # probe_env runs once per run_job, not once per clip
    try:
        for idx in cut_ok:
            clip = job.clips[idx]
            for lang in s.languages:
                _process_clip_lang(store, rep, s, lazy, transcript, clip, lang, hf_env_cache)
    except BaseException:
        _mark_job_interrupted(store, log)
        raise

    states = [c.langs[lang] for c in job.clips for lang in s.languages if lang in c.langs]
    failed = [st for st in states if st.error]
    if states and len(failed) == len(states):
        job.status = "failed"
        job.error = "every clip failed; see `autoclipper status " + job.id + "`"
    else:
        job.status = "done"
        job.error = None
    store.save()
    log(f"[{job.id}] {job.status}: {len(states) - len(failed)}/{len(states)} clip-languages rendered"
        + (f", {len(failed)} failed" if failed else ""))
    return store


def rerender(job_id: str, clip: int, lang: str, *, settings: Settings | None = None,
             deps: Deps | None = None, log: LogFn = print, debug: bool = False) -> Path:
    """Re-render one clip in one language from its (possibly user-edited) script.json.

    The voice is regenerated only when the narration changed (sha256 vs voice.json narration_hash)
    or the voice is missing. Captions, final.mp4 and metadata.json are always rebuilt.
    """
    from .stages.script import load_script

    s = settings if settings is not None else load_settings()
    try:
        store = JobStore.load(job_id)
    except FileNotFoundError as e:
        raise PipelineError(f"job {job_id!r} not found (see `autoclipper jobs`)") from e
    job = store.job
    lang = (parse_langs(lang) or [lang])[0]
    if not job.clips:
        raise PipelineError(f"job {job_id} has no clips yet - run it first (`autoclipper run --job {job_id} ...`)")
    if not 0 <= clip < len(job.clips):
        raise PipelineError(f"clip {clip} does not exist in job {job_id} (valid: 0..{len(job.clips) - 1})")
    cs = job.clips[clip]
    # Validate before touching job.json: a failed check must not leave a phantom ClipLangState behind.
    if not output_exists(store.dir / "clips" / f"{clip:02d}" / "cut.mp4"):
        raise PipelineError(f"clip {clip:02d} has no cut.mp4 - resume the job first")
    script_path = store.dir / "clips" / f"{clip:02d}" / lang / "script.json"
    if not output_exists(script_path):
        raise PipelineError(f"{store.rel(script_path)} not found - run the job for language {lang!r} first")
    state = cs.langs.setdefault(lang, ClipLangState())
    for st in CLIP_STAGES:
        state.stages.setdefault(st, StageState())
    store.save()
    out_dir = store.clip_dir(clip, lang)

    rep = _Reporter(store, log, debug)
    s = settings_for_job(job, s)
    lazy = _LazyDeps(s, deps)
    state.error = None
    stage = "script"
    try:
        script = load_script(script_path)
        transcript: Transcript | None = None
        if s.voiceover and not script.narration.strip():
            transcript = _load_transcript(store)
            script = _add_narration(store, rep, s, lazy, transcript, cs, lang, script, script_path)
        else:
            rep.done("script", clip, lang, "loaded script.json")
        stage = "voice"
        if not s.voiceover:
            _skip_voice(store, rep, cs, lang)
        elif _voice_is_current(out_dir, script, s, lang):
            rep.skip("voice", clip, lang, "narration unchanged")
        else:
            script = _run_voice(store, rep, s, lazy, cs, lang, script, script_path, allow_lengthen=False)
        stage = "captions"
        _run_captions(store, rep, s, lazy, cs, lang, script, transcript)
        stage = "render"
        final = _run_render(store, rep, s, cs, lang, script, {})
    except Exception as e:
        state.error = f"{stage}: {_error_text(e)}"
        rep.fail(stage, e, clip, lang)
        store.save()
        raise PipelineError(f"rerender {clip:02d}/{lang} failed at {stage}: {_error_text(e)}") from e
    except BaseException:
        state.error = f"{stage}: {INTERRUPTED}"
        rep.interrupted(stage, clip, lang)
        _mark_job_interrupted(store, log)
        raise

    state.final = store.rel(final)
    if job.status == "failed" and _job_complete(store):
        job.status = "done"
        job.error = None
    store.save()
    return final


def _job_complete(store: JobStore) -> bool:
    """True only when every job stage is done/skipped and every clip has a finished render, with no
    error, in every language of the job (the languages any clip has state for)."""
    job = store.job
    finished = ("done", "skipped")
    for s in JOB_STAGES:
        st = job.stages.get(s)
        if st is None and s in OPTIONAL_JOB_STAGES:
            continue  # a job made before this stage existed
        if st is None or st.status not in finished:
            return False
    langs = {lang for c in job.clips for lang in c.langs}
    if not job.clips or not langs:
        return False
    for c in job.clips:
        for lang in langs:
            st = c.langs.get(lang)
            if st is None or st.error:
                return False
            render = st.stages.get("render")
            if render is None or render.status not in finished:
                return False
    return True
