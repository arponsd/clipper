"""`autoclipper schedule add|list|cancel|edit` (G6, docs/CONTRACT_STEP4.md section 15.6).

Schedules are stored in data/schedules.json; they fire only while `autoclipper serve` is running (its
scheduler thread). Exit codes: 0 ok, 2 usage / not ready (`error: ...`, no traceback).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import LANG_NAMES, PLATFORM_NAMES, PLATFORMS


def _out(text: str = "") -> None:
    print(text, flush=True)


def _when(s: dict) -> str:
    from .schedules import fmt_local
    return fmt_local(float(s["publish_at"]), s.get("timezone") or "")


def _print_schedule(s: dict) -> None:
    _out(f"{s['id'] or '(dry run)'}  {s['state']:<9} {_when(s)}  {s['job_id']} clip {int(s['clip']):02d} "
         f"{LANG_NAMES.get(s['lang'], s['lang'])}  {s.get('title') or ''}")
    for p, it in s["items"].items():
        _out(f"    {PLATFORM_NAMES[p]:<9} {it['state']:<10} {it['mode']:<6} {it.get('message') or ''}")
        if it.get("url"):
            _out(f"              {it['url']}")


def cmd_add(args: argparse.Namespace) -> int:
    from .cli import _platforms, _print_plan, _problem_exit, _settings
    from . import schedules, service

    settings = _settings()
    platforms = _platforms(args.platform)
    dry = bool(args.dry_run) or settings.publish.dry_run
    try:
        t, label = schedules.parse_time(args.at, args.tz, settings.publish.schedule.timezone)
        if dry or not args.yes:
            res = schedules.preview(args.job, args.clip, args.lang, platforms, t, label, settings)
            for plan in res["plans"].values():
                _print_plan(plan)
                sch = plan.get("schedule") or {}
                _out(f"   schedule: {sch.get('mode')} - {sch.get('mode_note')}")
            _print_schedule(res["schedule"])
            if dry:
                _out("\nDry run: nothing was scheduled." + ("" if args.dry_run else
                     " Turn dry run off (`autoclipper settings set publish.dry_run=false`) to schedule for real."))
                if args.yes and not args.dry_run:
                    # same as the web API (409): --yes asked for a real schedule, and none was made
                    print("error: publishing is in dry-run mode - nothing was scheduled; turn dry run off to "
                          "schedule for real", flush=True, file=sys.stderr)
                    return 2
                return 0
            print("error: add --yes to schedule for real (it posts at that time without asking again)",
                  flush=True, file=sys.stderr)
            return 2
        s = schedules.create(args.job, args.clip, args.lang, platforms, t, label, settings)
    except (service.PublishInputError, service.PublishConflict, service.PublishNotFound) as e:
        return _problem_exit(e)
    _print_schedule(s)
    _out("\nScheduled. It is posted by `autoclipper serve`: keep the server running at that time.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    from . import schedules

    rows = schedules.list_schedules(args.state)
    if args.json:
        _out(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        _out("no schedules")
    for s in rows:
        _print_schedule(s)
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    from .cli import _platforms, _problem_exit
    from . import schedules, service

    try:
        s = schedules.cancel(args.id, _platforms(args.platform) or None)
    except (service.PublishInputError, service.PublishConflict, service.PublishNotFound) as e:
        return _problem_exit(e)
    _print_schedule(s)
    for p, why in (s.get("refused") or {}).items():
        _out(f"    {PLATFORM_NAMES.get(p, p)} not cancelled: {'; '.join(why)}")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    from .cli import _problem_exit, _settings
    from . import schedules, service

    settings = _settings()
    try:
        t, label = schedules.parse_time(args.at, args.tz, settings.publish.schedule.timezone)
        s = schedules.edit(args.id, t, label, settings)
    except (service.PublishInputError, service.PublishConflict, service.PublishNotFound) as e:
        return _problem_exit(e)
    _print_schedule(s)
    return 0


def register(sub: Any, common: argparse.ArgumentParser) -> None:
    from .cli import _non_negative_int

    sc = sub.add_parser("schedule", parents=[common], help="schedule clips to publish later (add, list, cancel, edit)")
    sc.set_defaults(func=cmd_list, state="upcoming", json=False)
    ss = sc.add_subparsers(dest="schedule_command", metavar="<add|list|cancel|edit>")
    ad = ss.add_parser("add", parents=[common], help="schedule a clip-lang (dry run unless settings say otherwise)")
    ad.add_argument("job", help="job id")
    ad.add_argument("--clip", type=_non_negative_int, required=True)
    ad.add_argument("--lang", choices=["en", "hi"], required=True)
    ad.add_argument("--platform", action="append", required=True, choices=[*PLATFORMS, "all"],
                    help="youtube, facebook, instagram or all (repeatable)")
    ad.add_argument("--at", required=True, help='local time, e.g. "2026-11-24 18:30" (or with +06:00)')
    ad.add_argument("--tz", default=None, help="time zone name, e.g. Asia/Dhaka (default: settings, else this computer)")
    ad.add_argument("--dry-run", action="store_true", help="only show the plan with the time")
    ad.add_argument("--yes", action="store_true", help="confirm (needs publish.dry_run=false)")
    ad.set_defaults(func=cmd_add)
    ls = ss.add_parser("list", parents=[common], help="list schedules")
    ls.add_argument("--state", default="all", choices=["all", "upcoming", "done", "missed", "failed", "cancelled"])
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)
    cn = ss.add_parser("cancel", parents=[common], help="cancel a schedule (or some platforms of it)")
    cn.add_argument("id", help="schedule id (s_...)")
    cn.add_argument("--platform", action="append", choices=[*PLATFORMS, "all"])
    cn.set_defaults(func=cmd_cancel)
    ed = ss.add_parser("edit", parents=[common], help="move a schedule that has not started to a new time")
    ed.add_argument("id")
    ed.add_argument("--at", required=True)
    ed.add_argument("--tz", default=None)
    ed.set_defaults(func=cmd_edit)
