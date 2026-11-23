"""HTML template of the HyperFrames caption overlay (CONTRACT_STEP3 section 3.2).

One standalone ``index.html``: a transparent 1080x1920 root with one ``.cap.clip`` group per caption line
and one ``.w`` span per word. GSAP comes from a local copy (``assets/vendor/gsap.min.js``) and the fonts
from ``assets/fonts``, so the page needs no network. Word text appears only HTML-escaped inside the spans;
the script holds ids, numbers and colours only. The output is a pure function of its inputs (byte-identical
for the same inputs), which is what makes the overlay cache in ``hyperframes.make_overlay`` valid.
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass

STYLES = ("pop", "karaoke")
POP_SCALE = 1.18  # upper bound; each word's real pop is capped in the page (see _pop_js)
WORD_MARGIN_EM = 0.13  # per side: a 0.26em gap between words, about one normal space (ASS)
SAFE_BOX = 0.9  # the caption box is 90 % of the frame width
POP_IN = 0.08
GROUP_FADE = 0.06
KARAOKE_IN = 0.05
LAST_GROUP_HOLD = 0.6
MIN_GROUP = 0.05


@dataclass(frozen=True)
class FontFace:
    family: str  # CSS family alias, e.g. "AC Caption HI"
    file: str | None  # file name under assets/fonts, or None -> src: local(<local_name>)
    local_name: str  # the system family name used when file is None


@dataclass(frozen=True)
class WordSpec:
    id: str
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class GroupSpec:
    id: str
    start: float
    end: float
    words: tuple[WordSpec, ...]


@dataclass(frozen=True)
class TemplateParams:
    lang: str
    width: int
    height: int
    fps: int
    duration: float
    style: str
    fonts: tuple[FontFace, ...]
    font_stack: str  # CSS font-family value
    font_px: int
    stroke_px: int
    primary: str  # CSS colour
    highlight: str  # CSS colour
    top_px: float | None  # centre of the line from the top; None -> bottom anchored
    bottom_px: int  # used when top_px is None


# Pop only: the popped word grows around its centre, so cap its scale per word: it may use up its own side
# margin (never its neighbours') and never get wider than the 90 % caption box. Measured once, after the
# fonts have loaded, so the widths are the real glyph widths (deterministic for the same page).
POP_FN_JS = """  const MAX_POP = @MAX@;
  const BOX = @BOX@;
  function popScale(el) {
    const w = el.offsetWidth;
    if (!(w > 0)) return 1;
    const m = parseFloat(getComputedStyle(el).marginLeft) || 0;
    return Math.max(1, Math.min(MAX_POP, 1 + 2 * m / w, BOX / w));
  }
"""


def _t(x: float) -> float:
    """Seconds rounded to ms (keeps the JSON short and the output stable)."""
    return round(float(x) + 0.0, 3)


def _css_str(value: str) -> str:
    """A CSS double-quoted string (font names come from settings)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("<", "\\3c ") + '"'


def group_words(words: list, per_line: int, duration: float) -> list[GroupSpec]:
    """Chunk words exactly like captions.build_ass (words[k:k+per_line]) and give each chunk a
    non-overlapping time window: from its first word to the next group's start (the last group holds
    LAST_GROUP_HOLD s, capped at the clip duration), never ending before its last word, at least MIN_GROUP s.
    ``words`` are objects with start / end / text."""
    per_line = max(1, int(per_line))
    chunks = [words[k:k + per_line] for k in range(0, len(words), per_line)]
    out: list[GroupSpec] = []
    prev_end = 0.0
    for gi, chunk in enumerate(chunks):
        first, last = chunk[0], chunk[-1]
        gs = max(0.0, float(first.start), prev_end)
        if gi + 1 < len(chunks):
            ge = float(chunks[gi + 1][0].start)
        else:
            ge = min(float(duration), float(last.end) + LAST_GROUP_HOLD)
        ge = max(ge, float(last.end), gs + MIN_GROUP)
        gs, ge = _t(gs), _t(ge)
        if ge - gs < MIN_GROUP:
            ge = _t(gs + MIN_GROUP)
        ws = tuple(WordSpec(id=f"w{gi}_{k}", text=str(w.text), start=_t(max(float(w.start), gs)),
                            end=_t(max(float(w.end), float(w.start), gs)))
                   for k, w in enumerate(chunk))
        out.append(GroupSpec(id=f"g{gi}", start=gs, end=ge, words=ws))
        prev_end = ge
    return out


def _json_for_script(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")


def render_html(groups: list[GroupSpec], p: TemplateParams) -> str:
    if p.style not in STYLES:
        raise ValueError(f"unknown caption style {p.style!r}")
    faces = []
    for f in p.fonts:
        src = f'url("assets/fonts/{f.file}")' if f.file else f"local({_css_str(f.local_name)})"
        faces.append(f"@font-face {{ font-family: {_css_str(f.family)}; src: {src}; font-weight: 700; "
                     f"font-style: normal; }}")
    if p.top_px is not None:
        place = f"top: {p.top_px:.0f}px; transform: translateY(-50%);"
    else:
        place = f"bottom: {int(p.bottom_px)}px;"
    W, H = int(p.width), int(p.height)

    body = []
    for g in groups:
        spans = "".join(f'<span id="{w.id}" class="w">{html.escape(w.text, quote=True)}</span>' for w in g.words)
        body.append(f'<div id="{g.id}" class="cap clip" data-start="{g.start:.3f}" '
                    f'data-duration="{max(MIN_GROUP, g.end - g.start):.3f}" data-track-index="1">{spans}</div>')

    w_data = [[w.id, w.start, w.end] for g in groups for w in g.words]
    # Fade a group in only when no group was on screen right before it. A line that replaces another one
    # switches at once (like ASS): fading it in from 0 left a blank frame at every line change.
    g_data = []
    prev_end: float | None = None
    for g in groups:
        fade = prev_end is None or g.start > prev_end + 1e-6
        g_data.append([g.id, g.start, 1 if fade else 0])
        prev_end = g.end
    families = [f.family for f in p.fonts]
    pop_fn = ""
    if p.style == "pop":
        pop_fn = POP_FN_JS.replace("@MAX@", repr(POP_SCALE)).replace("@BOX@", f"{W * SAFE_BOX:.1f}")
    colors = {"p": p.primary, "h": p.highlight}
    if p.style == "pop":
        word_js = (f'const k = popScale(el);\n'
                   f'    tl.fromTo(el, {{scale: 1, color: C.p}}, {{scale: k, color: C.h, duration: {POP_IN}, '
                   f'ease: "back.out(3)"}}, s);\n'
                   f'    tl.to(el, {{scale: 1, color: C.p, duration: {POP_IN}}}, Math.max(s + {POP_IN}, e));')
    else:  # karaoke: the word turns to the highlight colour and stays so until its group ends
        word_js = (f'tl.fromTo(el, {{color: C.p}}, {{color: C.h, duration: {KARAOKE_IN}, ease: "none"}}, s);')

    return f"""<!doctype html>
<html lang="{html.escape(p.lang, quote=True)}">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width={W}, height={H}" />
<title>Auto Clipper captions</title>
<script src="assets/vendor/gsap.min.js"></script>
<style>
{chr(10).join(faces)}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {W}px; height: {H}px; overflow: hidden; background: transparent; }}
#root {{ position: relative; width: {W}px; height: {H}px; overflow: hidden; background: transparent; }}
.cap {{ position: absolute; left: 5%; width: 90%; {place} display: flex; flex-wrap: wrap;
  justify-content: center; align-items: center; overflow: visible; }}
.w {{ display: inline-block; margin: 0 {WORD_MARGIN_EM}em; font-family: {p.font_stack}; font-size: {int(p.font_px)}px;
  font-weight: 700; line-height: 1.25; color: {p.primary}; -webkit-text-stroke: {int(p.stroke_px)}px #000;
  paint-order: stroke fill; text-shadow: 0 4px 14px rgba(0,0,0,.55); white-space: nowrap; }}
</style>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{p.duration:.3f}" data-width="{W}" data-height="{H}" data-fps="{int(p.fps)}">
{chr(10).join(body)}
</div>
<script>
(function () {{
  const C = {_json_for_script(colors)};
  const W = {_json_for_script(w_data)};
  const G = {_json_for_script(g_data)};
  const F = {_json_for_script(families)};
{pop_fn}  function build() {{
    const tl = gsap.timeline({{ paused: true }});
    for (const [id, s, e] of W) {{
      const el = document.getElementById(id);
      {word_js}
    }}
    for (const [id, s, fade] of G) {{
      if (fade) tl.fromTo(document.getElementById(id), {{opacity: 0}}, {{opacity: 1, duration: {GROUP_FADE}}}, s);
    }}
    window.__timelines = window.__timelines || {{}};
    window.__timelines["main"] = tl;
  }}
  const fonts = document.fonts;
  if (!fonts || !fonts.load) {{ build(); return; }}
  Promise.all(F.map((f) => fonts.load("700 {int(p.font_px)}px " + JSON.stringify(f)).catch(() => null)))
    .then(() => fonts.ready).catch(() => null).then(build);
}})();
</script>
</body>
</html>
"""
