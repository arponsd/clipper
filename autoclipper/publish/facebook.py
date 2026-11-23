"""Facebook Page Reels via the Graph API `video_reels` flow + rupload (CONTRACT_STEP4 2.2)."""
from __future__ import annotations

import time
from typing import Any

import httpx

from . import records
from .base import Http, Progress, PublishError, PublishItem, Sleep, StatusWriter, classify_meta, file_blocks
from .common import common_problems, plan_skeleton
from .metadata import facebook_payload

GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com/video-upload"
MIN_SECONDS, MAX_SECONDS = 3.0, 90.0
DAILY_LIMIT = 30


def _ai_flag(target: Any) -> str:
    return "true" if getattr(target, "is_ai_generated", True) else "false"


def reel_url(video_id: str) -> str:
    return f"https://www.facebook.com/reel/{video_id}"


class FacebookPublisher:
    platform = "facebook"

    def __init__(self, settings: Any, *, http: httpx.Client | None = None, sleep: Sleep = time.sleep):
        self.ps = settings
        self.sleep = sleep
        self.v = settings.graph_api_version
        self.http = Http(http, "facebook", max_retries=settings.max_retries, sleep=sleep,
                         classify=lambda r: classify_meta(r, "facebook"))

    @property
    def api_version(self) -> str:
        return self.v

    def reels_url(self, page_id: str) -> str:
        return f"{GRAPH}/{self.v}/{page_id}/video_reels"

    def upload_url(self, video_id: str) -> str:
        return f"{RUPLOAD}/{self.v}/{video_id}"

    def video_url(self, video_id: str) -> str:
        return f"{GRAPH}/{self.v}/{video_id}"

    # ---- no network ---------------------------------------------------------------------------
    def preflight(self, item: PublishItem, target: Any, account: dict | None) -> list[str]:
        page_id = getattr(target, "page_id", "")
        problems = common_problems(item, target, account, self.platform, page_id)
        if item.size <= 0:
            problems.append("final.mp4 is empty")
        if item.duration < MIN_SECONDS or item.duration > MAX_SECONDS:
            problems.append(f"duration {item.duration:.1f} s is outside {MIN_SECONDS:.0f}-{MAX_SECONDS:.0f} s "
                            "(Facebook Reels limit)")
        if item.width > item.height:
            problems.append(f"video is {item.width}x{item.height}; Facebook Reels must be vertical (9:16)")
        elif item.width < 540 or item.height < 960:
            problems.append(f"video is {item.width}x{item.height}; Facebook Reels need at least 540x960")
        acc_id = (account or {}).get("id") or page_id
        if acc_id:
            n = records.window_count(self.platform, acc_id)
            if n >= DAILY_LIMIT:
                first = records.oldest_in_window(self.platform, acc_id)
                when = time.strftime("%H:%M", time.localtime(first + 24 * 3600)) if first else "later"
                problems.append(f"Facebook: the Page has reached {DAILY_LIMIT} API-published reels in 24 h; "
                                f"try after {when}")
        return problems

    def warnings(self, item: PublishItem) -> list[str]:
        w = ["Facebook reels are published publicly on the Page"]
        if item.height and abs(item.width / item.height - 9 / 16) > 0.02:
            w.append(f"aspect {item.width}x{item.height} is not 9:16")
        return w

    def plan(self, item: PublishItem, target: Any, account: dict | None) -> dict:
        page_id = (account or {}).get("id") or getattr(target, "page_id", "") or "{page-id}"
        payload = facebook_payload(item.metadata)
        auth = {"Authorization": "Bearer ***"}
        requests = [
            {"step": "start", "method": "POST", "url": self.reels_url(page_id), "headers": auth,
             "body": {"upload_phase": "start"}, "note": "returns {video_id, upload_url}"},
            {"step": "upload", "method": "POST", "url": self.upload_url("{video_id}"),
             "headers": {"Authorization": "OAuth ***", "offset": "0", "file_size": str(item.size),
                         "Content-Type": "application/octet-stream"},
             "body": None, "note": f"body = final.mp4 ({item.size} bytes); returns {{success: true}}"},
            {"step": "finish", "method": "POST", "url": self.reels_url(page_id), "headers": auth,
             "body": {"video_id": "{video_id}", "upload_phase": "finish", "video_state": "PUBLISHED",
                      "title": payload["title"], "description": payload["description"],
                      "is_ai_generated": _ai_flag(target)},
             "note": "publishes the reel (sent once, never auto-retried)"},
            {"step": "status", "method": "GET", "url": self.video_url("{video_id}") + "?fields=status",
             "headers": auth, "body": None, "note": "polled until publishing is complete"},
        ]
        limits = {"min_duration": MIN_SECONDS, "max_duration": MAX_SECONDS, "aspect": "9:16",
                  "rate": f"{DAILY_LIMIT} reels per Page per 24 h"}
        return plan_skeleton(item, self.platform, self.v, account, payload, requests, limits,
                             self.preflight(item, target, account), self.warnings(item))

    # ---- network ------------------------------------------------------------------------------
    def _status(self, token: str, video_id: str) -> dict:
        data = self.http.json("GET", self.video_url(video_id), params={"fields": "status"},
                              headers={"Authorization": f"Bearer {token}"})
        st = data.get("status")
        return st if isinstance(st, dict) else {}

    def _upload(self, item: PublishItem, token: str, video_id: str, offset: int,
                on_progress: Progress) -> None:
        failures = 0
        while True:
            on_progress(offset / item.size if item.size else 0.0,
                        f"uploading {int(100 * offset / item.size) if item.size else 0}%")
            try:
                resp = self.http.send_once("POST", self.upload_url(video_id),
                                           content=file_blocks(item.video, offset),
                                           headers={"Authorization": f"OAuth {token}", "offset": str(offset),
                                                    "file_size": str(item.size),
                                                    "Content-Type": "application/octet-stream",
                                                    "Content-Length": str(item.size - offset)})
                if resp.status_code == 200:
                    data = resp.json() if resp.content else {}
                    if isinstance(data, dict) and data.get("success") is False:
                        raise PublishError("Facebook upload was not accepted", kind="transient",
                                           platform=self.platform)
                    return
                err = classify_meta(resp, self.platform)
            except PublishError as e:
                err = e
            except ValueError:
                return  # 200 with a non-JSON body: treat as accepted, the status check follows
            if not err.transient:
                raise err
            failures += 1
            if failures > self.http.max_retries:
                raise err
            self.http.backoff(failures - 1, err)
            st = self._status(token, video_id)
            done = ((st.get("uploading_phase") or {}).get("bytes_transfered"))
            offset = int(done) if isinstance(done, (int, float, str)) and str(done).isdigit() else 0

    def publish(self, item: PublishItem, target: Any, account: dict, *, status: StatusWriter,
                on_progress: Progress) -> dict:
        token = account["token"]
        page_id = account["id"]
        payload = facebook_payload(item.metadata)
        auth = {"Authorization": f"Bearer {token}"}
        resume = status.load_resume() or {}
        video_id = None
        uploaded = finished = False
        offset = 0
        if resume.get("sha256") == item.sha256 and resume.get("video_id"):
            video_id = str(resume["video_id"])
            uploaded = bool(resume.get("uploaded"))
            finished = bool(resume.get("finished"))
            if not uploaded:
                st = self._status(token, video_id)
                up = st.get("uploading_phase") or {}
                if up.get("status") == "complete":
                    uploaded = True
                else:
                    done = up.get("bytes_transfered")
                    offset = int(done) if str(done).isdigit() else 0
        if video_id is None:
            data = self.http.json("POST", self.reels_url(page_id), data={"upload_phase": "start"}, headers=auth)
            video_id = str(data.get("video_id") or "")
            if not video_id.isdigit():
                raise PublishError("Facebook did not return a video id", kind="unknown", platform=self.platform)
            status.save_resume({"video_id": video_id, "sha256": item.sha256, "created": time.time()})
        status.update(remote_id=video_id)
        if not uploaded:
            self._upload(item, token, video_id, offset, on_progress)
            status.save_resume({"video_id": video_id, "sha256": item.sha256, "uploaded": True,
                                "created": time.time()})
        if not finished and resume.get("sent") and resume.get("video_id") == video_id:
            # an earlier finish call may have gone through even though its answer was lost
            finished = self._publishing_started(self._status(token, video_id))
        if not finished:
            on_progress(0.97, "publishing")
            status.update(state="publishing")
            # commit point: from here on the reel may be live, so the runner keeps this record (never a new
            # `start` without force). A repeated finish only ever targets this same video id.
            status.save_resume({"video_id": video_id, "sha256": item.sha256, "uploaded": True, "sent": True,
                                "created": time.time()})
            try:
                self.http.json("POST", self.reels_url(page_id), headers=auth, retries=0, data={
                    "video_id": video_id, "upload_phase": "finish", "video_state": "PUBLISHED",
                    "title": payload["title"], "description": payload["description"],
                    "is_ai_generated": _ai_flag(target)})
            except PublishError:
                try:
                    st = self._status(token, video_id)
                except PublishError:
                    raise_err = True  # unknown: keep the `sent` record, the next run checks again first
                else:
                    raise_err = not self._publishing_started(st)
                    if raise_err:  # verified not published: no need to hold on to this video id
                        status.save_resume({"video_id": video_id, "sha256": item.sha256, "uploaded": True,
                                            "created": time.time()})
                if raise_err:
                    raise
            status.save_resume({"video_id": video_id, "sha256": item.sha256, "uploaded": True, "sent": True,
                                "finished": True, "created": time.time()})
        self._wait_published(token, video_id, on_progress)
        return {"remote_id": video_id, "url": reel_url(video_id)}

    @staticmethod
    def _publishing_started(st: dict) -> bool:
        """True once Facebook has accepted the finish call (publishing under way or done)."""
        pub = str((st.get("publishing_phase") or {}).get("status") or "")
        return pub not in ("", "not_started") or str(st.get("video_status") or "") in ("ready", "published")

    def _wait_published(self, token: str, video_id: str, on_progress: Progress) -> None:
        deadline = time.monotonic() + float(self.ps.poll_timeout_seconds)
        waited = 0.0
        while True:
            st = self._status(token, video_id)
            vs = str(st.get("video_status") or "")
            proc = st.get("processing_phase") or {}
            pub = st.get("publishing_phase") or {}
            if vs in ("error", "upload_failed", "expired") or proc.get("status") == "error" \
                    or pub.get("status") == "error":
                detail = (proc.get("errors") or pub.get("errors") or [{}])
                msg = detail[0].get("message") if isinstance(detail, list) and detail and isinstance(detail[0], dict) else None
                raise PublishError(f"Facebook could not process the reel ({vs or 'error'})"
                                   + (f": {msg}" if msg else ""), kind="processing", platform=self.platform)
            if pub.get("status") == "complete" or vs in ("ready", "published"):
                return
            if time.monotonic() >= deadline or waited >= float(self.ps.poll_timeout_seconds):
                raise PublishError(f"Facebook is still processing reel {video_id}; it may still appear on the "
                                   "Page - check it before publishing again", kind="processing",
                                   platform=self.platform)
            on_progress(0.98, f"Facebook is processing the reel ({vs or 'processing'})")
            self.sleep(float(self.ps.poll_interval_seconds))
            waited += float(self.ps.poll_interval_seconds)
