"""`settings.publish` (CONTRACT_STEP4 section 3). Imports only pydantic/re: config.py imports this module."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ID_YT = re.compile(r"\AUC[A-Za-z0-9_-]{22}\Z")  # channel id; "" = not mapped yet
_ID_NUM = re.compile(r"\A[0-9]{1,32}\Z")  # Page id / IG user id; "" = not mapped yet
_CATEGORY = re.compile(r"\A[0-9]{1,3}\Z")
_GRAPH_VER = re.compile(r"\Av[1-9][0-9]?\.[0-9]\Z")


def _check(pattern: re.Pattern[str], what: str, v: str) -> str:
    if v != "" and not pattern.match(v):
        raise ValueError(f"invalid {what}")
    return v


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class YouTubeTarget(_Strict):
    enabled: bool = False
    channel_id: str = ""  # filled by connect (when empty); publish refuses if the connected channel differs
    privacy: Literal["private", "unlisted", "public"] = "private"
    category_id: str = "22"  # 22 = People & Blogs
    made_for_kids: bool = False
    contains_synthetic_media: bool = True  # AI narration voice -> disclose by default
    notify_subscribers: bool = False
    add_shorts_hashtag: bool = True  # append "#Shorts" to the description if absent

    @field_validator("channel_id")
    @classmethod
    def _cid(cls, v: str) -> str:
        return _check(_ID_YT, "YouTube channel id (UC + 22 characters)", v)

    @field_validator("category_id")
    @classmethod
    def _cat(cls, v: str) -> str:
        if not _CATEGORY.match(v):
            raise ValueError("invalid category id (1-3 digits, e.g. 22)")
        return v


class FacebookTarget(_Strict):
    enabled: bool = False
    page_id: str = ""
    is_ai_generated: bool = True  # AI narration voice -> disclose on the reel (finish `is_ai_generated`)

    @field_validator("page_id")
    @classmethod
    def _pid(cls, v: str) -> str:
        return _check(_ID_NUM, "Facebook Page id (digits only)", v)


class InstagramTarget(_Strict):
    enabled: bool = False
    ig_user_id: str = ""
    share_to_feed: bool = True

    @field_validator("ig_user_id")
    @classmethod
    def _iid(cls, v: str) -> str:
        return _check(_ID_NUM, "Instagram user id (digits only)", v)


class LangTargets(_Strict):
    youtube: YouTubeTarget = Field(default_factory=YouTubeTarget)
    facebook: FacebookTarget = Field(default_factory=FacebookTarget)
    instagram: InstagramTarget = Field(default_factory=InstagramTarget)


class DailyLimits(_Strict):
    """G6: scheduled + published posts per account per local calendar day (refused above this)."""
    youtube: int = Field(5, ge=1, le=100)
    facebook: int = Field(10, ge=1, le=30)  # Facebook: 30 API-published reels per Page per 24 h
    instagram: int = Field(10, ge=1, le=100)  # Instagram: 100 API-published posts per 24 h


class ScheduleSettings(_Strict):
    """G6 scheduled publishing (docs/CONTRACT_STEP4.md section 15)."""
    # Use the platform's own scheduling where the API has it (YouTube status.publishAt for public videos,
    # Facebook video_state=SCHEDULED); Instagram is always posted by Auto Clipper at the time.
    prefer_native: bool = True
    # Native schedules are handed to the platform (uploaded) this many minutes before the time.
    native_lead_minutes: int = Field(60, ge=15, le=1440)
    # A schedule whose time passed while the server was not running is still posted within this window,
    # later it is marked "missed".
    grace_minutes: int = Field(30, ge=0, le=1440)
    daily_limits: DailyLimits = Field(default_factory=DailyLimits)
    # IANA zone ("Asia/Dhaka") for CLI times without an offset and for daily limits; "" = this computer.
    timezone: str = ""
    check_interval_seconds: float = Field(15.0, ge=1, le=300)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        if v and not re.match(r"\A[A-Za-z][A-Za-z0-9_+-]{0,40}(/[A-Za-z0-9_+-]{1,40}){0,2}\Z", v):
            raise ValueError("invalid time zone name (like Asia/Dhaka)")
        if v and v.upper() != "UTC":
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(v)
            except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, no tzdata, ...
                raise ValueError(f"unknown time zone {v!r} on this computer (use an IANA name like "
                                 "Asia/Dhaka, or leave it empty for this computer's zone)") from None
        return v


class PublishSettings(_Strict):
    dry_run: bool = True  # GLOBAL: nothing is sent while true
    graph_api_version: str = "v27.0"
    en: LangTargets = Field(default_factory=LangTargets)
    hi: LangTargets = Field(default_factory=LangTargets)
    allow_same_account: bool = False  # en and hi on the same channel/page/IG -> preflight problem unless true
    chunk_mb: int = Field(8, ge=1, le=256)  # YouTube chunk = chunk_mb MiB (a multiple of 256 KiB)
    max_retries: int = Field(3, ge=0, le=10)  # transient errors per request
    # Meta asks to poll an IG container about once a minute for at most 5 minutes: IG polling starts at
    # poll_interval_seconds and backs off (doubling) up to 60 s; the default total wait is 5 minutes.
    poll_interval_seconds: float = Field(10.0, ge=1, le=60)
    poll_timeout_seconds: float = Field(300.0, ge=60, le=3600)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)  # G6

    @field_validator("graph_api_version")
    @classmethod
    def _ver(cls, v: str) -> str:
        if not _GRAPH_VER.match(v):
            raise ValueError("invalid Graph API version (like v27.0)")
        return v

    def lang(self, lang: str) -> LangTargets:
        if lang not in ("en", "hi"):
            raise KeyError(lang)
        return self.en if lang == "en" else self.hi

    def target(self, lang: str, platform: str) -> BaseModel:
        if platform not in ("youtube", "facebook", "instagram"):
            raise KeyError(platform)
        return getattr(self.lang(lang), platform)
