"""Shared publisher types, error mapping and the retrying HTTP helper (CONTRACT_STEP4 section 7)."""
from __future__ import annotations

import email.utils
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

import httpx

from . import PLATFORM_NAMES
from .secrets import redact

Sleep = Callable[[float], None]
Progress = Callable[[float, str], None]

ERROR_KINDS = ("auth", "quota", "rate_limit", "invalid", "unsupported_video", "transient", "processing",
               "network", "unknown")
TRANSIENT_STATUS = (500, 502, 503, 504)
META_RATE_CODES = {4, 17, 32, 613, 80001, 80002}
READ_BLOCK = 1024 * 1024


def default_timeout() -> httpx.Timeout:
    return httpx.Timeout(60.0, connect=15.0)


def new_client() -> httpx.Client:
    return httpx.Client(timeout=default_timeout(), follow_redirects=False)


class PublishError(RuntimeError):
    """A platform request failed. The message is already redacted and meant for the user."""

    def __init__(self, message: str, *, kind: str = "unknown", platform: str = "", code: str | None = None,
                 retry_after: float | None = None, transient: bool | None = None):
        msg = redact(str(message))
        super().__init__(msg)
        self.message = msg
        self.kind = kind if kind in ERROR_KINDS else "unknown"
        self.platform = platform
        self.code = None if code is None else redact(str(code))[:80]
        self.retry_after = retry_after
        self.transient = (kind in ("transient", "network")) if transient is None else transient

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "code": self.code, "retry_after": self.retry_after}


@dataclass
class PublishItem:
    """What gets posted: one clip-lang's final.mp4 and its metadata.json."""
    job_id: str
    clip: int
    lang: str
    video: Path
    size: int
    duration: float
    width: int
    height: int
    sha256: str
    metadata: dict
    rel_path: str = ""


class StatusWriter(Protocol):
    def update(self, **fields: Any) -> dict: ...

    def save_resume(self, data: dict | None) -> None: ...

    def load_resume(self) -> dict | None: ...


class Publisher(Protocol):
    platform: str

    def plan(self, item: PublishItem, target: Any, account: dict | None) -> dict: ...

    def preflight(self, item: PublishItem, target: Any, account: dict | None) -> list[str]: ...

    def publish(self, item: PublishItem, target: Any, account: dict, *, status: StatusWriter,
                on_progress: Progress) -> dict: ...


def make_publisher(platform: str, settings: Any, *, http: httpx.Client | None = None,
                   sleep: Sleep = time.sleep) -> Publisher:
    """`settings` = PublishSettings. `http` = the client used for every request (tests: MockTransport)."""
    if platform == "youtube":
        from .youtube import YouTubePublisher
        return YouTubePublisher(settings, http=http, sleep=sleep)
    if platform == "facebook":
        from .facebook import FacebookPublisher
        return FacebookPublisher(settings, http=http, sleep=sleep)
    if platform == "instagram":
        from .instagram import InstagramPublisher
        return InstagramPublisher(settings, http=http, sleep=sleep)
    raise ValueError(f"unknown platform {platform!r}")


# ---------------------------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------------------------
def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except (ValueError, UnicodeDecodeError):
        return None


def retry_after_seconds(resp: httpx.Response | None) -> float | None:
    if resp is None:
        return None
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def _auth_msg(platform: str) -> str:
    return f"{PLATFORM_NAMES.get(platform, platform)} login expired or was revoked - reconnect on the Accounts page"


def classify_google(resp: httpx.Response, platform: str = "youtube") -> PublishError:
    data = _json(resp)
    status = resp.status_code
    reason = None
    message = ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, str):  # oauth2 token endpoint: {"error": "invalid_grant", "error_description": ...}
            reason = err
            message = str(data.get("error_description") or err)
        elif isinstance(err, dict):
            message = str(err.get("message") or "")
            errs = err.get("errors")
            if isinstance(errs, list) and errs and isinstance(errs[0], dict):
                reason = errs[0].get("reason")
            if not reason:
                st = err.get("status")
                reason = st if isinstance(st, str) else None
    reason = str(reason) if reason else None
    ra = retry_after_seconds(resp)
    name = PLATFORM_NAMES.get(platform, platform)
    if status == 401 or reason in ("invalid_grant", "unauthorized_client", "authError", "invalid_client"):
        return PublishError(_auth_msg(platform), kind="auth", platform=platform, code=reason or str(status))
    if reason == "uploadLimitExceeded":  # the channel's own upload cap, not the project's API quota
        return PublishError("this YouTube channel has reached YouTube's upload limit for now; try again later "
                            "(this is a channel limit, not the API quota)", kind="quota", platform=platform,
                            code=reason, retry_after=ra)
    if reason in ("quotaExceeded", "dailyLimitExceeded"):
        return PublishError("YouTube daily upload quota used up; it resets at midnight Pacific time",
                            kind="quota", platform=platform, code=reason, retry_after=ra)
    if status == 429 or reason in ("rateLimitExceeded", "userRateLimitExceeded"):
        return PublishError(f"{name} is rate limiting uploads; wait a few minutes and try again",
                            kind="rate_limit", platform=platform, code=reason or "429", retry_after=ra)
    if status in TRANSIENT_STATUS:
        return PublishError(f"{name} server error {status}; it will be retried", kind="transient",
                            platform=platform, code=str(status), retry_after=ra)
    if reason == "forbiddenPrivacySetting":
        return PublishError("YouTube refused the privacy setting for this channel (forbiddenPrivacySetting)",
                            kind="invalid", platform=platform, code=reason)
    text = message or resp.reason_phrase or "request rejected"
    return PublishError(f"{name} rejected the request ({status}{', ' + reason if reason else ''}): {text[:300]}",
                        kind="invalid", platform=platform, code=reason or str(status))


def classify_meta(resp: httpx.Response, platform: str) -> PublishError:
    data = _json(resp)
    status = resp.status_code
    name = PLATFORM_NAMES.get(platform, platform)
    err: dict = {}
    if isinstance(data, dict):
        e = data.get("error")
        if isinstance(e, dict):
            err = e
        elif isinstance(data.get("debug_info"), dict):  # rupload errors
            err = {"message": data["debug_info"].get("message"), "type": data["debug_info"].get("type")}
    code = err.get("code")
    sub = err.get("error_subcode")
    try:
        code_i = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_i = None
    try:
        sub_i = int(sub) if sub is not None else None
    except (TypeError, ValueError):
        sub_i = None
    message = str(err.get("error_user_msg") or err.get("message") or resp.reason_phrase or "request rejected")
    code_s = str(code_i) if code_i is not None else (str(status) if status else None)
    ra = retry_after_seconds(resp)
    if code_i == 190 or status == 401:
        return PublishError(_auth_msg(platform), kind="auth", platform=platform, code=code_s or "190")
    if code_i in (10, 200) or (code_i is not None and 200 <= code_i <= 299):
        return PublishError(f"{name}: permission missing ({message[:200]}) - check the token's permissions",
                            kind="auth", platform=platform, code=code_s)
    if sub_i == 2207042:
        return PublishError("Instagram: the account has reached its limit of API-published posts in 24 h; "
                            "try again tomorrow", kind="rate_limit", platform=platform, code="2207042",
                            retry_after=ra)
    if sub_i == 2207026:
        return PublishError("Instagram does not support this video format (2207026)", kind="unsupported_video",
                            platform=platform, code="2207026")
    if code_i in META_RATE_CODES or status == 429:
        return PublishError(f"{name} is rate limiting this app/Page (code {code_s}); wait and try again later",
                            kind="rate_limit", platform=platform, code=code_s, retry_after=ra)
    if err.get("is_transient") is True or status in TRANSIENT_STATUS or code_i in (1, 2):
        return PublishError(f"{name} temporary error ({code_s}): {message[:200]}", kind="transient",
                            platform=platform, code=code_s, retry_after=ra)
    return PublishError(f"{name} rejected the request ({code_s}): {message[:300]}", kind="invalid",
                        platform=platform, code=code_s)


# ---------------------------------------------------------------------------------------------
# HTTP with retries
# ---------------------------------------------------------------------------------------------
class Http:
    """Retrying request helper bound to one platform. Every exception it raises is a PublishError."""

    def __init__(self, client: httpx.Client | None, platform: str, *, max_retries: int, sleep: Sleep,
                 classify: Callable[[httpx.Response], PublishError]):
        self.client = client if client is not None else new_client()
        self.platform = platform
        self.max_retries = max_retries
        self.sleep = sleep
        self.classify = classify

    def backoff(self, attempt: int, err: PublishError | None = None) -> None:
        wait = float(2 ** attempt)
        if err is not None and err.retry_after is not None:
            wait = max(wait, min(err.retry_after, 300.0))
        self.sleep(wait)

    def send_once(self, method: str, url: str, **kw: Any) -> httpx.Response:
        try:
            return self.client.request(method, url, **kw)
        except httpx.TimeoutException as e:
            raise PublishError(f"{PLATFORM_NAMES.get(self.platform)}: request timed out ({type(e).__name__})",
                               kind="network", platform=self.platform) from None
        except httpx.HTTPError as e:
            raise PublishError(f"{PLATFORM_NAMES.get(self.platform)} unreachable ({type(e).__name__}: "
                               f"{redact(str(e))[:200]})", kind="network", platform=self.platform) from None

    def request(self, method: str, url: str, *, ok: tuple[int, ...] = (200, 201),
                content_factory: Callable[[], Any] | None = None, retries: int | None = None,
                **kw: Any) -> httpx.Response:
        """Send with up to `max_retries` retries on network errors / 5xx / transient errors.
        `retries=0` = exactly one attempt: used for the non-idempotent final POST (IG media_publish, FB
        finish), where a retry after a lost success response would fail or post twice."""
        max_retries = self.max_retries if retries is None else retries
        attempt = 0
        while True:
            if content_factory is not None:
                kw["content"] = content_factory()
            try:
                resp = self.send_once(method, url, **kw)
            except PublishError as e:
                if attempt >= max_retries:
                    raise
                self.backoff(attempt, e)
                attempt += 1
                continue
            if resp.status_code in ok:
                return resp
            err = self.classify(resp)
            if err.transient and attempt < max_retries:
                self.backoff(attempt, err)
                attempt += 1
                continue
            raise err

    def json(self, method: str, url: str, **kw: Any) -> dict:
        resp = self.request(method, url, **kw)
        data = _json(resp)
        if not isinstance(data, dict):
            raise PublishError(f"{PLATFORM_NAMES.get(self.platform)} returned an unexpected response",
                               kind="unknown", platform=self.platform)
        return data


def file_blocks(path: Path, start: int = 0, length: int | None = None,
                block: int = READ_BLOCK) -> Iterator[bytes]:
    """Stream part of a file (never the whole file into RAM)."""
    with open(path, "rb") as f:
        f.seek(start)
        left = length
        while left is None or left > 0:
            n = block if left is None else min(block, left)
            data = f.read(n)
            if not data:
                return
            if left is not None:
                left -= len(data)
            yield data


def mask_headers(headers: dict[str, str]) -> dict[str, str]:
    out = {}
    for k, v in headers.items():
        if k.lower() == "authorization":
            scheme = v.split(" ", 1)[0] if " " in v else "Bearer"
            out[k] = f"{scheme} ***"
        else:
            out[k] = v
    return out
