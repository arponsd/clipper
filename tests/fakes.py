"""Shared test fakes (owned by the team lead). Never touch the real Anthropic API or Voicebox.

Import in tests with:  from fakes import FakeLLM, FakeVoicebox, FakeTranscriber, make_tone, make_video
(pytest puts tests/ on sys.path because tests/ has no __init__.py - rootdir-relative import).
These helpers call the ffmpeg binary directly so they do not depend on autoclipper.ffmpeg.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from autoclipper.models import ClipScript, Moment, Segment, Transcript, Word


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_video(dst: Path, seconds: float = 40.0, width: int = 1280, height: int = 720,
               audio: bool = True) -> Path:
    """Synthetic test video: testsrc2 + 440Hz sine, h264/aac mp4."""
    dst = Path(dst)
    args = ["-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=25:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    _ffmpeg(*args, str(dst))
    return dst


def make_tone(dst: Path, seconds: float = 3.0, freq: int = 660, sample_rate: int = 24000) -> Path:
    """Mono PCM wav sine tone (stands in for Voicebox output)."""
    dst = Path(dst)
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate={sample_rate}:duration={seconds}",
            "-ac", "1", "-c:a", "pcm_s16le", str(dst))
    return dst


def fake_transcript(duration: float = 40.0, seg_len: float = 5.0, language: str = "en") -> Transcript:
    segs: list[Segment] = []
    t, i = 0.0, 0
    while t < duration - 1e-6:
        end = min(t + seg_len, duration)
        texts = [f"word{i}a", f"word{i}b", f"word{i}c", f"word{i}d"]
        step = (end - t) / len(texts)
        words = [Word(start=t + k * step, end=t + (k + 1) * step, text=w) for k, w in enumerate(texts)]
        segs.append(Segment(start=t, end=end, text=" ".join(texts), words=words))
        t, i = end, i + 1
    return Transcript(language=language, duration=duration, segments=segs)


class FakeLLM:
    """Deterministic LLM. Records calls in self.calls as (method, kwargs)."""

    def __init__(self, moments: list[Moment] | None = None, narration_words: int | None = None):
        self.moments = moments
        self.narration_words = narration_words
        self.calls: list[tuple[str, dict]] = []

    def pick_moments(self, transcript_text, *, count, min_seconds, max_seconds, video_duration):
        self.calls.append(("pick_moments", dict(count=count, min_seconds=min_seconds,
                                                max_seconds=max_seconds, video_duration=video_duration)))
        if self.moments is not None:
            return list(self.moments)
        span = max(min_seconds, min(max_seconds, 12.0))
        out, start = [], 0.0
        for k in range(count):
            if start + span > video_duration:
                break
            out.append(Moment(start=start, end=start + span, hook=f"hook {k}", reason="test", score=1.0 - k * 0.1))
            start += span + 1.0
        return out

    def write_script(self, *, lang, clip_transcript, moment, duration, target_words, target_seconds=None):
        call = dict(lang=lang, duration=duration, target_words=target_words)
        if target_seconds is not None:  # recorded only when given, so older call assertions still match
            call["target_seconds"] = target_seconds
        self.calls.append(("write_script", call))
        n = self.narration_words or max(3, target_words)
        base = "यह एक परीक्षण वाक्य है" if lang == "hi" else "this is a test sentence"
        words = (base.split() * (n // len(base.split()) + 1))[:n]
        return ClipScript(narration=" ".join(words), title=f"Test title {lang}",
                          description=f"Test description {lang}", hashtags=["#test", "#clip", f"#{lang}"])

    def shorten(self, *, lang, narration, max_words):
        self.calls.append(("shorten", dict(lang=lang, max_words=max_words)))
        return " ".join(narration.split()[:max_words])

    def lengthen(self, *, lang, narration, target_words):
        """Step 2: repeat the narration's words until it has target_words words."""
        self.calls.append(("lengthen", dict(lang=lang, target_words=target_words)))
        words = narration.split() or ["word"]
        return " ".join((words * (target_words // len(words) + 1))[:max(target_words, len(words))])


    def write_publish_metadata(self, *, lang, clip_transcript, moment, duration):
        """Voice-over off: post metadata only (narration empty)."""
        self.calls.append(("write_publish_metadata", dict(lang=lang, duration=duration)))
        return ClipScript(narration="", title=f"Captions title {lang}",
                          description=f"Captions description {lang}", hashtags=["#captions", f"#{lang}"])

    def translate_segments(self, *, lang, source_lang, segments):
        """Voice-over off: 'translates' each line to a fixed Devanagari/Latin phrase keeping its id."""
        self.calls.append(("translate_segments", dict(lang=lang, source_lang=source_lang,
                                                      ids=[s["id"] for s in segments])))
        word = "अनुवाद" if lang == "hi" else "translated"
        return {s["id"]: f"{word} पंक्ति {s['id']}" if lang == "hi" else f"{word} line {s['id']}" for s in segments}


class FakeVoicebox:
    """Writes a sine wav whose length is words * seconds_per_word."""

    def __init__(self, seconds_per_word: float = 0.3, fail: bool = False):
        self.seconds_per_word = seconds_per_word
        self.fail = fail
        self.calls: list[dict] = []

    def health(self) -> None:
        return None

    def list_profiles(self) -> list[dict]:
        return [{"id": "fake-en", "name": "Fake EN"}, {"id": "fake-hi", "name": "Fake HI"}]

    def synthesize(self, text, *, profile_id, language, engine, dst, on_progress=None):
        self.calls.append(dict(text=text, profile_id=profile_id, language=language, engine=engine, dst=str(dst)))
        if self.fail:
            raise RuntimeError("fake voicebox failure")
        seconds = max(0.5, len(text.split()) * self.seconds_per_word)
        return make_tone(Path(dst), seconds=seconds)


class FakeTranscriber:
    """Returns fake_transcript for source audio; for voice audio returns no words (forces fallback
    distribution) unless words_for_voice is set."""

    def __init__(self, duration: float = 40.0, words_for_voice: list[Word] | None = None):
        self.duration = duration
        self.words_for_voice = words_for_voice
        self.calls: list[tuple[str, str | None]] = []

    def transcribe(self, audio, language=None, on_progress=None):
        self.calls.append((str(audio), language))
        if Path(audio).name == "audio.wav":
            return fake_transcript(self.duration)
        words = self.words_for_voice or []
        segs = [Segment(start=words[0].start, end=words[-1].end,
                        text=" ".join(w.text for w in words), words=words)] if words else []
        return Transcript(language=language or "en", duration=words[-1].end if words else 0.0, segments=segs)


# --------------------------------------------------------------------------------------------- step 3
def make_alpha_overlay(dst: Path, seconds: float, width: int = 1080, height: int = 1920, fmt: str = "webm",
                       alpha: bool = True) -> Path:
    """Transparent overlay with an opaque yellow box in the middle (stands in for a HyperFrames render).
    webm: VP9 + alpha (tag alpha_mode=1); mov: ProRes 4444. alpha=False writes an opaque yuv420p webm."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    bw, bh = max(2, width // 3), max(2, height // 8)
    box = f"drawbox=x={(width - bw) // 2}:y={(height - bh) // 2}:w={bw}:h={bh}:t=fill"
    base = f"color=c=black:s={width}x{height}:r=25:d={seconds}"
    # drawbox does not write the alpha plane, so the alpha comes from a separate mask (alphamerge)
    graph = (f"{base},{box}:color=yellow,format=yuv420p[c];{base},{box}:color=white,format=gray[m];"
             f"[c][m]alphamerge,format=yuva420p[v]")
    if fmt == "webm" and alpha:
        codec = ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", "-deadline", "realtime",
                 "-cpu-used", "8", "-b:v", "200k"]
    elif fmt == "webm":
        codec = ["-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p", "-deadline", "realtime", "-cpu-used", "8",
                 "-b:v", "200k"]
    else:
        codec = ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le"]
    if alpha or fmt == "webm":
        _ffmpeg("-filter_complex", graph, "-map", "[v]", *codec, "-t", str(seconds), str(dst))
    else:
        _ffmpeg("-f", "lavfi", "-i", f"{base},{box}:color=yellow", *codec, "-t", str(seconds), str(dst))
    return dst


class FakeHFRunner:
    """Stands in for hyperframes._popen_runner: records (args, cwd, env, timeout), feeds ``lines`` to
    on_line, and (write=True) writes an alpha overlay at the `-o` path sized from cwd/index.html.

    ``exc`` is raised after the lines (e.g. HyperFramesTimeout); ``rc`` is returned otherwise."""

    def __init__(self, rc: int = 0, lines: list[str] | None = None, write: bool = True, delay: float = 0.0,
                 exc: BaseException | None = None, alpha: bool = True, seconds: float | None = None):
        self.rc = rc
        self.lines = lines if lines is not None else [
            '@hf-progress {"code":"compile","done":0,"total":1,"pct":5}',
            "some log line",
            '@hf-progress {"code":"capture","done":10,"total":20,"pct":40}',
            '@hf-progress {"code":"capture","done":20,"total":20,"pct":80}',
            '@hf-progress {"code":"encode","done":20,"total":20,"pct":95}',
        ]
        self.write = write
        self.delay = delay
        self.exc = exc
        self.alpha = alpha
        self.seconds = seconds
        self.calls: list[dict] = []

    def __call__(self, args, cwd, env, timeout, on_line):
        import re
        import time

        self.calls.append(dict(args=list(args), cwd=Path(cwd), env=dict(env), timeout=timeout,
                               tmp_existed=Path(env.get("TMPDIR", "/nonexistent")).is_dir()))
        for line in self.lines:
            if self.delay:
                time.sleep(self.delay)
            on_line(line)
        if self.exc is not None:
            raise self.exc
        if self.write and self.rc == 0:
            out = Path(args[args.index("-o") + 1])
            fmt = args[args.index("--format") + 1]
            html = (Path(cwd) / "index.html").read_text(encoding="utf-8")
            dur = float(re.search(r'id="root"[^>]*data-duration="([0-9.]+)"', html).group(1))
            w = int(re.search(r'id="root"[^>]*data-width="(\d+)"', html).group(1))
            h = int(re.search(r'id="root"[^>]*data-height="(\d+)"', html).group(1))
            make_alpha_overlay(out, self.seconds if self.seconds is not None else dur, w, h, fmt, alpha=self.alpha)
        return self.rc
