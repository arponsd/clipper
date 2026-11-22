"""Local-app auth (CONTRACT_WEB section 2): per-install token, Host guard, Origin guard.

The guard is a pure ASGI middleware (no request-body buffering, so uploads still stream). Order on every
HTTP request: Host (403) -> Origin / Sec-Fetch-Site on writes (403) -> X-Auth-Token on /api/* (401/403).
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

from ..config import data_dir
from .errors import error_response

TOKEN_HEADER = "X-Auth-Token"
DEFAULT_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
MIN_TOKEN_LEN = 32
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# The two media GETs (a <video src> / download link cannot send headers) also accept ?token=.
MEDIA_PATH = re.compile(r"^/api/jobs/[^/]+/clips/[^/]+/[^/]+/(video|download)$")


def token_path() -> Path:
    return data_dir() / "web" / "token"


def load_or_create_token() -> str:
    p = token_path()
    try:
        existing = p.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if len(existing) >= MIN_TOKEN_LEN:
        return existing
    p.parent.mkdir(parents=True, exist_ok=True)
    p.unlink(missing_ok=True)  # empty / short token file: regenerate
    token = secrets.token_urlsafe(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    try:
        os.chmod(p, 0o600)
    except OSError:  # pragma: no cover - Windows
        pass
    return token


def check_token(given: str | None, expected: str) -> bool:
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _host_only(host_header: str) -> str:
    h = host_header.strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[: end + 1] if end != -1 else h
    if h.count(":") == 1:
        return h.split(":", 1)[0]
    return h  # bare IPv6 (no port possible) or plain name


def host_allowed(host_header: str | None, allowed: set[str]) -> bool:
    if not host_header:
        return False
    return _host_only(host_header) in {a.lower() for a in allowed}


def origin_allowed(origin: str | None, allowed: set[str]) -> bool:
    if origin is None:
        return True
    origin = origin.strip()
    if not origin or origin.lower() == "null":
        return False
    try:
        parts = urlsplit(origin)
        hostname = parts.hostname
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not hostname:
        return False
    names = {a.lower().strip("[]") for a in allowed}
    return hostname.lower().strip("[]") in names


def allowed_hosts_for(bind_host: str | None) -> set[str]:
    """DEFAULT_ALLOWED_HOSTS plus the --host value when it is a concrete address."""
    hosts = set(DEFAULT_ALLOWED_HOSTS)
    if bind_host and bind_host not in ("0.0.0.0", "::", "[::]", ""):
        h = bind_host.lower()
        hosts.add(h)
        try:
            if ipaddress.ip_address(h.strip("[]")).version == 6:
                hosts.add(f"[{h.strip('[]')}]")
                hosts.add(h.strip("[]"))
        except ValueError:
            pass
    return hosts


def is_loopback(host: str) -> bool:
    h = host.strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


ASGIApp = Callable[[dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]]


class GuardMiddleware:
    """Host guard, CSRF guard and token check for every HTTP request."""

    def __init__(self, app: ASGIApp, *, token: str, allowed_hosts: set[str]):
        self.app = app
        self.token = token
        self.allowed_hosts = set(allowed_hosts)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers: dict[str, str] = {}
        for k, v in scope.get("headers") or []:
            headers.setdefault(k.decode("latin-1").lower(), v.decode("latin-1"))
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")

        rejection = self._reject(method, path, headers, scope.get("query_string", b""))
        if rejection is not None:
            status, message = rejection
            await error_response(status, message)(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _reject(self, method: str, path: str, headers: dict[str, str], query: bytes) -> tuple[int, str] | None:
        if not host_allowed(headers.get("host"), self.allowed_hosts):
            return 403, "forbidden host"
        if method in WRITE_METHODS:
            if not origin_allowed(headers.get("origin"), self.allowed_hosts):
                return 403, "cross-origin request rejected"
            if headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
                return 403, "cross-origin request rejected"
        if path == "/api" or path.startswith("/api/"):
            given = headers.get(TOKEN_HEADER.lower(), "").strip()
            if not given and method in ("GET", "HEAD") and MEDIA_PATH.match(path):
                values = parse_qs(query.decode("latin-1")).get("token") or [""]
                given = values[0].strip()
            if not given:
                return 401, "missing auth token"
            if not check_token(given, self.token):
                return 403, "invalid auth token"
        return None


# ---- access log redaction (UX-5) ----------------------------------------------------------------
_TOKEN_IN_TEXT = re.compile(r"token=[^&\s\"]+")


def redact_token(value: Any) -> Any:
    """`token=<anything>` -> `token=***` in a string; other values are returned unchanged."""
    if isinstance(value, str):
        return _TOKEN_IN_TEXT.sub("token=***", value)
    return value


class TokenRedactFilter(logging.Filter):
    """Removes ?token= values from log records (uvicorn's access log prints the full request path)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_token(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_token(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact_token(v) for k, v in record.args.items()}
        return True


def install_access_log_filter(logger_name: str = "uvicorn.access") -> TokenRedactFilter:
    logger = logging.getLogger(logger_name)
    for f in logger.filters:
        if isinstance(f, TokenRedactFilter):
            return f
    f = TokenRedactFilter()
    logger.addFilter(f)
    return f
