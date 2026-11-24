"""The agreed interface for web routes and the CLI (CONTRACT_STEP4 section 7).

Raises only the exceptions below (never FastAPI types). No function here sends anything to a social
platform: live publishing is `runner.run_publish`, run by the worker / CLI after `check_live`.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from ..config import Settings, load_settings, settings_path
from ..jobstore import jobs_root, read_job_text
from ..models import Job
from ..stages._atomic import write_text_atomic
from . import LANG_NAMES, LANGS, PLATFORMS, accounts, oauth_google, records
from .base import Http, PublishError, PublishItem, classify_google, classify_meta, make_publisher, new_client
from .secrets import delete_secret, read_secret, redact, remember, write_secret
from .settings import PublishSettings

JOB_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
DIGITS_RE = re.compile(r"\A[0-9]{1,32}\Z")
TOKEN_RE = re.compile(r"\A\S{20,1024}\Z")
MAX_CLIENT_JSON = 64 * 1024


# ---------------------------------------------------------------------------------------------
# exceptions
# ---------------------------------------------------------------------------------------------
class PublishInputError(Exception):
    """-> 422"""

    def __init__(self, errors: dict[str, list[str]], message: str = "invalid input"):
        super().__init__(message)
        self.message = message
        self.errors = errors


class PublishConflict(Exception):
    """-> 409"""

    def __init__(self, message: str, errors: dict | None = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or {}


class PublishNotFound(Exception):
    """-> 404"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class PublishUpstreamError(Exception):
    """-> 503 (platform unreachable)"""

    def __init__(self, message: str):
        super().__init__(redact(message))
        self.message = redact(message)


class OAuthStateError(Exception):
    """Bad / expired / used OAuth state or a refused consent -> 400 page."""

    def __init__(self, message: str):
        super().__init__(redact(message))
        self.message = redact(message)


# ---------------------------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------------------------
def _check_lang(lang: object) -> str:
    if lang not in LANGS:
        raise PublishInputError({"lang": ["must be en or hi"]})
    return str(lang)


def _check_platform(platform: object, *, allow_meta: bool = False) -> str:
    ok = PLATFORMS + (("meta",) if allow_meta else ())
    if platform not in ok:
        raise PublishInputError({"platform": [f"must be one of {', '.join(ok)}"]})
    return str(platform)


def check_platforms(platforms: object) -> list[str]:
    if not isinstance(platforms, list) or not 1 <= len(platforms) <= len(PLATFORMS):
        raise PublishInputError({"platforms": ["give 1 to 3 platforms"]})
    out: list[str] = []
    for p in platforms:
        if p not in PLATFORMS:
            raise PublishInputError({"platforms": [f"unknown platform {p!r}; use youtube, facebook, instagram"]})
        if p in out:
            raise PublishInputError({"platforms": [f"duplicate platform {p!r}"]})
        out.append(p)
    return out


def _http(http: httpx.Client | None) -> tuple[httpx.Client, bool]:
    return (http, False) if http is not None else (new_client(), True)


class _NoNetwork(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:  # pragma: no cover - a bug guard
        raise RuntimeError("dry run must not touch the network")


_OFFLINE: httpx.Client | None = None


def _offline_client() -> httpx.Client:
    global _OFFLINE
    if _OFFLINE is None:
        _OFFLINE = httpx.Client(transport=_NoNetwork())
    return _OFFLINE


# ---------------------------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------------------------
_settings_lock = threading.Lock()


def _validation_errors(e: ValidationError, prefix: str = "") -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        key = f"{prefix}.{loc}" if prefix and loc else (prefix or loc or "body")
        msg = str(err.get("msg", "invalid"))
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        errors.setdefault(key, []).append(msg)
    return errors


def save_publish_settings(ps: PublishSettings) -> None:
    """Replace only the `publish` section of settings.json (atomic)."""
    with _settings_lock:
        p = settings_path()
        raw: Any = None
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
        if not isinstance(raw, dict):
            raw = Settings().model_dump()
        raw["publish"] = ps.model_dump()
        Settings.model_validate(raw)  # never write a file the app cannot load
        write_text_atomic(p, json.dumps(raw, indent=2, ensure_ascii=False))


def _load() -> Settings:
    return load_settings()


def get_publish_settings() -> dict:
    return _load().publish.model_dump()


def put_publish_settings(body: object) -> dict:
    if not isinstance(body, dict):
        raise PublishInputError({"body": ["expected a JSON object"]})
    try:
        ps = PublishSettings.model_validate(body, strict=True)
    except ValidationError as e:
        raise PublishInputError(_validation_errors(e)) from None
    save_publish_settings(ps)
    return ps.model_dump()


def _update_settings(fn: Any) -> PublishSettings:
    s = _load()
    ps = s.publish.model_copy(deep=True)
    fn(ps)
    ps = PublishSettings.model_validate(ps.model_dump())
    save_publish_settings(ps)
    return ps


# ---------------------------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------------------------
def accounts_status(settings: Settings) -> dict:
    return accounts.all_status(settings.publish)


def set_google_client(data: object) -> dict:
    if isinstance(data, (bytes, str)):
        if len(data) > MAX_CLIENT_JSON:
            raise PublishInputError({"body": ["client_secret.json is too large (max 64 KiB)"]})
        try:
            data = json.loads(data)
        except ValueError:
            raise PublishInputError({"body": ["not valid JSON"]}) from None
    if not isinstance(data, dict):
        raise PublishInputError({"body": ["expected the JSON object from client_secret.json"]})
    if len(json.dumps(data)) > MAX_CLIENT_JSON:
        raise PublishInputError({"body": ["client_secret.json is too large (max 64 KiB)"]})
    block = data.get("installed")
    if not isinstance(block, dict):
        if isinstance(data.get("web"), dict):
            raise PublishInputError({"installed": ["this is a Web application client; create a Desktop app "
                                                   "client in Google Cloud and download its JSON"]})
        raise PublishInputError({"installed": ["missing the \"installed\" block of client_secret.json"]})
    errors: dict[str, list[str]] = {}
    out: dict[str, str] = {}
    for key in ("client_id", "client_secret"):
        v = block.get(key)
        if not isinstance(v, str) or not v.strip():
            errors[f"installed.{key}"] = ["required"]
        elif len(v) > 1024 or any(c.isspace() for c in v.strip()):
            errors[f"installed.{key}"] = ["invalid value"]
        else:
            out[key] = v.strip()
    if errors:
        raise PublishInputError(errors)
    pid = block.get("project_id")
    out["project_id"] = pid.strip()[:200] if isinstance(pid, str) else ""
    write_secret("google_client", out)
    return {"configured": True, "client_id_hint": accounts.client_id_hint(out["client_id"])}


def clear_google_client() -> None:
    delete_secret("google_client")


def begin_youtube_connect(lang: str, redirect_uri: str) -> dict:
    _check_lang(lang)
    if not isinstance(redirect_uri, str) or not oauth_google.is_loopback_redirect(redirect_uri):
        raise PublishConflict("YouTube can only be connected from this computer (open the app via "
                              "http://127.0.0.1)", {"redirect_uri": ["must be a loopback http URL"]})
    client = accounts.google_client()
    if client is None:
        raise PublishConflict("Google OAuth client not configured - upload client_secret.json first",
                              {"google_client": ["not configured"]})
    verifier = oauth_google.make_verifier()
    state = oauth_google.PENDING.add(lang, redirect_uri, verifier)
    return {"auth_url": oauth_google.auth_url(client["client_id"], redirect_uri, state, verifier),
            "state": state, "expires_in": int(oauth_google.STATE_TTL), "redirect_uri": redirect_uri}


def complete_youtube_connect(state: str, code: str | None, error: str | None, *,
                             http: httpx.Client | None = None) -> dict:
    pending = oauth_google.PENDING.pop(state)
    if pending is None:
        raise OAuthStateError("this sign-in link expired or was already used - start Connect again")
    if error:
        text = "you declined access" if error == "access_denied" else f"Google returned an error ({error[:60]})"
        raise OAuthStateError(f"YouTube was not connected: {text}")
    if not isinstance(code, str) or not code or len(code) > 2048:
        raise OAuthStateError("Google did not return an authorization code")
    remember(code)
    client = accounts.google_client()
    if client is None:
        raise OAuthStateError("Google OAuth client not configured - upload client_secret.json first")
    c, close = _http(http)
    h = Http(c, "youtube", max_retries=2, sleep=time.sleep, classify=classify_google)
    try:
        tokens = oauth_google.exchange_code(h, client, code, pending.verifier, pending.redirect_uri)
        refresh = tokens.get("refresh_token")
        access = tokens.get("access_token")
        remember(refresh, access)
        if not isinstance(refresh, str) or not refresh:
            raise OAuthStateError("Google returned no refresh token - remove the app's access at "
                                  "myaccount.google.com/permissions and connect again")
        if not isinstance(access, str) or not access:
            raise OAuthStateError("Google returned no access token")
        channel = oauth_google.my_channel(h, access)
    except PublishError as e:
        if e.kind in ("network", "transient"):
            raise PublishUpstreamError(f"Google is unreachable: {e.message}") from None
        raise OAuthStateError(f"YouTube was not connected: {e.message}") from None
    finally:
        if close:
            c.close()
    granted = tokens.get("scope")
    scopes = granted.split() if isinstance(granted, str) else list(oauth_google.SCOPES)
    write_secret(f"youtube_{pending.lang}", {
        "refresh_token": refresh, "scopes": scopes, "channel_id": channel["id"],
        "channel_title": channel["title"], "connected_at": time.time()})

    def fill(ps: PublishSettings) -> None:
        t = ps.lang(pending.lang).youtube
        if not t.channel_id:
            t.channel_id = channel["id"]

    ps = _update_settings(fill)
    return {**accounts.account_status(ps, pending.lang, "youtube"), "lang": pending.lang}


def connect_meta(lang: str, *, page_id: str, page_token: str, ig_user_id: str | None,
                 ig_token: str | None, http: httpx.Client | None = None) -> dict:
    _check_lang(lang)
    errors: dict[str, list[str]] = {}
    if not isinstance(page_id, str) or not DIGITS_RE.match(page_id):
        errors["page_id"] = ["digits only (1-32)"]
    if not isinstance(page_token, str) or not TOKEN_RE.match(page_token):
        errors["page_token"] = ["20-1024 characters, no spaces"]
    if ig_user_id is not None and (not isinstance(ig_user_id, str) or not DIGITS_RE.match(ig_user_id)):
        errors["ig_user_id"] = ["digits only (1-32) or null"]
    if ig_token is not None and (not isinstance(ig_token, str) or not TOKEN_RE.match(ig_token)):
        errors["ig_token"] = ["20-1024 characters, no spaces, or null"]
    if ig_token is not None and ig_user_id is None and "ig_user_id" not in errors:
        errors["ig_user_id"] = ["required when ig_token is given"]
    if errors:
        raise PublishInputError(errors)
    remember(page_token, ig_token)
    ps0 = _load().publish
    c, close = _http(http)
    v = ps0.graph_api_version
    fb = Http(c, "facebook", max_retries=2, sleep=time.sleep, classify=lambda r: classify_meta(r, "facebook"))
    ig = Http(c, "instagram", max_retries=2, sleep=time.sleep, classify=lambda r: classify_meta(r, "instagram"))
    ig_username = None
    try:
        try:
            page = fb.json("GET", f"https://graph.facebook.com/{v}/{page_id}",
                           params={"fields": "id,name,instagram_business_account"},
                           headers={"Authorization": f"Bearer {page_token}"})
        except PublishError as e:
            if e.kind in ("network", "transient"):
                raise PublishUpstreamError(f"Facebook is unreachable: {e.message}") from None
            if e.kind == "auth":
                raise PublishInputError({"page_token": [f"rejected by Facebook (code {e.code or '190'})"]}) from None
            raise PublishInputError({"page_id": [f"Facebook did not accept this Page id: {e.message}"]}) from None
        if str(page.get("id")) != page_id:
            raise PublishInputError({"page_id": ["the token is not for this Page"]})
        linked = (page.get("instagram_business_account") or {}).get("id") \
            if isinstance(page.get("instagram_business_account"), dict) else None
        if ig_user_id is not None:
            if str(linked or "") != ig_user_id:
                raise PublishInputError({"ig_user_id": ["not the Instagram account linked to this Page"]})
            try:
                info = ig.json("GET", f"https://graph.facebook.com/{v}/{ig_user_id}",
                               params={"fields": "id,username"},
                               headers={"Authorization": f"Bearer {ig_token or page_token}"})
                ig_username = info.get("username") if isinstance(info.get("username"), str) else None
            except PublishError as e:
                if e.kind in ("network", "transient"):
                    raise PublishUpstreamError(f"Instagram is unreachable: {e.message}") from None
                key = "ig_token" if ig_token is not None else "page_token"
                raise PublishInputError({key: [f"rejected by Instagram: {e.message}"]}) from None
    finally:
        if close:
            c.close()
    write_secret(f"meta_{lang}", {
        "page_id": page_id, "page_name": str(page.get("name") or page_id)[:200], "page_token": page_token,
        "ig_user_id": ig_user_id, "ig_username": ig_username, "ig_token": ig_token, "connected_at": time.time()})

    def fill(ps: PublishSettings) -> None:
        t = ps.lang(lang)
        t.facebook.page_id = page_id
        if ig_user_id is not None:
            t.instagram.ig_user_id = ig_user_id

    ps = _update_settings(fill)
    return {"facebook": accounts.account_status(ps, lang, "facebook"),
            "instagram": accounts.account_status(ps, lang, "instagram")}


def disconnect(lang: str, platform: str, *, revoke: bool = True, http: httpx.Client | None = None) -> None:
    """Idempotent. Removes the secret; settings ids and enabled flags stay as they are.
    facebook: the Page token is removed; Instagram stays connected only if it has its own ig_token."""
    _check_lang(lang)
    _check_platform(platform, allow_meta=True)
    if platform == "youtube":
        data = read_secret(f"youtube_{lang}")
        if revoke and data and isinstance(data.get("refresh_token"), str):
            c, close = _http(http)
            try:
                oauth_google.revoke(c, data["refresh_token"])
            finally:
                if close:
                    c.close()
        delete_secret(f"youtube_{lang}")
        return
    if platform == "meta":
        delete_secret(f"meta_{lang}")
        return
    data = read_secret(f"meta_{lang}")
    if not data:
        return
    if platform == "facebook":
        if data.get("ig_user_id") and data.get("ig_token"):
            data.update(page_id=None, page_name=None, page_token=None)
            write_secret(f"meta_{lang}", data)
        else:
            delete_secret(f"meta_{lang}")
        return
    # instagram
    if data.get("page_token"):
        data.update(ig_user_id=None, ig_username=None, ig_token=None)
        write_secret(f"meta_{lang}", data)
    else:
        delete_secret(f"meta_{lang}")


# ---------------------------------------------------------------------------------------------
# clips
# ---------------------------------------------------------------------------------------------
_probe_cache: dict[tuple[str, int, int], tuple[float, int, int]] = {}


def probe_video(path: Path) -> tuple[float, int, int]:
    """(duration, width, height) via ffprobe, cached by path + size + mtime. Tests may monkeypatch it."""
    from .. import ffmpeg

    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    hit = _probe_cache.get(key)
    if hit:
        return hit
    info = ffmpeg.probe(path)
    res = (float(info.duration or 0.0), int(info.width or 0), int(info.height or 0))
    if len(_probe_cache) > 256:
        _probe_cache.clear()
    _probe_cache[key] = res
    return res


def _job(job_id: str) -> Job:
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise PublishNotFound(f"job {job_id!r} not found")
    p = jobs_root() / job_id / "job.json"
    try:
        return Job.model_validate_json(read_job_text(p))
    except FileNotFoundError:
        raise PublishNotFound(f"job {job_id} not found") from None
    except (OSError, ValueError):
        raise PublishConflict(f"job {job_id} has an unreadable job.json") from None


def clip_dir(job_id: str, clip: int, lang: str) -> Path:
    return jobs_root() / job_id / "clips" / f"{clip:02d}" / lang


def _check_clip(job_id: str, clip: int, lang: str) -> Job:
    _check_lang(lang)
    if isinstance(clip, bool) or not isinstance(clip, int) or not 0 <= clip <= 999:
        raise PublishInputError({"clip": ["must be 0-999"]})
    job = _job(job_id)
    for c in job.clips:
        if c.index == clip:
            if lang not in c.langs:
                raise PublishNotFound(f"language {lang} not found for clip {clip} in job {job_id}")
            return job
    raise PublishNotFound(f"clip {clip} not found in job {job_id}")


def _final(job_id: str, clip: int, lang: str) -> Path | None:
    p = clip_dir(job_id, clip, lang) / "final.mp4"
    try:
        return p if p.is_file() and p.stat().st_size > 0 else None
    except OSError:
        return None


def read_metadata(job_id: str, clip: int, lang: str) -> tuple[dict, bool]:
    """(metadata, from_metadata_json). Falls back to script.json when metadata.json is missing."""
    d = clip_dir(job_id, clip, lang)
    for name, real in (("metadata.json", True), ("script.json", False)):
        try:
            data = json.loads((d / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            meta = {"language": lang, "title": data.get("title") or "", "description": data.get("description") or "",
                    "hashtags": data.get("hashtags") if isinstance(data.get("hashtags"), list) else []}
            return meta, real
    return {"language": lang, "title": "", "description": "", "hashtags": []}, False


VOICE_STALE_PROBLEM = "narration changed since the last render - re-render the clip before publishing"


def voice_stale(job_id: str, clip: int, lang: str) -> bool:
    """True when script.json's narration no longer matches the voice that final.mp4 was rendered with
    (same rule as the web clip view's `voice_stale`): metadata.json is already rewritten on script save, so
    publishing now would pair the new title with the old narration."""
    from ..stages.render import read_render_stamp, rendered_voiceover
    from ..stages.voice import narration_hash

    d = clip_dir(job_id, clip, lang)
    if not rendered_voiceover(read_render_stamp(d)):
        return False  # rendered with the voice-over off: the narration is not part of final.mp4
    try:
        script = json.loads((d / "script.json").read_text(encoding="utf-8"))
        voice = json.loads((d / "voice.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(script, dict) or not isinstance(voice, dict):
        return False
    narration, saved = script.get("narration"), voice.get("narration_hash")
    if not isinstance(narration, str) or not isinstance(saved, str) or not saved:
        return False
    return saved != narration_hash(narration)


def load_item(job_id: str, clip: int, lang: str) -> tuple[PublishItem, list[str]]:
    """-> (item, extra problems). PublishNotFound / PublishConflict("clip has no final render yet")."""
    _check_clip(job_id, clip, lang)
    final = _final(job_id, clip, lang)
    if final is None:
        raise PublishConflict("clip has no final render yet", {"final": ["final.mp4 missing - render the clip first"]})
    meta, real = read_metadata(job_id, clip, lang)
    extra = [] if real else ["metadata.json missing - re-render"]
    if voice_stale(job_id, clip, lang):
        extra.append(VOICE_STALE_PROBLEM)
    try:
        duration, width, height = probe_video(final)
    except Exception as e:  # noqa: BLE001 - ffprobe failure: report, do not crash
        raise PublishConflict(f"cannot read final.mp4: {str(e)[:200]}") from None
    item = PublishItem(job_id=job_id, clip=clip, lang=lang, video=final, size=final.stat().st_size,
                       duration=round(duration, 3), width=width, height=height,
                       sha256=records.file_sha256(final), metadata=meta,
                       rel_path=f"clips/{clip:02d}/{lang}/final.mp4")
    from .cover import item_cover  # G7

    item.cover = item_cover(job_id, clip, lang, item.sha256, item.duration)
    return item, extra


def platform_problems(item: PublishItem, ps: PublishSettings, platform: str, extra: list[str],
                      publisher: Any = None) -> tuple[list[str], Any, Any]:
    """(problems, target, account) for one platform; no network."""
    target = ps.target(item.lang, platform)
    acc = accounts.account(item.lang, platform)
    pub = publisher or make_publisher(platform, ps, http=_offline_client())
    problems = list(pub.preflight(item, target, acc)) + list(extra)
    for p in accounts.mapping_problems(ps, item.lang, platform, acc):
        if p not in problems:
            problems.append(p)
    return problems, target, acc


def publish_view(job_id: str, clip: int, lang: str, settings: Settings, *, active: dict | None) -> dict:
    _check_clip(job_id, clip, lang)
    ps = settings.publish
    final = _final(job_id, clip, lang)
    item: PublishItem | None = None
    extra: list[str] = []
    item_error = None
    if final is not None:
        try:
            item, extra = load_item(job_id, clip, lang)
        except PublishConflict as e:
            item_error = e.message
    active_platforms = set()
    if active and active.get("state") in ("queued", "running"):
        active_platforms = set(active.get("platforms") or [])
    platforms: dict[str, Any] = {}
    for p in PLATFORMS:
        target = ps.target(lang, p)
        acc = accounts.account(lang, p)
        raw = records.read_status_raw(job_id, clip, lang, p)
        if item is not None:
            problems, _, _ = platform_problems(item, ps, p, extra)
        else:
            problems = [item_error or "clip has no final render yet"]
        outdated = bool(records.is_published(raw) and item is not None
                        and raw.get("video_sha256") != item.sha256)
        platforms[p] = {
            "enabled": bool(target.enabled),
            "connected": acc is not None,
            "account": acc.public if acc is not None else None,
            "plan": records.read_plan(job_id, clip, lang, p),
            "status": records.public_status(raw, active=p in active_platforms),
            "published_render_outdated": outdated,
            "problems": problems,
        }
    return {"dry_run": bool(ps.dry_run), "final": final is not None,
            "final_sha256": item.sha256 if item is not None else None,
            "task": active, "platforms": platforms}


def build_plans(item: PublishItem, extra: list[str], platforms: list[str], ps: PublishSettings) -> dict[str, dict]:
    plans = {}
    offline = _offline_client()
    for p in platforms:
        pub = make_publisher(p, ps, http=offline)
        target = ps.target(item.lang, p)
        acc = accounts.account(item.lang, p)
        plan = pub.plan(item, target, acc)
        for x in list(extra) + accounts.mapping_problems(ps, item.lang, p, acc):
            if x not in plan["problems"]:
                plan["problems"].append(x)
        plans[p] = plan
    return plans


def dry_run(job_id: str, clip: int, lang: str, platforms: list[str], settings: Settings) -> dict:
    platforms = check_platforms(platforms)
    item, extra = load_item(job_id, clip, lang)
    plans = build_plans(item, extra, platforms, settings.publish)
    for p, plan in plans.items():
        records.write_plan(job_id, clip, lang, p, plan)
    return {"dry_run": True, "plans": plans}


def check_live(job_id: str, clip: int, lang: str, platforms: list[str], settings: Settings, *,
               force: bool) -> tuple[list[str], dict]:
    platforms = check_platforms(platforms)
    item, extra = load_item(job_id, clip, lang)
    ps = settings.publish
    to_run: list[str] = []
    already: dict[str, dict] = {}
    errors: dict[str, list[str]] = {}
    for p in platforms:
        raw = records.read_status_raw(job_id, clip, lang, p)
        if records.is_published(raw) and not force:
            already[p] = records.public_status(raw)  # type: ignore[assignment]
            continue
        problems, _, _ = platform_problems(item, ps, p, extra)
        if ps.dry_run:
            problems.insert(0, "publishing is in dry-run mode - turn dry run off on the Accounts page")
        if problems:
            errors[p] = problems
        else:
            to_run.append(p)
    if errors:
        raise PublishConflict("cannot publish", errors)
    return to_run, already


def lang_name(lang: str) -> str:
    return LANG_NAMES.get(lang, lang)
