"""`autoclipper publish ...` and `autoclipper accounts ...` (CONTRACT_STEP4 section 10).

Exit codes: 0 ok / already published, 1 a platform failed, 2 usage / not ready. Tokens are read with
getpass (or AUTOCLIPPER_PAGE_TOKEN / AUTOCLIPPER_IG_TOKEN), never from argv, and never printed.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import LANG_NAMES, PLATFORM_NAMES, PLATFORMS

MAX_CLIENT_FILE = 64 * 1024


def _out(text: str = "") -> None:
    print(text, flush=True)


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _non_negative_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from None
    if not 0 <= n <= 999:
        raise argparse.ArgumentTypeError(f"must be 0-999, got {n}")
    return n


def _platforms(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        for p in (PLATFORMS if v == "all" else (v,)):
            if p not in out:
                out.append(p)
    return out


def _settings() -> Any:
    from ..config import load_settings
    return load_settings()


def _problem_exit(e: Exception) -> int:
    from .service import PublishConflict, PublishInputError

    msg = getattr(e, "message", None) or str(e)
    _err(f"error: {msg}")
    errors = getattr(e, "errors", None)
    if isinstance(e, (PublishConflict, PublishInputError)) and isinstance(errors, dict):
        for k, v in errors.items():
            for line in (v if isinstance(v, list) else [v]):
                _err(f"  {k}: {line}")
    return 2


def _print_plan(plan: dict) -> None:
    p = plan["platform"]
    acc = plan.get("account")
    _out(f"== {PLATFORM_NAMES[p]} ({LANG_NAMES.get(plan['lang'], plan['lang'])}) - API {plan['api_version']}")
    _out(f"   account: {acc['name'] + ' (' + acc['id'] + ')' if acc else 'not connected'}")
    v = plan["video"]
    _out(f"   video:   {v['path']}  {v['size']} bytes  {v['duration']:.2f} s  {v['width']}x{v['height']}")
    _out("   metadata: " + json.dumps(plan["metadata"], ensure_ascii=False))
    for r in plan["requests"]:
        _out(f"   {r['step']:>14}: {r['method']} {r['url']}")
    for w in plan.get("warnings") or []:
        _out(f"   warning: {w}")
    if plan.get("problems"):
        for x in plan["problems"]:
            _out(f"   PROBLEM: {x}")
    else:
        _out("   ready: this request plan would be sent")


# ---------------------------------------------------------------------------------------------
def cmd_publish(args: argparse.Namespace) -> int:
    from ..joblock import JobLocked, hold_job_lock
    from . import records, runner, service

    platforms = _platforms(args.platform)
    settings = _settings()
    dry = bool(args.dry_run) or settings.publish.dry_run
    try:
        if dry:
            res = service.dry_run(args.job, args.clip, args.lang, platforms, settings)
            for plan in res["plans"].values():
                _print_plan(plan)
                _out(f"   plan saved: {records.plan_path(args.job, args.clip, args.lang, plan['platform'])}")
            if not args.dry_run:
                _out("\nDry run is ON in settings: nothing was posted. Turn it off with "
                     "`autoclipper settings set publish.dry_run=false` to publish for real.")
            else:
                _out("\nDry run: nothing was posted.")
            return 0
        to_run, already = service.check_live(args.job, args.clip, args.lang, platforms, settings,
                                             force=bool(args.force))
    except (service.PublishInputError, service.PublishConflict, service.PublishNotFound) as e:
        return _problem_exit(e)
    for p, st in already.items():
        _out(f"{PLATFORM_NAMES[p]}: already published {st.get('url') or st.get('remote_id')} (use --force to post again)")
    if not to_run:
        return 0
    if not args.yes:
        item, extra = service.load_item(args.job, args.clip, args.lang)
        plans = service.build_plans(item, extra, to_run, settings.publish)
        _out("This would post FOR REAL:")
        for p in to_run:
            plan = plans[p]
            acc = plan.get("account") or {}
            extra_info = ""
            if p == "youtube":
                extra_info = f", privacy {plan['metadata']['status']['privacyStatus']}"
            title = plan["metadata"].get("snippet", {}).get("title") if p == "youtube" else \
                plan["metadata"].get("title") or (plan["metadata"].get("caption", "").split("\n")[0])
            _out(f"  {PLATFORM_NAMES[p]} -> {acc.get('name')} ({acc.get('id')}){extra_info}: {title}")
        _err("error: add --yes to publish for real")
        return 2
    try:
        with hold_job_lock(args.job):
            results = runner.run_publish(args.job, args.clip, args.lang, platforms=to_run, force=bool(args.force),
                                         settings=settings, log=lambda line: _out(line) if line else None)
    except JobLocked as e:
        _err(f"error: {e}")
        return 2
    except (service.PublishInputError, service.PublishConflict, service.PublishNotFound) as e:
        return _problem_exit(e)
    failed = False
    _out()
    for p, st in results.items():
        if st.get("state") == "published":
            _out(f"{PLATFORM_NAMES[p]}: published {st.get('url') or st.get('remote_id')}")
        else:
            failed = True
            err = st.get("error") or {}
            _out(f"{PLATFORM_NAMES[p]}: FAILED ({err.get('kind', 'unknown')}): {err.get('message') or st.get('message')}")
    return 1 if failed else 0


def cmd_accounts_status(_args: argparse.Namespace) -> int:
    from . import service

    st = service.accounts_status(_settings())
    _out(f"dry run: {'ON (nothing is posted)' if st['dry_run'] else 'OFF (Publish posts for real)'}")
    gc = st["google_client"]
    _out(f"Google OAuth client: {'configured ' + (gc['client_id_hint'] or '') if gc['configured'] else 'not configured'}")
    for lang, plats in st["langs"].items():
        for p, a in plats.items():
            state = "connected" if a["connected"] else "not connected"
            en = "enabled" if a["enabled"] else "disabled"
            name = f" -> {a['account_name']} ({a['account_id']})" if a["connected"] else ""
            _out(f"{lang}  {PLATFORM_NAMES[p]:<9}  {state:<13} {en:<8}{name}")
            for x in a["problems"]:
                _out(f"      problem: {x}")
    return 0


def cmd_google_client(args: argparse.Namespace) -> int:
    from . import service

    path = Path(args.file)
    try:
        if path.stat().st_size > MAX_CLIENT_FILE:
            _err("error: file is too large to be a client_secret.json (max 64 KiB)")
            return 2
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        _err(f"error: cannot read {path}: {e.strerror or e}")
        return 2
    try:
        res = service.set_google_client(text)
    except service.PublishInputError as e:
        return _problem_exit(e)
    _out(f"Google OAuth client saved ({res['client_id_hint']}). You can delete the downloaded file now.")
    return 0


def _token(env: str, prompt: str, *, optional: bool = False) -> str | None:
    v = os.environ.get(env)
    if v:
        return v.strip()
    v = getpass.getpass(prompt)
    v = v.strip()
    if not v and optional:
        return None
    return v


def cmd_connect(args: argparse.Namespace) -> int:
    from . import oauth_google, service

    try:
        if args.target == "youtube":
            acc = oauth_google.run_loopback_flow(args.lang, open_browser=not args.no_browser, announce=_out)
            _out(f"YouTube ({LANG_NAMES[args.lang]}) connected: {acc.get('account_name')} ({acc.get('account_id')})")
            for x in acc.get("problems") or []:
                _out(f"  problem: {x}")
            return 0
        if not args.page_id:
            _err("error: --page-id is required for meta")
            return 2
        page_token = _token("AUTOCLIPPER_PAGE_TOKEN", "Facebook Page access token (hidden): ")
        ig_token = None
        if args.ig_user_id:
            ig_token = _token("AUTOCLIPPER_IG_TOKEN", "Instagram token (hidden, Enter = use the Page token): ",
                              optional=True)
        res = service.connect_meta(args.lang, page_id=args.page_id, page_token=page_token or "",
                                   ig_user_id=args.ig_user_id, ig_token=ig_token)
    except (service.PublishInputError, service.PublishConflict) as e:
        return _problem_exit(e)
    except (service.OAuthStateError, service.PublishUpstreamError, TimeoutError) as e:
        _err(f"error: {getattr(e, 'message', None) or e}")
        return 1
    for p in ("facebook", "instagram"):
        a = res[p]
        _out(f"{PLATFORM_NAMES[p]} ({LANG_NAMES[args.lang]}): "
             + (f"connected {a['account_name']} ({a['account_id']})" if a["connected"] else "not connected"))
        for x in a["problems"]:
            _out(f"  problem: {x}")
    return 0


def cmd_disconnect(args: argparse.Namespace) -> int:
    from . import service

    service.disconnect(args.lang, args.target, revoke=not args.no_revoke)
    _out(f"{args.target} ({LANG_NAMES[args.lang]}) disconnected; the stored credentials were deleted.")
    return 0


# ---------------------------------------------------------------------------------------------
def register(sub: Any, common: argparse.ArgumentParser) -> None:
    pb = sub.add_parser("publish", parents=[common],
                        help="publish a clip to YouTube/Facebook/Instagram (dry run unless settings say otherwise)")
    pb.add_argument("job", help="job id")
    pb.add_argument("--clip", type=_non_negative_int, required=True, help="clip index (as in clips/NN)")
    pb.add_argument("--lang", choices=["en", "hi"], required=True, help="language")
    pb.add_argument("--platform", action="append", required=True, choices=[*PLATFORMS, "all"],
                    help="youtube, facebook, instagram or all (repeatable)")
    pb.add_argument("--dry-run", action="store_true", help="only build and print the request plan")
    pb.add_argument("--force", action="store_true", help="post again even if already published")
    pb.add_argument("--yes", action="store_true", help="confirm a live publish (needs publish.dry_run=false)")
    pb.set_defaults(func=cmd_publish)

    ac = sub.add_parser("accounts", parents=[common], help="publishing accounts: status, connect, disconnect")
    ac.set_defaults(func=cmd_accounts_status)
    ac_sub = ac.add_subparsers(dest="accounts_command", metavar="<google-client|connect|disconnect>")
    gc = ac_sub.add_parser("google-client", parents=[common], help="store the Google client_secret.json (Desktop app)")
    gc.add_argument("file", help="path to client_secret.json")
    gc.set_defaults(func=cmd_google_client)
    cn = ac_sub.add_parser("connect", parents=[common], help="connect youtube (browser sign-in) or meta (Page token)")
    cn.add_argument("target", choices=["youtube", "meta"])
    cn.add_argument("--lang", choices=["en", "hi"], required=True)
    cn.add_argument("--no-browser", action="store_true", help="youtube: only print the sign-in URL")
    cn.add_argument("--page-id", help="meta: Facebook Page id")
    cn.add_argument("--ig-user-id", help="meta: Instagram professional account id linked to the Page")
    cn.set_defaults(func=cmd_connect)
    dc = ac_sub.add_parser("disconnect", parents=[common], help="delete stored credentials")
    dc.add_argument("target", choices=[*PLATFORMS, "meta"])
    dc.add_argument("--lang", choices=["en", "hi"], required=True)
    dc.add_argument("--no-revoke", action="store_true", help="youtube: do not revoke the token at Google")
    dc.set_defaults(func=cmd_disconnect)

    from .schedule_cli import register as register_schedule  # G6: `autoclipper schedule ...`
    register_schedule(sub, common)
