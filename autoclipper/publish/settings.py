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
