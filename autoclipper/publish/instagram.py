"""Instagram Reels via the Instagram Graph API content publishing flow (CONTRACT_STEP4 2.3):
REELS container (upload_type=resumable) -> rupload -> poll status_code -> media_publish -> permalink."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import httpx

from .base import Http, Progress, PublishError, PublishItem, Sleep, StatusWriter, classify_meta, file_blocks
from .common import common_problems, mb, plan_skeleton
from .metadata import IG_MAX_HASHTAGS, count_hashtags, instagram_payload

GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload"
MIN_SECONDS, MAX_SECONDS = 3.0, 900.0
MAX_BYTES = 300 * 1024 * 1024
MAX_WIDTH = 1920
DAILY_LIMIT = 100
CONTAINER_REUSE_SECONDS = 23 * 3600
MAX_POLL_INTERVAL = 60.0  # Meta: query the container status about once per minute


class InstagramPublisher:
    platform = "instagram"

    def __init__(self, settings: Any, *, http: httpx.Client | None = None, sleep: Sleep = time.sleep):
        self.ps = settings
        self.sleep = sleep
        self.v = settings.graph_api_version
        self.http = Http(http, "instagram", max_retries=settings.max_retries, sleep=sleep,
                         classify=lambda r: classify_meta(r, "instagram"))

    @property
    def api_version(self) -> str:
        return self.v

    def g(self, path: str) -> str:
        return f"{GRAPH}/{self.v}/{path}"

    def upload_url(self, container_id: str) -> str:
        return f"{RUPLOAD}/{self.v}/{container_id}"

    # ---- no network ---------------------------------------------------------------------------
    def preflight(self, item: PublishItem, target: Any, account: dict | None) -> list[str]:
        problems = common_problems(item, target, account, self.platform, getattr(target, "ig_user_id", ""))
        if item.size <= 0:
            problems.append("final.mp4 is empty")
        if item.size > MAX_BYTES:
            problems.append(f"file is {mb(item.size)}; Instagram Reels accept at most 300 MB")
        if item.duration < MIN_SECONDS or item.duration > MAX_SECONDS:
            problems.append(f"duration {item.duration:.1f} s is outside 3 s - 15 min (Instagram Reels limit)")
        if item.width > MAX_WIDTH:
            problems.append(f"video width {item.width} > {MAX_WIDTH} (Instagram limit)")
        if item.publish_at is not None:
            problems.append("Instagram's API has no scheduling; Auto Clipper posts it at the time instead")
        return problems

    def warnings(self, item: PublishItem, payload: dict) -> list[str]:
        w = ["Instagram reels are public on a professional account"]
        if item.height and abs(item.width / item.height - 9 / 16) > 0.02:
            w.append(f"aspect {item.width}x{item.height} is not 9:16")
        n = len([t for t in (item.metadata.get("hashtags") or []) if isinstance(t, str) and t.strip("# ")])
        if n > IG_MAX_HASHTAGS:
            w.append(f"only the first {IG_MAX_HASHTAGS} hashtags are used")
        if count_hashtags(payload["caption"]) > IG_MAX_HASHTAGS:
            w.append(f"caption has more than {IG_MAX_HASHTAGS} hashtags; Instagram may reject it")
        c = item.cover
        if c and c.get("thumb_offset_ms") is None:
            w.append("the uploaded cover image is not used: Instagram needs a public cover_url, and Auto "
                     "Clipper only runs on this computer")
        elif c and c.get("title"):
            w.append("Instagram uses the picked frame as the cover (thumb_offset); the title text is not shown")
        return w

    def container_form(self, item: PublishItem, payload: dict) -> dict:
        form = {"media_type": "REELS", "upload_type": "resumable", "caption": payload["caption"],
                "share_to_feed": "true" if payload["share_to_feed"] else "false"}
        if item.cover and item.cover.get("thumb_offset_ms") is not None:  # G7: frame cover
            form["thumb_offset"] = str(int(item.cover["thumb_offset_ms"]))
        return form

    def plan(self, item: PublishItem, target: Any, account: dict | None) -> dict:
        ig = (account or {}).get("id") or getattr(target, "ig_user_id", "") or "{ig-user-id}"
        payload = instagram_payload(item.metadata, target)
        auth = {"Authorization": "Bearer ***"}
        requests = [
            {"step": "quota", "method": "GET", "url": self.g(f"{ig}/content_publishing_limit") +
             "?fields=quota_usage,config", "headers": auth, "body": None,
             "note": f"checked live before sending ({DAILY_LIMIT} posts per 24 h)"},
            {"step": "container", "method": "POST", "url": self.g(f"{ig}/media"), "headers": auth,
             "body": self.container_form(item, payload),
             "note": "returns {id: <container id>, uri}"},
            {"step": "upload", "method": "POST", "url": self.upload_url("{container_id}"),
             "headers": {"Authorization": "OAuth ***", "offset": "0", "file_size": str(item.size)},
             "body": None, "note": f"body = final.mp4 ({item.size} bytes)"},
            {"step": "status", "method": "GET", "url": self.g("{container_id}") + "?fields=status_code,status",
             "headers": auth, "body": None,
             "note": f"polled from every {self.ps.poll_interval_seconds:g} s backing off to 60 s, at most "
                     f"{self.ps.poll_timeout_seconds:g} s, until FINISHED"},
            {"step": "publish", "method": "POST", "url": self.g(f"{ig}/media_publish"), "headers": auth,
             "body": {"creation_id": "{container_id}"},
             "note": "returns {id: <media id>}; sent once, never auto-retried"},
            {"step": "permalink", "method": "GET", "url": self.g("{media_id}") + "?fields=permalink",
             "headers": auth, "body": None, "note": "the post URL"},
        ]
        limits = {"min_duration": MIN_SECONDS, "max_duration": MAX_SECONDS, "max_bytes": MAX_BYTES,
                  "caption_chars": 2200, "hashtags": IG_MAX_HASHTAGS, "rate": f"{DAILY_LIMIT} posts per 24 h"}
        return plan_skeleton(item, self.platform, self.v, account, payload, requests, limits,
                             self.preflight(item, target, account), self.warnings(item, payload))

    # ---- network ------------------------------------------------------------------------------
    def _check_quota(self, ig: str, auth: dict) -> None:
        data = self.http.json("GET", self.g(f"{ig}/content_publishing_limit"),
                              params={"fields": "quota_usage,config"}, headers=auth)
        rows = data.get("data") or []
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
        usage = row.get("quota_usage")
        total = (row.get("config") or {}).get("quota_total", DAILY_LIMIT)
        if isinstance(usage, (int, float)) and isinstance(total, (int, float)) and usage >= total:
            raise PublishError(f"Instagram: the account has used {int(usage)} of {int(total)} API posts in the "
                               "last 24 h; try again later", kind="rate_limit", platform=self.platform,
                               code="content_publishing_limit")

    def _container_status(self, cid: str, auth: dict) -> tuple[str, str]:
        data = self.http.json("GET", self.g(cid), params={"fields": "status_code,status"}, headers=auth)
        return str(data.get("status_code") or ""), str(data.get("status") or "")

    def _upload(self, item: PublishItem, cid: str, token: str, on_progress: Progress) -> None:
        on_progress(0.0, "uploading 0%")
        self.http.request("POST", self.upload_url(cid), ok=(200, 201),
                          content_factory=lambda: file_blocks(item.video),
                          headers={"Authorization": f"OAuth {token}", "offset": "0",
                                   "file_size": str(item.size), "Content-Length": str(item.size)})

    def _wait_finished(self, cid: str, auth: dict, on_progress: Progress) -> None:
        """Poll status_code: starts at poll_interval_seconds and doubles up to 60 s (Meta: about once a
        minute), until poll_timeout_seconds in total."""
        waited = 0.0
        interval = float(self.ps.poll_interval_seconds)
        limit = float(self.ps.poll_timeout_seconds)
        while True:
            code, detail = self._container_status(cid, auth)
            if code in ("FINISHED", "PUBLISHED"):
                return
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram could not process the video ({code})"
                                   + (f": {detail[:200]}" if detail else ""), kind="processing",
                                   platform=self.platform)
            if waited >= limit:
                raise PublishError(f"Instagram was still processing the video after {int(limit)} s; publish "
                                   "again later to continue", kind="processing", platform=self.platform)
            on_progress(0.92, f"Instagram is processing the video ({code or 'IN_PROGRESS'})")
            step = min(interval, limit - waited)
            self.sleep(step)
            waited += step
            interval = min(interval * 2, MAX_POLL_INTERVAL)

    def _find_published_media(self, ig: str, auth: dict, caption: str, since: float) -> tuple[str, str | None] | None:
        """Look up the post a PUBLISHED container became: the account's recent media with exactly this
        caption, posted after the container was created. None unless exactly one matches."""
        try:
            data = self.http.json("GET", self.g(f"{ig}/media"), headers=auth,
                                  params={"fields": "id,caption,permalink,timestamp", "limit": "10"})
        except PublishError:
            return None
        hits = []
        for row in data.get("data") or []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            if str(row.get("caption") or "").strip() != caption.strip():
                continue
            try:
                t = datetime.strptime(str(row.get("timestamp")), "%Y-%m-%dT%H:%M:%S%z").timestamp()
            except ValueError:
                continue
            if t >= since - 300:
                hits.append(row)
        if len(hits) != 1:
            return None
        link = hits[0].get("permalink")
        return str(hits[0]["id"]), (link if isinstance(link, str) else None)

    def _already_published(self, ig: str, auth: dict, cid: str, resume: dict, caption: str,
                           same_render: bool) -> tuple[str, str | None]:
        """The container is PUBLISHED: the reel is live. Recover its media id, never post again."""
        found = self._find_published_media(ig, auth, caption, float(resume.get("created") or 0)) \
            if same_render else None
        if found:
            return found
        what = "this clip" if same_render else "an earlier render of this clip"
        raise PublishError(f"Instagram reports that {what} was already published (container {cid} is "
                           "PUBLISHED) but the post could not be identified - check the account; use "
                           "\"publish again\" (force) only if the reel is really missing", kind="processing",
                           platform=self.platform, code="maybe_published")

    def publish(self, item: PublishItem, target: Any, account: dict, *, status: StatusWriter,
                on_progress: Progress) -> dict:
        token = account["token"]
        ig = account["id"]
        auth = {"Authorization": f"Bearer {token}"}
        payload = instagram_payload(item.metadata, target)
        resume = status.load_resume() or {}
        same_render = resume.get("sha256") == item.sha256
        media_id = resume.get("media_id") if same_render else None
        url: str | None = None
        cid: str | None = None
        if not media_id:
            old = str(resume.get("container_id") or "")
            reusable = same_render and bool(resume.get("uploaded")) \
                and time.time() - float(resume.get("created") or 0) < CONTAINER_REUSE_SECONDS
            # any uploaded container may have been published by an earlier run whose answer was lost
            if old and (resume.get("sent") or resume.get("uploaded")):
                code, _ = self._container_status(old, auth)
                if code == "PUBLISHED":  # an earlier media_publish went through: never post a second reel
                    media_id, url = self._already_published(ig, auth, old, resume, payload["caption"], same_render)
                elif reusable and code in ("FINISHED", "IN_PROGRESS"):
                    cid = old
        if not media_id:
            self._check_quota(ig, auth)
            if cid is None:
                data = self.http.json("POST", self.g(f"{ig}/media"), headers=auth,
                                      data=self.container_form(item, payload))
                cid = str(data.get("id") or "")
                if not cid.isdigit():
                    raise PublishError("Instagram did not return a container id", kind="unknown",
                                       platform=self.platform)
                created = time.time()
                status.save_resume({"container_id": cid, "sha256": item.sha256, "created": created})
                self._upload(item, cid, token, on_progress)
                status.save_resume({"container_id": cid, "sha256": item.sha256, "uploaded": True,
                                    "created": created})
                on_progress(0.9, "uploaded; Instagram is processing it")
                resume = {"created": created}
            status.update(state="processing")
            self._wait_finished(cid, auth, on_progress)
            status.update(state="publishing")
            on_progress(0.96, "publishing")
            created = float(resume.get("created") or time.time())
            # commit point: from here on the reel may be live, so the runner keeps this record and the next
            # run checks the container before doing anything else. media_publish is sent exactly once.
            status.save_resume({"container_id": cid, "sha256": item.sha256, "uploaded": True, "sent": True,
                                "created": created})
            try:
                data = self.http.json("POST", self.g(f"{ig}/media_publish"), headers=auth, retries=0,
                                      data={"creation_id": cid})
                media_id = str(data.get("id") or "")
            except PublishError:
                try:
                    code, _ = self._container_status(cid, auth)
                except PublishError:
                    code = ""  # unknown: keep the `sent` record, the next run checks again first
                if code in ("FINISHED", "ERROR", "EXPIRED"):  # verified not published
                    status.save_resume({"container_id": cid, "sha256": item.sha256, "uploaded": True,
                                        "created": created})
                if code != "PUBLISHED":
                    raise
                media_id, url = self._already_published(ig, auth, cid, {"created": created}, payload["caption"],
                                                        True)
            if not media_id:
                raise PublishError("Instagram did not return a media id; check the account before publishing "
                                   "again", kind="unknown", platform=self.platform)
            status.save_resume({"container_id": cid, "sha256": item.sha256, "uploaded": True, "sent": True,
                                "media_id": media_id, "created": created})
        if url is None:
            try:
                data = self.http.json("GET", self.g(str(media_id)), params={"fields": "permalink"}, headers=auth)
                url = data.get("permalink") if isinstance(data.get("permalink"), str) else None
            except PublishError:
                url = None  # published anyway; the link is a nicety
        out: dict[str, Any] = {"remote_id": str(media_id), "url": url}
        if item.cover:
            off = item.cover.get("thumb_offset_ms")
            out["cover"] = ({"state": "set", "message": f"cover frame at {off} ms (thumb_offset)"} if off is not None
                            else {"state": "skipped", "message": "uploaded cover image not supported by the "
                                                                 "Instagram API without a public URL"})
        return out
