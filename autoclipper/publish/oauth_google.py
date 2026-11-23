"""Google OAuth for installed apps (loopback redirect + PKCE), token refresh and revoke.

Docs: developers.google.com/identity/protocols/oauth2/native-app. The client is the user's own Desktop-app
client (`client_secret.json`). Pending states live only in this process (TTL 600 s, max 10, single use); the
PKCE verifier never leaves it.
"""
from __future__ import annotations

import base64
import hashlib
import html
import secrets as _rnd
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
SCOPES = ("https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly")
STATE_TTL = 600.0
MAX_PENDING = 10
_UNRESERVED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


def make_verifier(n: int = 64) -> str:
    if not 43 <= n <= 128:
        raise ValueError("PKCE verifier length must be 43..128")
    return "".join(_rnd.choice(_UNRESERVED) for _ in range(n))


def challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass
class Pending:
    lang: str
    redirect_uri: str
    verifier: str = field(repr=False)
    created: float


class PendingStates:
    def __init__(self, ttl: float = STATE_TTL, max_items: int = MAX_PENDING,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl, self.max_items, self.clock = ttl, max_items, clock
        self._items: dict[str, Pending] = {}
        self._lock = threading.Lock()

    def _purge(self) -> None:
        now = self.clock()
        for k in [k for k, v in self._items.items() if now - v.created > self.ttl]:
            del self._items[k]

    def add(self, lang: str, redirect_uri: str, verifier: str) -> str:
        state = _rnd.token_urlsafe(32)
        with self._lock:
            self._purge()
            while len(self._items) >= self.max_items:  # drop the oldest
                oldest = min(self._items, key=lambda k: self._items[k].created)
                del self._items[oldest]
            self._items[state] = Pending(lang, redirect_uri, verifier, self.clock())
        return state

    def pop(self, state: str | None) -> Pending | None:
        if not isinstance(state, str) or not state:
            return None
        with self._lock:
            self._purge()
            return self._items.pop(state, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            self._purge()
            return len(self._items)


PENDING = PendingStates()


def auth_url(client_id: str, redirect_uri: str, state: str, verifier: str) -> str:
    q = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge(verifier),
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent select_account",
        "include_granted_scopes": "false",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def is_loopback_redirect(uri: str) -> bool:
    try:
        u = urllib.parse.urlsplit(uri)
    except ValueError:
        return False
    return (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1") and not u.username
            and not u.query and not u.fragment)


# ---- token endpoint helpers (all raise PublishError; callers map to their own errors) -----------
def exchange_code(http_: Any, client: dict, code: str, verifier: str, redirect_uri: str) -> dict:
    return http_.json("POST", TOKEN_URL, data={
        "client_id": client["client_id"], "client_secret": client["client_secret"], "code": code,
        "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect_uri})


def refresh_access_token(http_: Any, client: dict, refresh_token: str) -> str:
    data = http_.json("POST", TOKEN_URL, data={
        "client_id": client["client_id"], "client_secret": client["client_secret"],
        "grant_type": "refresh_token", "refresh_token": refresh_token})
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        from .base import PublishError
        raise PublishError("Google returned no access token - reconnect YouTube", kind="auth", platform="youtube")
    from .secrets import remember
    remember(token)
    return token


def my_channel(http_: Any, access_token: str) -> dict:
    data = http_.json("GET", CHANNELS_URL, params={"part": "snippet", "mine": "true"},
                      headers={"Authorization": f"Bearer {access_token}"})
    items = data.get("items") or []
    if not items or not isinstance(items[0], dict) or not items[0].get("id"):
        from .base import PublishError
        raise PublishError("this Google account has no YouTube channel - create one or pick the brand "
                           "account in Google's chooser", kind="invalid", platform="youtube")
    ch = items[0]
    return {"id": ch["id"], "title": ((ch.get("snippet") or {}).get("title") or ch["id"])}


def revoke(client: httpx.Client, token: str) -> bool:
    try:
        r = client.post(REVOKE_URL, data={"token": token},
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        return r.status_code == 200
    except httpx.HTTPError:
        return False


# ---- CLI loopback flow ------------------------------------------------------------------------
_PAGE = ("<!doctype html><meta charset='utf-8'><title>Auto Clipper</title>"
         "<body style='font-family:sans-serif;padding:2em'><p>{msg}</p></body>")


def run_loopback_flow(lang: str, *, open_browser: bool = True, timeout: float = 300,
                      http: httpx.Client | None = None, announce: Callable[[str], None] = print) -> dict:
    """CLI connect: one-shot http.server on 127.0.0.1:<random port>, path `/`, then complete the connect."""
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from . import service

    result: dict[str, Any] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            u = urllib.parse.urlsplit(self.path)
            if u.path != "/":
                self.send_response(404)
                self.end_headers()
                return
            q = urllib.parse.parse_qs(u.query)
            st = (q.get("state") or [None])[0]
            code = (q.get("code") or [None])[0]
            err = (q.get("error") or [None])[0]
            try:
                acc = service.complete_youtube_connect(st, code, err, http=http)
                result["account"] = acc
                msg = f"Connected {html.escape(str(acc.get('account_name')))} - you can close this tab."
                code_ = 200
            except Exception as e:  # noqa: BLE001 - shown to the user, already redacted
                result["error"] = e
                msg = html.escape(str(e)) or "connect failed"
                code_ = 400
            body = _PAGE.format(msg=msg).encode("utf-8")
            self.send_response(code_)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *_a: Any) -> None:  # the query holds the auth code: never log it
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    try:
        begin = service.begin_youtube_connect(lang, f"http://127.0.0.1:{port}/")
        announce("Open this URL in a browser, sign in and pick the channel for "
                 f"{'Hindi' if lang == 'hi' else 'English'}:\n{begin['auth_url']}")
        if open_browser:
            try:
                webbrowser.open(begin["auth_url"])
            except Exception:  # noqa: BLE001
                pass
        server.timeout = 1.0
        deadline = time.monotonic() + timeout
        while not done.is_set() and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "account" in result:
        return result["account"]
    if "error" in result:
        raise result["error"]
    raise TimeoutError(f"no answer from Google within {int(timeout)} s")
