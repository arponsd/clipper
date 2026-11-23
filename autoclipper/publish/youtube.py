"""YouTube Shorts via the YouTube Data API v3 `videos.insert` resumable upload (CONTRACT_STEP4 2.1)."""
from __future__ import annotations

import re
import time
from typing import Any

import httpx

from . import oauth_google
from .accounts import google_client
from .base import Http, Progress, PublishError, PublishItem, Sleep, StatusWriter, classify_google, file_blocks
from .common import common_problems, mb, plan_skeleton
from .metadata import YT_TITLE_MAX, youtube_payload
from .secrets import remember

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SESSION_PREFIX = "https://www.googleapis.com/"
MAX_BYTES = 256 * 1024 ** 3
MAX_SHORT_SECONDS = 180.0
QUANTUM = 256 * 1024
_RANGE = re.compile(r"bytes=(\d+)-(\d+)")


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/shorts/{video_id}"


class YouTubePublisher:
    platform = "youtube"
    api_version = "v3"

    def __init__(self, settings: Any, *, http: httpx.Client | None = None, sleep: Sleep = time.sleep):
        self.ps = settings
        self.sleep = sleep
        self.http = Http(http, "youtube", max_retries=settings.max_retries, sleep=sleep, classify=classify_google)

    # ---- no network ---------------------------------------------------------------------------
    @property
    def chunk_size(self) -> int:
        size = int(self.ps.chunk_mb) * 1024 * 1024
        assert size % QUANTUM == 0
        return size

    def start_url(self, target: Any) -> str:
        notify = "true" if getattr(target, "notify_subscribers", False) else "false"
        return f"{UPLOAD_URL}?uploadType=resumable&part=snippet,status&notifySubscribers={notify}"

    def preflight(self, item: PublishItem, target: Any, account: dict | None) -> list[str]:
        problems = common_problems(item, target, account, self.platform, getattr(target, "channel_id", ""))
        if item.size <= 0:
            problems.append("final.mp4 is empty")
        if item.size > MAX_BYTES:
            problems.append(f"file is {mb(item.size)}; YouTube accepts at most 256 GB")
        if item.duration > MAX_SHORT_SECONDS:
            problems.append(f"duration {item.duration:.1f} s > {MAX_SHORT_SECONDS:.0f} s (YouTube Shorts limit)")
        if item.width > item.height:
            problems.append(f"video is {item.width}x{item.height} (landscape); a Short must be vertical or square")
        return problems

    def warnings(self, item: PublishItem, target: Any, payload: dict) -> list[str]:
        w: list[str] = []
        privacy = getattr(target, "privacy", "private")
        if privacy == "private":
            w.append("privacy is private: only you will see it")
        elif privacy == "unlisted":
            w.append("privacy is unlisted: anyone with the link can see it")
        else:
            w.append("privacy is public: everyone can see it")
        title = str(item.metadata.get("title") or "")
        if len(title) > YT_TITLE_MAX:
            w.append(f"title shortened to {YT_TITLE_MAX} characters")
        n_tags = len([t for t in (item.metadata.get("hashtags") or []) if isinstance(t, str) and t.strip("# ")])
        if len(payload["snippet"]["tags"]) < n_tags:
            w.append("some hashtags dropped from tags (500 character limit)")
        return w

    def plan(self, item: PublishItem, target: Any, account: dict | None) -> dict:
        payload = youtube_payload(item.metadata, item.lang, target)
        chunk = self.chunk_size
        n_chunks = max(1, -(-item.size // chunk))
        last = item.size - chunk * (n_chunks - 1)
        requests = [
            {"step": "access_token", "method": "POST", "url": oauth_google.TOKEN_URL,
             "headers": {"Content-Type": "application/x-www-form-urlencoded"},
             "body": {"client_id": "***", "client_secret": "***", "grant_type": "refresh_token",
                      "refresh_token": "***"},
             "note": "returns a short-lived access token"},
            {"step": "start_session", "method": "POST", "url": self.start_url(target),
             "headers": {"Authorization": "Bearer ***", "Content-Type": "application/json; charset=UTF-8",
                         "X-Upload-Content-Length": str(item.size), "X-Upload-Content-Type": "video/mp4"},
             "body": payload, "note": "returns Location: <session URI>"},
            {"step": "upload", "method": "PUT", "url": "<session URI>",
             "headers": {"Authorization": "Bearer ***", "Content-Type": "video/mp4",
                         "Content-Length": str(min(chunk, item.size)),
                         "Content-Range": f"bytes 0-{min(chunk, item.size) - 1}/{item.size}"},
             "body": None,
             "note": f"{n_chunks} chunk(s) of {chunk} bytes (last {last}); 308 + Range until the last chunk, "
                     "then 200/201 with the video id"},
        ]
        limits = {"max_duration": MAX_SHORT_SECONDS, "max_bytes": MAX_BYTES, "rate": "100 uploads/day (quota)"}
        return plan_skeleton(item, self.platform, self.api_version, account, payload, requests, limits,
                             self.preflight(item, target, account), self.warnings(item, target, payload))

    # ---- network ------------------------------------------------------------------------------
    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _start(self, item: PublishItem, target: Any, payload: dict, token: str) -> str:
        resp = self.http.request("POST", self.start_url(target), json=payload, headers={
            **self._auth(token), "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(item.size), "X-Upload-Content-Type": "video/mp4"}, ok=(200, 201))
        loc = resp.headers.get("Location") or ""
        if not loc.startswith(SESSION_PREFIX):
            raise PublishError("YouTube did not return an upload session", kind="unknown", platform=self.platform)
        remember(loc)
        return loc

    def _video_from(self, resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict) or not data.get("id"):
            raise PublishError("YouTube finished the upload but returned no video id", kind="unknown",
                               platform=self.platform)
        return data

    def _probe(self, session: str, total: int, token: str) -> tuple[str, Any]:
        """-> ("done", video) | ("offset", next_byte) | ("expired", None)."""
        attempt = 0
        while True:
            try:
                resp = self.http.send_once("PUT", session, headers={
                    **self._auth(token), "Content-Length": "0", "Content-Range": f"bytes */{total}"})
            except PublishError as e:
                if attempt >= self.http.max_retries:
                    raise
                self.http.backoff(attempt, e)
                attempt += 1
                continue
            if resp.status_code in (200, 201):
                return "done", self._video_from(resp)
            if resp.status_code == 308:
                m = _RANGE.match(resp.headers.get("Range", ""))
                return "offset", (int(m.group(2)) + 1 if m else 0)
            if resp.status_code in (404, 410):
                return "expired", None
            err = classify_google(resp, self.platform)
            if err.transient and attempt < self.http.max_retries:
                self.http.backoff(attempt, err)
                attempt += 1
                continue
            raise err

    def publish(self, item: PublishItem, target: Any, account: dict, *, status: StatusWriter,
                on_progress: Progress) -> dict:
        client = google_client()
        if client is None:
            raise PublishError("Google OAuth client not configured - upload client_secret.json on the Accounts page",
                               kind="auth", platform=self.platform)
        token = oauth_google.refresh_access_token(self.http, client, account["refresh_token"])
        payload = youtube_payload(item.metadata, item.lang, target)
        total = item.size
        session: str | None = None
        offset = 0
        video: dict | None = None
        resume = status.load_resume()
        if resume and resume.get("sha256") == item.sha256 and isinstance(resume.get("session_uri"), str) \
                and resume["session_uri"].startswith(SESSION_PREFIX):
            session = resume["session_uri"]
            remember(session)
            on_progress(0.0, "resuming the interrupted upload")
            kind, value = self._probe(session, total, token)
            if kind == "done":
                video = value
            elif kind == "offset":
                offset = value
            else:
                session = None
        restarts = 0
        if video is None and session is None:
            session = self._start(item, target, payload, token)
            status.save_resume({"session_uri": session, "sha256": item.sha256, "created": time.time()})
        chunk = self.chunk_size
        failures = 0
        refreshed = False
        while video is None:
            assert session is not None
            end = min(offset + chunk, total)
            length = end - offset
            on_progress(offset / total if total else 0.0, f"uploading {int(100 * offset / total) if total else 0}%")
            try:
                resp = self.http.send_once("PUT", session, content=file_blocks(item.video, offset, length),
                                           headers={**self._auth(token), "Content-Type": "video/mp4",
                                                    "Content-Length": str(length),
                                                    "Content-Range": f"bytes {offset}-{end - 1}/{total}"})
            except PublishError as e:
                failures += 1
                if failures > self.http.max_retries:
                    raise
                self.http.backoff(failures - 1, e)
                kind, value = self._probe(session, total, token)
                if kind == "done":
                    video = value
                elif kind == "offset":
                    offset = value
                else:
                    session, offset = None, 0
                if session is None:
                    session = self._new_session(item, target, payload, token, status, restarts)
                    restarts += 1
                continue
            code = resp.status_code
            if code in (200, 201):
                video = self._video_from(resp)
                break
            if code == 308:
                m = _RANGE.match(resp.headers.get("Range", ""))
                offset = int(m.group(2)) + 1 if m else 0
                failures = 0
                continue
            if code in (404, 410):  # session expired: start over with a new one
                session = self._new_session(item, target, payload, token, status, restarts)
                restarts += 1
                offset = 0
                continue
            if code == 401 and not refreshed:
                token = oauth_google.refresh_access_token(self.http, client, account["refresh_token"])
                refreshed = True
                continue
            err = classify_google(resp, self.platform)
            if err.transient:
                failures += 1
                if failures > self.http.max_retries:
                    raise err
                self.http.backoff(failures - 1, err)
                kind, value = self._probe(session, total, token)
                if kind == "done":
                    video = value
                elif kind == "offset":
                    offset = value
                else:
                    session = self._new_session(item, target, payload, token, status, restarts)
                    restarts += 1
                    offset = 0
                continue
            raise err
        vid = str(video["id"])
        on_progress(1.0, "uploaded; YouTube is processing it")
        return {"remote_id": vid, "url": watch_url(vid)}

    def _new_session(self, item: PublishItem, target: Any, payload: dict, token: str, status: StatusWriter,
                     restarts: int) -> str:
        if restarts >= 2:
            raise PublishError("YouTube upload session kept expiring; try again later", kind="transient",
                               platform=self.platform)
        session = self._start(item, target, payload, token)
        status.save_resume({"session_uri": session, "sha256": item.sha256, "created": time.time()})
        return session
