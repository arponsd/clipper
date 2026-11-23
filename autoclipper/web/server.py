"""FastAPI app for the local web UI (CONTRACT_WEB sections 1-4).

Request handlers only READ job.json (Job.model_validate_json). Every job.json write goes through the
worker thread; the one exception is Worker.submit_job creating a brand-new job.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import typing
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Callable, Literal
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, Path as PathParam, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.requests import ClientDisconnect
from starlette.responses import FileResponse, MalformedRangeHeader, RangeNotSatisfiable

from ..cli import run_doctor
from ..config import Settings, data_dir, load_settings, settings_path
from ..jobstore import JOB_STAGES, jobs_root
from ..models import ClipScript, ClipState, Job
from ..stages._atomic import write_text_atomic
from ..stages.script import MAX_HASHTAGS
from .auth import DEFAULT_ALLOWED_HOSTS, GuardMiddleware, is_loopback, load_or_create_token
from .errors import APIError, VALIDATION_FAILED, error_body, error_response, install_error_handlers, validation_errors
from ..joblock import has_lock_file
from .publish_routes import install_publish_routes  # step 4
from .worker import RECOVER_MIN_AGE, JobOptions, Task, Worker, WorkerConflict, read_job, read_options, uploads_root

log = logging.getLogger("autoclipper.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
TOKEN_PLACEHOLDER = "__AUTOCLIPPER_TOKEN__"
CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": CSP,
}
CHUNK = 1024 * 1024
IDEMPOTENCY_TTL = 24 * 3600
DEFAULT_MAX_UPLOAD_MB = 4096
DEFAULT_EXTENSIONS = [".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ogv"]
LANGS = ("en", "hi")

JOB_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
HEX32_RE = r"^[0-9a-f]{32}$"
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
ASS_COLOR_RE = re.compile(r"^&H[0-9A-Fa-f]{8}\Z")  # \Z: "$" would accept a trailing newline
MAX_SETTING_STR = 200
MAX_URL_LEN = 2048

JobId = Annotated[str, PathParam(pattern=JOB_ID_RE)]
ClipNo = Annotated[int, PathParam(ge=0, le=999)]
Lang = Annotated[Literal["en", "hi"], PathParam()]
TaskId = Annotated[str, PathParam(pattern=HEX32_RE)]

FALLBACK_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="autoclipper-token" content="__AUTOCLIPPER_TOKEN__"><title>Auto Clipper</title></head>
<body><p>Auto Clipper is running, but the UI files (autoclipper/web/static/index.html) are missing.</p></body></html>
"""


# ---------------------------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------------------------
class JobOptionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    clips: int | None = Field(None, ge=1, le=30)
    min_seconds: float | None = Field(None, ge=5, le=180, allow_inf_nan=False)
    max_seconds: float | None = Field(None, ge=10, le=180, allow_inf_nan=False)
    langs: list[Literal["en", "hi"]] | None = Field(None, min_length=1, max_length=2)
    # Voice-over for this job; None = the `voiceover` setting. False = captions only (original audio).
    voiceover: bool | None = None

    @field_validator("langs")
    @classmethod
    def _unique(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(set(v)) != len(v):
            raise ValueError("languages must be unique")
        return v


class NewJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str | None = Field(None, min_length=1, max_length=2048)
    upload_id: str | None = Field(None, pattern=HEX32_RE)
    options: JobOptionsIn | None = None


class KeywordIn(BaseModel):
    """G5: one key word of a clip (PUT .../script "keywords")."""
    model_config = ConfigDict(extra="forbid", strict=True)
    word: str = Field(max_length=200)
    emoji: str = Field("", max_length=64)

    @field_validator("word")
    @classmethod
    def _word(cls, v: str) -> str:
        from ..stages.caption_style import keyword_error

        err = keyword_error(v)
        if err:
            raise ValueError(err)
        return v

    @field_validator("emoji")
    @classmethod
    def _emoji(cls, v: str) -> str:
        from ..stages.caption_style import emoji_error

        err = emoji_error(v.strip())
        if err:
            raise ValueError(err)
        return v.strip()


class ClipScriptIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    narration: str = Field(max_length=20000)
    title: str = Field(max_length=2000)
    description: str = Field("", max_length=5000)
    hashtags: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(default_factory=list, max_length=MAX_HASHTAGS)
    # G4 hook line ("" = no hook) and G5 keywords; left out (null) = keep what script.json has.
    hook: str | None = Field(None, max_length=1000)
    keywords: list[KeywordIn] | None = Field(None, max_length=8)

    @field_validator("hook")
    @classmethod
    def _hook(cls, v: str | None) -> str | None:
        from ..stages.caption_style import hook_error

        if v is None:
            return v
        err = hook_error(v)
        if err:
            raise ValueError(err)
        return v

    @field_validator("narration")
    @classmethod
    def _narration(cls, v: str) -> str:
        # Emptiness is checked by the route: an empty narration is fine when the job's voice-over is off.
        n = len(v.strip())
        if n > 5000:
            raise ValueError("narration must be at most 5000 characters")
        return v

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        n = len(v.strip())
        if n == 0:
            raise ValueError("title must not be empty")
        if n > 200:
            raise ValueError("title must be at most 200 characters")
        return v

    @field_validator("hashtags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        for tag in v:
            if not tag.strip():
                raise ValueError("hashtags must not be empty")
        return v


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
class JobModeIn(BaseModel):
    """PUT /api/jobs/{id}/mode: the job's voice-over mode (true = voice-over + captions, false = original
    audio + captions)."""
    model_config = ConfigDict(extra="forbid", strict=True)
    voiceover: bool


def _web_settings(settings: Settings) -> tuple[int, list[str]]:
    web = getattr(settings, "web", None)
    mb = getattr(web, "max_upload_mb", DEFAULT_MAX_UPLOAD_MB) if web is not None else DEFAULT_MAX_UPLOAD_MB
    exts = getattr(web, "allowed_extensions", DEFAULT_EXTENSIONS) if web is not None else DEFAULT_EXTENSIONS
    return int(mb), [str(e).lower() for e in exts]


def _nonempty_file(p: Path) -> bool:
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _job_dir(job_id: str) -> Path:
    return jobs_root() / job_id


def _load_job(job_id: str) -> Job:
    try:
        job = read_job(job_id)
    except (OSError, ValueError):
        raise APIError(409, f"job {job_id} has an unreadable job.json")
    if job is None:
        raise APIError(404, f"job {job_id} not found")
    return job


def _clip_of(job: Job, clip: int) -> ClipState:
    for c in job.clips:
        if c.index == clip:
            return c
    raise APIError(404, f"clip {clip} not found in job {job.id}")


def _clip_lang(job: Job, clip: int, lang: str) -> ClipState:
    c = _clip_of(job, clip)
    if lang not in c.langs:
        raise APIError(404, f"language {lang} not found for clip {clip} in job {job.id}")
    return c


def _clip_dir(job_id: str, clip: int, lang: str | None = None) -> Path:
    p = _job_dir(job_id) / "clips" / f"{clip:02d}"
    return p / lang if lang else p


def _voice_stale(out_dir: Path, narration: str | None) -> bool:
    from ..stages.render import read_render_stamp, rendered_voiceover
    from ..stages.voice import narration_hash

    if narration is None:
        return False
    if _nonempty_file(out_dir / "final.mp4") and not rendered_voiceover(read_render_stamp(out_dir)):
        return False  # final.mp4 has no voice-over: the narration text is not in it
    voice = _read_json(out_dir / "voice.json")
    if not isinstance(voice, dict) or not voice.get("narration_hash"):
        return False
    return voice["narration_hash"] != narration_hash(narration)


def _saved_narration(out_dir: Path) -> str:
    data = _read_json(out_dir / "script.json")
    n = data.get("narration") if isinstance(data, dict) else None
    return n if isinstance(n, str) else ""


def _job_settings(job: Job, settings: Settings | None) -> Settings | None:
    from ..pipeline import settings_for_job

    return settings_for_job(job, settings) if settings is not None else None


def _job_voiceover(job: Job, settings: Settings | None) -> bool:
    """The job's voice-over mode, fixed in job.json at creation (pipeline.job_voiceover; legacy = on)."""
    from ..pipeline import job_voiceover

    return job_voiceover(job, settings)


def _render_info(out_dir: Path, final: bool, settings: Settings | None) -> tuple[str | None, str | None, bool]:
    """(caption_engine actually used, its style, render_stale) of a clip-lang final.mp4 (CONTRACT_STEP3 7)."""
    from ..stages.render import read_render_stamp, render_settings_changed

    if not final:
        return None, None, False
    stamp = read_render_stamp(out_dir)
    engine = stamp.get("engine_used") if stamp.get("engine_used") in ("ass", "hyperframes") else "ass"
    style = stamp.get("style") if engine == "hyperframes" and isinstance(stamp.get("style"), str) else None
    stale = settings is not None and render_settings_changed(out_dir, settings)
    return engine, style, bool(stale)


def _style_stale(out_dir: Path, final: bool, settings: Settings | None) -> bool:
    """G4/G5: the caption style, hook or keywords changed since final.mp4 was rendered."""
    from ..stages.render import read_render_stamp, style_changed

    if not final or settings is None:
        return False
    return bool(style_changed(read_render_stamp(out_dir), out_dir, settings))


def _rendered_voiceover(out_dir: Path) -> bool:
    from ..stages.render import read_render_stamp, rendered_voiceover

    return rendered_voiceover(read_render_stamp(out_dir))


_AUDIO_KEYS = ("mix", "background", "background_outside", "background_note", "separation_seconds",
               "separation_cached", "clip_duration", "duration", "voice_end", "trimmed_to", "trim_reason",
               "voice_lufs", "background_lufs_under_voice", "background_lufs", "voice_gain_db",
               "background_gain_db_under_voice", "background_gain_db_outside", "warning")


def _audio_stale(out_dir: Path, settings: Settings) -> bool:
    """A voice-over final made with an older mix or other audio settings (render.json audio_key)."""
    from ..stages.render import audio_settings_changed, read_render_stamp

    return audio_settings_changed(read_render_stamp(out_dir), settings)


def _render_audio(out_dir: Path) -> dict | None:
    """render.json "audio" (what the mix did: background, loudness, trim), known keys only."""
    from ..stages.render import read_render_stamp

    audio = read_render_stamp(out_dir).get("audio")
    if not isinstance(audio, dict):
        return None
    return {k: audio.get(k) for k in _AUDIO_KEYS}


def _read_script(path: Path) -> ClipScript | None:
    try:
        return ClipScript.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _stage_dict(stages: dict, names: list[str]) -> dict:
    out = {}
    for name in names:
        st = stages.get(name)
        out[name] = (st.model_dump() if st is not None
                     else {"status": "pending", "progress": 0.0, "message": ""})
    for name, st in stages.items():
        out.setdefault(name, st.model_dump())
    return out


class App:
    """Holds the per-app state the routes need (no module globals: tests build many apps)."""

    def __init__(self, worker: Worker, settings_loader: Callable[[], Settings], doctor: Callable[[Settings], Any],
                 voicebox_factory: Callable[[Settings], Any] | None, static_dir: Path, token: str):
        self.worker = worker
        self.settings_loader = settings_loader
        self.doctor = doctor
        self.voicebox_factory = voicebox_factory
        self.static_dir = static_dir
        self.token = token
        self.idem_lock = threading.Lock()

    # ---- shapes ---------------------------------------------------------------------------
    def summary(self, job: Job) -> dict:
        rendered = failed = 0
        langs: list[str] = []
        for c in job.clips:
            for lang, st in c.langs.items():
                if lang not in langs:
                    langs.append(lang)
                if st.error:
                    failed += 1
                elif st.final:
                    rendered += 1
        return {
            "id": job.id, "source": job.source, "status": job.status, "error": job.error,
            "created_at": float(job.created_at), "clip_count": len(job.clips),
            "langs": [x for x in LANGS if x in langs] + [x for x in langs if x not in LANGS],
            "rendered": rendered, "failed": failed, "interrupted": job.error == "interrupted",
            "task": self.worker.task_dict(self.worker.active_task_for(job.id)),
        }

    def unreadable_summary(self, job_id: str) -> dict:
        try:
            created = (_job_dir(job_id) / "job.json").stat().st_mtime
        except OSError:
            created = 0.0
        return {"id": job_id, "source": "", "status": "failed", "error": "job.json unreadable",
                "created_at": float(created), "clip_count": 0, "langs": [], "rendered": 0, "failed": 0,
                "interrupted": False, "task": self.worker.task_dict(self.worker.active_task_for(job_id))}

    def detail(self, job: Job) -> dict:
        from ..jobstore import CLIP_STAGES

        d = self.summary(job)
        d["stages"] = _stage_dict(job.stages, list(JOB_STAGES))
        opts = read_options(job.id)
        d["options"] = opts.to_dict() if opts is not None else None
        try:
            settings: Settings | None = self.settings_loader()
        except (ValidationError, ValueError, OSError):
            settings = None  # a broken settings.json must not break the job page
        d["voiceover"] = _job_voiceover(job, settings)
        d["voiceover_override"] = job.voiceover
        settings = _job_settings(job, settings)
        clips = []
        for c in job.clips:
            cdir = _clip_dir(job.id, c.index)
            langs = {}
            for lang, st in c.langs.items():
                out = cdir / lang
                final = bool(st.final) and _nonempty_file(out / "final.mp4")
                script = _read_script(out / "script.json")
                meta = _read_json(out / "metadata.json")
                if isinstance(meta, dict):
                    meta = {"language": meta.get("language"), "title": meta.get("title"),
                            "description": meta.get("description"), "hashtags": meta.get("hashtags")}
                else:
                    meta = None
                voice = _read_json(out / "voice.json")
                if isinstance(voice, dict):
                    voice = {"duration": voice.get("final_duration"), "clip_duration": voice.get("clip_duration"),
                             "tempo": voice.get("tempo"), "shortened": bool(voice.get("shortened", False)),
                             "trimmed": bool(voice.get("trimmed", False)),
                             "lengthened": bool(voice.get("lengthened", False)),
                             "truncated": bool(voice.get("truncated", False)),
                             "warnings": [str(w) for w in voice.get("warnings") or [] if isinstance(w, str)]}
                else:
                    voice = None
                base = f"/api/jobs/{job.id}/clips/{c.index}/{lang}"
                caption_engine, caption_style, render_stale = _render_info(out, final, settings)
                rendered_vo = _rendered_voiceover(out) if final else None
                langs[lang] = {
                    "stages": _stage_dict(st.stages, list(CLIP_STAGES)),
                    "error": st.error,
                    "final": final,
                    "video_url": f"{base}/video" if final else None,
                    "download_url": f"{base}/download" if final else None,
                    "script": script.model_dump() if script is not None else None,
                    "metadata": meta,
                    "voice": voice,
                    "voice_stale": _voice_stale(out, script.narration if script is not None else None),
                    "warning": st.warning,
                    "caption_engine": caption_engine,
                    "caption_style": caption_style,
                    "render_stale": render_stale,
                    "voiceover": d["voiceover"],
                    "rendered_voiceover": rendered_vo,
                    "audio": _render_audio(out) if final else None,
                    "audio_stale": bool(final and settings is not None and _audio_stale(out, settings)),
                    "style_stale": _style_stale(out, final, settings),
                    "task": self.worker.task_dict(self.worker.active_task_for(job.id, c.index, lang)),
                }
            m = c.moment
            clips.append({
                "index": c.index,
                "moment": {"start": m.start, "end": m.end, "duration": round(m.end - m.start, 3),
                           "hook": m.hook, "reason": m.reason, "score": m.score},
                "cut": _nonempty_file(cdir / "cut.mp4"),
                "langs": langs,
            })
        d["clips"] = clips
        return d

    def load_settings(self) -> Settings:
        try:
            return self.settings_loader()
        except ValidationError as e:
            raise APIError(500, f"{settings_path().name} is invalid; fix it on the settings page or delete it",
                           validation_errors(e.errors()))
        except ValueError:
            raise APIError(500, f"{settings_path().name} is not valid JSON; fix or delete it")

    # ---- idempotency ----------------------------------------------------------------------
    @staticmethod
    def idem_path() -> Path:
        return data_dir() / "web" / "idempotency.json"

    def idem_load(self) -> dict:
        data = _read_json(self.idem_path())
        if not isinstance(data, dict):
            return {}
        now = time.time()
        return {k: v for k, v in data.items()
                if isinstance(v, dict) and isinstance(v.get("created_at"), (int, float))
                and now - v["created_at"] < IDEMPOTENCY_TTL}

    def idem_save(self, data: dict) -> None:
        write_text_atomic(self.idem_path(), json.dumps(data, ensure_ascii=False, indent=2))


class _MediaFileResponse(FileResponse):
    """FileResponse with JSON errors: malformed Range -> whole file (RFC 9110 lets us ignore it);
    unsatisfiable -> 416 JSON with Content-Range."""

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        headers = list(scope.get("headers") or [])
        rng = next((v.decode("latin-1") for k, v in headers if k.lower() == b"range"), None)
        if rng is not None:
            try:
                size = os.stat(self.path).st_size
                self._parse_range_header(rng, size)
            except MalformedRangeHeader:
                scope = dict(scope)
                scope["headers"] = [(k, v) for k, v in headers if k.lower() != b"range"]
            except RangeNotSatisfiable as exc:
                await error_response(416, "requested range not satisfiable",
                                     headers={"Content-Range": f"bytes */{exc.max_size}"})(scope, receive, send)
                return
            except OSError:
                pass
        await super().__call__(scope, receive, send)


def _settings_unknown_keys(data: Any, model: type[BaseModel], prefix: str = "") -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    if not isinstance(data, dict):
        return errors
    fields = model.model_fields
    for key, value in data.items():
        path = f"{prefix}{key}"
        if key not in fields:
            errors.setdefault(path, []).append("unknown setting")
            continue
        sub = _model_of(fields[key].annotation)
        if sub is not None and isinstance(value, dict):
            errors.update(_settings_unknown_keys(value, sub, path + "."))
    return errors


def _model_of(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        m = _model_of(arg)
        if m is not None:
            return m
    return None


def _check_url(url: str) -> str | None:
    """None when ok, else the reason."""
    if url != url.strip() or _CONTROL.search(url) or " " in url:
        return "URL must not contain spaces or control characters"
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port  # raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        return "URL is not valid (check the host and port)"
    if port == 0:
        return "URL is not valid (port must be 1-65535)"
    if parts.scheme.lower() not in ("http", "https"):
        return "URL must start with http:// or https://"
    if not host:
        return "URL must include a host"
    return None


def _settings_value_errors(s: Settings) -> dict[str, list[str]]:
    """Checks the Settings model cannot express (it is owned by config.py): formats, lengths, cross-field."""
    errors: dict[str, list[str]] = {}
    url = s.voicebox.url
    if len(url) > MAX_URL_LEN:
        errors["voicebox.url"] = [f"must be at most {MAX_URL_LEN} characters"]
    else:
        reason = _check_url(url)
        if reason:
            errors["voicebox.url"] = [reason.replace("URL", "Voicebox URL", 1) if reason.startswith("URL") else reason]
    c = s.captions
    if not 0 <= c.margin_v <= s.clips.height:
        errors["captions.margin_v"] = [f"must be between 0 and clips.height ({s.clips.height})"]
    for name in ("primary_color", "highlight_color"):
        if not ASS_COLOR_RE.match(getattr(c, name)):
            errors[f"captions.{name}"] = ["must be an ASS colour like &H00FFFFFF (&H + 8 hex digits, AABBGGRR)"]
    errors.update(s.cross_field_errors())  # G4: the hook stays above the captions
    strings = {
        "llm.model": s.llm.model, "llm.api_key_env": s.llm.api_key_env,
        "captions.font_en": c.font_en, "captions.font_hi": c.font_hi,
        "whisper.model": s.whisper.model, "whisper.device": s.whisper.device,
        "whisper.compute_type": s.whisper.compute_type, "whisper.language": s.whisper.language,
        "voicebox.english.profile_id": s.voicebox.english.profile_id,
        "voicebox.english.engine": s.voicebox.english.engine,
        "voicebox.hindi.profile_id": s.voicebox.hindi.profile_id,
        "voicebox.hindi.engine": s.voicebox.hindi.engine,
    }
    for key, value in strings.items():
        if value is not None and len(value) > MAX_SETTING_STR:
            errors.setdefault(key, []).append(f"must be at most {MAX_SETTING_STR} characters")
    return errors


def _check_filename(name: str, allowed: list[str]) -> str | None:
    if "/" in name or "\\" in name or "\x00" in name or ".." in name or _CONTROL.search(name):
        return "filename must be a plain file name (no path, no '..')"
    if name.strip() != name or not name.strip(". "):
        return "filename is not valid"
    ext = os.path.splitext(name)[1].lower()
    if ext not in allowed:
        return f"file type {ext or '(none)'} is not allowed (allowed: {', '.join(allowed)})"
    return None


def _find_upload(upload_id: str) -> tuple[Path, str] | None:
    d = uploads_root() / upload_id
    if not d.is_dir():
        return None
    for p in sorted(d.glob("source.*")):
        if ".part" not in p.name and _nonempty_file(p):
            meta = _read_json(d / "upload.json")
            name = meta.get("filename") if isinstance(meta, dict) and isinstance(meta.get("filename"), str) else p.name
            return p, name
    return None


# ---------------------------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------------------------
def create_app(*, worker: Worker | None = None, settings_loader: Callable[[], Settings] = load_settings,
               doctor: Callable[[Settings], Any] = run_doctor, voicebox_factory: Callable[[Settings], Any] | None = None,
               allowed_hosts: set[str] | None = None, start_worker: bool = True,
               static_dir: Path | None = None, stop_on_signal: bool = False,
               publish_http: Callable[[], Any] | None = None) -> FastAPI:
    if worker is None:
        worker = Worker(settings_loader=settings_loader)
    token = load_or_create_token()
    state = App(worker, settings_loader, doctor, voicebox_factory, Path(static_dir or STATIC_DIR), token)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # uvicorn installs its own signal handlers before the lifespan starts: chain ours in front of them
        restore = install_stop_hook(worker) if stop_on_signal else None
        if start_worker:
            await run_in_threadpool(worker.start)
        try:
            yield
        finally:
            if restore is not None:
                restore()
            await run_in_threadpool(worker.stop)

    app = FastAPI(title="Auto Clipper", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.worker = worker
    app.state.token = token
    app.state.ctx = state
    install_error_handlers(app)
    app.add_middleware(GuardMiddleware, token=token, allowed_hosts=set(allowed_hosts or DEFAULT_ALLOWED_HOSTS))

    # ---- page / static ----------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index(request: Request) -> HTMLResponse:
        p = state.static_dir / "index.html"
        try:
            html = p.read_text(encoding="utf-8")
        except OSError:
            html = FALLBACK_PAGE
        # The Host header is client-chosen: only a loopback PEER gets the token (API-1). Others get an
        # empty token, and the page shows its "no access token" banner.
        peer = request.client.host if request.client is not None else ""
        given = token if peer and is_loopback(peer) else ""
        return HTMLResponse(html.replace(TOKEN_PLACEHOLDER, given), headers=PAGE_HEADERS)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    @app.api_route("/static/{file:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def static(file: str) -> Response:
        root = state.static_dir.resolve()
        if not file or "\x00" in file:
            raise APIError(404, "not found")
        try:
            p = (root / file).resolve()
        except (OSError, ValueError):
            raise APIError(404, "not found")
        if root not in p.parents or not p.is_file():
            raise APIError(404, "not found")
        return FileResponse(p, headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    # ---- uploads ----------------------------------------------------------------------------
    @app.post("/api/uploads", status_code=201)
    async def upload(request: Request,
                     filename: Annotated[str, Query(min_length=1, max_length=255)]) -> JSONResponse:
        ctype = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if ctype and not (ctype == "application/octet-stream" or ctype.startswith("video/")
                          or ctype == "application/ogg"):
            raise APIError(415, "upload the raw file bytes (Content-Type application/octet-stream or video/*)")
        settings = await run_in_threadpool(state.load_settings)
        max_mb, allowed = _web_settings(settings)
        reason = _check_filename(filename, allowed)
        if reason:
            raise APIError(422, VALIDATION_FAILED, {"filename": [reason]})
        limit = max_mb * 1024 * 1024
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                declared_n = int(declared)
            except ValueError:
                raise APIError(422, VALIDATION_FAILED, {"body": ["invalid Content-Length"]})
            if declared_n > limit:
                raise APIError(413, f"file is larger than the {max_mb} MB upload limit")
        upload_id = uuid.uuid4().hex
        ext = os.path.splitext(filename)[1].lower()
        d = uploads_root() / upload_id
        d.mkdir(parents=True, exist_ok=False)
        part = d / f"source.part{ext}"
        size = 0
        ok = False
        try:
            with open(part, "wb") as f:
                buf = bytearray()
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > limit:
                        raise APIError(413, f"file is larger than the {max_mb} MB upload limit")
                    buf += chunk
                    if len(buf) >= CHUNK:
                        await run_in_threadpool(f.write, bytes(buf))
                        buf.clear()
                if buf:
                    await run_in_threadpool(f.write, bytes(buf))
            if size == 0:
                raise APIError(422, VALIDATION_FAILED, {"body": ["the uploaded file is empty"]})
            write_text_atomic(d / "upload.json", json.dumps({"filename": filename, "size": size,
                                                             "created_at": time.time()}, ensure_ascii=False))
            os.replace(part, d / f"source{ext}")
            ok = True
        except ClientDisconnect:
            raise APIError(422, "upload interrupted", {"body": ["the client disconnected"]})
        finally:
            if not ok:
                part.unlink(missing_ok=True)
                (d / "upload.json").unlink(missing_ok=True)
                try:
                    d.rmdir()
                except OSError:
                    pass
        return JSONResponse({"upload_id": upload_id, "filename": filename, "size": size}, status_code=201)

    # ---- jobs -------------------------------------------------------------------------------
    @app.post("/api/jobs", status_code=201)
    def create_job(request: Request, body: Annotated[NewJobRequest, Body()]) -> JSONResponse:
        key = request.headers.get("idempotency-key")
        if key is not None and not IDEMPOTENCY_RE.match(key):
            raise APIError(422, VALIDATION_FAILED,
                           {"Idempotency-Key": ["must be 1..200 characters of A-Z a-z 0-9 _ . : -"]})
        errors: dict[str, list[str]] = {}
        if (body.url is None) == (body.upload_id is None):
            msg = "give exactly one of url or upload_id"
            errors = {"url": [msg], "upload_id": [msg]}
        elif body.url is not None:
            reason = _check_url(body.url)
            if reason:
                errors["url"] = [reason]
        o = body.options or JobOptionsIn()
        if o.min_seconds is not None or o.max_seconds is not None:
            s = state.load_settings()
            lo = o.min_seconds if o.min_seconds is not None else s.clips.min_seconds
            hi = o.max_seconds if o.max_seconds is not None else s.clips.max_seconds
            if hi < lo:
                errors["options.max_seconds"] = [f"must be >= min_seconds ({lo:g})"]
        if errors:
            raise APIError(422, VALIDATION_FAILED, errors)

        canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with state.idem_lock:
            store = state.idem_load() if key is not None else {}
            if key is not None and key in store:
                rec = store[key]
                if rec.get("hash") != digest:
                    raise APIError(409, "Idempotency-Key was already used for a different request")
                job = read_job(str(rec.get("job_id", ""))) if _job_id_ok(rec.get("job_id")) else None
                if job is not None:
                    return JSONResponse(state.summary(job), status_code=200)
                store.pop(key, None)  # the job is gone: treat the key as fresh
            vo = o.voiceover
            if vo is None:  # fix the job's mode now: a later Settings change must not switch this job
                vo = state.load_settings().voiceover
            options = JobOptions(clips=o.clips, min_seconds=o.min_seconds, max_seconds=o.max_seconds,
                                 langs=list(o.langs) if o.langs is not None else None, voiceover=vo)
            if body.upload_id is not None:
                found = _find_upload(body.upload_id)
                if found is None:
                    raise APIError(404, f"upload {body.upload_id} not found")
                path, name = found
                task = state.worker.submit_job(f"upload:{name}", options=options, upload=path)
            else:
                task = state.worker.submit_job(body.url or "", options=options)
            if key is not None:
                store[key] = {"hash": digest, "job_id": task.job_id, "created_at": time.time()}
                state.idem_save(store)
        job = _load_job(task.job_id)
        return JSONResponse(state.summary(job), status_code=201)

    @app.get("/api/jobs")
    def list_jobs(limit: Annotated[int, Query(ge=1, le=200)] = 50,
                  offset: Annotated[int, Query(ge=0, le=100000)] = 0) -> dict:
        from ..jobstore import JobStore

        ids = JobStore.list_ids()
        out = []
        for job_id in ids[offset:offset + limit]:
            try:
                job = read_job(job_id)
            except (OSError, ValueError):
                job = None
            out.append(state.summary(job) if job is not None else state.unreadable_summary(job_id))
        return {"jobs": out, "total": len(ids), "limit": limit, "offset": offset}

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: JobId) -> dict:
        p = _job_dir(job_id) / "job.json"
        if not p.exists():
            raise APIError(404, f"job {job_id} not found")
        try:
            job = read_job(job_id)
        except (OSError, ValueError):
            d = state.unreadable_summary(job_id)
            d.update({"stages": _stage_dict({}, list(JOB_STAGES)), "options": None, "clips": []})
            return d
        if job is None:
            raise APIError(404, f"job {job_id} not found")
        return state.detail(job)

    @app.get("/api/jobs/{job_id}/log")
    def job_log(job_id: JobId, tail: Annotated[int, Query(ge=1, le=2000)] = 200) -> dict:
        from collections import deque

        _load_job(job_id)
        p = _job_dir(job_id) / "web.log"
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                lines = [ln.rstrip("\n") for ln in deque(f, maxlen=tail)]
        except FileNotFoundError:
            lines = []
        return {"lines": lines}

    @app.post("/api/jobs/{job_id}/resume")
    def resume(job_id: JobId) -> JSONResponse:
        job = _load_job(job_id)
        active = state.worker.active_run_job(job_id)
        if active is not None:
            return JSONResponse({"job": state.summary(job), "task": state.worker.task_dict(active)}, status_code=200)
        any_failed = any(st.error for c in job.clips for st in c.langs.values())
        if job.status == "done" and not any_failed and job.error != "interrupted":
            raise APIError(409, "job is already complete")
        _check_no_other_owner(state.worker, job_id)
        if job.status == "running" and not has_lock_file(job_id):
            # no job.lock at all (an older CLI): fall back to the job.json mtime heuristic
            try:
                age = time.time() - (_job_dir(job_id) / "job.json").stat().st_mtime
            except OSError:
                age = 0.0
            if age < RECOVER_MIN_AGE:
                raise APIError(409, "job is being run by another process (job.json is changing)")
        task, created = state.worker.submit_resume(job_id)
        return JSONResponse({"job": state.summary(_load_job(job_id)), "task": state.worker.task_dict(task)},
                            status_code=202 if created else 200)

    @app.put("/api/jobs/{job_id}/mode")
    def put_mode(job_id: JobId, body: Annotated[JobModeIn, Body()]) -> JSONResponse:
        """Switch a job between voice-over + captions and original audio + captions, and queue a run that
        re-renders every clip-language made in the other mode (CONTRACT_WEB section 12)."""
        from ..stages.render import render_settings_changed

        job = _load_job(job_id)
        try:
            settings: Settings | None = state.settings_loader()
        except (ValidationError, ValueError, OSError):
            settings = None
        current = _job_voiceover(job, settings)
        want = body.voiceover
        active = state.worker.active_run_job(job_id)
        target = _job_settings(job, settings)
        if target is not None:
            target = target.model_copy(update={"voiceover": want})

        def up_to_date(out: Path) -> bool:
            # rendered in that mode and with the current audio/caption settings (else: re-rendered)
            if not _nonempty_file(out / "final.mp4") or _rendered_voiceover(out) != want:
                return False
            return target is None or not render_settings_changed(out, target)

        if active is None and current == want and job.clips and all(
                up_to_date(_clip_dir(job_id, c.index, lang)) for c in job.clips for lang in c.langs):
            return JSONResponse({"job": state.summary(job), "voiceover": want, "changed": False, "task": None},
                                status_code=200)
        if active is None:
            _check_no_other_owner(state.worker, job_id)
        try:
            # the task carries the switch only when it is one; a run already queued in the job's current mode
            # is the answer to "same mode" (no 409)
            task, created = state.worker.submit_resume(job_id, voiceover=want if want != current else None,
                                                       current=current)
        except WorkerConflict as e:
            raise APIError(409, str(e))
        return JSONResponse({"job": state.summary(_load_job(job_id)), "voiceover": want, "changed": current != want,
                             "task": state.worker.task_dict(task)}, status_code=202 if created else 200)

    # ---- clip review ------------------------------------------------------------------------
    @app.get("/api/jobs/{job_id}/clips/{clip}/{lang}/script")
    def get_script(job_id: JobId, clip: ClipNo, lang: Lang) -> dict:
        job = _load_job(job_id)
        _clip_lang(job, clip, lang)
        p = _clip_dir(job_id, clip, lang) / "script.json"
        if not p.exists():
            raise APIError(404, f"script for clip {clip} ({lang}) not found")
        script = _read_script(p)
        if script is None:
            raise APIError(409, f"script.json for clip {clip} ({lang}) is unreadable")
        return script.model_dump()

    @app.put("/api/jobs/{job_id}/clips/{clip}/{lang}/script")
    def put_script(job_id: JobId, clip: ClipNo, lang: Lang, body: Annotated[ClipScriptIn, Body()]) -> dict:
        from ..stages.render import write_metadata
        from ..stages.script import normalize_script, save_script

        job = _load_job(job_id)
        _clip_lang(job, clip, lang)
        if not body.narration.strip():
            try:
                vo = _job_voiceover(job, state.load_settings())
            except APIError:
                vo = True
            # With the voice-over off (captions only) the narration is not used and may be empty. With it
            # on, an empty narration is kept only if none was written yet (Re-render then writes one).
            if vo and _saved_narration(_clip_dir(job_id, clip, lang)).strip():
                raise APIError(422, VALIDATION_FAILED, {"narration": ["narration must not be empty"]})
        out = _clip_dir(job_id, clip, lang)
        try:
            with state.worker.exclusive(job_id, clip, lang):
                # hook / keywords left out of the body keep the saved ones (older clients send neither)
                old = _read_script(out / "script.json")
                hook = body.hook if body.hook is not None else (old.hook if old is not None else "")
                keywords = ([k.model_dump() for k in body.keywords] if body.keywords is not None
                            else ([k.model_dump() for k in old.keywords] if old is not None else []))
                script = normalize_script(ClipScript(narration=body.narration, title=body.title,
                                                     description=body.description, hashtags=list(body.hashtags),
                                                     hook=hook, keywords=keywords))
                out.mkdir(parents=True, exist_ok=True)
                save_script(script, out / "script.json")
                if _nonempty_file(out / "final.mp4"):
                    write_metadata(script, lang, out / "metadata.json")
        except WorkerConflict:
            raise APIError(409, "clip is being rendered; try again when it finishes")
        try:
            js = _job_settings(job, state.settings_loader())
        except (ValidationError, ValueError, OSError):
            js = None
        return {"script": script.model_dump(), "voice_stale": _voice_stale(out, script.narration),
                "style_stale": _style_stale(out, _nonempty_file(out / "final.mp4"), js)}

    @app.post("/api/jobs/{job_id}/clips/{clip}/{lang}/rerender")
    def post_rerender(job_id: JobId, clip: ClipNo, lang: Lang) -> JSONResponse:
        job = _load_job(job_id)
        _clip_lang(job, clip, lang)
        if not _nonempty_file(_clip_dir(job_id, clip) / "cut.mp4") or \
                not _nonempty_file(_clip_dir(job_id, clip, lang) / "script.json"):
            raise APIError(409, "clip is not ready to re-render")
        _check_no_other_owner(state.worker, job_id)
        try:
            task, created = state.worker.submit_rerender(job_id, clip, lang)
        except WorkerConflict as e:
            raise APIError(409, str(e))
        return JSONResponse({"task": state.worker.task_dict(task)}, status_code=202 if created else 200)

    def _final(job_id: str, clip: int, lang: str) -> Path:
        job = _load_job(job_id)
        _clip_lang(job, clip, lang)
        p = _clip_dir(job_id, clip, lang) / "final.mp4"
        if not _nonempty_file(p):
            raise APIError(404, f"final.mp4 for clip {clip} ({lang}) not found")
        return p

    @app.api_route("/api/jobs/{job_id}/clips/{clip}/{lang}/video", methods=["GET", "HEAD"])
    def video(job_id: JobId, clip: ClipNo, lang: Lang) -> Response:
        return _MediaFileResponse(_final(job_id, clip, lang), media_type="video/mp4",
                                  headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                                  content_disposition_type="inline")

    @app.api_route("/api/jobs/{job_id}/clips/{clip}/{lang}/download", methods=["GET", "HEAD"])
    def download(job_id: JobId, clip: ClipNo, lang: Lang) -> Response:
        name = f"{job_id}_clip{clip:02d}_{lang}.mp4".encode("ascii", "ignore").decode("ascii")
        return _MediaFileResponse(_final(job_id, clip, lang), media_type="video/mp4", filename=name,
                                  headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    # ---- queue / tasks ----------------------------------------------------------------------
    @app.get("/api/queue")
    def queue() -> dict:
        return state.worker.snapshot()

    @app.get("/api/tasks/{task_id}")
    def task_status(task_id: TaskId) -> dict:
        task = state.worker.status(task_id)
        if task is None:
            raise APIError(404, f"task {task_id} not found")
        return state.worker.task_dict(task)  # type: ignore[return-value]

    @app.post("/api/tasks/{task_id}/cancel")
    def task_cancel(task_id: TaskId) -> JSONResponse:
        task, outcome = state.worker.cancel_ex(task_id)
        if task is None:
            raise APIError(404, f"task {task_id} not found")
        if outcome == "finished":
            raise APIError(409, f"task is already {task.state}")
        return JSONResponse(state.worker.task_dict(task), status_code=200 if outcome == "removed" else 202)

    # ---- caption presets (G5) ---------------------------------------------------------------
    @app.get("/api/caption-presets")
    def caption_presets() -> dict:
        from ..caption_presets import presets_payload

        return {"presets": presets_payload()}

    # ---- settings ---------------------------------------------------------------------------
    @app.get("/api/settings")
    def get_settings() -> dict:
        return state.load_settings().model_dump()

    @app.get("/api/settings/schema")
    def get_schema() -> dict:
        return Settings.model_json_schema()

    @app.put("/api/settings")
    def put_settings(body: Annotated[dict[str, Any], Body()]) -> dict:
        unknown = _settings_unknown_keys(body, Settings)
        if unknown:
            raise APIError(422, VALIDATION_FAILED, unknown)
        try:
            s = Settings.model_validate(body, strict=True)
        except ValidationError as e:
            raise APIError(422, VALIDATION_FAILED, validation_errors(e.errors()))
        errors: dict[str, list[str]] = {}
        if s.clips.min_seconds > s.clips.max_seconds:
            errors["clips.max_seconds"] = [f"must be >= clips.min_seconds ({s.clips.min_seconds:g})"]
        if not s.languages:
            errors["languages"] = ["choose at least one language"]
        elif len(set(s.languages)) != len(s.languages):
            errors["languages"] = ["languages must be unique"]
        errors.update(_settings_value_errors(s))
        if errors:
            raise APIError(422, VALIDATION_FAILED, errors)
        from ..publish import service as publish_service  # step 4: one settings write lock

        with publish_service._settings_lock:
            if "publish" not in body:
                # step 4: the Accounts page owns `publish` (dry-run switch, mapping, ids filled by connect);
                # a body without it keeps the stored section instead of resetting or reverting it
                s.publish = load_settings().publish
            write_text_atomic(settings_path(), json.dumps(s.model_dump(), indent=2, ensure_ascii=False))
        return s.model_dump()

    @app.get("/api/voicebox/profiles")
    def voicebox_profiles() -> dict:
        s = state.load_settings()
        url = s.voicebox.url
        client = None
        try:
            if state.voicebox_factory is not None:
                client = state.voicebox_factory(s)
            else:
                from ..voicebox import VoiceboxClient

                client = VoiceboxClient.from_settings(s.voicebox, timeout=10)
            raw = client.list_profiles()
        except Exception as e:  # noqa: BLE001 - VoiceboxError, httpx errors, anything: Voicebox is unusable
            log.warning("voicebox profiles failed: %s", e)
            raise APIError(503, f"Voicebox is not reachable at {url} - is it running?")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        profiles = []
        for p in raw or []:
            if not isinstance(p, dict):
                continue
            pid = p.get("id") or p.get("profile_id")
            if pid is None or pid == "":
                continue
            lang = p.get("language")
            profiles.append({"id": str(pid), "name": str(p.get("name") or ""),
                             "language": str(lang) if lang else None})
        return {"profiles": profiles}

    # ---- doctor -----------------------------------------------------------------------------
    @app.get("/api/doctor")
    async def doctor_route() -> dict:
        s = await run_in_threadpool(state.load_settings)
        checks = await run_in_threadpool(state.doctor, s)
        out = [{"name": str(c.name), "level": str(c.level), "detail": str(c.detail)} for c in checks]
        return {"checks": out, "problems": sum(c["level"] == "fail" for c in out),
                "warnings": sum(c["level"] == "warn" for c in out)}

    install_publish_routes(app, state, publish_http=publish_http)  # step 4 (CONTRACT_STEP4 section 9)
    return app


def _check_no_other_owner(worker: Worker, job_id: str) -> None:
    lock = worker.live_owner(job_id)
    if lock is not None:
        raise APIError(409, f"job is being run by another process (pid {lock.get('pid')}); "
                            "wait for it to finish")


def install_stop_hook(worker: Worker) -> Callable[[], None] | None:
    """Chain SIGINT/SIGTERM so the worker's stop flag is set the moment the signal arrives, before
    uvicorn's graceful shutdown wait (WORKER-3). Main thread only; returns a restore function."""
    import signal

    if threading.current_thread() is not threading.main_thread():
        return None
    previous: dict[int, Any] = {}
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            old = signal.getsignal(sig)
        except (ValueError, OSError):  # pragma: no cover
            continue

        def handler(signum: int, frame: Any, _old: Any = old) -> None:
            try:
                worker.begin_stop()
            finally:
                if callable(_old):
                    _old(signum, frame)
                elif signum == signal.SIGINT and _old != signal.SIG_IGN:
                    raise KeyboardInterrupt

        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # pragma: no cover
            continue
        previous[sig] = old

    def restore() -> None:
        for sig, old in previous.items():
            try:
                if old is not None:
                    signal.signal(sig, old)
            except (ValueError, OSError, TypeError):  # pragma: no cover
                pass

    return restore


def _job_id_ok(value: Any) -> bool:
    return isinstance(value, str) and re.match(JOB_ID_RE, value) is not None


__all__ = ["create_app", "error_body", "Task"]
