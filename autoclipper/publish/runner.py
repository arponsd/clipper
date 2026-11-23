"""Live publishing of one clip-lang to the requested platforms (worker `publish` task and the CLI).

Runs platforms in the given order; one failing platform does not stop the next. Everything is re-checked
right before sending (dry run off, enabled, connected, account mapping, limits). The status file of each
platform is written atomically; the ledger gets one line per successful publish.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from ..config import Settings
from . import LANG_NAMES, PLATFORM_NAMES, records
from .base import PublishError, make_publisher
from .secrets import redact
from .service import PublishConflict, check_platforms, load_item, platform_problems, build_plans


def _base_status(job_id: str, clip: int, lang: str, platform: str, *, item: Any, account: dict | None,
                 attempts: int, idempotency_key: str | None) -> dict:
    now = time.time()
    return {"platform": platform, "lang": lang, "job_id": job_id, "clip": clip, "state": "queued",
            "progress": 0.0, "message": "queued", "attempts": attempts, "error": None, "remote_id": None,
            "url": None, "account": ({"id": account.get("id"), "name": account.get("name")} if account else None),
            "video_sha256": item.sha256 if item else None, "video_size": item.size if item else None,
            "idempotency_key": idempotency_key, "started_at": now, "updated_at": now, "published_at": None}


def run_publish(job_id: str, clip: int, lang: str, *, platforms: list[str], force: bool, settings: Settings,
                log: Callable[[str], None], http: httpx.Client | None = None,
                sleep: Callable[[float], None] = time.sleep, idempotency_key: str | None = None) -> dict:
    """-> {platform: Status (public)}. Raises PublishNotFound / PublishConflict for clip-level problems."""
    platforms = check_platforms(platforms)
    ps = settings.publish
    item, extra = load_item(job_id, clip, lang)
    tag = f"[{job_id}] publish {clip:02d}/{lang}"
    results: dict[str, dict] = {}
    writers: dict[str, records.FileStatusWriter] = {}

    # mark every requested platform queued first, so the UI shows the whole set at once
    for p in platforms:
        raw = records.read_status_raw(job_id, clip, lang, p)
        if records.is_published(raw) and not force:
            continue
        if force and records.is_published(raw):
            records.append_history(job_id, clip, lang, p, raw)  # type: ignore[arg-type]
        if force and records.resume_sent(job_id, clip, lang, p):
            # explicit "publish again": forget the earlier, possibly live, attempt and post anew
            records.clear_resume(job_id, clip, lang, p)
        attempts = int((raw or {}).get("attempts") or 0) + 1 if raw and not records.is_published(raw) else 1
        writers[p] = records.FileStatusWriter(job_id, clip, lang, p, _base_status(
            job_id, clip, lang, p, item=item, account=None, attempts=attempts, idempotency_key=idempotency_key))

    for p in platforms:
        name = PLATFORM_NAMES[p]
        if p not in writers:
            raw = records.read_status_raw(job_id, clip, lang, p)
            results[p] = records.public_status(raw)  # type: ignore[assignment]
            log(f"{tag}: {name} already published: {raw.get('url') if raw else ''}")
            continue
        sw = writers[p]
        publisher = make_publisher(p, ps, http=http, sleep=sleep)
        problems, target, acc = platform_problems(item, ps, p, extra, publisher=None)
        if ps.dry_run:
            problems.insert(0, "publishing is in dry-run mode - turn dry run off on the Accounts page")
        if problems or acc is None:
            msg = "; ".join(problems) or "not connected"
            sw.update(state="failed", message=msg, account=acc.public if acc else None,
                      error={"kind": "invalid", "message": msg, "code": None, "retry_after": None})
            log(f"{tag}: {name} not sent: {msg}")
            results[p] = records.public_status(sw.data)  # type: ignore[assignment]
            continue
        # the plan file is rewritten with dry_run=false right before sending
        plan = build_plans(item, extra, [p], ps)[p]
        plan["dry_run"] = False
        records.write_plan(job_id, clip, lang, p, plan)
        sw.update(state="uploading", message=f"sending to {name}", account=acc.public, progress=0.0)
        log(f"{tag}: {name} -> {acc.get('name')} ({LANG_NAMES.get(lang, lang)})")
        last = {"pct": -1}

        def on_progress(frac: float, message: str, _sw: Any = sw, _name: str = name) -> None:
            frac = max(0.0, min(1.0, float(frac)))
            state = _sw.data.get("state")
            if state not in ("uploading", "processing", "publishing"):
                state = "uploading"
            if frac >= 0.9 and "process" in message:
                state = "processing"
            _sw.update(progress=round(frac, 4), message=message, state=state)
            pct = int(frac * 100)
            if pct // 10 != last["pct"] // 10 or "process" in message:
                last["pct"] = pct
                log(f"{tag}: {_name} {redact(message)}")
            else:
                log("")  # no output line, but a cancel point between chunks/polls

        try:
            out = publisher.publish(item, target, acc, status=sw, on_progress=on_progress)
        except PublishError as e:
            msg = e.message
            sent = records.resume_sent(job_id, clip, lang, p)
            if sent and e.code != "maybe_published":
                msg = (f"{msg} - the post may already be live; publishing again checks {name} first and does "
                       "not post twice")
            sw.update(state="failed", message=msg, error=e.to_dict())
            log(f"{tag}: {name} failed ({e.kind}): {msg}")
            # keep the resume record once the publishing request may have gone out (SEC-2/F1): clearing it
            # would let the next plain Publish start a brand-new upload and post the clip twice
            if not e.transient and e.kind not in ("processing",) and not sent:
                records.clear_resume(job_id, clip, lang, p)
            results[p] = records.public_status(sw.data)  # type: ignore[assignment]
            continue
        except Exception as e:  # noqa: BLE001 - unexpected: the platform fails, the next one still runs
            msg = redact(f"{type(e).__name__}: {e}")[:300]
            sw.update(state="failed", message=msg, error={"kind": "unknown", "message": msg, "code": None,
                                                          "retry_after": None})
            log(f"{tag}: {name} failed: {msg}")
            results[p] = records.public_status(sw.data)  # type: ignore[assignment]
            continue
        except BaseException:  # TaskCancelled / KeyboardInterrupt: resumable later
            sw.update(state="interrupted", message="interrupted - publish again to resume")
            raise
        now = time.time()
        sw.update(state="published", progress=1.0, message="published", error=None,
                  remote_id=out.get("remote_id"), url=out.get("url"), published_at=now)
        records.append_ledger(p, str(acc.get("id")), lang, now)
        records.clear_resume(job_id, clip, lang, p)
        log(f"{tag}: {name} published: {out.get('url') or out.get('remote_id')}")
        results[p] = records.public_status(sw.data)  # type: ignore[assignment]
    return results


def all_failed(results: dict[str, dict]) -> str | None:
    """First failure message when every platform failed, else None."""
    if not results:
        return None
    states = [r.get("state") if r else None for r in results.values()]
    if all(s == "failed" for s in states):
        for r in results.values():
            if r and r.get("message"):
                return str(r["message"])
        return "publish failed"
    return None


__all__ = ["run_publish", "all_failed", "PublishConflict"]
