"""Command line interface: `autoclipper <command> ...` (entry point in pyproject.toml).

Exit codes: 0 ok, 1 error, 2 usage error.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
import types
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pydantic import ValidationError

from . import __version__
from .config import FONTS_DIR, Settings, data_dir, load_settings, save_settings, settings_path

MIN_NODE_MAJOR = 22
REQUIRED_FILTERS = ("subtitles", "ass", "amix", "atempo")
REQUIRED_FONTS = ("Noto Sans", "Noto Sans Devanagari")


class CLIError(RuntimeError):
    """Expected, user-facing error: printed as `error: <message>`, exit code 1."""


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def _out(text: str = "") -> None:
    print(text, flush=True)


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {n}")
    return n


def _non_negative_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {n}")
    return n


def _positive_float(value: str) -> float:
    try:
        x = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected seconds as a number, got {value!r}") from None
    if not x > 0 or x != x or x == float("inf"):
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, got {value!r}")
    return x


def _langs_arg(value: str) -> list[str]:
    from .pipeline import PipelineError, parse_langs

    try:
        langs = parse_langs(value)
    except PipelineError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    assert langs is not None
    return langs


def _validation_text(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg', 'invalid')}" if loc else str(err.get("msg", "invalid")))
    return "; ".join(parts)


def _load_settings() -> Settings:
    try:
        return load_settings()
    except ValidationError as e:
        raise CLIError(f"{settings_path()} is invalid: {_validation_text(e)} "
                       "(fix it with `autoclipper settings set ...` or delete the file to reset)") from e
    except json.JSONDecodeError as e:
        raise CLIError(f"{settings_path()} is not valid JSON: {e}") from e


# ---------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    from .joblock import hold_job_lock
    from .jobstore import JobStore
    from .pipeline import run_job

    settings = _load_settings()
    job_id = args.job
    source = args.source
    if not job_id and source:
        # Create the job first (as the web worker does) so job.lock covers the whole run: a web server
        # started meanwhile then sees a live owner and neither marks it interrupted nor resumes it.
        job_id = JobStore.create(source).job.id
    with hold_job_lock(job_id) if job_id else contextlib.nullcontext():
        store = run_job(source, settings=settings, langs=args.langs, count=args.clips,
                        min_seconds=args.min, max_seconds=args.max, job_id=job_id, log=_out, debug=args.debug)
    _out()
    _print_status(store.job, store.dir)
    return 0 if store.job.status == "done" else 1


def cmd_rerender(args: argparse.Namespace) -> int:
    from .joblock import hold_job_lock
    from .pipeline import rerender

    settings = _load_settings()
    with hold_job_lock(args.job):
        final = rerender(args.job, args.clip, args.lang, settings=settings, log=_out, debug=args.debug)
    _out(f"final: {final}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from .jobstore import JobStore

    try:
        store = JobStore.load(args.job)
    except FileNotFoundError:
        raise CLIError(f"job {args.job!r} not found (see `autoclipper jobs`)") from None
    _print_status(store.job, store.dir)
    return 0


def _print_status(job: Any, job_dir: Path) -> None:
    _out(f"job {job.id}  status={job.status}")
    _out(f"  source: {job.source}")
    _out(f"  dir:    {job_dir}")
    if job.error:
        _out(f"  error:  {job.error}")
    _out("  stages: " + "  ".join(f"{name}={st.status}" for name, st in job.stages.items()))
    if not job.clips:
        return
    for clip in job.clips:
        m = clip.moment
        _out(f"  clip {clip.index:02d}  {m.start:.1f}-{m.end:.1f}s  score={m.score:.2f}  {m.hook}".rstrip())
        for lang, st in clip.langs.items():
            stages = " ".join(f"{name}={s.status}" for name, s in st.stages.items())
            _out(f"    {lang}: {stages}")
            if st.final and not st.error:
                _out(f"        final: {job_dir / st.final}")
            if st.error:
                _out(f"        error: {st.error}")


def cmd_jobs(_args: argparse.Namespace) -> int:
    from .jobstore import JobStore
    from .models import Job

    ids = JobStore.list_ids()
    if not ids:
        _out("no jobs yet - start one with `autoclipper run <file-or-url>`")
        return 0
    from .jobstore import jobs_root

    for job_id in ids:
        path = jobs_root() / job_id / "job.json"
        try:
            job = Job.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            _out(f"{job_id}  (unreadable job.json: {e.__class__.__name__})")
            continue
        n_final = sum(1 for c in job.clips for st in c.langs.values() if st.final and not st.error)
        n_total = sum(len(c.langs) for c in job.clips)
        _out(f"{job.id}  {job.status:<7}  {n_final}/{n_total} rendered  {job.source}")
    return 0


def cmd_profiles(_args: argparse.Namespace) -> int:
    from .voicebox import VoiceboxClient

    s = _load_settings()
    client = VoiceboxClient.from_settings(s.voicebox)
    profiles = client.list_profiles()
    if not profiles:
        _out(f"Voicebox at {s.voicebox.url} has no voice profiles - create one in the Voicebox app first")
    for p in profiles:
        pid = p.get("id") or p.get("profile_id") or "?"
        name = p.get("name", "")
        extra = {k: v for k, v in p.items() if k in ("language", "engine", "description") and v}
        tail = "  " + json.dumps(extra, ensure_ascii=False) if extra else ""
        _out(f"{pid}  {name}{tail}")
    _out()
    _out(f"english profile_id = {s.voicebox.english.profile_id or '(not set)'}  engine = {s.voicebox.english.engine}")
    _out(f"hindi   profile_id = {s.voicebox.hindi.profile_id or '(not set)'}  engine = chatterbox (always)")
    _out("set with: autoclipper settings set voicebox.english.profile_id=<id>")
    _out("          autoclipper settings set voicebox.hindi.profile_id=<id>")
    return 0


def cmd_settings_show(_args: argparse.Namespace) -> int:
    s = _load_settings()
    _out(f"# {settings_path()}")
    _out(json.dumps(s.model_dump(), indent=2, ensure_ascii=False))
    return 0


def parse_value(raw: str) -> Any:
    """JSON if it parses (numbers, true/false/null, lists, quoted strings), else the raw string."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _is_str_field(parts: list[str]) -> bool:
    """True when the Settings field at ``parts`` is annotated as str or str | None."""
    model: Any = Settings
    field = None
    for part in parts:
        fields = getattr(model, "model_fields", None)
        if not fields or part not in fields:
            return False
        field = fields[part]
        model = field.annotation
    if field is None:
        return False
    ann = field.annotation
    if ann is str:
        return True
    args = [a for a in typing.get_args(ann) if a is not type(None)]
    return typing.get_origin(ann) in (typing.Union, types.UnionType) and args == [str]


def apply_setting(settings: Settings, assignment: str) -> Settings:
    """`key.path=value` -> new validated Settings. Unknown keys are an error (no silent typos)."""
    if "=" not in assignment:
        raise CLIError(f"expected key.path=value, got {assignment!r}")
    key, raw = assignment.split("=", 1)
    key = key.strip()
    parts = [p for p in key.split(".") if p]
    if not parts:
        raise CLIError(f"empty setting name in {assignment!r}")
    data = settings.model_dump()
    node: Any = data
    for i, part in enumerate(parts[:-1]):
        if not isinstance(node, dict) or part not in node:
            raise CLIError(f"unknown setting {'.'.join(parts[:i + 1])!r} (see `autoclipper settings show`)")
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        raise CLIError(f"unknown setting {key!r} (see `autoclipper settings show`)")
    if isinstance(node[parts[-1]], dict):
        raise CLIError(f"{key!r} is a group; set one of its keys, e.g. {key}.{next(iter(node[parts[-1]]))}=...")
    value = parse_value(raw)
    if (value is not None and not isinstance(value, str) and not raw.strip().startswith(('"', "'"))
            and (isinstance(node[parts[-1]], str) or _is_str_field(parts))):
        # e.g. voicebox.english.profile_id=12345 or captions.primary_color=1e5: keep the text as typed.
        value = raw.strip()
    if key == "languages" and isinstance(value, str):
        value = [x.strip() for x in value.split(",") if x.strip()]
    node[parts[-1]] = value
    try:
        new = Settings.model_validate(data)
    except ValidationError as e:
        raise CLIError(f"invalid value for {key}: {_validation_text(e)}") from e
    if new.clips.min_seconds > new.clips.max_seconds:
        raise CLIError(f"clips.min_seconds ({new.clips.min_seconds}) must not exceed clips.max_seconds "
                       f"({new.clips.max_seconds})")
    return new


def cmd_settings_set(args: argparse.Namespace) -> int:
    s = _load_settings()
    for assignment in args.assignments:
        s = apply_setting(s, assignment)
    save_settings(s)
    for assignment in args.assignments:
        key = assignment.split("=", 1)[0].strip()
        node: Any = s.model_dump()
        for part in key.split("."):
            node = node[part]
        _out(f"{key} = {json.dumps(node, ensure_ascii=False)}")
    _out(f"saved {settings_path()}")
    return 0


# ---------------------------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    level: str  # "ok" | "warn" | "fail"
    detail: str


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _run_cmd(args: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=30, check=False)


def check_binaries(which: Callable[[str], str | None] = shutil.which) -> list[Check]:
    out = []
    for name in ("ffmpeg", "ffprobe"):
        path = which(name)
        out.append(Check(name, "ok", path) if path else
                   Check(name, "fail", f"{name} not found on PATH - install FFmpeg (see README)"))
    return out


def check_filters(run: Runner = _run_cmd, which: Callable[[str], str | None] = shutil.which) -> Check:
    ffmpeg = which("ffmpeg")
    if not ffmpeg:
        return Check("ffmpeg filters", "fail", "ffmpeg missing")
    try:
        proc = run([ffmpeg, "-hide_banner", "-filters"])
    except (OSError, subprocess.SubprocessError) as e:
        return Check("ffmpeg filters", "fail", f"could not run ffmpeg -filters: {e}")
    names = set()
    for line in proc.stdout.splitlines():
        cols = line.split()
        if len(cols) >= 2 and re.fullmatch(r"[A-Z.|]{2,4}", cols[0]):
            names.add(cols[1])
    missing = [f for f in REQUIRED_FILTERS if f not in names]
    if missing:
        return Check("ffmpeg filters", "fail",
                     f"missing {', '.join(missing)} - install an FFmpeg build with libass (e.g. gyan.dev full build on Windows)")
    return Check("ffmpeg filters", "ok", ", ".join(REQUIRED_FILTERS))


def _font_files(fonts_dir: Path) -> list[str]:
    if not fonts_dir.is_dir():
        return []
    return [p.stem.replace(" ", "").replace("-", "").lower() for p in fonts_dir.iterdir()
            if p.suffix.lower() in (".ttf", ".otf")]


def _family_in_stems(family: str, stems: list[str]) -> bool:
    key = family.replace(" ", "").lower()
    if family == "Noto Sans":
        return any(s.startswith(key) and not s.startswith(key + "devanagari") for s in stems)
    return any(s.startswith(key) for s in stems)


def windows_font_dirs(environ: Mapping[str, str] | None = None) -> list[Path]:
    """%WINDIR%\\Fonts (all users) and %LOCALAPPDATA%\\Microsoft\\Windows\\Fonts (current user)."""
    env = os.environ if environ is None else environ
    dirs = [Path(env.get("WINDIR") or env.get("SystemRoot") or r"C:\Windows") / "Fonts"]
    if env.get("LOCALAPPDATA"):
        dirs.append(Path(env["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts")
    return dirs


def check_fonts(run: Runner = _run_cmd, which: Callable[[str], str | None] = shutil.which,
                fonts_dir: Path = FONTS_DIR, system_font_dirs: Sequence[Path] | None = None) -> Check:
    """Fonts are found in assets/fonts, then (Windows, no fc-list) the Windows font folders by file
    name, then through fc-list. `system_font_dirs` defaults to the Windows folders when os.name == 'nt'."""
    found: dict[str, str] = {}
    stems = _font_files(fonts_dir)
    for family in REQUIRED_FONTS:
        if _family_in_stems(family, stems):
            found[family] = "assets/fonts"
    if system_font_dirs is None:
        system_font_dirs = windows_font_dirs() if os.name == "nt" else []
    if len(found) < len(REQUIRED_FONTS) and system_font_dirs:
        sys_stems: list[str] = []
        for d in system_font_dirs:
            try:
                sys_stems += _font_files(Path(d))
            except OSError:  # unreadable folder: treat as empty
                pass
        for family in REQUIRED_FONTS:
            if family not in found and _family_in_stems(family, sys_stems):
                found[family] = "system"
    fc = which("fc-list")
    if fc and len(found) < len(REQUIRED_FONTS):
        try:
            proc = run([fc, ":", "family"])
            families = {f.strip() for line in proc.stdout.splitlines() for f in line.split(",")}
            for family in REQUIRED_FONTS:
                if family not in found and family in families:
                    found[family] = "system"
        except (OSError, subprocess.SubprocessError):
            pass
    missing = [f for f in REQUIRED_FONTS if f not in found]
    if missing:
        return Check("fonts", "fail", f"missing {', '.join(missing)} - copy the .ttf files into {fonts_dir} "
                                      "or install them system-wide (see README)")
    return Check("fonts", "ok", ", ".join(f"{k} ({v})" for k, v in found.items()))


def check_voicebox(settings: Settings, client_factory: Callable[[Settings], Any] | None = None) -> list[Check]:
    out = []
    try:
        if client_factory is None:
            from .voicebox import VoiceboxClient

            client = VoiceboxClient.from_settings(settings.voicebox, timeout=10.0)
        else:
            client = client_factory(settings)
        client.health()
        out.append(Check("voicebox", "ok", f"reachable at {settings.voicebox.url}"))
    except Exception as e:  # any failure here is reported, never raised
        out.append(Check("voicebox", "fail", str(e) or f"not reachable at {settings.voicebox.url}"))
    for lang in settings.languages:
        prof = settings.voice_for(lang)
        key = "voicebox.hindi.profile_id" if lang == "hi" else "voicebox.english.profile_id"
        if prof.profile_id:
            out.append(Check(f"voice profile {lang}", "ok", prof.profile_id))
        else:
            out.append(Check(f"voice profile {lang}", "fail",
                             f"not set - run `autoclipper profiles`, then `autoclipper settings set {key}=<id>`"))
    return out


def _ant_profile_available() -> bool:
    try:
        import anthropic

        from .llm import has_credentials
        return has_credentials(anthropic.Anthropic())
    except Exception:
        return False


def check_api_key(settings: Settings, environ: Mapping[str, str] | None = None,
                  profile_available: bool | None = None) -> Check:
    env = os.environ if environ is None else environ
    name = settings.llm.api_key_env
    if env.get(name, "").strip():
        return Check("api key", "ok", f"{name} is set")
    if profile_available is None:
        profile_available = _ant_profile_available()
    if profile_available:
        return Check("api key", "ok", f"{name} not set; using the `ant auth login` profile")
    return Check("api key", "fail",
                 f"no credentials - run `ant auth login`, or put {name}=... in .env (see .env.example)")


def parse_node_major(version: str) -> int | None:
    m = re.match(r"\s*v?(\d+)", version)
    return int(m.group(1)) if m else None


def check_node(run: Runner = _run_cmd, which: Callable[[str], str | None] = shutil.which) -> Check:
    node = which("node")
    if not node:
        return Check("node", "warn", f"not found - only needed later for HyperFrames captions (Node {MIN_NODE_MAJOR}+)")
    try:
        version = run([node, "--version"]).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return Check("node", "warn", f"could not run node --version: {e}")
    major = parse_node_major(version)
    if major is None or major < MIN_NODE_MAJOR:
        return Check("node", "warn", f"{version or 'unknown version'} - HyperFrames (step 3) needs Node "
                                     f"{MIN_NODE_MAJOR}+; not needed for the current CLI")
    return Check("node", "ok", version)


def run_doctor(settings: Settings) -> list[Check]:
    checks = [Check("python", "ok", sys.version.split()[0]), Check("data dir", "ok", str(data_dir()))]
    checks += check_binaries()
    checks.append(check_filters())
    checks.append(check_fonts())
    checks += check_voicebox(settings)
    checks.append(check_api_key(settings))
    checks.append(check_node())
    return checks


def cmd_doctor(_args: argparse.Namespace) -> int:
    checks = run_doctor(_load_settings())
    labels = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    for c in checks:
        _out(f"[{labels[c.level]}] {c.name:<18} {c.detail}")
    fails = sum(c.level == "fail" for c in checks)
    warns = sum(c.level == "warn" for c in checks)
    _out()
    _out(f"{fails} problem(s), {warns} warning(s)" if fails or warns else "all checks passed")
    return 1 if fails else 0


# ---------------------------------------------------------------------------------------------
# serve (step 2 web UI)
# ---------------------------------------------------------------------------------------------
def _port(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a port number, got {value!r}") from None
    if not 1 <= n <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 1..65535, got {n}")
    return n


def cmd_serve(args: argparse.Namespace) -> int:
    import threading
    import webbrowser

    import uvicorn

    from .web.auth import allowed_hosts_for, install_access_log_filter, is_loopback
    from .web.server import create_app

    _load_settings()  # fail early with a clear message when settings.json is broken
    host = args.host
    if not is_loopback(host):
        _err(f"warning: binding {host}: the UI token is only given to loopback clients; "
             "other machines cannot use the app")
    shown = host if is_loopback(host) and host != "localhost" else "127.0.0.1"
    if ":" in shown and not shown.startswith("["):
        shown = f"[{shown}]"
    url = f"http://{shown}:{args.port}/"
    install_access_log_filter()
    app = create_app(allowed_hosts=allowed_hosts_for(host), stop_on_signal=True)
    _out(f"Auto Clipper web UI: {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        def _open() -> None:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001 - never fail because of the browser
                pass

        threading.Timer(1.0, _open).start()
    try:
        uvicorn.run(app, host=host, port=args.port, log_level="info")
    except KeyboardInterrupt:
        pass
    finally:
        app.state.worker.stop()
    return 0


# ---------------------------------------------------------------------------------------------
# parser / main
# ---------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autoclipper",
                                description="Turn a long video into captioned 9:16 shorts with EN/HI narration.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--debug", action="store_true", help="print tracebacks on errors")
    # also accept `autoclipper <command> --debug`; SUPPRESS keeps the top-level value when absent
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--debug", action="store_true", default=argparse.SUPPRESS,
                        help="print tracebacks on errors")
    sub = p.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    r = sub.add_parser("run", parents=[common], help="process a video file or URL end to end")
    r.add_argument("source", nargs="?", default="", help="video file path or http(s) URL")
    r.add_argument("--clips", type=_positive_int, help="number of clips (default: settings clips.count)")
    r.add_argument("--min", type=_positive_float, help="min clip length in seconds")
    r.add_argument("--max", type=_positive_float, help="max clip length in seconds")
    r.add_argument("--langs", type=_langs_arg, help="comma separated languages, e.g. en,hi")
    r.add_argument("--job", help="resume an existing job id instead of creating a new one")
    r.set_defaults(func=cmd_run)

    rr = sub.add_parser("rerender", parents=[common], help="re-render one clip from its edited script.json")
    rr.add_argument("job", help="job id")
    rr.add_argument("clip", type=_non_negative_int, help="clip index (0-based, as in clips/NN)")
    rr.add_argument("lang", choices=["en", "hi"], help="language")
    rr.set_defaults(func=cmd_rerender)

    st = sub.add_parser("status", parents=[common], help="show a job's progress")
    st.add_argument("job", help="job id")
    st.set_defaults(func=cmd_status)

    sub.add_parser("jobs", parents=[common], help="list jobs").set_defaults(func=cmd_jobs)
    sub.add_parser("profiles", parents=[common], help="list Voicebox voice profiles").set_defaults(func=cmd_profiles)

    se = sub.add_parser("settings", parents=[common], help="show or change settings (data/settings.json)")
    se_sub = se.add_subparsers(dest="settings_command", metavar="<show|set>")
    se_sub.required = True
    se_sub.add_parser("show", parents=[common], help="print all settings").set_defaults(func=cmd_settings_show)
    ss = se_sub.add_parser("set", parents=[common], help="set key.path=value (value parsed as JSON, else string)")
    ss.add_argument("assignments", nargs="+", metavar="key.path=value")
    ss.set_defaults(func=cmd_settings_set)

    sub.add_parser("doctor", parents=[common], help="check ffmpeg, fonts, Voicebox, API key, node").set_defaults(func=cmd_doctor)

    sv = sub.add_parser("serve", parents=[common], help="start the local web UI (http://127.0.0.1:8765/)")
    sv.add_argument("--host", default="127.0.0.1", help="address to bind (default 127.0.0.1, loopback only)")
    sv.add_argument("--port", type=_port, default=8765, help="port (default 8765)")
    sv.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    sv.set_defaults(func=cmd_serve)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # never crash printing Devanagari on a legacy Windows code page
            stream.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as e:  # argparse: 0 for --help/--version, 2 for usage errors
        return int(e.code) if isinstance(e.code, int) else 2
    if args.command == "run" and not args.source and not args.job:
        parser.print_usage(sys.stderr)
        _err("autoclipper run: error: give a video file/URL, or --job <id> to resume")
        return 2
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        _err("interrupted")
        return 130
    except Exception as e:
        if args.debug:
            traceback.print_exc()
        msg = str(e).strip() or type(e).__name__
        _err(f"error: {msg}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
