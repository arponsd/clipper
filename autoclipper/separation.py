"""Vocal separation with Demucs (optional): the clip's audio without its speech, as a voice-over background.

Demucs (https://github.com/adefossez/demucs) runs in its own Python venv (settings ``separation.python``)
as a subprocess, so torch is never imported into Auto Clipper. One clip is separated once: the
``no_vocals`` stem is cached in the clip folder (``clips/NN/bed_no_vocals.wav`` + ``bed_no_vocals.json``),
keyed by the sha256 of cut.mp4 and the model, and reused by every language and re-render.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from . import ffmpeg
from .config import SeparationSettings, expand_path
from .interfaces import ProgressFn
from .stages._atomic import write_text_atomic

log = logging.getLogger(__name__)

BED_WAV = "bed_no_vocals.wav"
BED_JSON = "bed_no_vocals.json"
_SAMPLE_RATE = 44100
_ERR_TAIL = 400

# Transformer models (htdemucs, htdemucs_ft, htdemucs_6s) refuse segments above 7.8 s.
TRANSFORMER_MAX_SEGMENT = 7
_POLL_SECONDS = 0.5
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# runner(cmd, env=..., timeout=..., cwd=..., poll=...) -> CompletedProcess; tests pass a fake (no real Demucs).
Runner = Callable[..., subprocess.CompletedProcess]


class SeparationError(RuntimeError):
    """Demucs is missing, failed or timed out (the caller falls back to the original audio)."""


def is_transformer_model(model: str) -> bool:
    return model.lower().startswith("htdemucs")


def _default_runner(cmd: list[str], *, env: dict, timeout: float, cwd: str,
                    poll: Callable[[], None] | None = None) -> subprocess.CompletedProcess:
    return run_cancellable(cmd, env=env, timeout=timeout, cwd=cwd, poll=poll)


def run_cancellable(cmd: list[str], *, env: dict | None, timeout: float, cwd: str,
                    poll: Callable[[], None] | None = None) -> subprocess.CompletedProcess:
    """Run Demucs in its own process group. ``poll()`` is called every 0.5 s (the worker's cancel point:
    it raises to cancel). On a timeout, or anything ``poll`` raises (cancel, server stop, Ctrl-C), the whole
    process tree is killed first, so no 1.4 GB Demucs is left running. stdout/stderr go to temp files
    (Demucs' progress bar could fill a pipe)."""
    from .stages.hyperframes import _kill_tree

    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, cwd=cwd, **kw)
        deadline = time.monotonic() + float(timeout)
        try:
            while True:
                try:
                    proc.wait(timeout=_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                if poll is not None:
                    poll()
        except BaseException:
            _kill_tree(proc)
            raise

        def text(f) -> str:
            f.seek(0)
            return f.read().decode("utf-8", errors="replace")

        return subprocess.CompletedProcess(cmd, proc.returncode, text(out), text(err))


def python_path(s: SeparationSettings) -> Path:
    return expand_path(s.python)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_key(cut: Path, s: SeparationSettings) -> dict:
    return {"cut_sha256": file_sha256(Path(cut)), "model": s.model}


def cached_bed(clip_dir: Path, key: dict) -> Path | None:
    """The cached no_vocals stem when its key matches, else None."""
    wav, meta = Path(clip_dir) / BED_WAV, Path(clip_dir) / BED_JSON
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or any(data.get(k) != v for k, v in key.items()):
        return None
    if not wav.is_file() or wav.stat().st_size == 0:
        return None
    return wav


def _env(s: SeparationSettings) -> dict:
    """Environment of the Demucs child: no secrets (ANTHROPIC_* from .env never reach torch/Demucs)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("ANTHROPIC_")}
    if s.models_dir.strip():
        env["TORCH_HOME"] = str(expand_path(s.models_dir))
    if s.threads > 0:
        for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS"):
            env[k] = str(s.threads)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def effective_segment(s: SeparationSettings) -> int:
    """The segment passed to Demucs (transformer models are capped at 7; settings validation rejects more)."""
    return min(s.segment, TRANSFORMER_MAX_SEGMENT) if is_transformer_model(s.model) else s.segment


def demucs_command(py: Path, wav: Path, out: Path, s: SeparationSettings) -> list[str]:
    return [str(py), "-m", "demucs.separate", "-n", s.model, "--two-stems", "vocals", "-d", "cpu",
            "--segment", str(effective_segment(s)), "-j", "1", "-o", str(out), "--filename", "{stem}.{ext}",
            str(wav)]


def _tail(text: str) -> str:
    """The last lines of Demucs' output, without terminal colour codes or progress-bar carriage returns."""
    text = _ANSI_RE.sub("", text or "").replace("\r", "\n")
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    msg = " | ".join(lines[-3:])
    return msg if len(msg) <= _ERR_TAIL else "..." + msg[-_ERR_TAIL:]


def _remove_stale_temp(clip_dir: Path) -> None:
    """demucs-* folders left by a killed process (the job lock is held, so none is in use)."""
    for d in clip_dir.glob("demucs-*"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


def no_vocals_bed(cut: Path, clip_dir: Path, s: SeparationSettings, *, runner: Runner | None = None,
                  on_progress: ProgressFn | None = None, poll: Callable[[], None] | None = None,
                  info: dict | None = None) -> Path:
    """The clip's audio with the vocals removed (cached). Raises SeparationError on any failure.

    ``poll`` is the caller's cancel point (called while Demucs runs; whatever it raises propagates after
    the Demucs process tree is killed). ``info`` receives {"cached": bool, "seconds": separation time}."""
    cut, clip_dir = Path(cut), Path(clip_dir)
    progress = on_progress or (lambda _f, _m="": None)
    info = info if info is not None else {}
    if not cut.is_file():
        raise SeparationError(f"{cut.name} not found")
    key = cache_key(cut, s)
    hit = cached_bed(clip_dir, key)
    if hit is not None:
        info["cached"] = True
        try:
            info["seconds"] = json.loads((clip_dir / BED_JSON).read_text(encoding="utf-8")).get("seconds")
        except (OSError, ValueError, AttributeError):
            info["seconds"] = None
        return hit
    _remove_stale_temp(clip_dir)
    py = python_path(s)
    if not py.is_file():
        raise SeparationError(f"Demucs Python not found at {py} (setting separation.python; see README "
                              "\"Vocal separation\")")
    media = ffmpeg.probe(cut)
    if not media.has_audio:
        raise SeparationError(f"{cut.name} has no audio to separate")
    run = runner or _default_runner
    progress(0.0, f"separating vocals (Demucs {s.model})")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="demucs-", dir=clip_dir) as tmp_name:
        tmp = Path(tmp_name)
        wav = tmp / "in.wav"
        ffmpeg.to_wav(cut, wav, sample_rate=_SAMPLE_RATE, channels=2)
        out = tmp / "out"
        cmd = demucs_command(py, wav, out, s)
        try:
            kw = {"poll": poll} if poll is not None else {}
            proc = run(cmd, env=_env(s), timeout=s.timeout_seconds, cwd=str(tmp), **kw)
        except subprocess.TimeoutExpired:
            raise SeparationError(f"Demucs did not finish within {s.timeout_seconds:.0f}s "
                                  "(separation.timeout_seconds)") from None
        except OSError as e:
            raise SeparationError(f"Demucs could not start: {e}") from None
        if proc.returncode != 0:
            raise SeparationError(f"Demucs failed (exit {proc.returncode}): {_tail(proc.stderr)}")
        stem = out / s.model / "no_vocals.wav"
        if not stem.is_file() or stem.stat().st_size == 0:
            raise SeparationError(f"Demucs wrote no no_vocals stem ({_tail(proc.stderr) or 'no output'})")
        try:
            got = ffmpeg.duration(stem)
        except Exception as e:  # noqa: BLE001 - an unreadable stem is a separation failure
            raise SeparationError(f"Demucs wrote an unreadable stem: {e}") from None
        if got < 0.2:
            raise SeparationError(f"Demucs wrote an empty stem ({got:.1f}s)")
        dst = clip_dir / BED_WAV
        part = clip_dir / (BED_WAV + ".part")
        shutil.move(str(stem), str(part))
        os.replace(part, dst)
    elapsed = time.monotonic() - started
    info.update(cached=False, seconds=round(elapsed, 1))
    write_text_atomic(clip_dir / BED_JSON, json.dumps({**key, "segment": effective_segment(s),
                                                       "seconds": round(elapsed, 1),
                                                       "duration": round(got, 3)}, indent=2))
    log.info("separated %s with Demucs %s in %.1fs", cut, s.model, elapsed)
    progress(1.0, f"vocals separated in {elapsed:.0f}s")
    return dst


def check_demucs(s: SeparationSettings, *, runner: Runner | None = None, timeout: float = 120) -> tuple[bool, str]:
    """(ok, detail) for the doctor: the configured Python exists and imports demucs + torch."""
    py = python_path(s)
    if not py.is_file():
        return False, f"Python not found at {py} (separation.python)"
    run = runner or _default_runner
    code = "import demucs, torch; print(demucs.__version__, torch.__version__)"
    try:
        proc = run([str(py), "-c", code], env=_env(s), timeout=timeout, cwd=str(py.parent))
    except subprocess.TimeoutExpired:
        return False, f"{py} -c 'import demucs' did not finish within {timeout:.0f}s"
    except OSError as e:
        return False, f"cannot run {py}: {e}"
    if proc.returncode != 0:
        return False, f"demucs is not importable with {py}: {_tail(proc.stderr)}"
    parts = (proc.stdout or "").split()
    ver = f"demucs {parts[0]}, torch {parts[1]}" if len(parts) >= 2 else (proc.stdout or "").strip()
    return True, f"{ver} ({py}), model {s.model}"
