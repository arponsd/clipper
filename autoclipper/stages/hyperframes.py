"""HyperFrames caption engine (CONTRACT_STEP3 section 3): words.json -> transparent caption overlay video.

The overlay is rendered by the optional HyperFrames CLI (Node 22+, `npm --prefix <package_dir> install
hyperframes`) and composited over cut.mp4 by render.render_final. Every failure raises HyperFramesError (or
a subclass); render.render_clip turns that into an ASS fallback, so a job never fails because of this module.
The subprocess goes through a `Runner` seam so tests never need Node.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .. import config, ffmpeg
from ..config import HyperFramesSettings, Settings, expand_path
from ..interfaces import ProgressFn
from ..models import ClipScript, Word
from . import caption_style, hf_template
from ._atomic import write_text_atomic

log = logging.getLogger(__name__)

HF_PACKAGE = "hyperframes"
HF_VERSION_PIN = "0.9.52"
MIN_NODE_MAJOR = 22
README_HINT = "see README 'HyperFrames animated captions'"
ERROR_TAIL_LINES = 15
_KEEP_LINES = 30
_PROBE_TIMEOUT = 30.0

# Runner(args, cwd, env, timeout, on_line) -> return code
Runner = Callable[[list[str], Path, dict[str, str], float, Callable[[str], None]], int]


class HyperFramesError(RuntimeError):
    """Any HyperFrames failure; the caller falls back to ASS captions for that clip-language."""


class HyperFramesUnavailable(HyperFramesError):
    """node / the CLI / GSAP is missing or node is too old (no render was attempted)."""


class HyperFramesTimeout(HyperFramesError):
    """The overlay render ran longer than hyperframes.timeout_seconds (the process tree was killed)."""


@dataclass(frozen=True)
class HFEnv:
    node: Path
    cli: Path
    node_version: str
    hf_version: str | None
    gsap: Path  # vendored gsap.min.js inside the installed package


# --------------------------------------------------------------------------------------------- probing
def _run(args: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None,
         timeout: float = _PROBE_TIMEOUT) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(args, cwd=str(cwd) if cwd else None, env=dict(env) if env is not None else None,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, check=False)


def _node_names() -> tuple[str, ...]:
    return ("node.exe", "node") if os.name == "nt" else ("node",)


def resolve_node(s: HyperFramesSettings, which: Callable[[str], str | None] = shutil.which) -> Path | None:
    """<node_bin_dir>/node[.exe], then <node_bin_dir>/bin/node[.exe]; an empty node_bin_dir uses PATH."""
    if not s.node_bin_dir.strip():
        found = which("node")
        return Path(found) if found else None
    d = expand_path(s.node_bin_dir.strip())
    for sub in (d, d / "bin"):
        for name in _node_names():
            p = sub / name
            if p.is_file():
                return p
    return None


def parse_major(version: str) -> int | None:
    m = re.match(r"\s*v?(\d+)", version or "")
    return int(m.group(1)) if m else None


def node_version(node: Path, run: Callable[..., "subprocess.CompletedProcess[str]"] = _run) -> str:
    try:
        proc = run([str(node), "--version"])
    except (OSError, subprocess.SubprocessError) as e:
        raise HyperFramesUnavailable(f"could not run {node} --version: {e}") from e
    version = (proc.stdout or "").strip()
    if proc.returncode != 0 or parse_major(version) is None:
        raise HyperFramesUnavailable(f"could not read the node version from {node} "
                                     f"({version or (proc.stderr or '').strip()[:200] or 'no output'})")
    return version


def cli_path(s: HyperFramesSettings) -> Path:
    return expand_path(s.package_dir.strip()) / "node_modules" / HF_PACKAGE / "bin" / "hyperframes.mjs"


def resolve_cli(s: HyperFramesSettings) -> Path | None:
    p = cli_path(s)
    return p if p.is_file() else None


def hf_version(node: Path, cli: Path, run: Callable[..., "subprocess.CompletedProcess[str]"] = _run,
               env: Mapping[str, str] | None = None) -> str | None:
    try:
        proc = run([str(node), str(cli), "--version"], cwd=cli.parent, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not out:
        return None
    m = re.search(r"\d+\.\d+\.\d+\S*", out[-1])
    return m.group(0) if m else out[-1].strip()[:40]


def _version_key(name: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", name.split("@", 1)[-1]))


def find_gsap(cli: Path) -> Path | None:
    """The package's own vendored GSAP (dist/vendor/npm/gsap@<v>/dist/gsap.min.js), highest version."""
    vendor = cli.parent.parent / "dist" / "vendor" / "npm"
    try:
        dirs = sorted((d for d in vendor.glob("gsap@*") if d.is_dir()), key=lambda d: _version_key(d.name))
    except OSError:
        return None
    for d in reversed(dirs):
        f = d / "dist" / "gsap.min.js"
        if f.is_file():
            return f
    return None


def probe_env(s: HyperFramesSettings, *, which: Callable[[str], str | None] = shutil.which,
              run: Callable[..., "subprocess.CompletedProcess[str]"] = _run) -> HFEnv:
    """Everything needed to render, or HyperFramesUnavailable with an actionable message."""
    node = resolve_node(s, which)
    if node is None:
        where = f"in {s.node_bin_dir}" if s.node_bin_dir.strip() else "on PATH"
        raise HyperFramesUnavailable(f"node not found {where} - HyperFrames needs Node {MIN_NODE_MAJOR}+ "
                                     f"(set hyperframes.node_bin_dir; {README_HINT})")
    version = node_version(node, run)
    major = parse_major(version)
    if major is None or major < MIN_NODE_MAJOR:
        raise HyperFramesUnavailable(f"node {version} is too old - HyperFrames needs Node {MIN_NODE_MAJOR}+ "
                                     f"({node})")
    cli = resolve_cli(s)
    if cli is None:
        raise HyperFramesUnavailable(f"HyperFrames not installed at {cli_path(s)} - {README_HINT}")
    gsap = find_gsap(cli)
    if gsap is None:
        raise HyperFramesUnavailable(f"GSAP not found inside the HyperFrames package ({cli.parent.parent}) - "
                                     f"reinstall hyperframes@{HF_VERSION_PIN}")
    hv = hf_version(node, cli, run, env=child_env(s, None, node=node))
    return HFEnv(node=node, cli=cli, node_version=version, hf_version=hv, gsap=gsap)


_NO_TELEMETRY = {
    "HYPERFRAMES_NO_UPDATE_CHECK": "1",
    "HYPERFRAMES_SKIP_SKILLS": "1",
    "HYPERFRAMES_NO_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
    "HYPERFRAMES_NO_AUTO_INSTALL": "1",
    "HYPERFRAMES_NO_OPTIONAL_INSTALL": "1",
}


def child_env(s: HyperFramesSettings, tmp_dir: Path | None, base: Mapping[str, str] | None = None, *,
              api_key_env: str | None = None, node: Path | None = None) -> dict[str, str]:
    """Environment of every HyperFrames child: no secrets, node first on PATH, a private TMP dir."""
    env = dict(os.environ if base is None else base)
    for name in list(env):
        if name.upper().startswith("ANTHROPIC_"):
            del env[name]
    if api_key_env:
        env.pop(api_key_env, None)
    if node is None:
        node = resolve_node(s)
    if node is not None:
        path = env.get("PATH", "")
        env["PATH"] = str(node.parent) + (os.pathsep + path if path else "")
    if tmp_dir is not None:
        tmp_dir = Path(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        for k in ("TMPDIR", "TEMP", "TMP"):
            env[k] = str(tmp_dir)
    env.update(_NO_TELEMETRY)
    return env


# --------------------------------------------------------------------------------------------- composition
def _css_or_black(color: str) -> str:
    """The outline colour as CSS; the default opaque black stays "#000" (the step-3 page, byte for byte)."""
    css = ass_color_to_css(color)
    return "#000" if css == "rgba(0,0,0,1)" else css


def ass_color_to_css(color: str) -> str:
    """ASS &HAABBGGRR (AA 00 = opaque) -> CSS rgba()."""
    h = color.strip().upper().removeprefix("&H").rstrip("&")
    if not re.fullmatch(r"[0-9A-F]{1,8}", h):
        raise ValueError(f"not an ASS colour: {color!r}")
    h = h.rjust(8, "0")
    a, b, g, r = (int(h[i:i + 2], 16) for i in (0, 2, 4, 6))
    alpha = round(1 - a / 255, 2)
    return f"rgba({r},{g},{b},{alpha:g})"


def _key(name: str) -> str:
    return re.sub(r"[\s_-]+", "", name).lower()


def _font_in_dir(d: Path, family: str, bold: bool) -> Path | None:
    try:
        files = [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in (".ttf", ".otf")]
    except OSError:
        return None
    by_stem = {_key(p.stem): p for p in sorted(files)}
    fam = _key(family)
    for weight in (("bold", "regular", "") if bold else ("regular", "", "bold")):
        p = by_stem.get(fam + weight)
        if p is not None:
            return p
    return None


def _fc_match(family: str, bold: bool) -> Path | None:
    fc = shutil.which("fc-match")
    if not fc:
        return None
    pattern = f"{family}:bold" if bold else family
    try:
        proc = _run([fc, "-f", "%{family}\n%{file}", pattern], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (proc.stdout or "").splitlines()
    if proc.returncode != 0 or len(lines) < 2:
        return None
    families = {_key(f) for f in lines[0].split(",")}
    p = Path(lines[1].strip())
    if _key(family) in families and p.is_file() and p.suffix.lower() in (".ttf", ".otf"):
        return p
    return None


def find_font_file(family: str, *, bold: bool = True) -> Path | None:
    """assets/fonts by stem (NotoSansDevanagari-Bold, then -Regular), then fc-match (Linux/macOS),
    then the Windows font folders. None when nothing matches (the page then uses local(<family>))."""
    p = _font_in_dir(Path(config.FONTS_DIR), family, bold)
    if p is not None:
        return p
    if os.name != "nt":
        p = _fc_match(family, bold)
        if p is not None:
            return p
    else:
        from ..cli import windows_font_dirs

        for d in windows_font_dirs():
            p = _font_in_dir(d, family, bold)
            if p is not None:
                return p
    return None


# libass sizes a font so that (OS/2 usWinAscent + usWinDescent) == Fontsize (hhea ascender - descender
# when the OS/2 values are 0), not the em box. Chrome's font-size is the em box. To make captions.font_size
# look the same in both engines, the CSS size is font_size * unitsPerEm / (winAscent + winDescent).
ASS_OUTLINE_PX = 5  # the Outline of the ASS Default style (captions.build_ass)
DEFAULT_ASS_EM_RATIO = 0.75  # used when the font file is unknown/unreadable (Noto Sans 0.734, Devanagari 0.767)


def ass_em_ratio(font_file: Path | None) -> float:
    """unitsPerEm / (usWinAscent + usWinDescent) of a TTF/OTF, i.e. how big libass draws the em box
    relative to the ASS Fontsize. DEFAULT_ASS_EM_RATIO when the file is missing or not parseable."""
    if font_file is None:
        return DEFAULT_ASS_EM_RATIO
    try:
        data = Path(font_file).read_bytes()
        if data[:4] == b"ttcf":  # a collection: use the first face
            data_off = struct.unpack(">I", data[12:16])[0]
        else:
            data_off = 0
        num = struct.unpack(">H", data[data_off + 4:data_off + 6])[0]
        tables: dict[bytes, int] = {}
        for i in range(num):
            rec = data_off + 12 + 16 * i
            tag, _cs, off, _ln = struct.unpack(">4sIII", data[rec:rec + 16])
            tables[tag] = off
        head = tables[b"head"]
        upm = struct.unpack(">H", data[head + 18:head + 20])[0]
        total = 0
        if b"OS/2" in tables:
            o = tables[b"OS/2"]
            win_a, win_d = struct.unpack(">HH", data[o + 74:o + 78])
            total = win_a + win_d
        if total <= 0 and b"hhea" in tables:
            h = tables[b"hhea"]
            asc, desc = struct.unpack(">hh", data[h + 4:h + 8])
            total = asc - desc
        if upm <= 0 or total <= 0:
            return DEFAULT_ASS_EM_RATIO
        ratio = upm / total
    except (OSError, KeyError, struct.error, IndexError):
        return DEFAULT_ASS_EM_RATIO
    return ratio if 0.3 <= ratio <= 1.5 else DEFAULT_ASS_EM_RATIO


EMOJI_FAMILY = "Noto Color Emoji"
EMOJI_ALIAS = "AC Emoji"


def _copy_font(f: Path, fonts_dir: Path) -> str:
    """Put font file ``f`` into the composition's assets/fonts (a hard link when possible: the 10 MB emoji
    font is then not duplicated per clip-language). Returns the file name used in the page."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", f.name)
    dst = fonts_dir / name
    if not (dst.is_file() and dst.stat().st_size == f.stat().st_size):
        dst.unlink(missing_ok=True)
        try:
            os.link(f, dst)
        except OSError:  # another file system, or links not allowed
            shutil.copyfile(f, dst)
    return name


def _font_faces(settings: Settings, lang: str, fonts_dir: Path
                ) -> tuple[tuple[hf_template.FontFace, ...], str, float]:
    c = settings.captions
    specs = [("AC Caption EN", c.font_en), ("AC Caption HI", c.font_hi)]
    faces = []
    primary_file: Path | None = None
    for alias, family in specs:
        f = find_font_file(family, bold=c.bold)
        if (alias == "AC Caption HI") == (lang == "hi"):
            primary_file = f
        name = None
        if f is not None:
            name = re.sub(r"[^A-Za-z0-9._-]", "_", f.name)
            dst = fonts_dir / name
            if not (dst.is_file() and dst.stat().st_size == f.stat().st_size):
                shutil.copyfile(f, dst)
        else:
            log.warning("font %r not found as a file; the captions page uses local(%r)", family, family)
        faces.append(hf_template.FontFace(family=alias, file=name, local_name=family))
    order = ['"AC Caption HI"', '"AC Caption EN"'] if lang == "hi" else ['"AC Caption EN"', '"AC Caption HI"']
    return tuple(faces), ", ".join(order + ["sans-serif"]), ass_em_ratio(primary_file)


def build_composition(words: list[Word], *, lang: str, duration: float, settings: Settings, out_dir: Path,
                      gsap: Path, fps: int, width: int = 1080, height: int = 1920,
                      script: ClipScript | None = None) -> Path:
    """Write out_dir/index.html + out_dir/assets/{fonts,vendor}; return index.html. No subprocess
    (except an fc-match font lookup). ``script`` (script.json) gives the hook line and the keywords (G4/G5)."""
    out_dir = Path(out_dir)
    fonts_dir = out_dir / "assets" / "fonts"
    vendor = out_dir / "assets" / "vendor"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    vendor.mkdir(parents=True, exist_ok=True)
    gsap_dst = vendor / "gsap.min.js"
    if not (gsap_dst.is_file() and gsap_dst.read_bytes() == Path(gsap).read_bytes()):
        shutil.copyfile(gsap, gsap_dst)
    faces, stack, em_ratio = _font_faces(settings, lang, fonts_dir)
    keep = {f.file for f in faces if f.file}
    c = settings.captions
    per_line = max(1, int(c.words_per_line))
    marks = (caption_style.emphasis_marks(list(words), script.keywords, per_line=per_line,
                                          max_per_line=c.emphasis_max_per_line)
             if script is not None and caption_style.emphasis_active(settings, script) else {})
    if not c.emoji:
        marks = {k: "" for k in marks}
    emoji_file = None
    if any(marks.values()):
        ef = find_font_file(EMOJI_FAMILY, bold=False)
        if ef is not None:
            emoji_file = _copy_font(ef, fonts_dir)
            keep.add(emoji_file)
        else:
            log.warning("font %r not found as a file; emojis use local(%r)", EMOJI_FAMILY, EMOJI_FAMILY)
    for old in fonts_dir.iterdir():  # stale fonts of an earlier font setting would change the hash
        if old.name not in keep:
            old.unlink(missing_ok=True)

    pct = c.position_pct
    hook = None
    text = caption_style.hook_text(script, settings)
    if text:
        h = settings.hook
        lay = caption_style.hook_layout(text, settings, width=int(width), height=int(height))
        hook = hf_template.HookSpec(
            text=text, end=caption_style.hook_end(settings, duration), top_px=lay.top_px,
            font_px=max(1, int(round(lay.font_size * em_ratio))), pad_px=lay.pad_px if h.style == "box" else 0,
            wrap_px=lay.wrap_px, color=ass_color_to_css(h.text_color),
            box=ass_color_to_css(h.box_color) if h.style == "box" else None,
            outline=ass_color_to_css(h.outline_color), stroke_px=2 * lay.pad_px, animation=h.animation)
    params = hf_template.TemplateParams(
        lang=lang, width=int(width), height=int(height), fps=int(fps), duration=float(duration),
        style=c.highlight_style or settings.hyperframes.style, fonts=faces, font_stack=stack,
        # same visual size as the ASS captions (ASS PlayResY = the video height, so no height scaling)
        font_px=max(1, int(round(c.font_size * em_ratio))),
        # -webkit-text-stroke is centred on the glyph edge and paint-order puts the fill on top, so only
        # half of it shows: 2 x the ASS Outline (5 px in captions.build_ass) gives the same visible border.
        stroke_px=2 * c.outline_px,
        primary=ass_color_to_css(c.primary_color), highlight=ass_color_to_css(c.highlight_color),
        top_px=(pct / 100 * height) if pct is not None else None,
        bottom_px=int(c.margin_v),
        bold=c.bold, outline=_css_or_black(c.outline_color), shadow_px=c.shadow_px,
        box=ass_color_to_css(c.box_color) if c.box else None,
        emphasis=ass_color_to_css(c.emphasis_color), emphasis_scale=c.emphasis_scale,
        emoji_family=EMOJI_ALIAS if any(marks.values()) else None, emoji_file=emoji_file,
        hook=hook,
    )
    shown = [w.model_copy(update={"text": caption_style.display_text(w.text, c.uppercase)}) for w in words]
    groups = hf_template.group_words(shown, c.words_per_line, duration)
    if marks:
        k = 0
        out_groups = []
        for g in groups:
            ws = []
            for w in g.words:
                if k in marks:
                    w = dataclasses.replace(w, emph=True, emoji=marks[k])
                ws.append(w)
                k += 1
            out_groups.append(dataclasses.replace(g, words=tuple(ws)))
        groups = out_groups
    index = out_dir / "index.html"
    write_text_atomic(index, hf_template.render_html(groups, params))
    return index


def composition_hash(out_dir: Path) -> str:
    """sha256 over index.html and the names/sizes of the files under assets/."""
    out_dir = Path(out_dir)
    h = hashlib.sha256()
    h.update((out_dir / "index.html").read_bytes())
    assets = out_dir / "assets"
    if assets.is_dir():
        for p in sorted(assets.rglob("*")):
            if p.is_file():
                h.update(f"\0{p.relative_to(out_dir).as_posix()}\0{p.stat().st_size}".encode())
    return h.hexdigest()


# --------------------------------------------------------------------------------------------- running
_TERM_GRACE = 5.0  # seconds between SIGTERM and SIGKILL


def _descendants(root: int) -> list[tuple[int, int]]:
    """(pid, pgid) of every live descendant of ``root``, read from /proc (Linux); [] elsewhere.

    Puppeteer starts Chrome with ``detached: true``, i.e. in its own session and process group, so a
    killpg on node's group does not reach it. Chrome is still node's child, so the ppid tree finds it."""
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return []
    parent: dict[int, int] = {}
    pgid: dict[int, int] = {}
    for d in proc_dir.iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2:].split()  # comm may contain spaces/parens
        if len(fields) < 3 or fields[0] == "Z":
            continue
        try:
            parent[int(d.name)] = int(fields[1])
            pgid[int(d.name)] = int(fields[2])
        except ValueError:
            continue
    children: dict[int, list[int]] = {}
    for pid, ppid in parent.items():
        children.setdefault(ppid, []).append(pid)
    out: list[tuple[int, int]] = []
    stack = list(children.get(root, []))
    seen = {root}
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append((pid, pgid.get(pid, pid)))
        stack.extend(children.get(pid, []))
    return out


def _signal_all(group: int, procs: list[tuple[int, int]], sig: int) -> None:
    own = {os.getpgrp(), os.getpid()}
    for pg in {group, *(pg for _pid, pg in procs)}:
        if pg in own or pg <= 1:
            continue
        try:
            os.killpg(pg, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    for pid, _pg in procs:
        if pid in own or pid <= 1:
            continue
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
    else:
        # Snapshot the tree first: once node dies its children are re-parented and can no longer be found.
        tree = _descendants(proc.pid)
        _signal_all(proc.pid, tree, signal.SIGTERM)
        try:
            proc.wait(timeout=_TERM_GRACE)
        except subprocess.TimeoutExpired:
            pass
        # Chrome may survive node (its own group, or node wedged): SIGKILL every group and pid we saw,
        # plus anything started since the snapshot while node was still alive.
        if proc.poll() is None:
            tree = tree + [t for t in _descendants(proc.pid) if t not in tree]
        _signal_all(proc.pid, tree, signal.SIGKILL)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        pass


def _popen_runner(args: list[str], cwd: Path, env: dict[str, str], timeout: float,
                  on_line: Callable[[str], None]) -> int:
    """Run ``args``; stream stdout+stderr lines to ``on_line`` (called on this thread). On timeout or
    any exception from on_line (cancel, Ctrl-C) the whole process tree is killed first."""
    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True
    try:
        proc = subprocess.Popen(args, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **kw)
    except OSError as e:
        raise HyperFramesError(f"could not start hyperframes: {e}") from e
    lines: "queue.Queue[str | None]" = queue.Queue()
    assert proc.stdout is not None

    def pump() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                lines.put(line.rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)

    reader = threading.Thread(target=pump, name="hyperframes-output", daemon=True)
    reader.start()
    deadline = time.monotonic() + float(timeout)
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise HyperFramesTimeout(f"hyperframes render timed out after {timeout:g} s "
                                         "(raise hyperframes.timeout_seconds)")
            try:
                line = lines.get(timeout=min(0.25, left))
            except queue.Empty:
                continue
            if line is None:
                break
            on_line(line)
        return proc.wait(timeout=max(1.0, deadline - time.monotonic()))
    except BaseException as e:
        _kill_tree(proc)
        reader.join(timeout=5)
        try:
            proc.stdout.close()
        except (OSError, ValueError):
            pass
        if isinstance(e, subprocess.TimeoutExpired):
            raise HyperFramesTimeout(f"hyperframes did not exit within {timeout:g} s") from e
        raise


_PROGRESS = "@hf-progress"


def render_overlay(comp_dir: Path, dst: Path, *, env: HFEnv, s: HyperFramesSettings, fps: int, duration: float,
                   width: int = 1080, height: int = 1920, on_progress: ProgressFn | None = None,
                   runner: Runner | None = None, api_key_env: str | None = None) -> Path:
    """Render comp_dir/index.html to comp_dir/out/overlay.<fmt>, verify it, move it to ``dst``.
    comp_dir/.tmp (Chrome profiles) and comp_dir/out are always removed afterwards."""
    comp_dir = Path(comp_dir)
    dst = Path(dst)
    fmt = s.format
    runner = runner or _popen_runner
    out_dir = comp_dir / "out"
    tmp_dir = comp_dir / ".tmp"
    out_file = out_dir / f"overlay.{fmt}"
    args = [str(env.node), str(env.cli), "render", str(comp_dir), "--format", fmt, "--fps", str(int(fps)),
            "--quality", "looks", "-o", str(out_file)]
    if s.workers:
        args += ["--workers", str(s.workers)]
    tail: deque[str] = deque(maxlen=_KEEP_LINES)
    last = [0.0]

    def on_line(line: str) -> None:
        text = line.strip()
        if text.startswith(_PROGRESS):
            try:
                data = json.loads(text[len(_PROGRESS):].strip())
                pct = float(data.get("pct", 0))
                code = str(data.get("code", "render"))[:40]
            except (ValueError, TypeError, AttributeError):
                tail.append(text)
                return
            f = min(0.99, max(last[0], pct / 100.0))
            last[0] = f
            if on_progress is not None:
                on_progress(f, f"hyperframes {code}")
            return
        if text:
            tail.append(text[:500])

    try:
        shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        cenv = child_env(s, tmp_dir, api_key_env=api_key_env, node=env.node)
        rc = runner(args, comp_dir, cenv, float(s.timeout_seconds), on_line)
        if rc != 0:
            lines = "\n".join(list(tail)[-ERROR_TAIL_LINES:])
            raise HyperFramesError(f"hyperframes render failed (rc {rc}):\n{lines}".rstrip())
        if not out_file.is_file() or out_file.stat().st_size == 0:
            lines = "\n".join(list(tail)[-ERROR_TAIL_LINES:])
            raise HyperFramesError(f"hyperframes render wrote no {out_file.name}:\n{lines}".rstrip())
        verify_overlay(out_file, fmt=fmt, width=width, height=height, duration=duration)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(out_file, dst)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)
    return dst


# --------------------------------------------------------------------------------------------- checks
def _ffprobe_streams(path: Path) -> list[dict]:
    cmd = [ffmpeg.ffprobe_bin(), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    try:
        proc = _run(cmd, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise HyperFramesError(f"overlay check failed: ffprobe could not run ({e})") from e
    if proc.returncode != 0:
        raise HyperFramesError(f"overlay check failed: ffprobe error {(proc.stderr or '').strip()[:200]}")
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as e:
        raise HyperFramesError(f"overlay check failed: bad ffprobe output ({e})") from e
    streams = list(data.get("streams") or [])
    fmt_dur = (data.get("format") or {}).get("duration")
    for st in streams:
        st.setdefault("_format_duration", fmt_dur)
    return streams


def verify_overlay(path: Path, *, fmt: str, width: int, height: int, duration: float) -> None:
    """Video stream, the cut's size, the cut's duration (+-max(0.25 s, 2 frames)) and an alpha channel."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise HyperFramesError(f"overlay check failed: {path.name} is missing or empty")
    try:
        streams = _ffprobe_streams(path)
    except ffmpeg.FFmpegError as e:
        raise HyperFramesError(f"overlay check failed: {e}") from e
    video = [st for st in streams if st.get("codec_type") == "video"]
    if not video:
        raise HyperFramesError(f"overlay check failed: {path.name} has no video stream")
    v = video[0]
    w, h = v.get("width"), v.get("height")
    if (w, h) != (width, height):
        raise HyperFramesError(f"overlay check failed: size {w}x{h}, expected {width}x{height}")
    dur = None
    for cand in (v.get("_format_duration"), v.get("duration")):
        try:
            dur = float(cand)
            break
        except (TypeError, ValueError):
            continue
    fps = _rate(v.get("r_frame_rate")) or 25.0
    tol = max(0.25, 2.0 / fps)
    if dur is None or abs(dur - duration) > tol:
        raise HyperFramesError(f"overlay check failed: duration {dur if dur is not None else '?'} s, "
                               f"expected {duration:.3f} s")
    pix = str(v.get("pix_fmt") or "").lower()
    tags = {str(k).lower(): str(val) for k, val in (v.get("tags") or {}).items()}
    if fmt == "webm":
        ok = tags.get("alpha_mode") == "1" or pix.startswith("yuva")
    else:
        ok = pix.startswith("yuva") or "rgba" in pix or "argb" in pix
    if not ok:
        raise HyperFramesError(f"overlay check failed: {path.name} has no alpha channel (pix_fmt {pix or '?'})")


def _rate(value: object) -> float | None:
    try:
        if isinstance(value, str) and "/" in value:
            n, d = value.split("/", 1)
            r = float(n) / float(d)
        else:
            r = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return r if r > 0 else None


def cut_fps(cut: Path) -> int:
    """Rounded r_frame_rate of the cut's video, clamped to 12..60 (30 when unreadable)."""
    try:
        streams = _ffprobe_streams(Path(cut))
    except (HyperFramesError, ffmpeg.FFmpegError):
        return 30
    for st in streams:
        if st.get("codec_type") == "video":
            r = _rate(st.get("r_frame_rate"))
            if r:
                return max(12, min(60, int(round(r))))
    return 30


_DECODERS: dict[str, bool] = {}


def has_vp9_alpha_decoder() -> bool:
    """True when ffmpeg has the libvpx-vp9 decoder (FFmpeg's native vp9 decoder drops WebM alpha)."""
    try:
        exe = ffmpeg.ffmpeg_bin()
    except ffmpeg.FFmpegError:
        return False
    if exe not in _DECODERS:
        try:
            proc = _run([exe, "-hide_banner", "-decoders"], timeout=30)
            _DECODERS[exe] = proc.returncode == 0 and bool(re.search(r"^\s*\S+\s+libvpx-vp9\s", proc.stdout or "",
                                                                     re.M))
        except (OSError, subprocess.SubprocessError):
            _DECODERS[exe] = False
    return _DECODERS[exe]


# --------------------------------------------------------------------------------------------- entry point
OVERLAY_JSON = "overlay.json"


def overlay_name(fmt: str) -> str:
    return f"captions_overlay.{fmt}"


def _load_words(words_json: Path, *, allow_empty: bool = False) -> list[Word]:
    try:
        raw = json.loads(Path(words_json).read_text(encoding="utf-8"))
        words = [Word(**w) for w in raw]
    except FileNotFoundError as e:
        raise HyperFramesError(f"{Path(words_json).name} not found (run the captions stage)") from e
    except (OSError, ValueError, TypeError) as e:
        raise HyperFramesError(f"{Path(words_json).name} is not valid: {str(e).splitlines()[0][:200]}") from e
    if not words and not allow_empty:
        raise HyperFramesError(f"{Path(words_json).name} has no words")
    return words


def make_overlay(*, words_json: Path, cut: Path, lang: str, settings: Settings, out_dir: Path,
                 on_progress: ProgressFn | None = None, runner: Runner | None = None,
                 env: HFEnv | None = None, script: ClipScript | None = None) -> Path:
    """Build the composition for one clip-language and render (or reuse) its transparent overlay.

    Returns out_dir/captions_overlay.<fmt>. ``env`` lets the pipeline pass a memoised probe_env result.
    Raises HyperFramesError (incl. Unavailable / Timeout) on any failure.
    """
    s = settings.hyperframes
    fmt = s.format
    out_dir = Path(out_dir)
    if env is None:
        env = probe_env(s)
    if fmt == "webm" and not has_vp9_alpha_decoder():
        raise HyperFramesError("ffmpeg has no libvpx-vp9 decoder (needed for WebM alpha); set hyperframes.format=mov")
    if script is None:  # hook + keywords come from the clip-language's script.json (G4/G5)
        from .render import load_clip_script

        script = load_clip_script(out_dir)
    # a clip with no words still gets an overlay when it has a hook line to draw (G4)
    words = _load_words(words_json, allow_empty=bool(caption_style.hook_text(script, settings)))
    try:
        info = ffmpeg.probe(Path(cut))
    except ffmpeg.FFmpegError as e:
        raise HyperFramesError(f"cannot probe {Path(cut).name}: {str(e).splitlines()[0]}") from e
    if not info.has_video or not info.width or not info.height or info.duration <= 0:
        raise HyperFramesError(f"{Path(cut).name} has no usable video stream")
    duration, width, height = float(info.duration), int(info.width), int(info.height)
    fps = int(s.fps) if s.fps else cut_fps(Path(cut))

    comp = out_dir / "hf"
    build_composition(words, lang=lang, duration=duration, settings=settings, out_dir=comp, gsap=env.gsap,
                      fps=fps, width=width, height=height, script=script)
    dst = out_dir / overlay_name(fmt)
    stamp = {"composition_sha256": composition_hash(comp), "format": fmt, "fps": fps,
             "duration": round(duration, 3), "hf_version": env.hf_version}
    for other in ("webm", "mov"):
        if other != fmt:
            (out_dir / overlay_name(other)).unlink(missing_ok=True)
    cache = comp / OVERLAY_JSON
    try:
        cached = json.loads(cache.read_text(encoding="utf-8")) if cache.is_file() else None
    except (OSError, ValueError):
        cached = None
    if cached == stamp and dst.is_file() and dst.stat().st_size > 0:
        try:
            verify_overlay(dst, fmt=fmt, width=width, height=height, duration=duration)
            log.info("reusing %s (composition unchanged)", dst.name)
            if on_progress is not None:
                on_progress(0.99, "hyperframes overlay cached")
            return dst
        except HyperFramesError as e:
            log.warning("cached %s failed its check (%s); rendering again", dst.name, e)
    cache.unlink(missing_ok=True)
    render_overlay(comp, dst, env=env, s=s, fps=fps, duration=duration, width=width, height=height,
                   on_progress=on_progress, runner=runner, api_key_env=settings.llm.api_key_env)
    write_text_atomic(cache, json.dumps(stamp, indent=2, ensure_ascii=False))
    return dst


__all__ = [
    "HF_PACKAGE", "HF_VERSION_PIN", "MIN_NODE_MAJOR", "HyperFramesError", "HyperFramesUnavailable",
    "HyperFramesTimeout", "HFEnv", "Runner", "resolve_node", "node_version", "resolve_cli", "hf_version",
    "find_gsap", "probe_env", "child_env", "ass_color_to_css", "find_font_file", "build_composition",
    "composition_hash", "render_overlay", "verify_overlay", "make_overlay", "has_vp9_alpha_decoder",
    "cut_fps", "overlay_name", "parse_major", "ass_em_ratio",
]
