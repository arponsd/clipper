"""G6 scheduled publishing + G7 cover picker HTTP API (docs/CONTRACT_STEP4.md sections 15-16).

All checks live in `publish.schedules` / `publish.cover`; this module validates input, maps errors to
the uniform JSON shape and keeps the schedule Idempotency-Key records. Nothing here sends anything to a
platform: a schedule only becomes a publish task in the scheduler (web/scheduler.py) at its time.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import Body, FastAPI, Path as PathParam, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..publish import cover as covers
from ..publish import schedules, service
from .errors import APIError, VALIDATION_FAILED
from .publish_routes import _service_errors
from .scheduler import Scheduler
from .worker import WorkerConflict

if TYPE_CHECKING:  # pragma: no cover
    from .server import App

log = logging.getLogger("autoclipper.web.schedule")

IDEM_PREFIX = "schedule:"
JobId = Annotated[str, PathParam(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
ClipNo = Annotated[int, PathParam(ge=0, le=999)]
Lang = Annotated[Literal["en", "hi"], PathParam()]
ScheduleId = Annotated[str, PathParam(pattern=r"^s_[0-9a-f]{12}$")]
Platform = Literal["youtube", "facebook", "instagram"]
IMAGE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


class _Platforms(BaseModel):
    @field_validator("platforms", check_fields=False)
    @classmethod
    def _unique(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(set(v)) != len(v):
            raise ValueError("platforms must be unique")
        return v


class ScheduleIn(_Platforms):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    clip: int = Field(ge=0, le=999)
    lang: Literal["en", "hi"]
    platforms: list[Platform] = Field(min_length=1, max_length=3)
    publish_at: str = Field(min_length=1, max_length=40)
    timezone: str | None = Field(None, max_length=64)
    dry_run: bool | None = None
    confirm: bool = False


class ScheduleEditIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    publish_at: str = Field(min_length=1, max_length=40)
    timezone: str | None = Field(None, max_length=64)


class CancelIn(_Platforms):
    model_config = ConfigDict(extra="forbid", strict=True)
    platforms: list[Platform] | None = Field(None, min_length=1, max_length=3)


class CoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: Literal["frame", "upload"]
    time: float | None = None
    title: str | None = Field(None, max_length=covers.MAX_TITLE_CHARS)


def install_schedule_routes(app: FastAPI, state: "App") -> Scheduler:
    from . import server as srv  # late import: server imports this module

    scheduler = Scheduler(state.worker, settings_loader=state.load_settings)
    if state.worker.scheduler is None:
        state.worker.scheduler = scheduler
    else:  # pragma: no cover - a worker shared by two apps
        scheduler = state.worker.scheduler
    app.state.scheduler = scheduler

    def _clip(job_id: str, clip: int, lang: str) -> None:
        srv._clip_lang(srv._load_job(job_id), clip, lang)

    def _parse(publish_at: str, tz: str | None, settings: Any) -> tuple[float, str]:
        with _service_errors():
            return schedules.parse_time(publish_at, tz, settings.publish.schedule.timezone)

    # ---- schedules ------------------------------------------------------------------------
    @app.get("/api/schedules")
    def list_schedules(state_q: Annotated[Literal["all", "upcoming", "done", "missed", "failed", "cancelled"],
                                          Query(alias="state")] = "all",
                       job_id: Annotated[str | None, Query(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")] = None,
                       clip: Annotated[int | None, Query(ge=0, le=999)] = None,
                       lang: Annotated[Literal["en", "hi"] | None, Query()] = None) -> dict:
        settings = state.load_settings()
        ss = settings.publish.schedule
        rows = schedules.list_schedules(state_q, job_id=job_id, clip=clip, lang=lang)
        label = ss.timezone or schedules._offset_label(datetime.now().astimezone())
        return {"schedules": rows, "now": time.time(), "timezone": label, "dry_run": bool(settings.publish.dry_run),
                "handed_off": sum(1 for s in schedules.list_schedules("upcoming")
                                  for it in s["items"].values() if it.get("state") == "handed_off"),
                "scheduler": {"running": scheduler.alive, "blocked": scheduler.blocked,
                              "grace_minutes": ss.grace_minutes,
                              "native_lead_minutes": ss.native_lead_minutes, "prefer_native": ss.prefer_native}}

    @app.get("/api/schedules/{sid}")
    def get_schedule(sid: ScheduleId) -> dict:
        with _service_errors():
            return schedules.get(sid)

    @app.post("/api/schedules")
    def create_schedule(request: Request, body: Annotated[ScheduleIn, Body()]) -> JSONResponse:
        key = request.headers.get("idempotency-key")
        if key is not None and not srv.IDEMPOTENCY_RE.match(key):
            raise APIError(422, VALIDATION_FAILED,
                           {"Idempotency-Key": ["must be 1..200 characters of A-Z a-z 0-9 _ . : -"]})
        _clip(body.job_id, body.clip, body.lang)
        settings = state.load_settings()
        settings_dry = bool(settings.publish.dry_run)
        dry = body.dry_run if body.dry_run is not None else settings_dry
        if not dry:
            errors: dict[str, list[str]] = {}
            if key is None:
                errors["Idempotency-Key"] = ["required for a live schedule (send a fresh random key)"]
            if not body.confirm:
                errors["confirm"] = ["set confirm to true: a schedule posts for real at its time"]
            if errors:
                raise APIError(422, VALIDATION_FAILED, errors)
        if body.dry_run is False and settings_dry:
            raise APIError(409, "publishing is in dry-run mode - turn dry run off on the Accounts page")
        t, label = _parse(body.publish_at, body.timezone, settings)
        if dry:
            try:
                with state.worker.exclusive_publish(body.job_id, body.clip, body.lang):
                    with _service_errors():
                        out = schedules.preview(body.job_id, body.clip, body.lang, list(body.platforms), t, label,
                                                settings)
            except WorkerConflict as e:
                raise APIError(409, str(e) or "clip is busy; try again when the task finishes")
            return JSONResponse(out, status_code=200)

        assert key is not None
        canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        store_key = IDEM_PREFIX + key
        with state.idem_lock:
            store = state.idem_load()
            rec = store.get(store_key)
            if rec is not None:
                if rec.get("hash") != digest:
                    raise APIError(409, "Idempotency-Key was already used for a different request")
                with _service_errors():
                    return JSONResponse({"dry_run": False, "schedule": schedules.get(rec["schedule_id"])},
                                        status_code=200)
            with _service_errors():
                sched = schedules.create(body.job_id, body.clip, body.lang, list(body.platforms), t, label,
                                         settings, idempotency_key=key)
            store[store_key] = {"hash": digest, "schedule_id": sched["id"], "created_at": time.time()}
            state.idem_save(store)
        scheduler.kick()
        return JSONResponse({"dry_run": False, "schedule": sched}, status_code=201)

    @app.patch("/api/schedules/{sid}")
    def edit_schedule(sid: ScheduleId, body: Annotated[ScheduleEditIn, Body()]) -> dict:
        settings = state.load_settings()
        with _service_errors():
            schedules.get(sid)  # 404 before validating the time
        t, label = _parse(body.publish_at, body.timezone, settings)
        with _service_errors():
            out = schedules.edit(sid, t, label, settings)
        scheduler.kick()
        return out

    @app.post("/api/schedules/{sid}/cancel")
    async def cancel_schedule(request: Request, sid: ScheduleId) -> dict:
        raw = await request.body()
        platforms = None
        if raw.strip():
            try:
                data = json.loads(raw)
            except (ValueError, RecursionError):
                raise APIError(422, VALIDATION_FAILED, {"body": ["invalid JSON"]})
            if not isinstance(data, dict):
                raise APIError(422, VALIDATION_FAILED, {"body": ["must be a JSON object"]})
            platforms = CancelIn.model_validate(data).platforms

        def run() -> dict:
            def cancel_task(task_id: str) -> str:
                # not cancel_ex, which would interrupt a publish that already started
                return state.worker.cancel_if_queued(task_id)
            with _service_errors():
                return schedules.cancel(sid, platforms, cancel_task=cancel_task)

        out = await run_in_threadpool(run)
        scheduler.kick()
        return out

    # ---- covers ---------------------------------------------------------------------------
    def _final_facts(job_id: str, clip: int, lang: str) -> tuple[Any, float, str]:
        final = srv._clip_dir(job_id, clip, lang) / "final.mp4"
        if not srv._nonempty_file(final):
            raise APIError(409, "clip has no final render yet")
        try:
            duration, _w, _h = service.probe_video(final)
        except Exception:  # noqa: BLE001
            raise APIError(409, "cannot read final.mp4")
        from ..publish import records
        return final, float(duration), records.file_sha256(final)

    def _cover_error(e: covers.CoverError) -> APIError:
        if e.status >= 500:
            log.error("cover: %s", e.message)
            return APIError(500, "could not make the cover (see server log)")
        if e.status == 422:
            return APIError(422, VALIDATION_FAILED, {e.field: [e.message]})
        return APIError(e.status, e.message, {e.field: [e.message]})

    def _cover_view(job_id: str, clip: int, lang: str) -> dict:
        final = srv._clip_dir(job_id, clip, lang) / "final.mp4"
        duration = sha = None
        if srv._nonempty_file(final):
            try:
                _, duration, sha = _final_facts(job_id, clip, lang)
            except APIError:
                duration = sha = None
        up = covers.upload_path(job_id, clip, lang)
        return {"cover": covers.public_info(job_id, clip, lang, sha), "final": duration is not None,
                "duration": duration, "has_upload": up is not None, "max_title": covers.MAX_TITLE_CHARS,
                "max_upload_bytes": covers.MAX_UPLOAD_BYTES, "upload_types": sorted(covers.UPLOAD_TYPES)}

    base = "/api/jobs/{job_id}/clips/{clip}/{lang}/cover"

    @app.get(base)
    def get_cover(job_id: JobId, clip: ClipNo, lang: Lang) -> dict:
        _clip(job_id, clip, lang)
        return _cover_view(job_id, clip, lang)

    @app.get(base + "/frame")
    def cover_frame(job_id: JobId, clip: ClipNo, lang: Lang, t: Annotated[float, Query()]) -> Response:
        _clip(job_id, clip, lang)
        final, duration, _ = _final_facts(job_id, clip, lang)
        try:
            covers.check_time(t, duration)
            data = covers.preview_frame(final, t, final.parent)
        except covers.CoverError as e:
            if e.field == "time":  # the query parameter is `t`
                e.field = "t"
            raise _cover_error(e)
        except Exception:  # noqa: BLE001 - ffmpeg failure
            log.exception("cover preview failed")
            raise APIError(500, "could not read that frame (see server log)")
        return Response(data, media_type="image/jpeg", headers=IMAGE_HEADERS)

    @app.post(base + "/image", status_code=201)
    async def cover_image(request: Request, job_id: JobId, clip: ClipNo, lang: Lang) -> JSONResponse:
        _clip(job_id, clip, lang)
        ctype = request.headers.get("content-type", "")
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > covers.MAX_UPLOAD_BYTES:
                    raise APIError(413, "the image is larger than 10 MB")
            except ValueError:
                raise APIError(422, VALIDATION_FAILED, {"body": ["invalid Content-Length"]})
        raw = bytearray()
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > covers.MAX_UPLOAD_BYTES:
                raise APIError(413, "the image is larger than 10 MB")

        def run() -> dict:
            try:
                with state.worker.exclusive_publish(job_id, clip, lang):
                    return covers.save_upload(job_id, clip, lang, bytes(raw), ctype)
            except WorkerConflict as e:
                raise APIError(409, str(e) or "clip is busy; try again when the task finishes")
            except covers.CoverError as e:
                raise _cover_error(e)

        info = await run_in_threadpool(run)
        return JSONResponse({"image": info, **_cover_view(job_id, clip, lang)}, status_code=201)

    @app.put(base)
    def put_cover(job_id: JobId, clip: ClipNo, lang: Lang, body: Annotated[CoverIn, Body()]) -> dict:
        _clip(job_id, clip, lang)
        final, duration, sha = _final_facts(job_id, clip, lang)
        settings = state.load_settings()
        try:
            title = covers.check_title(body.title)
            t = None
            if body.source == "frame":
                if body.time is None:
                    raise covers.CoverError("give the frame time in seconds", field="time")
                t = covers.check_time(body.time, duration)
            font = settings.captions.font_hi if lang == "hi" else settings.captions.font_en
            with state.worker.exclusive_publish(job_id, clip, lang):
                covers.make_cover(job_id, clip, lang, source=body.source, t=t, title=title, final=final,
                                  final_sha256=sha, font=font)
        except WorkerConflict as e:
            raise APIError(409, str(e) or "clip is busy; try again when the task finishes")
        except covers.CoverError as e:
            raise _cover_error(e)
        return _cover_view(job_id, clip, lang)

    @app.get(base + ".jpg")
    def cover_jpg(job_id: JobId, clip: ClipNo, lang: Lang) -> Response:
        _clip(job_id, clip, lang)
        if covers.read_info(job_id, clip, lang) is None:
            raise APIError(404, "no cover picked for this clip yet")
        try:
            data = covers.cover_path(job_id, clip, lang).read_bytes()
        except OSError:
            raise APIError(404, "no cover picked for this clip yet")
        return Response(data, media_type="image/jpeg", headers=IMAGE_HEADERS)

    @app.delete(base, status_code=204)
    def delete_cover(job_id: JobId, clip: ClipNo, lang: Lang) -> Response:
        _clip(job_id, clip, lang)
        try:
            with state.worker.exclusive_publish(job_id, clip, lang):
                covers.delete_cover(job_id, clip, lang)
        except WorkerConflict as e:
            raise APIError(409, str(e) or "clip is busy; try again when the task finishes")
        return Response(status_code=204)

    return scheduler


__all__ = ["install_schedule_routes", "ScheduleIn", "ScheduleEditIn", "CancelIn", "CoverIn"]
