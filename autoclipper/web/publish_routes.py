"""Step 4 HTTP API: accounts, Google OAuth callback, publish settings, dry-run and live publish
(CONTRACT_STEP4 section 9).

All real work is in `autoclipper.publish.service`; this module only validates input, maps the service
exceptions to the uniform JSON errors and keeps the publish Idempotency-Key records. No route returns a
token, client secret or resumable session URI; nothing here logs a request body.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated, Any, Callable, Iterator, Literal

from fastapi import Body, FastAPI, Path as PathParam, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..publish import service
from .auth import _host_only, is_loopback
from .errors import APIError, VALIDATION_FAILED
from .worker import WorkerConflict

if TYPE_CHECKING:  # pragma: no cover
    import httpx

    from .server import App

log = logging.getLogger("autoclipper.web.publish")

PLATFORMS = ("youtube", "facebook", "instagram")
MAX_CLIENT_JSON = 64 * 1024
CALLBACK_PATH = "/oauth/google/callback"
IDEM_PREFIX = "publish:"
LANG_NAMES = {"en": "English", "hi": "Hindi"}
PLATFORM_NAMES = {"youtube": "YouTube", "facebook": "Facebook", "instagram": "Instagram"}

TOKEN_RE = r"^[^\s]{20,1024}$"  # pydantic-core (Rust regex): "$" is end-of-text only
NUM_ID_RE = r"^[0-9]{1,32}$"
OAUTH_PARAM_RE = re.compile(r"\A[A-Za-z0-9._~/+=-]{1,2048}\Z")

JobId = Annotated[str, PathParam(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
ClipNo = Annotated[int, PathParam(ge=0, le=999)]
Lang = Annotated[Literal["en", "hi"], PathParam()]
Platform = Literal["youtube", "facebook", "instagram"]
DisconnectPlatform = Annotated[Literal["youtube", "facebook", "instagram", "meta"], PathParam()]


# ---------------------------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------------------------
class MetaConnectIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    page_id: str = Field(pattern=NUM_ID_RE)
    page_token: str = Field(pattern=TOKEN_RE)
    ig_user_id: str | None = Field(None, pattern=NUM_ID_RE)
    ig_token: str | None = Field(None, pattern=TOKEN_RE)


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    platforms: list[Platform] = Field(min_length=1, max_length=3)
    dry_run: bool | None = None
    confirm: bool = False
    force: bool = False

    @field_validator("platforms")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("platforms must be unique")
        return v


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
@contextmanager
def _service_errors() -> Iterator[None]:
    """service exceptions -> uniform JSON errors (CONTRACT_STEP4 section 7)."""
    try:
        yield
    except service.PublishInputError as e:
        raise APIError(422, VALIDATION_FAILED, _errors_of(e))
    except service.PublishConflict as e:
        raise APIError(409, _message_of(e, "conflict"), _errors_of(e))
    except service.PublishNotFound as e:
        raise APIError(404, _message_of(e, "not found"))
    except service.PublishUpstreamError as e:
        raise APIError(503, _message_of(e, "the platform is not reachable; try again later"))


def _message_of(e: BaseException, default: str) -> str:
    msg = getattr(e, "message", None)
    if not isinstance(msg, str) or not msg:
        msg = str(e).strip() or default
    return msg.splitlines()[0][:500]


def _errors_of(e: BaseException) -> dict[str, list[str]]:
    raw = getattr(e, "errors", None)
    out: dict[str, list[str]] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            msgs = v if isinstance(v, (list, tuple)) else [v]
            out[str(k)] = [str(m) for m in msgs]
    return out


def strip_internal(status: Any) -> Any:
    """Status without its internal "resume" block (session URI, container id, offsets)."""
    if isinstance(status, dict):
        return {k: v for k, v in status.items() if k != "resume"}
    return status


def _loopback_origin(request: Request) -> str | None:
    """scheme://host[:port] of this request when its Host is a loopback name, else None."""
    host_header = (request.headers.get("host") or "").strip()
    host = _host_only(host_header)
    if not host or not is_loopback(host):
        return None
    return f"http://{host_header}"


_SECRET_QUERY = re.compile(r"\b(code|state)=[^&\s\"]+")


class OAuthQueryRedactFilter(logging.Filter):
    """uvicorn's access log prints the callback URL: hide the one-time code and state."""

    def filter(self, record: logging.LogRecord) -> bool:
        def red(v: Any) -> Any:
            return _SECRET_QUERY.sub(lambda m: m.group(1) + "=***", v) if isinstance(v, str) else v

        record.msg = red(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(red(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: red(v) for k, v in record.args.items()}
        return True


def _install_access_filter() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, OAuthQueryRedactFilter) for f in logger.filters):
        logger.addFilter(OAuthQueryRedactFilter())


CALLBACK_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _callback_page(status: int, title: str, text: str, csp: str) -> HTMLResponse:
    body = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)} - Auto Clipper</title>"
        "<link rel=\"stylesheet\" href=\"/static/styles.css\">"
        "<link rel=\"stylesheet\" href=\"/static/publish.css\"></head>"
        "<body class=\"oauth-page\"><main class=\"oauth-card\">"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>"
        "<p class=\"oauth-hint\">You can close this tab and go back to Auto Clipper.</p>"
        "</main></body></html>"
    )
    return HTMLResponse(body, status_code=status, headers={**CALLBACK_HEADERS, "Content-Security-Policy": csp})


# ---------------------------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------------------------
def install_publish_routes(app: FastAPI, state: "App", *,
                           publish_http: Callable[[], "httpx.Client"] | None = None) -> None:
    from . import server as srv  # late import: server imports this module

    _install_access_filter()

    def http() -> "httpx.Client | None":
        return publish_http() if publish_http is not None else None

    # JobDetail.clips[].langs[lang] gains "publish_url" (additive, section 9.3)
    base_detail = state.detail

    def detail_with_publish(job: Any) -> dict:
        d = base_detail(job)
        for c in d.get("clips", []):
            for lang, entry in (c.get("langs") or {}).items():
                entry["publish_url"] = f"/api/jobs/{d['id']}/clips/{c['index']}/{lang}/publish"
        return d

    state.detail = detail_with_publish  # type: ignore[method-assign]

    # ---- settings -------------------------------------------------------------------------
    @app.get("/api/publish/settings")
    def get_publish_settings() -> dict:
        with _service_errors():
            return service.get_publish_settings()

    @app.put("/api/publish/settings")
    def put_publish_settings(body: Annotated[dict[str, Any], Body()]) -> dict:
        with _service_errors():
            return service.put_publish_settings(body)

    # ---- accounts -------------------------------------------------------------------------
    @app.get("/api/publish/accounts")
    def get_accounts() -> dict:
        settings = state.load_settings()
        with _service_errors():
            return service.accounts_status(settings)

    @app.put("/api/publish/google-client")
    async def put_google_client(request: Request) -> dict:
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > MAX_CLIENT_JSON:
                    raise APIError(413, "client_secret.json is larger than 64 KiB; is it the right file?")
            except ValueError:
                raise APIError(422, VALIDATION_FAILED, {"body": ["invalid Content-Length"]})
        raw = bytearray()
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_CLIENT_JSON:
                raise APIError(413, "client_secret.json is larger than 64 KiB; is it the right file?")
        try:
            data = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise APIError(422, VALIDATION_FAILED, {"body": ["invalid JSON (upload the client_secret.json file)"]})
        if not isinstance(data, dict):
            raise APIError(422, VALIDATION_FAILED, {"body": ["must be a JSON object (the client_secret.json file)"]})
        from fastapi.concurrency import run_in_threadpool

        with _service_errors():
            return await run_in_threadpool(service.set_google_client, data)

    @app.delete("/api/publish/google-client", status_code=204)
    def delete_google_client() -> Response:
        with _service_errors():
            service.clear_google_client()
        return Response(status_code=204)

    @app.post("/api/publish/accounts/{lang}/youtube/connect")
    def youtube_connect(request: Request, lang: Lang) -> dict:
        origin = _loopback_origin(request)
        if origin is None:
            raise APIError(409, "open Auto Clipper at http://127.0.0.1:<port> to connect YouTube "
                                "(Google only redirects back to a loopback address)")
        with _service_errors():
            return service.begin_youtube_connect(lang, origin + CALLBACK_PATH)

    @app.get(CALLBACK_PATH, include_in_schema=False)
    def google_callback(request: Request) -> HTMLResponse:
        q = request.query_params
        st, code, error = q.get("state"), q.get("code"), q.get("error")
        bad = _callback_page(400, "Not connected",
                             "This sign-in link is not valid or has expired. Start again with Connect on the "
                             "Accounts page.", srv.CSP)
        if not st or not OAUTH_PARAM_RE.match(st):
            return bad
        if code is not None and not OAUTH_PARAM_RE.match(code):
            return bad
        if error is not None and not re.match(r"\A[A-Za-z0-9_.-]{1,100}\Z", error):
            error = "invalid_request"
        if code is None and error is None:
            return bad
        try:
            result = service.complete_youtube_connect(st, code, error, http=http())
        except service.OAuthStateError as e:  # message is ours (no secrets); escaped by _callback_page
            return _callback_page(400, "Not connected", _message_of(e, "the sign-in was not completed"), srv.CSP)
        except service.PublishUpstreamError:
            return _callback_page(503, "Google is not reachable",
                                  "Auto Clipper could not reach Google to finish the sign-in. Check the internet "
                                  "connection and press Connect again.", srv.CSP)
        except (service.PublishInputError, service.PublishConflict, service.PublishNotFound) as e:
            return _callback_page(400, "Not connected", _message_of(e, "the sign-in was not completed"), srv.CSP)
        name = (result.get("account_name") if isinstance(result, dict) else None) or "your channel"
        lang = result.get("lang") if isinstance(result, dict) else None
        tail = f" for {LANG_NAMES[lang]} clips" if lang in LANG_NAMES else ""
        return _callback_page(200, "YouTube connected", f"Connected {name}{tail}.", srv.CSP)

    @app.post("/api/publish/accounts/{lang}/meta/connect")
    def meta_connect(lang: Lang, body: Annotated[MetaConnectIn, Body()]) -> dict:
        with _service_errors():
            return service.connect_meta(lang, page_id=body.page_id, page_token=body.page_token,
                                        ig_user_id=body.ig_user_id, ig_token=body.ig_token, http=http())

    @app.delete("/api/publish/accounts/{lang}/{platform}", status_code=204)
    def disconnect(lang: Lang, platform: DisconnectPlatform,
                   revoke: Annotated[bool, Query()] = True) -> Response:
        with _service_errors():
            service.disconnect(lang, platform, revoke=revoke, http=http())
        return Response(status_code=204)

    # ---- per clip -------------------------------------------------------------------------
    def _check_clip(job_id: str, clip: int, lang: str) -> None:
        job = srv._load_job(job_id)
        srv._clip_lang(job, clip, lang)

    @app.get("/api/jobs/{job_id}/clips/{clip}/{lang}/publish")
    def publish_view(job_id: JobId, clip: ClipNo, lang: Lang) -> dict:
        _check_clip(job_id, clip, lang)
        settings = state.load_settings()
        active = state.worker.task_dict(state.worker.active_publish(job_id, clip, lang))
        with _service_errors():
            view = service.publish_view(job_id, clip, lang, settings, active=active)
        for p in (view.get("platforms") or {}).values():
            if isinstance(p, dict) and "status" in p:
                p["status"] = strip_internal(p["status"])
        return view

    @app.post("/api/jobs/{job_id}/clips/{clip}/{lang}/publish")
    def publish(request: Request, job_id: JobId, clip: ClipNo, lang: Lang,
                body: Annotated[PublishIn, Body()]) -> JSONResponse:
        key = request.headers.get("idempotency-key")
        if key is not None and not srv.IDEMPOTENCY_RE.match(key):
            raise APIError(422, VALIDATION_FAILED,
                           {"Idempotency-Key": ["must be 1..200 characters of A-Z a-z 0-9 _ . : -"]})
        _check_clip(job_id, clip, lang)
        settings = state.load_settings()
        settings_dry = bool(settings.publish.dry_run)
        dry = body.dry_run if body.dry_run is not None else settings_dry
        if not dry:
            errors: dict[str, list[str]] = {}
            if key is None:
                errors["Idempotency-Key"] = ["required for a live publish (send a fresh random key)"]
            if not body.confirm:
                errors["confirm"] = ["set confirm to true to publish for real"]
            if errors:
                raise APIError(422, VALIDATION_FAILED, errors)
        if body.dry_run is False and settings_dry:
            raise APIError(409, "publishing is in dry-run mode - turn dry run off on the Accounts page")

        out_dir = srv._clip_dir(job_id, clip, lang)
        if not srv._nonempty_file(out_dir / "final.mp4") or (not dry and not (out_dir / "metadata.json").is_file()):
            raise APIError(409, "clip has no final render yet")

        if dry:
            try:
                with state.worker.exclusive_publish(job_id, clip, lang):
                    with _service_errors():
                        result = service.dry_run(job_id, clip, lang, list(body.platforms), settings)
                    plans = result.get("plans", result) if isinstance(result, dict) else {}
            except WorkerConflict as e:
                raise APIError(409, str(e) or "clip is busy; try again when the task finishes")
            return JSONResponse({"dry_run": True, "plans": plans}, status_code=200)

        assert key is not None
        canonical = json.dumps({"job_id": job_id, "clip": clip, "lang": lang,
                                "body": body.model_dump(mode="json")}, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        store_key = IDEM_PREFIX + key
        with state.idem_lock:
            store = state.idem_load()
            rec = store.get(store_key)
            if rec is not None:
                if rec.get("hash") != digest:
                    raise APIError(409, "Idempotency-Key was already used for a different request")
                task_d = None
                tid = rec.get("task_id")
                if isinstance(tid, str):
                    task_d = state.worker.task_dict(state.worker.status(tid)) or rec.get("task")
                return JSONResponse({"dry_run": False, "task": task_d,
                                     "already_published": rec.get("already_published") or {}}, status_code=200)

            srv._check_no_other_owner(state.worker, job_id)
            with _service_errors():
                to_run, already = service.check_live(job_id, clip, lang, list(body.platforms), settings,
                                                     force=body.force)
            already = {p: strip_internal(s) for p, s in (already or {}).items()}
            task_d = None
            created = False
            if to_run:
                try:
                    task, created = state.worker.submit_publish(job_id, clip, lang, list(to_run),
                                                                force=body.force, idempotency_key=key)
                except WorkerConflict as e:
                    raise APIError(409, str(e) or "clip is busy; try again when the task finishes")
                task_d = state.worker.task_dict(task)
            store[store_key] = {"hash": digest, "task_id": task_d["id"] if task_d else None, "task": task_d,
                                "already_published": already, "created_at": time.time()}
            state.idem_save(store)
        return JSONResponse({"dry_run": False, "task": task_d, "already_published": already},
                            status_code=202 if created else 200)


__all__ = ["install_publish_routes", "MetaConnectIn", "PublishIn", "OAuthQueryRedactFilter", "strip_internal"]
