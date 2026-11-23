"""Helpers shared by the three platform clients (plan skeleton, common preflight checks)."""
from __future__ import annotations

import time
from typing import Any

from . import LANG_NAMES, PLATFORM_NAMES
from .base import PublishItem


def plan_skeleton(item: PublishItem, platform: str, api_version: str, account: dict | None, metadata: dict,
                  requests: list[dict], limits: dict, problems: list[str], warnings: list[str]) -> dict:
    return {
        "platform": platform, "lang": item.lang, "job_id": item.job_id, "clip": item.clip, "dry_run": True,
        "created_at": time.time(), "api_version": api_version,
        "account": ({"id": account.get("id"), "name": account.get("name")} if account else None),
        "video": {"path": item.rel_path, "size": item.size, "duration": item.duration, "width": item.width,
                  "height": item.height, "sha256": item.sha256},
        "metadata": metadata, "requests": requests, "limits": limits, "problems": problems,
        "warnings": warnings,
    }


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
