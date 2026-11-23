"""Helpers shared by the three platform clients (plan skeleton, common preflight checks)."""
from __future__ import annotations

import time
from typing import Any

from . import LANG_NAMES, PLATFORM_NAMES
from .base import PublishItem


def fmt_local(t: float) -> str:
    """A time as this computer shows it, with its UTC offset ("2026-11-23 17:30 (UTC+06:00)")."""
    dt = time.localtime(t)
    off = dt.tm_gmtoff // 60
    sign = "+" if off >= 0 else "-"
    off = abs(off)
    return time.strftime("%Y-%m-%d %H:%M", dt) + f" (UTC{sign}{off // 60:02d}:{off % 60:02d})"


def plan_skeleton(item: PublishItem, platform: str, api_version: str, account: dict | None, metadata: dict,
                  requests: list[dict], limits: dict, problems: list[str], warnings: list[str]) -> dict:
    if item.cover and item.cover.get("outdated"):
        warnings = [*warnings, "the cover was made from an earlier render of this clip - pick it again"]
    return {
        "platform": platform, "lang": item.lang, "job_id": item.job_id, "clip": item.clip, "dry_run": True,
        "created_at": time.time(), "api_version": api_version,
        "account": ({"id": account.get("id"), "name": account.get("name")} if account else None),
        "video": {"path": item.rel_path, "size": item.size, "duration": item.duration, "width": item.width,
                  "height": item.height, "sha256": item.sha256},
        "metadata": metadata, "requests": requests, "limits": limits, "problems": problems,
        "warnings": warnings,
        "cover": cover_plan(item, platform),  # G7
        "schedule": None,  # G6: filled by schedules.build_schedule_plans
    }


def cover_plan(item: PublishItem, platform: str) -> dict | None:
    """What the plan does with the clip's cover (G7), None = no cover picked."""
    c = item.cover
    if not c:
        return None
    use = {
        "youtube": "custom thumbnail via thumbnails.set after YouTube has processed the video",
        "facebook": "reel thumbnail via POST /{video-id}/thumbnails (is_preferred=true) before publishing",
        "instagram": (f"thumb_offset={c['thumb_offset_ms']} ms (the frame at {c['time']:.2f} s); the title text "
                      "is not used" if c.get("thumb_offset_ms") is not None else
                      "not used: the picked frame is past the end of this render - pick it again"
                      if c.get("past_end") else
                      "not used: Instagram only takes a frame offset or a public cover_url"),
    }[platform]
    return {"path": c["path"], "size": c["size"], "sha256": c["sha256"], "source": c["source"],
            "time": c.get("time"), "title": c.get("title") or "", "outdated": bool(c.get("outdated")),
            "use": use}


def common_problems(item: PublishItem, target: Any, account: dict | None, platform: str,
                    id_value: str) -> list[str]:
    name = PLATFORM_NAMES[platform]
    lang = LANG_NAMES.get(item.lang, item.lang)
    problems: list[str] = []
    if not getattr(target, "enabled", False):
        problems.append(f"{name} is disabled for {lang} in settings")
    if account is None:
        problems.append("not connected")
    elif id_value and account.get("id") != id_value:
        problems.append(f"connected {name} account differs from the one in settings")
    if not item.metadata.get("title"):
        problems.append("metadata.json missing or has no title - re-render")
    return problems


def mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"
