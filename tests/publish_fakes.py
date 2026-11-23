"""Fakes for step 4 publishing (owned by the core builder; web tests import, do not edit - ask in TEAM_LOG).

Nothing here touches the network: FakeGoogle / FakeGraph are `httpx.MockTransport` handlers that record
every request. Use:

    from publish_fakes import FakeNet, make_job, connect_all, fake_client
    net = FakeNet()                      # google + graph in one handler
    http = net.client()                  # httpx.Client(transport=MockTransport(net))
    job_id = make_job()                  # job with clip 1 en+hi, real tiny 1080x1920 final.mp4 + metadata.json
    connect_all(net)                     # writes google client + youtube/meta secrets for en and hi, maps settings

Token constants below are fake values; tests grep responses/logs/files for them (none must appear).
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from autoclipper.config import data_dir

GRAPH_VERSION = "v27.0"
CLIENT_ID = "123456789012-fakeclientid.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-FAKE-CLIENT-SECRET-0123456789"
ACCESS_TOKEN = "ya29.FAKE-ACCESS-TOKEN-0123456789abcdef"
REFRESH_TOKEN = {"en": "1//FAKE-REFRESH-TOKEN-EN-0123456789", "hi": "1//FAKE-REFRESH-TOKEN-HI-0123456789"}
CHANNEL = {"en": ("UC" + "E" * 22, "English Channel"), "hi": ("UC" + "H" * 22, "हिंदी चैनल")}
PAGE = {"en": ("1110001", "English Page"), "hi": ("2220002", "हिंदी पेज")}
IG = {"en": ("17841400000000001", "english_ig"), "hi": ("17841400000000002", "hindi_ig")}
PAGE_TOKEN = {"en": "EAAFAKEPAGETOKENEN0123456789abcdef", "hi": "EAAFAKEPAGETOKENHI0123456789abcdef"}
IG_TOKEN = {"en": "IGQFAKEIGTOKENEN0123456789abcdef", "hi": "IGQFAKEIGTOKENHI0123456789abcdef"}
AUTH_CODE = "4/0FAKE-AUTH-CODE-0123456789"

ALL_SECRETS = [CLIENT_SECRET, ACCESS_TOKEN, AUTH_CODE, *REFRESH_TOKEN.values(), *PAGE_TOKEN.values(),
               *IG_TOKEN.values()]

SAMPLE_META = {
    "en": {"language": "en", "title": "Reagan's Challenger Speech Ending Still Gives Everyone Chills",
           "description": "Hours after the Space Shuttle Challenger disaster, President Reagan closed his address "
                          "with one of the most famous lines in presidential history.",
           "hashtags": ["#Challenger", "#RonaldReagan", "#History", "#NASA", "#Speech", "#1986", "#SpaceHistory"]},
    "hi": {"language": "hi", "title": "वो चार मिनट जिसने पूरे अमेरिका को रुला दिया",
           "description": "चैलेंजर हादसे के बाद राष्ट्रपति रीगन के भाषण का आखिरी हिस्सा, इतिहास की सबसे यादगार "
                          "लाइनों में से एक। सात अंतरिक्ष यात्रियों को एक भावुक श्रद्धांजलि।",
           "hashtags": ["#चैलेंजर", "#रीगन", "#इतिहास", "#अंतरिक्ष", "#नासा", "#प्रेरणा", "#भाषण"]},
}


def _json(status: int, data: Any, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=data, headers=headers or {})


def google_error(status: int, reason: str, message: str = "error") -> httpx.Response:
    return _json(status, {"error": {"code": status, "message": message,
                                    "errors": [{"reason": reason, "message": message}]}})


def meta_error(code: int, message: str = "error", subcode: int | None = None, status: int = 400,
               transient: bool = False) -> httpx.Response:
    err: dict[str, Any] = {"message": message, "type": "OAuthException", "code": code, "fbtrace_id": "AbC"}
    if subcode is not None:
        err["error_subcode"] = subcode
    if transient:
        err["is_transient"] = True
    return _json(status, {"error": err})


@dataclass
class Recorded:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes

    @property
    def form(self) -> dict[str, str]:
        try:
            return dict(urllib.parse.parse_qsl(self.body.decode("utf-8"), keep_blank_values=True))
        except UnicodeDecodeError:
            return {}

    @property
    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError:
            return None


class _Base:
    def __init__(self) -> None:
        self.requests: list[Recorded] = []
        # step name -> list of httpx.Response / Exception returned (in order) instead of the normal answer
        self.inject: dict[str, list[Any]] = {}
        # step name -> n: do the real work for the next n calls, then raise ReadTimeout (lost response)
        self.lose: dict[str, int] = {}

    def _take(self, step: str, request: httpx.Request) -> httpx.Response | None:
        q = self.inject.get(step)
        if q:
            x = q.pop(0)
            if isinstance(x, BaseException):
                raise x
            if callable(x):
                return x(request)
            return x
        return None

    def _lost(self, step: str, request: httpx.Request) -> None:
        if self.lose.get(step):
            self.lose[step] -= 1
            raise httpx.ReadTimeout("fake: response lost", request=request)

    def _record(self, request: httpx.Request) -> Recorded:
        body = request.read()
        r = Recorded(request.method, str(request.url), dict(request.headers), body)
        self.requests.append(r)
        return r

    def steps(self) -> list[str]:
        return [f"{r.method} {r.url}" for r in self.requests]


class FakeGoogle(_Base):
    """oauth2 token/revoke, youtube channels, resumable videos.insert (start, chunk PUT, probe)."""

    def __init__(self) -> None:
        super().__init__()
        self.sessions: dict[str, dict] = {}
        self.videos: list[dict] = []
        self.channel_for_code = CHANNEL["hi"]
        self.channel_for_token: tuple[str, str] | None = None
        self.next_video = 1
        self.refresh_tokens_issued: list[str] = []
        self.code_refresh_token = REFRESH_TOKEN["hi"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        rec = self._record(request)
        url = request.url
        host, path = url.host, url.path
        if host == "oauth2.googleapis.com" and path == "/token":
            form = rec.form
            step = "token_" + form.get("grant_type", "")
            hit = self._take(step, request) or self._take("token", request)
            if hit is not None:
                return hit
            if form.get("client_secret") != CLIENT_SECRET or form.get("client_id") != CLIENT_ID:
                return _json(401, {"error": "invalid_client", "error_description": "The OAuth client was not found."})
            if form.get("grant_type") == "refresh_token":
                if form.get("refresh_token") not in REFRESH_TOKEN.values():
                    return _json(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
                return _json(200, {"access_token": ACCESS_TOKEN, "expires_in": 3599, "token_type": "Bearer",
                                   "scope": "https://www.googleapis.com/auth/youtube.upload"})
            if form.get("grant_type") == "authorization_code":
                if form.get("code") != AUTH_CODE or not form.get("code_verifier"):
                    return _json(400, {"error": "invalid_grant", "error_description": "Bad Request"})
                return _json(200, {"access_token": ACCESS_TOKEN, "refresh_token": self.code_refresh_token,
                                   "expires_in": 3599, "token_type": "Bearer",
                                   "scope": "https://www.googleapis.com/auth/youtube.upload "
                                            "https://www.googleapis.com/auth/youtube.readonly"})
            return _json(400, {"error": "unsupported_grant_type"})
        if host == "oauth2.googleapis.com" and path == "/revoke":
            return self._take("revoke", request) or httpx.Response(200)
        if request.headers.get("Authorization") != f"Bearer {ACCESS_TOKEN}":
            return google_error(401, "authError", "Invalid Credentials")
        if host == "www.googleapis.com" and path == "/youtube/v3/channels":
            hit = self._take("channels", request)
            if hit is not None:
                return hit
            cid, title = self.channel_for_token or self.channel_for_code
            return _json(200, {"kind": "youtube#channelListResponse",
                               "items": [{"kind": "youtube#channel", "id": cid, "snippet": {"title": title}}]})
        if host == "www.googleapis.com" and path == "/upload/youtube/v3/videos" and request.method == "POST":
            hit = self._take("start", request)
            if hit is not None:
                return hit
            sid = f"S{len(self.sessions) + 1}"
            total = int(request.headers.get("X-Upload-Content-Length", "0"))
            self.sessions[sid] = {"total": total, "data": bytearray(), "meta": rec.json, "video": None,
                                  "expired": False}
            loc = f"https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id={sid}"
            return httpx.Response(200, headers={"Location": loc}, json={})
        if host == "www.googleapis.com" and path == "/upload/youtube/v3/videos" and request.method == "PUT":
            sid = url.params.get("upload_id", "")
            sess = self.sessions.get(sid)
            cr = request.headers.get("Content-Range", "")
            is_probe = cr.startswith("bytes */")
            hit = self._take("probe" if is_probe else "chunk", request)
            if hit is not None:
                return hit
            if sess is None or sess["expired"]:
                return google_error(404, "notFound", "upload session expired")
            if sess["video"] is not None:
                return _json(200, sess["video"])
            if not is_probe:
                m = re.match(r"bytes (\d+)-(\d+)/(\d+)", cr)
                assert m, cr
                first, last, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
                assert total == sess["total"], (total, sess["total"])
                assert len(rec.body) == last - first + 1 == int(request.headers["Content-Length"])
                if first == len(sess["data"]):
                    sess["data"].extend(rec.body)
            n = len(sess["data"])
            if n == sess["total"] and n > 0:
                vid = f"VID{self.next_video:08d}"
                self.next_video += 1
                sess["video"] = {"kind": "youtube#video", "id": vid, "snippet": (sess["meta"] or {}).get("snippet"),
                                 "status": {"uploadStatus": "uploaded",
                                            **((sess["meta"] or {}).get("status") or {})}}
                self.videos.append({"id": vid, "data": bytes(sess["data"]), "meta": sess["meta"]})
                return _json(201 if not is_probe else 200, sess["video"])
            headers = {"Range": f"bytes=0-{n - 1}"} if n else {}
            return httpx.Response(308, headers=headers)
        return _json(404, {"error": {"code": 404, "message": f"fake google: no route {request.method} {url}"}})


class FakeGraph(_Base):
    """Graph API v27.0 (Page/IG lookups, video_reels, IG media/media_publish/limits) + rupload."""

    def __init__(self, version: str = GRAPH_VERSION) -> None:
        super().__init__()
        self.v = version
        self.reels: dict[str, dict] = {}
        self.containers: dict[str, dict] = {}
        self.media: dict[str, dict] = {}
        self.next_id = 900000001
        self.fb_status_seq: list[str] = ["processing", "complete"]  # publishing_phase.status after finish
        self.ig_status_seq: list[str] = ["IN_PROGRESS", "FINISHED"]
        self.ig_quota_usage = 0
        self.linked_ig = {PAGE[l][0]: IG[l][0] for l in ("en", "hi")}
        self.page_names = {PAGE[l][0]: PAGE[l][1] for l in ("en", "hi")}
        self.ig_names = {IG[l][0]: IG[l][1] for l in ("en", "hi")}
        self.tokens = set(PAGE_TOKEN.values()) | set(IG_TOKEN.values())

    def _id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def _token(self, request: httpx.Request, scheme: str = "Bearer") -> str | None:
        h = request.headers.get("Authorization", "")
        if not h.startswith(scheme + " "):
            return None
        tok = h[len(scheme) + 1:]
        return tok if tok in self.tokens else None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        rec = self._record(request)
        url = request.url
        assert "access_token" not in str(url), "token in URL"
        parts = [p for p in url.path.split("/") if p]
        if url.host == "rupload.facebook.com":
            return self._rupload(request, rec, parts)
        if url.host != "graph.facebook.com":
            return _json(404, {"error": {"message": "no route", "code": 803}})
        assert parts and parts[0] == self.v, f"wrong Graph version in {url}"
        if self._token(request) is None:
            return meta_error(190, "Invalid OAuth access token - Cannot parse access token")
        node = parts[1] if len(parts) > 1 else ""
        edge = parts[2] if len(parts) > 2 else ""
        form = rec.form
        if edge == "video_reels" and request.method == "POST":
            phase = form.get("upload_phase")
            hit = self._take("fb_" + phase, request)
            if hit is not None:
                return hit
            if phase == "start":
                vid = self._id()
                self.reels[vid] = {"page": node, "data": bytearray(), "size": None, "finished": False,
                                   "polls": 0, "form": None}
                return _json(200, {"video_id": vid, "upload_url": f"https://rupload.facebook.com/video-upload/{self.v}/{vid}"})
            if phase == "finish":
                r = self.reels.get(form.get("video_id", ""))
                if r is None:
                    return meta_error(100, "Invalid video id")
                r["finished"] = True
                r["form"] = form
                self._lost("fb_finish", request)
                return _json(200, {"success": True})
            return meta_error(100, "bad upload_phase")
        if edge == "content_publishing_limit":
            hit = self._take("ig_limit", request)
            if hit is not None:
                return hit
            return _json(200, {"data": [{"quota_usage": self.ig_quota_usage,
                                         "config": {"quota_total": 100, "quota_duration": 86400}}]})
        if edge == "media" and request.method == "POST":
            hit = self._take("ig_container", request)
            if hit is not None:
                return hit
            assert form.get("media_type") == "REELS" and form.get("upload_type") == "resumable", form
            cid = self._id()
            self.containers[cid] = {"ig": node, "form": form, "data": b"", "polls": 0}
            return _json(200, {"id": cid, "uri": f"https://rupload.facebook.com/ig-api-upload/{self.v}/{cid}"})
        if edge == "media_publish":
            hit = self._take("ig_publish", request)
            if hit is not None:
                return hit
            c = self.containers.get(form.get("creation_id", ""))
            if c is None:
                return meta_error(100, "invalid creation id")
            if c.get("published"):
                return meta_error(100, "The media has already been published", subcode=2207027)
            mid = self._id()
            c["published"] = True
            self.media[mid] = {"container": form["creation_id"], "ig": node,
                               "caption": c["form"].get("caption", ""),
                               "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime())}
            self._lost("ig_publish", request)
            return _json(200, {"id": mid})
        if edge == "media" and request.method == "GET":
            hit = self._take("ig_media_list", request)
            if hit is not None:
                return hit
            rows = [{"id": mid, "caption": m["caption"], "timestamp": m["timestamp"],
                     "permalink": f"https://www.instagram.com/reel/FAKE{mid}/"}
                    for mid, m in reversed(list(self.media.items())) if m["ig"] == node]
            return _json(200, {"data": rows[:10]})
        if not edge and request.method == "GET":
            fields = url.params.get("fields", "")
            if node in self.reels:
                hit = self._take("fb_status", request)
                if hit is not None:
                    return hit
                r = self.reels[node]
                up = {"status": "complete" if r["size"] and len(r["data"]) == r["size"] else "in_progress",
                      "bytes_transfered": len(r["data"])}
                pub = {"status": "not_started"}
                vs = "upload_complete" if up["status"] == "complete" else "uploading"
                if r["finished"]:
                    i = min(r["polls"], len(self.fb_status_seq) - 1)
                    r["polls"] += 1
                    s = self.fb_status_seq[i]
                    if s == "error":
                        return _json(200, {"id": node, "status": {"video_status": "error",
                                                                  "uploading_phase": up,
                                                                  "processing_phase": {"status": "error", "errors": [{"code": 1, "message": "bad video"}]},
                                                                  "publishing_phase": pub}})
                    pub = {"status": s}
                    vs = "ready" if s == "complete" else "processing"
                return _json(200, {"id": node, "status": {"video_status": vs, "uploading_phase": up,
                                                          "processing_phase": {"status": "in_progress"},
                                                          "publishing_phase": pub}})
            if node in self.containers:
                hit = self._take("ig_status", request)
                if hit is not None:
                    return hit
                c = self.containers[node]
                i = min(c["polls"], len(self.ig_status_seq) - 1)
                c["polls"] += 1
                code = "PUBLISHED" if c.get("published") else self.ig_status_seq[i]
                return _json(200, {"id": node, "status_code": code,
                                   "status": "Error: unsupported format" if code == "ERROR" else code})
            if node in self.media:
                return _json(200, {"id": node, "permalink": f"https://www.instagram.com/reel/FAKE{node}/"})
            if node in self.page_names:
                hit = self._take("page", request)
                if hit is not None:
                    return hit
                d: dict[str, Any] = {"id": node, "name": self.page_names[node]}
                if self.linked_ig.get(node):
                    d["instagram_business_account"] = {"id": self.linked_ig[node]}
                return _json(200, d)
            if node in self.ig_names:
                hit = self._take("ig_user", request)
                if hit is not None:
                    return hit
                return _json(200, {"id": node, "username": self.ig_names[node]})
            return meta_error(100, f"Unsupported get request. Object with ID '{node}' does not exist", status=400)
        return meta_error(100, f"fake graph: no route {request.method} {url}")

    def _rupload(self, request: httpx.Request, rec: Recorded, parts: list[str]) -> httpx.Response:
        if self._token(request, "OAuth") is None:
            return _json(400, {"debug_info": {"type": "AuthException", "message": "invalid token"}})
        kind = parts[0]
        assert parts[1] == self.v, f"wrong rupload version {parts}"
        target = parts[2]
        offset = int(request.headers["offset"])
        size = int(request.headers["file_size"])
        if kind == "video-upload":
            hit = self._take("fb_upload", request)
            if hit is not None:
                return hit
            r = self.reels.get(target)
            if r is None:
                return _json(400, {"debug_info": {"type": "NotFound", "message": "no such video"}})
            r["size"] = size
            assert offset <= len(r["data"]), (offset, len(r["data"]))
            del r["data"][offset:]
            r["data"].extend(rec.body)
            assert len(r["data"]) == size
            return _json(200, {"success": True})
        if kind == "ig-api-upload":
            hit = self._take("ig_upload", request)
            if hit is not None:
                return hit
            c = self.containers.get(target)
            if c is None:
                return _json(400, {"debug_info": {"type": "NotFound", "message": "no such container"}})
            assert offset == 0
            c["data"] = rec.body
            assert len(rec.body) == size
            return _json(200, {"success": True, "message": "Upload successful."})
        return _json(404, {"debug_info": {"message": "no route"}})


class FakeNet:
    """One MockTransport handler for Google + Meta. `calls` = every request in order."""

    def __init__(self) -> None:
        self.google = FakeGoogle()
        self.graph = FakeGraph()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host.endswith("googleapis.com"):
            return self.google(request)
        if host in ("graph.facebook.com", "rupload.facebook.com"):
            return self.graph(request)
        raise AssertionError(f"unexpected host {host}")

    @property
    def requests(self) -> list[Recorded]:
        return self.google.requests + self.graph.requests

    def client(self) -> httpx.Client:
        return fake_client(self)

    def reset(self) -> None:
        self.google.requests.clear()
        self.graph.requests.clear()


def fake_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0, follow_redirects=False)


def no_network_client() -> httpx.Client:
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"network call in a test that must not send anything: {request.method} {request.url}")
    return fake_client(boom)


# ---------------------------------------------------------------------------------------------
# jobs / videos
# ---------------------------------------------------------------------------------------------
def make_final(dst: Path, seconds: float = 6.0, width: int = 1080, height: int = 1920) -> Path:
    """Tiny real mp4 (solid colour, 2 fps, silent AAC) - fast to encode, probe-able."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c=navy:s={width}x{height}:r=2:d={seconds}",
                    "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo",
                    "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(dst)], check=True)
    return dst


def stub_probe(monkeypatch: Any, duration: float = 28.84, width: int = 1080, height: int = 1920) -> None:
    """Make service.probe_video return fixed facts (use with fake bytes as final.mp4)."""
    from autoclipper.publish import service
    monkeypatch.setattr(service, "probe_video", lambda path: (duration, width, height))


def make_job(job_id: str = "20261123-120000-pubtst", clips: dict[int, list[str]] | None = None, *,
             seconds: float = 6.0, width: int = 1080, height: int = 1920, final_bytes: int | None = None,
             metadata: dict[str, dict] | None = None, with_final: bool = True, with_metadata: bool = True) -> str:
    """Job dir under the test data dir with job.json + per clip-lang final.mp4 / metadata.json / script.json.

    final_bytes: write that many pseudo-random bytes instead of a real video (pair with stub_probe)."""
    from autoclipper.jobstore import jobs_root
    from autoclipper.models import ClipLangState, ClipState, Job, Moment, StageState

    clips = clips or {1: ["en", "hi"]}
    root = jobs_root() / job_id
    root.mkdir(parents=True, exist_ok=True)
    job = Job(id=job_id, source="sample.mp4", status="done", created_at=time.time(),
              stages={s: StageState(status="done", progress=1.0) for s in ("ingest", "transcribe", "moments", "cut")})
    shared_video: Path | None = None
    for idx, langs in clips.items():
        cs = ClipState(index=idx, moment=Moment(start=0, end=seconds, hook="t", reason="r"),
                       cut=f"clips/{idx:02d}/cut.mp4")
        for lang in langs:
            d = root / "clips" / f"{idx:02d}" / lang
            d.mkdir(parents=True, exist_ok=True)
            final = d / "final.mp4"
            if with_final:
                if final_bytes is not None:
                    import random
                    rnd = random.Random(f"{job_id}{idx}{lang}")
                    final.write_bytes(bytes(rnd.getrandbits(8) for _ in range(final_bytes)) if final_bytes < 200000
                                      else rnd.randbytes(final_bytes))
                elif shared_video is None:
                    shared_video = make_final(final, seconds, width, height)
                else:
                    final.write_bytes(shared_video.read_bytes())
            meta = (metadata or SAMPLE_META).get(lang, SAMPLE_META[lang])
            if with_metadata:
                (d / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            (d / "script.json").write_text(json.dumps({"lang": lang, "narration": "x", "title": meta["title"],
                                                       "description": meta["description"],
                                                       "hashtags": meta["hashtags"]}, ensure_ascii=False),
                                           encoding="utf-8")
            cs.langs[lang] = ClipLangState(final=f"clips/{idx:02d}/{lang}/final.mp4" if with_final else None,
                                           stages={s: StageState(status="done", progress=1.0)
                                                   for s in ("script", "voice", "captions", "render")})
        job.clips.append(cs)
    (root / "job.json").write_text(job.model_dump_json(indent=2), encoding="utf-8")
    return job_id


# ---------------------------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------------------------
def client_secret_json(kind: str = "installed") -> dict:
    return {kind: {"client_id": CLIENT_ID, "project_id": "fake-project", "client_secret": CLIENT_SECRET,
                   "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                   "token_uri": "https://oauth2.googleapis.com/token",
                   "redirect_uris": ["http://localhost"]}}


def connect_youtube(lang: str) -> None:
    """Write the youtube_<lang> secret + google client directly (no OAuth dance) and map settings."""
    from autoclipper.publish import secrets, service
    secrets.write_secret("google_client", {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                                           "project_id": "fake-project"})
    cid, title = CHANNEL[lang]
    secrets.write_secret(f"youtube_{lang}", {"refresh_token": REFRESH_TOKEN[lang], "scopes": [],
                                             "channel_id": cid, "channel_title": title,
                                             "connected_at": time.time()})

    def fill(ps: Any) -> None:
        ps.lang(lang).youtube.channel_id = cid
    service._update_settings(fill)


def connect_meta(lang: str, *, separate_ig_token: bool = False) -> None:
    from autoclipper.publish import secrets, service
    pid, pname = PAGE[lang]
    iid, iname = IG[lang]
    secrets.write_secret(f"meta_{lang}", {"page_id": pid, "page_name": pname, "page_token": PAGE_TOKEN[lang],
                                          "ig_user_id": iid, "ig_username": iname,
                                          "ig_token": IG_TOKEN[lang] if separate_ig_token else None,
                                          "connected_at": time.time()})

    def fill(ps: Any) -> None:
        ps.lang(lang).facebook.page_id = pid
        ps.lang(lang).instagram.ig_user_id = iid
    service._update_settings(fill)


def connect_all(*, enable: bool = True, dry_run: bool = False, langs: tuple[str, ...] = ("en", "hi"),
                fast: bool = True) -> None:
    """Connect every platform for `langs`, optionally enable them and turn dry run off.
    fast=True: poll interval 1 s / timeout 60 s and chunk 1 MiB (tests pass a no-op sleep)."""
    from autoclipper.publish import service
    for lang in langs:
        connect_youtube(lang)
        connect_meta(lang)

    def fill(ps: Any) -> None:
        ps.dry_run = dry_run
        if fast:
            ps.poll_interval_seconds = 1.0
            ps.poll_timeout_seconds = 60.0
            ps.chunk_mb = 1
        for lang in langs:
            for p in ("youtube", "facebook", "instagram"):
                ps.target(lang, p).enabled = enable
    service._update_settings(fill)


def set_publish(**fields: Any) -> None:
    """Change top-level publish settings (e.g. dry_run=False, chunk_mb=1)."""
    from autoclipper.publish import service

    def fill(ps: Any) -> None:
        for k, v in fields.items():
            setattr(ps, k, v)
    service._update_settings(fill)


def grep_secrets(*texts: str) -> list[str]:
    """Every fake secret value found in the given texts (should be [])."""
    found = []
    for t in texts:
        for s in ALL_SECRETS:
            if s in t:
                found.append(s)
    return found


def tree_text(root: Path | None = None, *, exclude_secrets_dir: bool = True) -> str:
    """Concatenated text of every file under the data dir (excluding data/secrets) for secret greps."""
    root = root or data_dir()
    out = []
    for p in sorted(Path(root).rglob("*")):
        if not p.is_file():
            continue
        if exclude_secrets_dir and "secrets" in p.relative_to(root).parts:
            continue
        if p.suffix in (".mp4", ".wav", ".ogv"):
            continue
        try:
            out.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(out)


@dataclass
class LogSink:
    lines: list[str] = field(default_factory=list)

    def __call__(self, line: str) -> None:
        if line:
            self.lines.append(str(line))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)
