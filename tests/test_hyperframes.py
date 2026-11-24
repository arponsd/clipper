"""Step 3 engine: autoclipper.stages.hyperframes + hf_template (CONTRACT_STEP3 sections 3 and 9).

The default suite never runs Node: the HyperFrames subprocess is FakeHFRunner. The one real render is
marked `hyperframes` + `slow` and is skipped when Node 22 / the CLI is missing.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fakes import FakeHFRunner, make_alpha_overlay, make_video

from autoclipper import config
from autoclipper.config import Settings
from autoclipper.models import Word
from autoclipper.stages import hf_template
from autoclipper.stages import hyperframes as hf

REAL_HOME = Path.home()  # captured at import, before conftest points HOME at tmp_path


# --------------------------------------------------------------------------------------------- helpers
def _proc(stdout: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, stdout, "")


def make_pkg(root: Path, gsap_versions: tuple[str, ...] = ("3.15.0",)) -> Path:
    """Fake `npm --prefix root install hyperframes` layout; returns the CLI path."""
    pkg = root / "node_modules" / "hyperframes"
    cli = pkg / "bin" / "hyperframes.mjs"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("// fake cli", encoding="utf-8")
    for v in gsap_versions:
        g = pkg / "dist" / "vendor" / "npm" / f"gsap@{v}" / "dist" / "gsap.min.js"
        g.parent.mkdir(parents=True, exist_ok=True)
        g.write_text(f"/* gsap {v} */", encoding="utf-8")
    return cli


def make_node(d: Path, name: str = "node") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\n", encoding="utf-8")
    return p


def hf_settings(tmp_path: Path, **kw) -> Settings:
    s = Settings()
    s.hyperframes.node_bin_dir = str(tmp_path / "node22" / "bin")
    s.hyperframes.package_dir = str(tmp_path / "hfpkg")
    for k, v in kw.items():
        setattr(s.hyperframes, k, v)
    return s


@pytest.fixture
def fonts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "fonts"
    d.mkdir()
    (d / "NotoSans-Bold.ttf").write_bytes(b"latin-font")
    (d / "NotoSansDevanagari-Bold.ttf").write_bytes(b"devanagari-font")
    (d / "NotoSansDevanagari-Regular.ttf").write_bytes(b"devanagari-regular")
    monkeypatch.setattr(config, "FONTS_DIR", d)
    return d


@pytest.fixture
def gsap(tmp_path: Path) -> Path:
    p = tmp_path / "gsap.min.js"
    p.write_text("/* gsap */", encoding="utf-8")
    return p


def words(texts: list[str], step: float = 0.5, start: float = 0.2) -> list[Word]:
    return [Word(start=round(start + i * step, 3), end=round(start + i * step + step * 0.8, 3), text=t)
            for i, t in enumerate(texts)]


def script_of(html: str) -> str:
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(blocks) == 1
    return blocks[0]


def groups_of(html: str) -> list[tuple[str, float, float]]:
    return [(g, float(s), float(d)) for g, s, d in
            re.findall(r'<div id="(g\d+)" class="cap clip" data-start="([0-9.]+)" data-duration="([0-9.]+)"', html)]


# --------------------------------------------------------------------------------------------- resolve
def test_resolve_node_layouts(tmp_path: Path) -> None:
    s = hf_settings(tmp_path).hyperframes
    assert hf.resolve_node(s, which=lambda _n: None) is None  # nothing found
    node = make_node(tmp_path / "node22" / "bin")
    assert hf.resolve_node(s) == node  # <dir>/node

    s2 = s.model_copy(update={"node_bin_dir": str(tmp_path / "node22")})  # no-bin/ dir: <dir>/bin/node
    assert hf.resolve_node(s2) == node

    flat = make_node(tmp_path / "flat")  # node directly in the dir (Windows zip layout)
    assert hf.resolve_node(s.model_copy(update={"node_bin_dir": str(tmp_path / "flat")})) == flat

    empty = s.model_copy(update={"node_bin_dir": ""})  # "" -> PATH
    assert hf.resolve_node(empty, which=lambda n: "/usr/bin/node" if n == "node" else None) == Path("/usr/bin/node")
    assert hf.resolve_node(empty, which=lambda _n: None) is None


def test_resolve_node_windows_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = make_node(tmp_path / "win" / "node22", "node.exe")
    monkeypatch.setattr(hf, "_node_names", lambda: ("node.exe", "node"))
    s = hf_settings(tmp_path).hyperframes.model_copy(update={"node_bin_dir": str(tmp_path / "win" / "node22")})
    assert hf.resolve_node(s) == exe


def test_resolve_node_expands_home_and_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = make_node(Path.home() / "tools" / "node22" / "bin")  # HOME is tmp_path/home (conftest)
    s = Settings().hyperframes
    assert s.node_bin_dir == "~/tools/node22/bin"
    assert hf.resolve_node(s) == node
    monkeypatch.setenv("AC_TEST_TOOLS", str(Path.home() / "tools"))
    assert hf.resolve_node(s.model_copy(update={"node_bin_dir": "$AC_TEST_TOOLS/node22"})) == node


def test_find_gsap_highest_version_and_missing(tmp_path: Path) -> None:
    cli = make_pkg(tmp_path / "p", ("3.9.1", "3.15.0", "3.12.5"))
    assert hf.find_gsap(cli).parent.parent.name == "gsap@3.15.0"
    cli2 = make_pkg(tmp_path / "q", ())
    assert hf.find_gsap(cli2) is None


def test_probe_env_ok_and_each_unavailable_case(tmp_path: Path) -> None:
    s = hf_settings(tmp_path)
    runs: list[list[str]] = []

    def run(args, **kw):
        runs.append(list(args))
        if args[-1] == "--version" and len(args) == 2:
            return _proc("v22.23.5\n")
        return _proc("0.9.52\n")

    with pytest.raises(hf.HyperFramesUnavailable, match="node not found"):
        hf.probe_env(s.hyperframes, which=lambda _n: None, run=run)
    make_node(tmp_path / "node22" / "bin")
    with pytest.raises(hf.HyperFramesUnavailable, match=r"v16\.20\.2 is too old"):
        hf.probe_env(s.hyperframes, run=lambda args, **kw: _proc("v16.20.2\n"))
    with pytest.raises(hf.HyperFramesUnavailable, match="could not read the node version"):
        hf.probe_env(s.hyperframes, run=lambda args, **kw: _proc("", rc=1))
    with pytest.raises(hf.HyperFramesUnavailable, match="HyperFrames not installed at .*hyperframes.mjs.*README"):
        hf.probe_env(s.hyperframes, run=run)
    cli = make_pkg(tmp_path / "hfpkg", ())
    with pytest.raises(hf.HyperFramesUnavailable, match="GSAP not found"):
        hf.probe_env(s.hyperframes, run=run)
    g = cli.parent.parent / "dist" / "vendor" / "npm" / "gsap@3.15.0" / "dist" / "gsap.min.js"
    g.parent.mkdir(parents=True)
    g.write_text("x", encoding="utf-8")
    env = hf.probe_env(s.hyperframes, run=run)
    assert env.node_version == "v22.23.5" and env.hf_version == "0.9.52" and env.cli == cli and env.gsap == g
    assert any(a[-1] == "--version" and a[1] == str(cli) for a in runs)


def test_child_env_strips_secrets_and_sets_path_tmp_and_privacy(tmp_path: Path) -> None:
    s = hf_settings(tmp_path)
    node = make_node(tmp_path / "node22" / "bin")
    base = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-secret", "ANTHROPIC_BASE_URL": "x",
            "MY_KEY": "sk-other", "KEEP": "1"}
    env = hf.child_env(s.hyperframes, tmp_path / "comp" / ".tmp", base, api_key_env="MY_KEY")
    assert not any(k.startswith("ANTHROPIC_") for k in env) and "MY_KEY" not in env
    assert "sk-secret" not in json.dumps(env) and env["KEEP"] == "1"
    assert env["PATH"].split(os.pathsep)[0] == str(node.parent)
    assert env["TMPDIR"] == env["TEMP"] == env["TMP"] == str(tmp_path / "comp" / ".tmp")
    assert (tmp_path / "comp" / ".tmp").is_dir()
    for k in ("HYPERFRAMES_NO_UPDATE_CHECK", "HYPERFRAMES_SKIP_SKILLS", "HYPERFRAMES_NO_TELEMETRY", "DO_NOT_TRACK",
              "HYPERFRAMES_NO_AUTO_INSTALL", "HYPERFRAMES_NO_OPTIONAL_INSTALL"):
        assert env[k] == "1"
    assert base["ANTHROPIC_API_KEY"] == "sk-secret"  # the base mapping is not mutated


def test_ass_color_to_css() -> None:
    assert hf.ass_color_to_css("&H00FFFFFF") == "rgba(255,255,255,1)"
    assert hf.ass_color_to_css("&H0000E5FF") == "rgba(255,229,0,1)"  # AABBGGRR: yellow
    assert hf.ass_color_to_css("&H80FFFFFF") == "rgba(255,255,255,0.5)"
    assert hf.ass_color_to_css("&HFF000000") == "rgba(0,0,0,0)"
    with pytest.raises(ValueError):
        hf.ass_color_to_css("red")


def test_find_font_file_fonts_dir_bold_then_regular(fonts_dir: Path) -> None:
    assert hf.find_font_file("Noto Sans Devanagari") == fonts_dir / "NotoSansDevanagari-Bold.ttf"
    assert hf.find_font_file("Noto Sans") == fonts_dir / "NotoSans-Bold.ttf"  # never the Devanagari file
    (fonts_dir / "NotoSansDevanagari-Bold.ttf").unlink()
    assert hf.find_font_file("Noto Sans Devanagari") == fonts_dir / "NotoSansDevanagari-Regular.ttf"


def test_find_font_file_fc_match_checks_family(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # _fc_match directly: find_font_file only asks fontconfig off Windows, and faking os.name="posix"
    # on Windows makes pathlib build PosixPath objects for the whole process.
    monkeypatch.setattr(config, "FONTS_DIR", tmp_path / "none")
    font = tmp_path / "Some-Bold.ttf"
    font.write_bytes(b"x")
    monkeypatch.setattr(hf.shutil, "which", lambda n: "/usr/bin/fc-match" if n == "fc-match" else None)
    monkeypatch.setattr(hf, "_run", lambda args, **kw: _proc(f"Noto Sans Devanagari,Noto Sans Devanagari Bold\n{font}"))
    assert hf._fc_match("Noto Sans Devanagari", True) == font
    if os.name != "nt":
        assert hf.find_font_file("Noto Sans Devanagari") == font
    # fontconfig substituted another family -> not accepted
    monkeypatch.setattr(hf, "_run", lambda args, **kw: _proc(f"DejaVu Sans\n{font}"))
    assert hf._fc_match("Noto Sans Devanagari", True) is None
    if os.name != "nt":
        assert hf.find_font_file("Noto Sans Devanagari") is None


# --------------------------------------------------------------------------------------------- composition
NASTY = ["<b>", "a&b", 'say "hi"', "</script><script>alert(1)</script>", "zebraword"]
HINDI = ["नमस्ते", "तिहत्तर", "छियासी", "चैलेंजर", "हीरो", "सात", "दोस्तों"]


def build(tmp_path: Path, ws: list[Word], *, lang: str = "en", duration: float = 6.0, settings: Settings | None = None,
          gsap: Path, name: str = "comp", fps: int = 25) -> tuple[Path, str]:
    s = settings or Settings()
    out = tmp_path / name
    index = hf.build_composition(ws, lang=lang, duration=duration, settings=s, out_dir=out, gsap=gsap, fps=fps)
    return out, index.read_text(encoding="utf-8")


def test_build_composition_escapes_text_and_keeps_it_out_of_the_script(tmp_path: Path, fonts_dir: Path,
                                                                       gsap: Path) -> None:
    out, html = build(tmp_path, words(NASTY), gsap=gsap)
    js = script_of(html)
    for t in NASTY:
        assert t not in js
    assert "&lt;b&gt;" in html and "a&amp;b" in html and "say &quot;hi&quot;" in html
    assert "&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert html.count("<script") == 2  # the gsap include + the timeline; the word text adds none
    assert "http" not in html.lower()
    assert '<script src="assets/vendor/gsap.min.js"></script>' in html
    assert (out / "assets" / "vendor" / "gsap.min.js").read_text(encoding="utf-8") == "/* gsap */"


def test_build_composition_hindi_fonts_and_root(tmp_path: Path, fonts_dir: Path, gsap: Path) -> None:
    s = Settings()
    out, html = build(tmp_path, words(HINDI), lang="hi", duration=28.84, settings=s, gsap=gsap, fps=25)
    assert '<html lang="hi">' in html and '<meta charset="UTF-8" />' in html
    assert '<meta name="viewport" content="width=1080, height=1920" />' in html
    for t in HINDI:
        assert f">{t}</span>" in html  # Devanagari stays literal UTF-8 in the HTML
        assert t not in script_of(html)
    assert ('<div id="root" data-composition-id="main" data-start="0" data-duration="28.840" '
            'data-width="1080" data-height="1920" data-fps="25">') in html
    assert sorted(p.name for p in (out / "assets" / "fonts").iterdir()) == ["NotoSans-Bold.ttf",
                                                                           "NotoSansDevanagari-Bold.ttf"]
    assert (out / "assets" / "fonts" / "NotoSansDevanagari-Bold.ttf").read_bytes() == b"devanagari-font"
    assert 'src: url("assets/fonts/NotoSansDevanagari-Bold.ttf")' in html
    assert 'font-family: "AC Caption HI", "AC Caption EN", sans-serif' in html
    _, html_en = build(tmp_path, words(["hello"]), lang="en", gsap=gsap, name="en")
    assert 'font-family: "AC Caption EN", "AC Caption HI", sans-serif' in html_en
    assert "background: transparent" in html and "height: 0" not in html and "height:0" not in html
    # the dummy font file is not parseable -> the default libass em ratio
    assert f"font-size: {round(s.captions.font_size * hf.DEFAULT_ASS_EM_RATIO)}px" in html
    assert f"-webkit-text-stroke: {2 * hf.ASS_OUTLINE_PX}px #000" in html  # same visible border as ASS
    # gotcha 2: spacing by margin, not whitespace/gap; 0.13em per side = about one normal space (triage R3,
    # visual reviewer: 0.24em gave a double-wide gap). The pop room comes from the per-word cap instead.
    assert "margin: 0 0.13em" in html and "0.24em" not in html


def test_build_composition_missing_font_uses_local(tmp_path: Path, gsap: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hf, "find_font_file", lambda family, bold=True: None)
    _, html = build(tmp_path, words(["a"]), gsap=gsap)
    assert 'src: local("Noto Sans Devanagari")' in html and 'src: local("Noto Sans")' in html


@pytest.mark.parametrize("n,per_line", [(1, 3), (7, 3), (9, 3), (10, 4), (5, 1)])
def test_groups_count_and_windows_do_not_overlap(tmp_path: Path, fonts_dir: Path, gsap: Path, n: int,
                                                 per_line: int) -> None:
    s = Settings()
    s.captions.words_per_line = per_line
    ws = words([f"w{i}" for i in range(n)], step=0.4)
    duration = ws[-1].end + 0.3
    _, html = build(tmp_path, ws, duration=duration, settings=s, gsap=gsap)
    groups = groups_of(html)
    assert len(groups) == math.ceil(n / per_line)
    for i, (gid, start, dur) in enumerate(groups):
        chunk = ws[i * per_line:(i + 1) * per_line]
        assert gid == f"g{i}" and dur >= 0.05
        assert start == pytest.approx(chunk[0].start, abs=1e-3)
        assert start + dur >= chunk[-1].end - 1e-3  # never ends before its last word
        if i + 1 < len(groups):
            assert start + dur <= groups[i + 1][1] + 1e-3  # no overlap: one group visible at a time
    last_chunk = ws[(len(groups) - 1) * per_line:]
    assert groups[-1][1] + groups[-1][2] == pytest.approx(max(last_chunk[-1].end, min(duration, last_chunk[-1].end + 0.6)),
                                                          abs=2e-3)
    w_ids = re.findall(r'<span id="(w\d+_\d+)" class="w">', html)
    assert len(w_ids) == n


def test_group_windows_with_overlapping_word_timings() -> None:
    ws = [Word(start=0.0, end=1.5, text="a"), Word(start=1.0, end=1.2, text="b"), Word(start=1.1, end=2.0, text="c")]
    gs = hf_template.group_words(ws, 1, 3.0)
    for a, b in zip(gs, gs[1:]):
        assert a.end <= b.start + 1e-9
        assert a.end >= a.start + hf_template.MIN_GROUP


def test_position_pct_top_and_margin_v_bottom(tmp_path: Path, fonts_dir: Path, gsap: Path) -> None:
    s = Settings()
    s.captions.position_pct = 72.0
    _, html = build(tmp_path, words(["a"]), settings=s, gsap=gsap, name="top")
    assert f"top: {0.72 * 1920:.0f}px; transform: translateY(-50%);" in html
    s.captions.position_pct = None
    s.captions.margin_v = 333
    _, html2 = build(tmp_path, words(["a"]), settings=s, gsap=gsap, name="bottom")
    assert "bottom: 333px;" in html2 and "translateY" not in html2


def test_styles_differ_and_timeline_rules(tmp_path: Path, fonts_dir: Path, gsap: Path) -> None:
    s = Settings()
    ws = words(["one", "two", "three", "four"])
    _, pop = build(tmp_path, ws, settings=s, gsap=gsap, name="pop")
    s.hyperframes.style = "karaoke"
    _, kar = build(tmp_path, ws, settings=s, gsap=gsap, name="kar")
    assert pop != kar
    js_pop, js_kar = script_of(pop), script_of(kar)
    assert "const MAX_POP = 1.18;" in js_pop and "scale: k" in js_pop and "back.out(3)" in js_pop
    assert "scale" not in js_kar
    for js in (js_pop, js_kar):
        assert 'window.__timelines["main"] = tl;' in js and "paused: true" in js
        assert js.rstrip().endswith("})();")
        assert js.index("window.__timelines[\"main\"]") > js.rindex("tl.fromTo")
        for banned in ("Date.now", "Math.random", "setTimeout", "setInterval", "repeat", "visibility", "display",
                       "autoAlpha"):
            assert banned not in js
    # colours from the caption settings (ASS -> CSS)
    assert '"h":"rgba(255,229,0,1)"' in js_pop and '"p":"rgba(255,255,255,1)"' in js_pop
    m = re.search(r"const W = (\[.*?\]);", js_pop)
    data = json.loads(m.group(1))
    assert data[0] == ["w0_0", ws[0].start, ws[0].end] and len(data) == 4


def test_pop_scale_is_capped_per_word_by_margin_and_box(tmp_path: Path, fonts_dir: Path, gsap: Path) -> None:
    """Triage R1/R2 (visual): a fixed 1.18 pop made long words cover their neighbours and run off the frame.
    Each word's scale is now min(1.18, 1 + 2*margin/width, box/width), measured after the fonts loaded."""
    _, html = build(tmp_path, words(["Speechwriter", "Peggy"]), gsap=gsap)
    js = script_of(html)
    assert "const BOX = 972.0;" in js  # 90 % of 1080
    assert "Math.min(MAX_POP, 1 + 2 * m / w, BOX / w)" in js and "Math.max(1," in js
    assert "getComputedStyle(el).marginLeft" in js and "el.offsetWidth" in js
    # measured only once the caption fonts are loaded; the timeline is registered after its tweens
    assert js.index("fonts.load(") < js.index(".then(build)")
    body = js[js.index("function build()"):js.index("const fonts = document.fonts")]
    assert body.index("popScale(el)") < body.index('window.__timelines["main"] = tl;')
    assert body.rindex("tl.") < body.index('window.__timelines["main"] = tl;')
    assert '"AC Caption EN"' in js  # the families passed to fonts.load


def test_contiguous_groups_switch_without_a_blank_frame() -> None:
    """Triage R2 (correctness): group N ended where group N+1 started and N+1 faded in from opacity 0, so
    the frame at the switch had no caption at all. Only a group with nothing on screen before it fades."""
    ws = [Word(start=0.0, end=0.4, text="a"), Word(start=0.5, end=0.9, text="b"), Word(start=1.0, end=1.4, text="c")]
    groups = hf_template.group_words(ws, 1, 2.0)
    assert groups[0].end == groups[1].start and groups[1].end == groups[2].start
    gap = hf_template.GroupSpec(id="g3", start=groups[2].end + 0.5, end=groups[2].end + 1.0,
                                words=(hf_template.WordSpec(id="w3_0", text="d", start=groups[2].end + 0.5,
                                                            end=groups[2].end + 0.9),))
    p = hf_template.TemplateParams(lang="en", width=1080, height=1920, fps=25, duration=4.0, style="pop",
                                   fonts=(hf_template.FontFace("AC Caption EN", None, "Noto Sans"),),
                                   font_stack='"AC Caption EN", sans-serif', font_px=57, stroke_px=10,
                                   primary="#fff", highlight="#ff0", top_px=1382.0, bottom_px=0)
    js = script_of(hf_template.render_html([*groups, gap], p))
    data = json.loads(re.search(r"const G = (\[.*?\]);", js).group(1))
    assert data == [["g0", 0.0, 1], ["g1", 0.5, 0], ["g2", 1.0, 0], ["g3", gap.start, 1]]
    assert "if (fade) tl.fromTo(" in js


def test_build_composition_is_deterministic(tmp_path: Path, fonts_dir: Path, gsap: Path) -> None:
    ws = words(HINDI)
    out1, a = build(tmp_path, ws, lang="hi", gsap=gsap, name="a")
    out2, b = build(tmp_path, ws, lang="hi", gsap=gsap, name="b")
    _, c = build(tmp_path, ws, lang="hi", gsap=gsap, name="a")  # rebuilt in place
    assert a == b == c
    assert hf.composition_hash(out1) == hf.composition_hash(out2)
    ws2 = list(ws)
    ws2[0] = Word(start=ws[0].start, end=ws[0].end, text="बदला")
    out3, _ = build(tmp_path, ws2, lang="hi", gsap=gsap, name="c")
    assert hf.composition_hash(out3) != hf.composition_hash(out1)


def test_render_html_rejects_unknown_style() -> None:
    p = hf_template.TemplateParams(lang="en", width=10, height=10, fps=25, duration=1, style="x", fonts=(),
                                   font_stack="sans-serif", font_px=10, stroke_px=4, primary="#fff",
                                   highlight="#ff0", top_px=5, bottom_px=0)
    with pytest.raises(ValueError):
        hf_template.render_html([], p)


# --------------------------------------------------------------------------------------------- render_overlay
W_, H_ = 160, 288  # small even size keeps the fake encodes fast


def fake_env(tmp_path: Path) -> hf.HFEnv:
    node = make_node(tmp_path / "node22" / "bin")
    cli = make_pkg(tmp_path / "hfpkg")
    return hf.HFEnv(node=node, cli=cli, node_version="v22.23.5", hf_version="0.9.52", gsap=hf.find_gsap(cli))


def make_comp(tmp_path: Path, duration: float = 1.0, fps: int = 25) -> Path:
    comp = tmp_path / "clip" / "hf"
    hf.build_composition(words(["a", "b"], step=0.3), lang="en", duration=duration, settings=Settings(),
                         out_dir=comp, gsap=fake_env(tmp_path).gsap, fps=fps, width=W_, height=H_)
    return comp


def test_render_overlay_progress_args_cwd_env(tmp_path: Path, fonts_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    env = fake_env(tmp_path)
    s = hf_settings(tmp_path, workers=2).hyperframes
    comp = make_comp(tmp_path)
    runner = FakeHFRunner()
    seen: list[tuple[float, str]] = []
    dst = hf.render_overlay(comp, tmp_path / "clip" / "captions_overlay.webm", env=env, s=s, fps=25, duration=1.0,
                            width=W_, height=H_, on_progress=lambda f, m="": seen.append((f, m)), runner=runner)
    assert dst.is_file() and dst.stat().st_size > 0
    fr = [f for f, _ in seen]
    assert fr == sorted(fr) and fr and max(fr) < 1.0 and max(fr) == pytest.approx(0.95)
    assert seen[0][1] == "hyperframes compile" and seen[-1][1] == "hyperframes encode"
    call = runner.calls[0]
    assert call["args"] == [str(env.node), str(env.cli), "render", str(comp), "--format", "webm", "--fps", "25",
                            "--quality", "looks", "-o", str(comp / "out" / "overlay.webm"), "--workers", "2"]
    assert "--quiet" not in call["args"]
    assert call["cwd"] == comp and call["timeout"] == 300.0
    assert "ANTHROPIC_API_KEY" not in call["env"] and call["env"]["TMPDIR"] == str(comp / ".tmp")
    assert call["tmp_existed"]
    assert call["env"]["PATH"].split(os.pathsep)[0] == str(env.node.parent)
    assert not (comp / ".tmp").exists() and not (comp / "out").exists()
    assert (comp / "index.html").is_file()  # the composition is kept


def test_render_overlay_workers_auto_omits_flag(tmp_path: Path, fonts_dir: Path) -> None:
    runner = FakeHFRunner()
    hf.render_overlay(make_comp(tmp_path), tmp_path / "o.webm", env=fake_env(tmp_path),
                      s=hf_settings(tmp_path).hyperframes, fps=25, duration=1.0, width=W_, height=H_, runner=runner)
    assert "--workers" not in runner.calls[0]["args"]


def test_render_overlay_failure_carries_last_lines(tmp_path: Path, fonts_dir: Path) -> None:
    comp = make_comp(tmp_path)
    lines = [f"line {i}" for i in range(40)] + ["✗  Render failed", "net::ERR_CONNECTION_REFUSED"]
    runner = FakeHFRunner(rc=1, lines=lines, write=False)
    with pytest.raises(hf.HyperFramesError, match=r"rc 1") as ei:
        hf.render_overlay(comp, tmp_path / "o.webm", env=fake_env(tmp_path), s=hf_settings(tmp_path).hyperframes,
                          fps=25, duration=1.0, width=W_, height=H_, runner=runner)
    msg = str(ei.value)
    assert "net::ERR_CONNECTION_REFUSED" in msg and "line 39" in msg and "line 20" not in msg
    assert not (comp / ".tmp").exists() and not (comp / "out").exists()
    assert not (tmp_path / "o.webm").exists()


def test_render_overlay_no_output_and_bad_overlay(tmp_path: Path, fonts_dir: Path) -> None:
    comp = make_comp(tmp_path)
    kw = dict(env=fake_env(tmp_path), s=hf_settings(tmp_path).hyperframes, fps=25, duration=1.0, width=W_, height=H_)
    with pytest.raises(hf.HyperFramesError, match="wrote no overlay.webm"):
        hf.render_overlay(comp, tmp_path / "o.webm", runner=FakeHFRunner(write=False), **kw)
    with pytest.raises(hf.HyperFramesError, match="no alpha"):
        hf.render_overlay(comp, tmp_path / "o.webm", runner=FakeHFRunner(alpha=False), **kw)
    assert not (comp / "out").exists() and not (tmp_path / "o.webm").exists()


def test_render_overlay_timeout_and_interrupt(tmp_path: Path, fonts_dir: Path) -> None:
    comp = make_comp(tmp_path)
    kw = dict(env=fake_env(tmp_path), s=hf_settings(tmp_path).hyperframes, fps=25, duration=1.0, width=W_, height=H_)
    with pytest.raises(hf.HyperFramesTimeout):
        hf.render_overlay(comp, tmp_path / "o.webm", runner=FakeHFRunner(exc=hf.HyperFramesTimeout("timed out")), **kw)
    assert not (comp / ".tmp").exists() and not (comp / "out").exists()

    def cancel(_f: float, _m: str = "") -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        hf.render_overlay(comp, tmp_path / "o.webm", runner=FakeHFRunner(), on_progress=cancel, **kw)
    assert not (comp / ".tmp").exists() and not (comp / "out").exists()


# --------------------------------------------------------------------------------------------- _popen_runner
def test_popen_runner_streams_lines_and_returns_rc(tmp_path: Path) -> None:
    got: list[str] = []
    code = "import sys; print('@hf-progress {\"pct\": 50}'); print('done', flush=True); sys.exit(3)"
    rc = hf._popen_runner([sys.executable, "-c", code], tmp_path, dict(os.environ), 30, got.append)
    assert rc == 3 and got == ['@hf-progress {"pct": 50}', "done"]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = Path(f"/proc/{pid}/status").read_text().split("State:")[1].split()[0]
        return state != "Z"
    except (OSError, IndexError):
        return True


@pytest.mark.skipif(os.name == "nt", reason="process-group kill is POSIX; Windows uses taskkill /T")
def test_popen_runner_timeout_kills_the_tree(tmp_path: Path) -> None:
    code = ("import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print('child', p.pid, flush=True)\n"
            "time.sleep(60)\n")
    got: list[str] = []
    t0 = time.monotonic()
    with pytest.raises(hf.HyperFramesTimeout):
        hf._popen_runner([sys.executable, "-c", code], tmp_path, dict(os.environ), 1.5, got.append)
    assert time.monotonic() - t0 < 20
    child = int(got[0].split()[1])
    deadline = time.monotonic() + 5
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(child)


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="descendant walk reads /proc (Linux)")
def test_popen_runner_kills_detached_grandchild_of_a_wedged_parent(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Triage R4: puppeteer starts Chrome detached (own session + group). With node ignoring SIGTERM, the
    old killpg(node) never reached it and Chrome was orphaned."""
    monkeypatch.setattr(hf, "_TERM_GRACE", 0.5)
    code = ("import signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
            "print('child', p.pid, flush=True)\n"
            "while True:\n"
            "    time.sleep(0.05)\n")
    got: list[str] = []
    with pytest.raises(hf.HyperFramesTimeout):
        hf._popen_runner([sys.executable, "-c", code], tmp_path, dict(os.environ), 1.5, got.append)
    child = int(got[0].split()[1])
    assert os.getpgid(os.getpid()) != child  # the test runner itself was never signalled
    deadline = time.monotonic() + 5
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(child)


def test_descendants_finds_detached_children() -> None:
    if not Path("/proc/self/stat").exists():
        assert hf._descendants(os.getpid()) == []
        return
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        found = dict(hf._descendants(os.getpid()))
        assert found.get(p.pid) == p.pid  # its own group, still our child
    finally:
        p.kill()
        p.wait()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_popen_runner_on_line_exception_kills_and_propagates(tmp_path: Path) -> None:
    code = "import time\nprint('tick', flush=True)\ntime.sleep(60)\n"

    def on_line(_line: str) -> None:
        raise KeyboardInterrupt

    t0 = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        hf._popen_runner([sys.executable, "-c", code], tmp_path, dict(os.environ), 60, on_line)
    assert time.monotonic() - t0 < 20


def test_popen_runner_missing_executable(tmp_path: Path) -> None:
    with pytest.raises(hf.HyperFramesError, match="could not start"):
        hf._popen_runner([str(tmp_path / "nope")], tmp_path, dict(os.environ), 5, lambda _l: None)


# --------------------------------------------------------------------------------------------- verify_overlay
def test_verify_overlay(tmp_path: Path) -> None:
    good = make_alpha_overlay(tmp_path / "a.webm", 1.0, W_, H_)
    hf.verify_overlay(good, fmt="webm", width=W_, height=H_, duration=1.0)
    hf.verify_overlay(good, fmt="webm", width=W_, height=H_, duration=1.2)  # within 0.25 s
    opaque = make_alpha_overlay(tmp_path / "o.webm", 1.0, W_, H_, alpha=False)
    with pytest.raises(hf.HyperFramesError, match="no alpha"):
        hf.verify_overlay(opaque, fmt="webm", width=W_, height=H_, duration=1.0)
    with pytest.raises(hf.HyperFramesError, match="size"):
        hf.verify_overlay(good, fmt="webm", width=W_ * 2, height=H_, duration=1.0)
    with pytest.raises(hf.HyperFramesError, match="duration"):
        hf.verify_overlay(good, fmt="webm", width=W_, height=H_, duration=3.0)
    with pytest.raises(hf.HyperFramesError, match="missing or empty"):
        hf.verify_overlay(tmp_path / "none.webm", fmt="webm", width=W_, height=H_, duration=1.0)
    mov = make_alpha_overlay(tmp_path / "a.mov", 1.0, W_, H_, fmt="mov")
    hf.verify_overlay(mov, fmt="mov", width=W_, height=H_, duration=1.0)


# --------------------------------------------------------------------------------------------- make_overlay
@pytest.fixture
def clip_dir(tmp_path: Path) -> Path:
    clip = tmp_path / "job" / "clips" / "00"
    (clip / "en").mkdir(parents=True)
    make_video(clip / "cut.mp4", seconds=1.6, width=W_, height=H_)
    (clip / "en" / "words.json").write_text(json.dumps([w.model_dump() for w in words(["hi", "there", "you"])]),
                                           encoding="utf-8")
    return clip


def test_make_overlay_renders_then_caches(tmp_path: Path, fonts_dir: Path, clip_dir: Path) -> None:
    s = hf_settings(tmp_path)
    env = fake_env(tmp_path)
    out = clip_dir / "en"
    runner = FakeHFRunner()
    kw = dict(words_json=out / "words.json", cut=clip_dir / "cut.mp4", lang="en", out_dir=out, runner=runner, env=env)
    dst = hf.make_overlay(settings=s, **kw)
    assert dst == out / "captions_overlay.webm" and dst.is_file()
    stamp = json.loads((out / "hf" / "overlay.json").read_text(encoding="utf-8"))
    assert stamp["format"] == "webm" and stamp["fps"] == 25 and stamp["hf_version"] == "0.9.52"
    assert stamp["duration"] == pytest.approx(1.6, abs=0.05)
    assert "--fps" in runner.calls[0]["args"] and runner.calls[0]["args"][runner.calls[0]["args"].index("--fps") + 1] == "25"
    assert len(runner.calls) == 1

    hf.make_overlay(settings=s, **kw)  # same inputs: cached
    assert len(runner.calls) == 1

    s.hyperframes.style = "karaoke"  # style changes the composition
    hf.make_overlay(settings=s, **kw)
    assert len(runner.calls) == 2
    hf.make_overlay(settings=s, **kw)
    assert len(runner.calls) == 2

    ws = json.loads((out / "words.json").read_text(encoding="utf-8"))
    ws[1]["text"] = "changed"
    (out / "words.json").write_text(json.dumps(ws), encoding="utf-8")
    hf.make_overlay(settings=s, **kw)
    assert len(runner.calls) == 3

    s.hyperframes.format = "mov"
    dst2 = hf.make_overlay(settings=s, **kw)
    assert len(runner.calls) == 4 and dst2 == out / "captions_overlay.mov"
    assert not (out / "captions_overlay.webm").exists()  # the other format is removed

    s.hyperframes.fps = 30  # explicit fps overrides the cut's rate
    hf.make_overlay(settings=s, **kw)
    assert len(runner.calls) == 5 and runner.calls[-1]["args"][runner.calls[-1]["args"].index("--fps") + 1] == "30"


def test_make_overlay_cache_rejects_broken_file(tmp_path: Path, fonts_dir: Path, clip_dir: Path) -> None:
    s = hf_settings(tmp_path)
    out = clip_dir / "en"
    runner = FakeHFRunner()
    kw = dict(words_json=out / "words.json", cut=clip_dir / "cut.mp4", lang="en", out_dir=out, runner=runner,
              env=fake_env(tmp_path), settings=s)
    dst = hf.make_overlay(**kw)
    dst.write_bytes(b"garbage")
    hf.make_overlay(**kw)
    assert len(runner.calls) == 2


@pytest.mark.parametrize("content,match", [(None, "not found"), ("[]", "has no words"), ("{bad", "not valid"),
                                           ('[{"start": 0}]', "not valid")])
def test_make_overlay_bad_words_json(tmp_path: Path, fonts_dir: Path, clip_dir: Path, content: str | None,
                                     match: str) -> None:
    out = clip_dir / "en"
    wj = out / "words.json"
    if content is None:
        wj.unlink()
    else:
        wj.write_text(content, encoding="utf-8")
    runner = FakeHFRunner()
    with pytest.raises(hf.HyperFramesError, match=match):
        hf.make_overlay(words_json=wj, cut=clip_dir / "cut.mp4", lang="en", settings=hf_settings(tmp_path),
                        out_dir=out, runner=runner, env=fake_env(tmp_path))
    assert runner.calls == []


def test_make_overlay_unavailable_without_env(tmp_path: Path, clip_dir: Path) -> None:
    out = clip_dir / "en"
    with pytest.raises(hf.HyperFramesUnavailable):
        hf.make_overlay(words_json=out / "words.json", cut=clip_dir / "cut.mp4", lang="en",
                        settings=hf_settings(tmp_path), out_dir=out, runner=FakeHFRunner())


def test_make_overlay_without_vp9_decoder_fails_fast(tmp_path: Path, fonts_dir: Path, clip_dir: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hf, "has_vp9_alpha_decoder", lambda: False)
    runner = FakeHFRunner()
    out = clip_dir / "en"
    with pytest.raises(hf.HyperFramesError, match="libvpx-vp9.*hyperframes.format=mov"):
        hf.make_overlay(words_json=out / "words.json", cut=clip_dir / "cut.mp4", lang="en",
                        settings=hf_settings(tmp_path), out_dir=out, runner=runner, env=fake_env(tmp_path))
    assert runner.calls == []


def test_cut_fps_and_vp9_decoder() -> None:
    assert hf._rate("30000/1001") == pytest.approx(29.97, abs=0.01) and hf._rate("0/0") is None
    assert hf.has_vp9_alpha_decoder() in (True, False)


# --------------------------------------------------------------------------------------------- real render
def _real_settings() -> Settings | None:
    s = Settings()
    s.hyperframes.node_bin_dir = os.environ.get("AC_HF_NODE_BIN_DIR", str(REAL_HOME / "tools" / "node22" / "bin"))
    s.hyperframes.package_dir = os.environ.get("AC_HF_PACKAGE_DIR", str(REAL_HOME / "tools" / "hyperframes"))
    try:
        hf.probe_env(s.hyperframes)
    except hf.HyperFramesError:
        return None
    return s


@pytest.mark.slow
@pytest.mark.hyperframes
def test_real_hyperframes_render_and_composite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _real_settings()
    if s is None:
        pytest.skip("Node 22 + hyperframes not installed (see README 'HyperFrames animated captions')")
    if not hf.has_vp9_alpha_decoder():
        pytest.skip("ffmpeg has no libvpx-vp9 decoder")
    monkeypatch.setenv("HOME", str(REAL_HOME))  # the renderer's Chrome lives in ~/.cache/hyperframes
    from autoclipper.stages.render import render_final

    clip = tmp_path / "clips" / "00"
    out = clip / "hi"
    out.mkdir(parents=True)
    cut = make_video(clip / "cut.mp4", seconds=2.0, width=1080, height=1920)
    (out / "words.json").write_text(json.dumps([{"start": 0.1, "end": 1.6, "text": "नमस्ते"}], ensure_ascii=False),
                                    encoding="utf-8")
    overlay = hf.make_overlay(words_json=out / "words.json", cut=cut, lang="hi", settings=s, out_dir=out)
    hf.verify_overlay(overlay, fmt="webm", width=1080, height=1920, duration=2.0)
    final = render_final(cut=cut, voice=None, ass=None, overlay=overlay, dst=out / "final.mp4", audio=s.audio)

    def frame(path: Path, t: float, decoder: list[str]) -> bytes:
        return subprocess.run(["ffmpeg", "-v", "error", *decoder, "-ss", str(t), "-i", str(path), "-frames:v", "1",
                               "-f", "rawvideo", "-pix_fmt", "rgba", "-"], capture_output=True, check=True).stdout

    rgba = frame(overlay, 1.0, ["-c:v", "libvpx-vp9"])
    alphas = rgba[3::4]
    opaque = sum(1 for a in alphas if a > 200)
    assert 0 < opaque < len(alphas) * 0.2  # some caption pixels, mostly transparent
    assert alphas[0] == 0  # the top-left corner is transparent
    f_final = frame(final, 1.0, [])
    f_cut = frame(cut, 1.0, [])
    top = 1080 * 4 * 100  # first 100 rows: far from the caption at 72 %
    diff = sum(abs(a - b) for a, b in zip(f_final[:top:97], f_cut[:top:97])) / len(f_final[:top:97])
    assert diff < 12  # outside the caption the clip is unchanged (no black box)


def _fake_sfnt(upm: int, win_a: int, win_d: int, asc: int = 0, desc: int = 0) -> bytes:
    """A minimal sfnt with head / hhea / OS/2 tables (only the fields ass_em_ratio reads)."""
    import struct

    head = bytearray(54)
    struct.pack_into(">H", head, 18, upm)
    hhea = bytearray(36)
    struct.pack_into(">hh", hhea, 4, asc, desc)
    os2 = bytearray(96)
    struct.pack_into(">HH", os2, 74, win_a, win_d)
    tables = [(b"OS/2", bytes(os2)), (b"head", bytes(head)), (b"hhea", bytes(hhea))]
    off = 12 + 16 * len(tables)
    hdr = struct.pack(">IHHHH", 0x00010000, len(tables), 0, 0, 0)
    recs, body = b"", b""
    for tag, data in tables:
        recs += struct.pack(">4sIII", tag, 0, off + len(body), len(data))
        body += data
    return hdr + recs + body


def test_ass_em_ratio_matches_libass_sizing(tmp_path: Path) -> None:
    f = tmp_path / "a.ttf"
    f.write_bytes(_fake_sfnt(1000, 896, 408))  # Noto Sans Devanagari Bold metrics
    assert hf.ass_em_ratio(f) == pytest.approx(1000 / 1304)
    f.write_bytes(_fake_sfnt(2048, 0, 0, asc=1638, desc=-410))  # no OS/2 win metrics -> hhea
    assert hf.ass_em_ratio(f) == pytest.approx(2048 / 2048)
    f.write_bytes(b"not a font")
    assert hf.ass_em_ratio(f) == hf.DEFAULT_ASS_EM_RATIO
    assert hf.ass_em_ratio(None) == hf.DEFAULT_ASS_EM_RATIO
    assert hf.ass_em_ratio(tmp_path / "missing.ttf") == hf.DEFAULT_ASS_EM_RATIO


def test_build_composition_font_size_follows_the_lang_font(tmp_path: Path, gsap: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "NotoSans-Bold.ttf").write_bytes(_fake_sfnt(1000, 1069, 293))
    (fonts / "NotoSansDevanagari-Bold.ttf").write_bytes(_fake_sfnt(1000, 896, 408))
    monkeypatch.setattr(hf.config, "FONTS_DIR", fonts)
    s = Settings()
    _, html_hi = build(tmp_path, words(["नमस्ते"]), lang="hi", settings=s, gsap=gsap, name="hi")
    _, html_en = build(tmp_path, words(["hello"]), lang="en", settings=s, gsap=gsap, name="en")
    assert f"font-size: {round(s.captions.font_size * 1000 / 1304)}px" in html_hi
    assert f"font-size: {round(s.captions.font_size * 1000 / 1362)}px" in html_en


@pytest.mark.slow
@pytest.mark.hyperframes
def test_real_render_line_switch_and_long_word_pop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Triage R2 (correctness) + R2 (visual), with the real renderer: no blank frame where one line
    replaces another, and a popped word that fills the caption box stays inside the frame."""
    s = _real_settings()
    if s is None:
        pytest.skip("Node 22 + hyperframes not installed (see README 'HyperFrames animated captions')")
    if not hf.has_vp9_alpha_decoder():
        pytest.skip("ffmpeg has no libvpx-vp9 decoder")
    monkeypatch.setenv("HOME", str(REAL_HOME))
    s.hyperframes.fps = 25
    s.hyperframes.style = "pop"

    def overlay(name: str, ws: list[tuple[float, float, str]], per_line: int, seconds: float) -> Path:
        clip = tmp_path / name
        out = clip / "en"
        out.mkdir(parents=True)
        cut = make_video(clip / "cut.mp4", seconds=seconds, width=1080, height=1920)
        (out / "words.json").write_text(json.dumps([{"start": a, "end": b, "text": t} for a, b, t in ws]),
                                        encoding="utf-8")
        s.captions.words_per_line = per_line
        return hf.make_overlay(words_json=out / "words.json", cut=cut, lang="en", settings=s, out_dir=out)

    def alpha(path: Path, t: float) -> bytes:
        return subprocess.run(["ffmpeg", "-v", "error", "-c:v", "libvpx-vp9", "-ss", str(t), "-i", str(path),
                               "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                              capture_output=True, check=True).stdout[3::4]

    ov = overlay("switch", [(0.0, 0.45, "one"), (0.5, 0.95, "two"), (1.0, 1.45, "three")], 1, 2.0)
    for t in (0.5, 0.52, 1.0):  # exactly on a line change (frame 25 at 25 fps) and just after it
        assert sum(1 for a in alpha(ov, t) if a > 200) > 1000, t

    ov = overlay("long", [(0.0, 0.5, "Internationalization"), (0.5, 1.0, "incomprehensibilities"),
                          (1.0, 1.8, "Supercalifragilisticexpialidocious")], 3, 2.2)
    a = alpha(ov, 1.2)  # the long word is popped
    edge = [a[y * 1080 + x] for y in range(1100, 1700) for x in (*range(0, 30), *range(1050, 1080))]
    assert max(edge) == 0  # nothing reaches the frame edges
