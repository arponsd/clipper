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

STYLES = ("pop", "karaoke", "color", "none")  # color / none: captions.highlight_style (G5)
HOOK_ANIMATIONS = ("pop", "slide", "fade")
HOOK_IN = 0.32
HOOK_OUT = 0.25
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
    emph: bool = False  # G5 keyword emphasis (emphasis colour, bigger)
    emoji: str = ""  # drawn after the word (G5, this engine only)


@dataclass(frozen=True)
class HookSpec:
    """G4: the hook line over the first seconds."""
    text: str
    end: float
    top_px: float  # top edge of the box / outlined text (the hook grows downwards from here)
    font_px: int
    pad_px: int  # vertical padding of the box (the same as the ASS box), or 0 for plain
    wrap_px: int  # the text wraps inside this width, like the ASS hook
    color: str  # CSS colour of the text
    box: str | None  # CSS colour of the box, or None for the plain (outlined) style
    outline: str  # CSS colour of the plain-style outline
    stroke_px: int
    animation: str  # pop | slide | fade


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
    # G5 style; the defaults are the step-3 look
    bold: bool = True
    outline: str = "#000"  # CSS colour of the text outline
    shadow_px: int = 2  # 2 -> "0 4px 14px", the step-3 shadow
    box: str | None = None  # CSS colour of a box behind each line (no outline then)
    emphasis: str = "#ffa500"  # CSS colour of emphasised words
    emphasis_scale: float = 1.2
    emoji_family: str | None = None  # @font-face alias of the emoji font, when an emoji is drawn
    emoji_file: str | None = None  # its file under assets/fonts (None -> local("Noto Color Emoji"))
    hook: HookSpec | None = None


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
    if p.hook is not None and p.hook.animation not in HOOK_ANIMATIONS:
        raise ValueError(f"unknown hook animation {p.hook.animation!r}")
    weight = 700 if p.bold else 400
    faces = []
    for f in p.fonts:
        src = f'url("assets/fonts/{f.file}")' if f.file else f"local({_css_str(f.local_name)})"
        faces.append(f"@font-face {{ font-family: {_css_str(f.family)}; src: {src}; font-weight: {weight}; "
                     f"font-style: normal; }}")
    any_emoji = any(w.emoji for g in groups for w in g.words)
    if any_emoji and p.emoji_family:
        src = f'url("assets/fonts/{p.emoji_file}")' if p.emoji_file else 'local("Noto Color Emoji")'
        faces.append(f"@font-face {{ font-family: {_css_str(p.emoji_family)}; src: {src}; }}")
    if p.top_px is not None:
        place = f"top: {p.top_px:.0f}px; transform: translateY(-50%);"
    else:
        place = f"bottom: {int(p.bottom_px)}px;"
    W, H = int(p.width), int(p.height)

    def span(w: WordSpec) -> str:
        cls = "w em" if w.emph else "w"
        emo = f'<span class="emo">{html.escape(w.emoji, quote=True)}</span>' if w.emoji else ""
        return f'<span id="{w.id}" class="{cls}">{html.escape(w.text, quote=True)}{emo}</span>'

    cap_cls = "cap box clip" if p.box else "cap clip"
    body = []
    for g in groups:
        spans = "".join(span(w) for w in g.words)
        body.append(f'<div id="{g.id}" class="{cap_cls}" data-start="{g.start:.3f}" '
                    f'data-duration="{max(MIN_GROUP, g.end - g.start):.3f}" data-track-index="1">{spans}</div>')
    hk = p.hook
    if hk is not None:
        body.append(f'<div id="hook" class="hook clip" data-start="0.000" data-duration="{max(MIN_GROUP, hk.end):.3f}" '
                    f'data-track-index="2"><span id="hook-in" class="hook-in">{html.escape(hk.text, quote=True)}'
                    f'</span></div>')

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
    if any(w.emoji for g in groups for w in g.words) and p.emoji_family:
        families.append(p.emoji_family)
    pop_fn = ""
    if p.style == "pop":
        pop_fn = POP_FN_JS.replace("@MAX@", repr(POP_SCALE)).replace("@BOX@", f"{W * SAFE_BOX:.1f}")
    colors = {"p": p.primary, "h": p.highlight}
    any_emph = any(w.emph for g in groups for w in g.words)
    if any_emph:
        # emphasised words rest in the emphasis colour; while spoken they take the highlight colour like any
        # other word, so the "current word" cue never disappears on the most important word
        colors["e"] = p.emphasis
        cp, ch = "cp", "C.h"
        pre = 'const cp = el.classList.contains("em") ? C.e : C.p;\n    '
    else:
        cp, ch, pre = "C.p", "C.h", ""
    if p.style == "pop":
        word_js = (f'{pre}const k = popScale(el);\n'
                   f'    tl.fromTo(el, {{scale: 1, color: {cp}}}, {{scale: k, color: {ch}, duration: {POP_IN}, '
                   f'ease: "back.out(3)"}}, s);\n'
                   f'    tl.to(el, {{scale: 1, color: {cp}, duration: {POP_IN}}}, Math.max(s + {POP_IN}, e));')
    elif p.style == "karaoke":  # the word turns to the highlight colour and stays so until its group ends
        word_js = (f'{pre}tl.fromTo(el, {{color: {cp}}}, {{color: {ch}, duration: {KARAOKE_IN}, ease: "none"}}, s);')
    elif p.style == "color":  # the spoken word takes the highlight colour, then goes back (no scale)
        word_js = (f'{pre}tl.fromTo(el, {{color: {cp}}}, {{color: {ch}, duration: {KARAOKE_IN}, ease: "none"}}, s);\n'
                   f'    tl.to(el, {{color: {cp}, duration: {KARAOKE_IN}, ease: "none"}}, Math.max(s + {KARAOKE_IN}, e));')
    else:  # none: no word animation
        word_js = "void el; void s; void e;"
    hook_js = ""
    if hk is not None:
        out_at = max(0.0, hk.end - HOOK_OUT)
        if hk.animation == "pop":
            hin = (f'tl.fromTo(h, {{scale: 0.55, opacity: 0}}, {{scale: 1, opacity: 1, duration: {HOOK_IN}, '
                   f'ease: "back.out(2.2)"}}, 0);')
        elif hk.animation == "slide":
            hin = (f'tl.fromTo(h, {{y: -140, opacity: 0}}, {{y: 0, opacity: 1, duration: {HOOK_IN}, '
                   f'ease: "power3.out"}}, 0);')
        else:
            hin = f'tl.fromTo(h, {{opacity: 0}}, {{opacity: 1, duration: {HOOK_IN}, ease: "none"}}, 0);'
        hook_js = (f'    const h = document.getElementById("hook-in");\n'
                   f'    {hin}\n'
                   f'    tl.to(h, {{opacity: 0, duration: {HOOK_OUT}, ease: "none"}}, {out_at:.3f});\n')

    stroke = f"{int(p.stroke_px)}px {p.outline}" if not p.box else "0"
    sh = max(0, int(p.shadow_px))
    shadow = f"0 {2 * sh}px {7 * sh}px rgba(0,0,0,.55)" if sh and not p.box else "none"
    extra = []
    if p.box:
        if p.top_px is not None:
            place_box = f"top: {p.top_px:.0f}px; transform: translate(-50%, -50%);"
        else:
            place_box = f"bottom: {int(p.bottom_px)}px; transform: translateX(-50%);"
        # max-content: a box at left 50% would otherwise shrink-wrap to the other half of the frame and wrap
        extra.append(f".cap.box {{ left: 50%; width: max-content; max-width: 90%; {place_box} background: {p.box}; "
                     f"padding: {0.12 * p.font_px:.0f}px {0.3 * p.font_px:.0f}px; border-radius: {0.22 * p.font_px:.0f}px; }}")
    if any_emph:
        extra.append(f".w.em {{ font-size: {round(p.font_px * p.emphasis_scale)}px; color: {p.emphasis}; }}")
    if any_emoji:
        fam = (_css_str(p.emoji_family) + ", ") if p.emoji_family else ""
        extra.append(f'.emo {{ font-family: {fam}"Noto Color Emoji", sans-serif; font-weight: 400; '
                     f"margin-left: 0.18em; -webkit-text-stroke: 0; text-shadow: none; }}")
    if hk is not None:
        hstroke = "0" if hk.box else f"{int(hk.stroke_px)}px {hk.outline}"
        look = (f"background: {hk.box}; padding: {int(hk.pad_px)}px {0.42 * hk.font_px:.0f}px; "
                f"border-radius: {0.2 * hk.font_px:.0f}px; box-shadow: 0 8px 24px rgba(0,0,0,.35);"
                if hk.box else "text-shadow: 0 6px 18px rgba(0,0,0,.6);")
        # top edge at top_px, growing downwards (never off the top of the frame); content-box max-width =
        # the ASS wrap width, so both engines break lines at about the same place
        extra.append(f".hook {{ position: absolute; left: 0; width: 100%; top: {hk.top_px:.0f}px; "
                     f"display: flex; justify-content: center; }}")
        extra.append(f".hook-in {{ display: inline-block; box-sizing: content-box; max-width: {int(hk.wrap_px)}px; "
                     f"text-align: center; text-wrap: balance; overflow-wrap: anywhere; font-family: "
                     f"{p.font_stack}; font-size: {int(hk.font_px)}px; font-weight: {weight}; line-height: 1.18; "
                     f"color: {hk.color}; -webkit-text-stroke: {hstroke}; paint-order: stroke fill; {look} }}")
    extra_css = "".join(x + "\n" for x in extra)
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
  font-weight: {weight}; line-height: 1.25; color: {p.primary}; -webkit-text-stroke: {stroke};
  paint-order: stroke fill; text-shadow: {shadow}; white-space: nowrap; }}
{extra_css}</style>
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
{hook_js}    window.__timelines = window.__timelines || {{}};
    window.__timelines["main"] = tl;
  }}
  const fonts = document.fonts;
  if (!fonts || !fonts.load) {{ build(); return; }}
  Promise.all(F.map((f) => fonts.load("{weight} {int(p.font_px)}px " + JSON.stringify(f)).catch(() => null)))
    .then(() => fonts.ready).catch(() => null).then(build);
}})();
</script>
</body>
</html>
"""
