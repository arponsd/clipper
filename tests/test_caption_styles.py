"""G4 hook text overlay + G5 caption style presets and keyword emphasis.

Generation (ASS + HyperFrames page), timing, escaping, staleness, presets, settings validation, the LLM
schema/prompt, and the API (happy path, validation 422, 401/403, 404, response contract, side effects).
Nothing here calls Anthropic, Voicebox, HyperFrames or the network.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fakes import FakeLLM, FakeTranscriber, FakeVoicebox, make_video
from test_web_support import (api, assert_error_shape, assert_keys, make_client,  # noqa: F401
                              make_job_on_disk, read_json)

from autoclipper import caption_presets, cli
from autoclipper.config import CaptionSettings, HookSettings, Settings, load_settings, save_settings
from autoclipper.llm import METADATA_SCHEMA, SCRIPT_SCHEMA, AnthropicLLM, hook_keywords_instruction
from autoclipper.models import ClipScript, Keyword, Moment, Word
from autoclipper.stages import caption_style as cst
from autoclipper.stages import hf_template
from autoclipper.stages import hyperframes as hf
from autoclipper.stages import render
from autoclipper.stages.captions import build_ass, rebuild_ass
from autoclipper.stages.script import normalize_script
from autoclipper.web.auth import TOKEN_HEADER


def W(*texts: str, step: float = 0.4) -> list[Word]:
    return [Word(start=round(i * step, 3), end=round(i * step + step * 0.9, 3), text=t) for i, t in enumerate(texts)]


def dialogues(ass: str, style: str = "Default") -> list[str]:
    return [ln for ln in ass.splitlines() if ln.startswith("Dialogue:") and f",{style}," in ln]


# ============================================================================ text clean-up
@pytest.mark.parametrize("text,ok", [("🔥", True), ("👍🏽", True), ("❤️", True), ("🏳️‍🌈", True), ("🇮🇳", True),
                                      ("", False), ("a", False), ("🔥a", False), ("1", False), (" ", False),
                                      ("🔥" * 11, False), ("‍", False), ("🔥🔥", False), ("🔥🔥🔥🔥🔥", False),
                                      ("👨‍👩‍👧", True), ("🇮🇳🇺🇸", False), ("👍🏽👍🏽", False)])
def test_is_emoji(text: str, ok: bool) -> None:
    assert cst.is_emoji(text) is ok


def test_clean_hook_one_line_no_quotes_capped() -> None:
    assert cst.clean_hook('  "Wait for\nthe\tlast  second"  ') == "Wait for the last second"
    assert cst.clean_hook("“यह देखो”") == "यह देखो"
    assert cst.clean_hook(None) == "" and cst.clean_hook("   ") == ""
    long = " ".join(["word"] * 30)
    assert len(cst.clean_hook(long).split()) == cst.HOOK_MAX_WORDS
    assert len(cst.clean_hook("x" * 300)) <= cst.HOOK_MAX_CHARS
    assert "\x07" not in cst.clean_hook("bell\x07ring")


def test_hook_and_keyword_errors() -> None:
    assert cst.hook_error("short hook") is None
    assert cst.hook_error("x" * 101) and "100" in cst.hook_error("x" * 101)
    assert cst.hook_error(" ".join(["a"] * 13))
    assert cst.hook_error("line one\nline two") is None  # joined, not an error
    assert cst.keyword_error("clutch") is None and cst.keyword_error("कमाल") is None
    assert cst.keyword_error("") and cst.keyword_error("   ") and cst.keyword_error("!!!")
    assert cst.keyword_error("a b c d") and cst.keyword_error("x" * 41)
    assert cst.emoji_error("") is None and cst.emoji_error("🔥") is None and cst.emoji_error("fire")


def test_clean_keywords_drops_bad_dedupes_and_caps() -> None:
    out = cst.clean_keywords([{"word": " Clutch ", "emoji": "🔥"}, {"word": "clutch!", "emoji": ""},
                              {"word": "", "emoji": "🔥"}, {"word": "insane", "emoji": "not emoji"},
                              "win", 42, Keyword(word="कमाल", emoji="😱")]
                             + [{"word": f"w{i}", "emoji": ""} for i in range(20)])
    assert out[:4] == [Keyword(word="Clutch", emoji="🔥"), Keyword(word="insane", emoji=""),
                       Keyword(word="win", emoji=""), Keyword(word="कमाल", emoji="😱")]
    assert len(out) == cst.MAX_KEYWORDS


def test_normalize_script_cleans_hook_and_keywords() -> None:
    s = normalize_script(ClipScript(narration="n", title="t", description="d", hashtags=[],
                                    hook='"  Big   hook "', keywords=[Keyword(word="x y z w", emoji=""),
                                                                      Keyword(word="ok", emoji="fire")]))
    assert s.hook == "Big hook" and s.keywords == [Keyword(word="ok", emoji="")]
    old = ClipScript.model_validate({"narration": "n", "title": "t", "description": "d", "hashtags": []})
    assert old.hook == "" and old.keywords == []  # script.json from before G4/G5 still loads


# ============================================================================ emphasis matching
def test_emphasis_marks_case_punct_inflection_and_line_cap() -> None:
    words = W("That", "CLUTCH,", "was", "clutched", "insane!", "win", "win", "win")
    kws = [Keyword(word="clutch", emoji="🔥"), Keyword(word="insane", emoji="😱"), Keyword(word="win", emoji="")]
    m = cst.emphasis_marks(words, kws, per_line=3, max_per_line=2)
    # line 1 = 0..2: "CLUTCH," (emoji, first time); line 2 = 3..5: "clutched" (inflection, no 2nd emoji) + insane
    assert m[1] == "🔥" and m[3] == "" and m[4] == "😱"
    assert 5 not in m  # line 2 already has 2 matches
    assert m[6] == "" and m[7] == ""  # line 3 = 6..7
    assert 0 not in m and 2 not in m


def test_emphasis_marks_short_keys_need_exact_match_and_phrases() -> None:
    words = W("go", "gone", "the", "final", "round", "now")
    m = cst.emphasis_marks(words, [Keyword(word="go"), Keyword(word="final round", emoji="🏆")], per_line=6,
                           max_per_line=3)
    assert m == {0: "", 3: "", 4: "🏆"}  # "gone" is not "go" (keys under 4 letters match exactly)
    # a phrase never spans two caption lines
    assert cst.emphasis_marks(words, [Keyword(word="final round")], per_line=4, max_per_line=3) == {}


def test_emphasis_marks_devanagari_spelling_variants() -> None:
    # nukta / chandrabindu / nasal+virama variants fold like the caption aligner
    words = W("हिन्दी", "में", "ज़बरदस्त", "कमाल")
    m = cst.emphasis_marks(words, [Keyword(word="हिंदी"), Keyword(word="जबरदस्त", emoji="🔥")],
                           per_line=4, max_per_line=3)
    assert m == {0: "", 2: "🔥"}
    assert cst.emphasis_marks([], [Keyword(word="x")], per_line=3) == {}
    assert cst.emphasis_marks(words, [], per_line=3) == {}


# ============================================================================ ASS generation
def test_build_ass_default_settings_are_the_step4_output() -> None:
    ws = W("one", "two", "three", "four")
    ass = build_ass(ws, lang="en", settings=CaptionSettings())
    assert re.search(r"^Style: Default,Noto Sans,78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,"
                     r"100,100,0,0,1,5,2,5,60,60,520,1$", ass, re.M)
    assert "Style: Hook" not in ass and "fscx" not in ass
    d = dialogues(ass)
    assert len(d) == 4 and d[0].endswith("{\\c&H00E5FF&}one{\\c&HFFFFFF&} two three")


def test_build_ass_style_fields_box_bold_outline_uppercase() -> None:
    cs = CaptionSettings(bold=False, uppercase=True, outline_color="&H00FF00FF", outline_px=9, shadow_px=0)
    ass = build_ass(W("hello", "नमस्ते"), lang="en", settings=cs)
    assert ",&H00FF00FF,&H80000000,0,0,0,0,100,100,0,0,1,9,0,5," in ass
    assert "HELLO" in ass and "नमस्ते" in ass  # Devanagari has no case
    boxed = build_ass(W("a"), lang="en", settings=CaptionSettings(box=True, box_color="&H30000000"))
    style = next(ln for ln in boxed.splitlines() if ln.startswith("Style: Default"))
    assert ",&H30000000,&H80000000,-1,0,0,0,100,100,0,0,3," in style and style.split(",")[17] == "0"


@pytest.mark.parametrize("hs,events,check", [
    ("color", 3, lambda d: d[1].endswith("a {\\c&H00E5FF&}b{\\c&HFFFFFF&} c")),
    ("pop", 3, lambda d: "{\\c&H00E5FF&\\fscx115\\fscy115}b{\\c&HFFFFFF&\\fscx100\\fscy100}" in d[1]),
    ("karaoke", 3, lambda d: d[2].count("{\\c&H00E5FF&}") == 3),
    ("none", 1, lambda d: d[0].endswith("}a b c") and "\\c" not in d[0]),
])
def test_build_ass_highlight_styles(hs, events, check) -> None:
    d = dialogues(build_ass(W("a", "b", "c"), lang="en", settings=CaptionSettings(highlight_style=hs)))
    assert len(d) == events and check(d)


def test_build_ass_none_style_line_timing_holds_across_short_pause() -> None:
    ws = [Word(start=0.0, end=0.5, text="a"), Word(start=0.5, end=1.0, text="b"),
          Word(start=1.3, end=1.8, text="c")]
    d = dialogues(build_ass(ws, lang="en", settings=CaptionSettings(highlight_style="none", words_per_line=2)))
    assert d[0].startswith("Dialogue: 0,0:00:00.00,0:00:01.30,") and d[1].startswith("Dialogue: 0,0:00:01.30,0:00:01.80,")


def test_build_ass_keyword_emphasis_colour_scale_no_emoji() -> None:
    cs = CaptionSettings(emphasis_color="&H00112233", emphasis_scale=1.5, highlight_style="pop")
    d = dialogues(build_ass(W("this", "clutch", "wow"), lang="en", settings=cs,
                            keywords=[Keyword(word="clutch", emoji="🔥")]))
    emph = "{\\c&H112233&\\fscx150\\fscy150}clutch{\\c&HFFFFFF&\\fscx100\\fscy100}"
    assert emph in d[0] and emph in d[2]
    # spoken + pop: 1.5 x 1.15, and the highlight colour wins while it is spoken (the "current word" cue)
    assert "{\\c&H00E5FF&\\fscx172\\fscy172}clutch" in d[1]
    assert all("🔥" not in x for x in d)  # libass has no colour emoji: never drawn by the ASS engine
    off = dialogues(build_ass(W("this", "clutch"), lang="en", settings=CaptionSettings(emphasis=False),
                              keywords=[Keyword(word="clutch")]))
    assert all("fscx" not in x for x in off)


def test_build_ass_hook_event_position_fade_and_clip_length() -> None:
    hs = HookSettings(duration=3.0, top_pct=8, font_size=90)
    ass = build_ass(W("a"), lang="en", settings=CaptionSettings(), hook="Wait for it", hook_settings=hs,
                    duration=2.2)
    hooks = dialogues(ass, "Hook")
    # \an8: top-centre anchor at 8 % (154 px) + the 16 px box padding, so the box top is at 154 px
    assert hooks == ["Dialogue: 1,0:00:00.00,0:00:02.20,Hook,,0,0,0,,{\\an8\\pos(540,170)\\fad(150,250)}Wait for it"]
    style = next(ln for ln in ass.splitlines() if ln.startswith("Style: Hook"))
    assert style.startswith("Style: Hook,Noto Sans,90,&H00FFFFFF,&H00FFFFFF,&H002A1AE8,") and ",3,16,0,8,90,90,0,1" in style
    plain = build_ass(W("a"), lang="hi", settings=CaptionSettings(), hook="देखो",
                      hook_settings=HookSettings(style="plain", outline_color="&H00010203"))
    pstyle = next(ln for ln in plain.splitlines() if ln.startswith("Style: Hook"))
    assert pstyle.startswith("Style: Hook,Noto Sans Devanagari,112,") and ",&H00010203,&H80000000," in pstyle
    assert ",1,8,3,8,90,90,0,1" in pstyle
    assert dialogues(plain, "Hook")[0].endswith("}देखो") and ",0:00:02.50," in dialogues(plain, "Hook")[0]


def test_build_ass_hook_is_escaped_and_works_without_words() -> None:
    ass = build_ass([], lang="en", settings=CaptionSettings(), hook="{\\b1}evil\\N", duration=10)
    h = dialogues(ass, "Hook")
    assert len(h) == 1 and h[0].endswith("\\{\\⁠b1\\}evil\\⁠N") and len(dialogues(ass)) == 0


def test_rebuild_ass_reads_words_script_and_settings(tmp_path: Path) -> None:
    out = tmp_path / "en"
    out.mkdir()
    assert rebuild_ass(out, lang="en", settings=Settings(), script=None, duration=5) is None  # no words.json
    (out / "words.json").write_text(json.dumps([w.model_dump() for w in W("big", "clutch")]), encoding="utf-8")
    s = Settings()
    s.hook.uppercase = True
    script = ClipScript(narration="n", title="t", description="", hook="watch this",
                        keywords=[Keyword(word="clutch")])
    ass = rebuild_ass(out, lang="en", settings=s, script=script, duration=5).read_text(encoding="utf-8")
    assert dialogues(ass, "Hook")[0].endswith("}WATCH THIS") and "fscx120" in ass
    s.hook.enabled = False
    ass = rebuild_ass(out, lang="en", settings=s, script=script, duration=5).read_text(encoding="utf-8")
    assert "Style: Hook" not in ass


# ============================================================================ HyperFrames page
def _params(**kw) -> hf_template.TemplateParams:
    base = dict(lang="en", width=1080, height=1920, fps=25, duration=6.0, style="pop", fonts=(),
                font_stack='"AC Caption EN", sans-serif', font_px=60, stroke_px=10, primary="rgba(255,255,255,1)",
                highlight="rgba(255,229,0,1)", top_px=1382.0, bottom_px=520)
    base.update(kw)
    return hf_template.TemplateParams(**base)


def _groups(*texts, emph=(), emoji=None):
    groups = hf_template.group_words(W(*texts), 3, 6.0)
    out, k = [], 0
    import dataclasses

    for g in groups:
        ws = []
        for w in g.words:
            if k in emph:
                w = dataclasses.replace(w, emph=True, emoji=(emoji or {}).get(k, ""))
            ws.append(w)
            k += 1
        out.append(dataclasses.replace(g, words=tuple(ws)))
    return out


def _hook(**kw) -> hf_template.HookSpec:
    base = dict(text="Watch <this> & \"that\"", end=2.5, top_px=154.0, font_px=63, pad_px=20, wrap_px=900,
                color="rgba(255,255,255,1)",
                box="rgba(232,26,42,1)", outline="rgba(0,0,0,1)", stroke_px=12, animation="pop")
    base.update(kw)
    return hf_template.HookSpec(**base)


def test_render_html_default_params_keep_the_step3_page() -> None:
    page = hf_template.render_html(_groups("a", "b"), _params())
    assert "-webkit-text-stroke: 10px #000;" in page and "text-shadow: 0 4px 14px rgba(0,0,0,.55);" in page
    assert "font-weight: 700;" in page and 'class="cap clip"' in page
    assert "hook" not in page and ".em" not in page and "emo" not in page
    assert "const em" not in page


def test_render_html_hook_markup_escaping_and_animations() -> None:
    for anim, needle in (("pop", "scale: 0.55"), ("slide", "y: -140"), ("fade", "{opacity: 0}, {opacity: 1")):
        page = hf_template.render_html(_groups("a"), _params(hook=_hook(animation=anim)))
        assert needle in page
        assert ('<div id="hook" class="hook clip" data-start="0.000" data-duration="2.500" data-track-index="2">'
                '<span id="hook-in" class="hook-in">Watch &lt;this&gt; &amp; &quot;that&quot;</span></div>') in page
        assert "tl.to(h, {opacity: 0, duration: 0.25, ease: \"none\"}, 2.250);" in page
        assert "top: 154px;" in page and "background: rgba(232,26,42,1);" in page
        assert "translateY(-50%)" not in page.split(".hook {")[1].split("}")[0]  # top edge, grows downwards
        assert "max-width: 900px;" in page and "padding: 20px 26px;" in page
        assert "text-wrap: balance;" in page  # no one-word last line
    plain = hf_template.render_html([], _params(hook=_hook(box=None)))
    assert "-webkit-text-stroke: 12px rgba(0,0,0,1);" in plain and "box-shadow" not in plain
    with pytest.raises(ValueError):
        hf_template.render_html([], _params(hook=_hook(animation="spin")))


def test_render_html_hook_text_cannot_break_out_of_the_page() -> None:
    page = hf_template.render_html([], _params(hook=_hook(text="</script><script>alert(1)</script>")))
    assert "<script>alert(1)" not in page and "&lt;/script&gt;" in page


def test_render_html_emphasis_emoji_box_and_styles() -> None:
    page = hf_template.render_html(_groups("a", "clutch", "c", emph={1}, emoji={1: "🔥"}),
                                   _params(emphasis="rgba(0,255,127,1)", emphasis_scale=1.25, box="rgba(0,0,0,0.8)",
                                           emoji_family="AC Emoji", emoji_file="NotoColorEmoji.ttf"))
    assert '<span id="w0_1" class="w em">clutch<span class="emo">🔥</span></span>' in page
    assert ".w.em { font-size: 75px; color: rgba(0,255,127,1); }" in page
    assert '@font-face { font-family: "AC Emoji"; src: url("assets/fonts/NotoColorEmoji.ttf"); }' in page
    assert '"AC Emoji"' in page.split("const F = ")[1].split(";")[0]  # loaded before the timeline is built
    assert 'class="cap box clip"' in page and ".cap.box {" in page and "-webkit-text-stroke: 0;" in page
    # the box must not shrink-wrap to the half frame right of left: 50% (real render: a 3-word line wrapped)
    assert "width: max-content; max-width: 90%;" in page
    # emphasised words rest in the emphasis colour but take the highlight colour while spoken
    assert 'const cp = el.classList.contains("em") ? C.e : C.p;' in page and "color: C.h, duration" in page
    for style, needle in (("color", "Math.max(s + 0.05, e)"), ("none", "void el;"), ("karaoke", "ease: \"none\"}, s);")):
        assert needle in hf_template.render_html(_groups("a"), _params(style=style))


def test_build_composition_hook_emphasis_uppercase_and_fonts(tmp_path: Path) -> None:
    gsap = tmp_path / "gsap.min.js"
    gsap.write_text("/* gsap */", encoding="utf-8")
    s = Settings()
    s.captions.uppercase = True
    s.captions.highlight_style = "karaoke"
    s.hook.animation = "slide"
    script = ClipScript(narration="n", title="t", description="", hook="Big moment",
                        keywords=[Keyword(word="clutch", emoji="🔥")])
    idx = hf.build_composition(W("that", "clutch", "play"), lang="en", duration=6.0, settings=s,
                               out_dir=tmp_path / "hf", gsap=gsap, fps=25, script=script)
    page = idx.read_text(encoding="utf-8")
    assert ">THAT</span>" in page and 'class="w em">CLUTCH<span class="emo">🔥</span>' in page
    assert ">Big moment</span>" in page and "y: -140" in page
    assert 'fromTo(el, {color: cp}, {color: C.h' in page  # karaoke from captions.highlight_style
    em_file = hf.find_font_file(hf.EMOJI_FAMILY, bold=False)
    if em_file is not None:
        assert (tmp_path / "hf" / "assets" / "fonts" / re.sub(r"[^A-Za-z0-9._-]", "_", em_file.name)).is_file()
    s.captions.emoji = False
    page2 = hf.build_composition(W("that", "clutch"), lang="en", duration=6.0, settings=s,
                                 out_dir=tmp_path / "hf2", gsap=gsap, fps=25, script=script).read_text("utf-8")
    assert "emo" not in page2 and 'class="w em"' in page2
    hi = hf.build_composition(W("यह", "कमाल"), lang="hi", duration=6.0, settings=Settings(),
                              out_dir=tmp_path / "hf3", gsap=gsap, fps=25,
                              script=ClipScript(narration="n", title="t", description="", hook="आखिरी सेकंड तक देखो",
                                                keywords=[Keyword(word="कमाल")])).read_text("utf-8")
    assert ">आखिरी सेकंड तक देखो</span>" in hi and 'class="w em">कमाल</span>' in hi
    assert '<html lang="hi">' in hi


def test_make_overlay_with_hook_and_no_words_still_renders(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "en"
    out.mkdir()
    (out / "words.json").write_text("[]", encoding="utf-8")
    seen = {}

    def fake_build(words, **kw):
        seen["words"], seen["script"] = words, kw.get("script")
        raise hf.HyperFramesError("stop here")

    monkeypatch.setattr(hf, "build_composition", fake_build)
    monkeypatch.setattr(hf, "has_vp9_alpha_decoder", lambda: True)
    cut = make_video(tmp_path / "cut.mp4", seconds=1.0, width=270, height=480)
    env = hf.HFEnv(node=Path("node"), cli=Path("cli"), gsap=Path("g"), node_version="v22", hf_version="0.9.52")
    with pytest.raises(hf.HyperFramesError, match="has no words"):
        hf.make_overlay(words_json=out / "words.json", cut=cut, lang="en", settings=Settings(), out_dir=out, env=env)
    (out / "script.json").write_text(ClipScript(narration="n", title="t", description="", hook="Hi").model_dump_json(),
                                     encoding="utf-8")
    with pytest.raises(hf.HyperFramesError, match="stop here"):
        hf.make_overlay(words_json=out / "words.json", cut=cut, lang="en", settings=Settings(), out_dir=out, env=env)
    assert seen["words"] == [] and seen["script"].hook == "Hi"


# ============================================================================ staleness
def _rendered(tmp_path: Path, script: ClipScript | None, settings: Settings, *, stamp_style: bool = True) -> Path:
    out = tmp_path / "en"
    out.mkdir(exist_ok=True)
    (out / "final.mp4").write_bytes(b"x" * 10)
    if script is not None:
        (out / "script.json").write_text(script.model_dump_json(), encoding="utf-8")
    render.write_render_stamp(out, settings, render.RenderOutcome(path=out / "final.mp4", engine_used="ass",
                                                                  warning=None))
    if not stamp_style:  # a render.json written before G4/G5
        data = json.loads((out / "render.json").read_text(encoding="utf-8"))
        data.pop("style_key")
        (out / "render.json").write_text(json.dumps(data), encoding="utf-8")
    return out


def test_legacy_stamp_is_current_only_for_the_step4_look(tmp_path: Path) -> None:
    s = Settings()
    plain = ClipScript(narration="n", title="t", description="")
    out = _rendered(tmp_path, plain, s, stamp_style=False)
    assert render.render_is_current(out, s) and not render.render_settings_changed(out, s)
    s.captions.font_size = 90  # an old field: never tracked before, so an old render stays current
    assert render.render_is_current(out, s)
    s.captions.box = True  # a G5 look field
    assert not render.render_is_current(out, s) and render.render_settings_changed(out, s)
    s.captions.box = False
    (out / "script.json").write_text(plain.model_copy(update={"hook": "Hook!"}).model_dump_json(), encoding="utf-8")
    assert not render.render_is_current(out, s)  # a hook to draw
    s.hook.enabled = False
    assert render.render_is_current(out, s)
    (out / "script.json").write_text(plain.model_copy(update={"keywords": [Keyword(word="x")]}).model_dump_json(),
                                     encoding="utf-8")
    assert not render.render_is_current(out, s)
    s.captions.emphasis = False
    assert render.render_is_current(out, s)


def test_style_key_tracks_hook_text_settings_keywords_and_style(tmp_path: Path) -> None:
    s = Settings()
    script = ClipScript(narration="n", title="t", description="", hook="Hook one", keywords=[Keyword(word="k")])
    out = _rendered(tmp_path, script, s)
    assert render.render_is_current(out, s)
    stamp = json.loads((out / "render.json").read_text(encoding="utf-8"))
    assert stamp["style_key"]["hook"]["text"] == "Hook one" and stamp["style_key"]["keywords"] == [{"word": "k", "emoji": ""}]
    for change in (lambda x: setattr(x.hook, "duration", 3.0), lambda x: setattr(x.hook, "style", "plain"),
                   lambda x: setattr(x.captions, "font_size", 99), lambda x: setattr(x.captions, "highlight_style", "pop"),
                   lambda x: setattr(x.captions, "emphasis_color", "&H00000000")):
        t = Settings()
        change(t)
        assert not render.render_is_current(out, t)
    t = Settings()
    t.captions.preset = "boxed"  # the name alone changes nothing on screen
    assert render.render_is_current(out, t)
    (out / "script.json").write_text(script.model_copy(update={"hook": "Hook two"}).model_dump_json(), encoding="utf-8")
    assert not render.render_is_current(out, s)


def test_hook_settings_do_not_stale_a_clip_without_hook(tmp_path: Path) -> None:
    s = Settings()
    out = _rendered(tmp_path, ClipScript(narration="n", title="t", description=""), s)
    s.hook.font_size = 150
    s.hook.style = "plain"
    assert render.render_is_current(out, s)


def test_render_key_style_follows_highlight_style() -> None:
    s = Settings()
    s.captions.engine = "hyperframes"
    assert render.render_key(s)["style"] == "pop"
    s.captions.highlight_style = "color"
    assert render.render_key(s)["style"] == "color"


# ============================================================================ presets
def test_presets_are_valid_and_apply_only_style_fields() -> None:
    assert set(caption_presets.PRESET_IDS) == {"clean", "bold_pop", "karaoke", "boxed", "gaming_neon"}
    base = Settings()
    base.captions.font_en, base.captions.position_pct, base.captions.engine = "My Font", 60.0, "hyperframes"
    for pid in caption_presets.PRESET_IDS:
        s = caption_presets.apply_preset(base, pid)
        assert s.captions.preset == pid and caption_presets.matches_preset(s.captions, pid)
        assert (s.captions.font_en, s.captions.position_pct, s.captions.engine) == ("My Font", 60.0, "hyperframes")
        assert base.captions.preset == "custom"  # a copy
    s = caption_presets.apply_preset(base, "bold_pop")
    s.captions.font_size = 50
    assert not caption_presets.matches_preset(s.captions, "bold_pop")
    with pytest.raises(caption_presets.PresetError):
        caption_presets.apply_preset(base, "nope")
    payload = caption_presets.presets_payload()
    assert [p["id"] for p in payload] == list(caption_presets.PRESET_IDS)
    for p in payload:
        assert_keys(p, {"id": str, "name": str, "description": str, "values": dict})


def test_every_preset_renders_valid_ass_and_html(tmp_path: Path) -> None:
    for pid in caption_presets.PRESET_IDS:
        s = caption_presets.apply_preset(Settings(), pid)
        ass = build_ass(W("one", "two", "three"), lang="en", settings=s.captions, keywords=[Keyword(word="two")],
                        hook="Hook", hook_settings=s.hook, duration=4)
        assert dialogues(ass) and dialogues(ass, "Hook")


def test_cli_settings_set_preset_fills_fields_then_override(capsys) -> None:
    save_settings(Settings())
    assert cli.main(["settings", "set", "captions.preset=gaming_neon", "captions.font_size=70"]) == 0
    s = load_settings()
    assert s.captions.preset == "gaming_neon" and s.captions.uppercase and s.captions.highlight_style == "pop"
    assert s.captions.font_size == 70  # the hand override after the preset wins
    assert cli.main(["settings", "presets"]) == 0
    out = capsys.readouterr().out
    assert "* gaming_neon" in out and "(changed by hand)" in out
    assert cli.main(["settings", "set", "captions.preset=nope"]) == 1


def test_cli_rejects_bad_style_and_hook_values(capsys) -> None:
    save_settings(Settings())
    for bad in ("hook.top_pct=60", "hook.top_pct=1", "captions.outline_color=red", "hook.duration=0",
                "captions.highlight_style=wobble", "captions.emphasis_scale=3", "hook.style=neon",
                "captions.outline_px=-1", "hook.text_color=&H00FFFFFF\n"):
        assert cli.main(["settings", "set", bad]) == 1, bad
    assert "hook.top_pct" in capsys.readouterr().err
    assert load_settings() == Settings()  # nothing saved


def test_old_settings_file_with_low_captions_still_loads() -> None:
    from autoclipper.config import settings_path

    data = Settings().model_dump()
    data["captions"]["position_pct"] = 12.0  # the default hook (top 8 %) cannot start above these captions
    del data["hook"]
    settings_path().write_text(json.dumps(data), encoding="utf-8")
    s = load_settings()  # allowed on load, refused on save
    assert s.hook == HookSettings() and "hook.top_pct" in s.cross_field_errors()


# ============================================================================ LLM schema + prompt
class _Resp:
    def __init__(self, data):
        self.stop_reason = "end_turn"
        self.content = [type("B", (), {"type": "text", "text": json.dumps(data, ensure_ascii=False)})()]


class _Client:
    def __init__(self, data):
        self.data, self.calls = data, []
        self.messages = self
        self.beta = type("Beta", (), {"messages": self})()

    def create(self, **kw):
        self.calls.append(kw)
        return _Resp(self.data)


def test_script_and_metadata_schemas_require_hook_and_keywords() -> None:
    for schema in (SCRIPT_SCHEMA, METADATA_SCHEMA):
        assert {"hook", "keywords"} <= set(schema["required"])
        item = schema["properties"]["keywords"]["items"]
        assert item["required"] == ["word", "emoji"] and item["additionalProperties"] is False
    hi = hook_keywords_instruction("hi", "your narration")
    assert "Devanagari" in hi and "at most 8 words" in hi and "Hindi" in hi
    assert "Devanagari" not in hook_keywords_instruction("en", "x")


def test_write_script_returns_hook_and_keywords_from_claude() -> None:
    from autoclipper.config import LLMSettings

    data = {"narration": "a b c", "title": "T", "description": "D", "hashtags": ["#x"], "hook": "यह देखो",
            "keywords": [{"word": "कमाल", "emoji": "🔥"}]}
    client = _Client(data)
    llm = AnthropicLLM(LLMSettings(use_fallbacks=False), client=client)
    s = llm.write_script(lang="hi", clip_transcript="[0-1] x", moment=Moment(start=0, end=10, hook="h"),
                         duration=10, target_words=20, target_seconds=8.8)
    assert s.hook == "यह देखो" and s.keywords == [Keyword(word="कमाल", emoji="🔥")]
    user = client.calls[0]["messages"][0]["content"]
    assert "hook" in user and "keywords" in user and "Devanagari" in user
    meta = AnthropicLLM(LLMSettings(use_fallbacks=False), client=_Client({**data, "hook": "Look"}))
    m = meta.write_publish_metadata(lang="en", clip_transcript="x", moment=Moment(start=0, end=5), duration=5)
    assert m.narration == "" and m.hook == "Look" and m.keywords[0].word == "कमाल"


# ============================================================================ pipeline (fake clients, real ffmpeg)
class HookLLM(FakeLLM):
    def write_script(self, **kw):
        s = super().write_script(**kw)
        return s.model_copy(update={"hook": "Watch this hook" if kw["lang"] == "en" else "यह देखो",
                                    "keywords": [Keyword(word=s.narration.split()[1], emoji="🔥")]})

    def write_publish_metadata(self, **kw):
        s = super().write_publish_metadata(**kw)
        return s.model_copy(update={"hook": "Original hook"})


def _pipeline(tmp_path: Path, voiceover: bool):
    from autoclipper.pipeline import Deps, rerender, run_job

    src = make_video(tmp_path / "src.mp4", seconds=30.0, width=320, height=180)
    s = Settings()
    s.clips.count, s.clips.min_seconds, s.clips.max_seconds = 1, 6, 10
    s.clips.width, s.clips.height = 270, 480
    s.audio.mix, s.audio.short_voice = "gain", "keep"
    s.voiceover = voiceover
    s.voicebox.english.profile_id = "fake-en"
    deps = Deps(llm=HookLLM(moments=[Moment(start=2, end=9, hook="h", reason="r", score=0.9)]),
                voicebox=FakeVoicebox(seconds_per_word=0.25), transcriber=FakeTranscriber(duration=30.0))
    store = run_job(str(src), settings=s, deps=deps, langs=["en"], log=lambda *_: None)
    return store, s, deps, rerender


@pytest.mark.parametrize("voiceover", [True, False])
def test_pipeline_writes_hook_into_captions_and_rerenders_on_edit(tmp_path: Path, voiceover: bool) -> None:
    store, s, deps, rerender = _pipeline(tmp_path, voiceover)
    out = store.clip_dir(0, "en")
    script = json.loads((out / "script.json").read_text(encoding="utf-8"))
    assert script["hook"] == ("Watch this hook" if voiceover else "Original hook")
    ass = (out / "captions.ass").read_text(encoding="utf-8")
    assert dialogues(ass, "Hook")[0].endswith("}" + script["hook"])
    assert (out / "final.mp4").stat().st_size > 0 and render.render_is_current(out, s)
    script["hook"] = "Edited {hook}"
    (out / "script.json").write_text(json.dumps(script), encoding="utf-8")
    assert not render.render_is_current(out, s)
    rerender(store.job.id, 0, "en", settings=s, deps=deps, log=lambda *_: None)
    ass = (out / "captions.ass").read_text(encoding="utf-8")
    assert dialogues(ass, "Hook")[0].endswith("}Edited \\{hook\\}") and render.render_is_current(out, s)


# ============================================================================ API
GOOD = {"narration": "this is a test narration", "title": "T", "description": "d", "hashtags": ["#a"]}


def url(store, clip=0, lang="en"):
    return f"/api/jobs/{store.job.id}/clips/{clip}/{lang}/script"


def test_get_caption_presets_contract(api):
    client, _, _ = api
    r = client.get("/api/caption-presets")
    assert r.status_code == 200
    assert_keys(r.json(), {"presets": list})
    first = r.json()["presets"][0]
    assert_keys(first, {"id": str, "name": str, "description": str, "values": dict})
    assert set(first["values"]) <= set(CaptionSettings.model_fields)


def test_caption_presets_auth(api):
    client, _, _ = api
    tok = client.headers.pop(TOKEN_HEADER)
    try:
        assert_error_shape(client.get("/api/caption-presets"), 401)
        assert_error_shape(client.get("/api/caption-presets", headers={TOKEN_HEADER: "wrong" * 8}), 403)
    finally:
        client.headers[TOKEN_HEADER] = tok


def test_put_script_hook_keywords_saved_and_contract(api):
    client, _, _ = api
    store = make_job_on_disk()
    body = {**GOOD, "hook": '  "Wait for\nit"  ', "keywords": [{"word": " test ", "emoji": "🔥"}, {"word": "narration"}]}
    r = client.put(url(store), json=body)
    assert r.status_code == 200, r.text
    assert_keys(r.json(), {"script": dict, "voice_stale": bool, "style_stale": bool})
    sc = r.json()["script"]
    assert_keys(sc, {"narration": str, "title": str, "description": str, "hashtags": list, "hook": str, "keywords": list})
    assert sc["hook"] == "Wait for it"
    assert sc["keywords"] == [{"word": "test", "emoji": "🔥"}, {"word": "narration", "emoji": ""}]
    saved = read_json(store.clip_dir(0, "en") / "script.json")
    assert saved == sc  # side effect: script.json on disk
    assert r.json()["style_stale"] is True  # the final.mp4 has no hook yet
    # an older client that sends neither field keeps both
    r = client.put(url(store), json={**GOOD, "title": "New"})
    assert r.json()["script"]["hook"] == "Wait for it" and len(r.json()["script"]["keywords"]) == 2
    # empty values clear them
    r = client.put(url(store), json={**GOOD, "hook": "", "keywords": []})
    assert r.json()["script"]["hook"] == "" and r.json()["script"]["keywords"] == []
    assert read_json(store.clip_dir(0, "en") / "script.json")["hook"] == ""
    # GET returns the new fields
    g = client.get(url(store))
    assert g.status_code == 200 and g.json()["hook"] == "" and g.json()["keywords"] == []


def test_put_script_hindi_hook_devanagari(api):
    client, _, _ = api
    store = make_job_on_disk()
    r = client.put(url(store, 0, "hi"), json={**GOOD, "narration": "यह एक परीक्षण है", "hook": "आखिरी सेकंड तक देखो",
                                               "keywords": [{"word": "परीक्षण", "emoji": "😱"}]})
    assert r.status_code == 200
    raw = (store.clip_dir(0, "hi") / "script.json").read_text(encoding="utf-8")
    assert "आखिरी सेकंड तक देखो" in raw and "परीक्षण" in raw


@pytest.mark.parametrize("patch,field", [
    ({"hook": "x" * 101}, "hook"),
    ({"hook": " ".join(["w"] * 13)}, "hook"),
    ({"hook": 5}, "hook"),
    ({"hook": ["a"]}, "hook"),
    ({"keywords": "word"}, "keywords"),
    ({"keywords": [{"word": ""}]}, "keywords"),
    ({"keywords": [{"word": "!!!"}]}, "keywords"),
    ({"keywords": [{"word": "a b c d"}]}, "keywords"),
    ({"keywords": [{"word": "x" * 41}]}, "keywords"),
    ({"keywords": [{"word": "ok", "emoji": "fire"}]}, "keywords"),
    ({"keywords": [{"word": "ok", "emoji": 1}]}, "keywords"),
    ({"keywords": [{"word": "ok", "extra": 1}]}, "keywords"),
    ({"keywords": [{"emoji": "🔥"}]}, "keywords"),
    ({"keywords": [{"word": f"w{i}"} for i in range(9)]}, "keywords"),
])
def test_put_script_hook_keyword_validation_422(api, patch, field):
    client, _, _ = api
    store = make_job_on_disk()
    before = (store.clip_dir(0, "en") / "script.json").read_bytes()
    r = client.put(url(store), json={**GOOD, **patch})
    err = assert_error_shape(r, 422)
    assert any(k == field or k.startswith(field + ".") for k in err["errors"]), err
    assert (store.clip_dir(0, "en") / "script.json").read_bytes() == before  # nothing written


def test_put_script_hook_404_401_403(api):
    client, _, _ = api
    store = make_job_on_disk()
    body = {**GOOD, "hook": "Hi"}
    assert_error_shape(client.put("/api/jobs/20990101-000000-ffffff/clips/0/en/script", json=body), 404)
    assert_error_shape(client.put(url(store, 7), json=body), 404)
    tok = client.headers.pop(TOKEN_HEADER)
    try:
        assert_error_shape(client.put(url(store), json=body), 401)
        assert_error_shape(client.put(url(store), json=body, headers={TOKEN_HEADER: "bad" * 10}), 403)
    finally:
        client.headers[TOKEN_HEADER] = tok
    assert read_json(store.clip_dir(0, "en") / "script.json").get("hook", "") == ""


def test_settings_put_style_and_hook_validation(api):
    client, _, _ = api
    s = client.get("/api/settings").json()
    for path, value in (("captions.outline_color", "red"), ("captions.box_color", "&H0000000"),
                        ("captions.highlight_style", "wobble"), ("captions.emphasis_scale", 0.5),
                        ("captions.outline_px", 21), ("captions.preset", "neon"), ("hook.duration", 9),
                        ("hook.top_pct", 50), ("hook.font_size", "big"), ("hook.animation", "spin"),
                        ("hook.text_color", "&H00FFFFFF\n"), ("hook.top_pct", 1.0), ("captions.emoji", "yes")):
        body = json.loads(json.dumps(s))
        sec, key = path.split(".")
        body[sec][key] = value
        r = client.put("/api/settings", json=body)
        err = assert_error_shape(r, 422)
        assert path in err["errors"], (path, err)
    body = json.loads(json.dumps(s))
    body["hook"]["top_pct"] = 40
    body["captions"]["position_pct"] = 40  # the hook would start on the caption line
    assert "hook.top_pct" in assert_error_shape(client.put("/api/settings", json=body), 422)["errors"]
    body["hook"]["enabled"] = False  # a hook that is off is never checked (other saves must not be blocked)
    assert client.put("/api/settings", json=body).status_code == 200
    body["hook"]["foo"] = 1
    assert "hook.foo" in assert_error_shape(client.put("/api/settings", json=body), 422)["errors"]


def test_settings_put_preset_values_saved(api):
    client, _, _ = api
    s = client.get("/api/settings").json()
    s["captions"].update(caption_presets.preset_values("boxed"), preset="boxed")
    s["hook"].update(style="plain", duration=3.0, animation="fade")
    r = client.put("/api/settings", json=s)
    assert r.status_code == 200, r.text
    saved = load_settings()
    assert saved.captions.box and saved.captions.preset == "boxed" and saved.hook.style == "plain"
    assert r.json()["hook"]["animation"] == "fade"


def test_job_detail_style_stale_flag(api):
    client, _, _ = api
    store = make_job_on_disk()
    out = store.clip_dir(0, "en")
    render.write_render_stamp(out, Settings(), render.RenderOutcome(path=out / "final.mp4", engine_used="ass",
                                                                    warning=None))
    cl = client.get(f"/api/jobs/{store.job.id}").json()["clips"][0]["langs"]["en"]
    assert cl["style_stale"] is False and cl["script"]["hook"] == ""
    client.put(url(store), json={**GOOD, "hook": "New hook"})
    cl = client.get(f"/api/jobs/{store.job.id}").json()["clips"][0]["langs"]["en"]
    assert cl["style_stale"] is True and cl["render_stale"] is True
    no_final = client.get(f"/api/jobs/{store.job.id}").json()["clips"][1]["langs"]["en"]
    assert isinstance(no_final["style_stale"], bool)


# ============================================================================ lead fixes (review of G4/G5)
def test_keyword_inflection_only_known_endings() -> None:
    words = W("paint", "start", "star", "steals", "stopped", "pains", "गाड़ियों", "गाड़ीवाला")
    m = cst.emphasis_marks(words, [Keyword(word="pain"), Keyword(word="star"), Keyword(word="steal"),
                                   Keyword(word="stop"), Keyword(word="गाड़ी")], per_line=8, max_per_line=3)
    assert 0 not in m and 1 not in m  # "pain" is not "paint", "star" is not "start"
    assert {2, 3, 4} <= set(m)  # star, steal+s, stop+ped
    m2 = cst.emphasis_marks(words, [Keyword(word="pain"), Keyword(word="गाड़ी")], per_line=8, max_per_line=3)
    assert 5 in m2 and 7 not in m2  # pain+s yes; a longer Hindi compound no


def test_check_llm_extras_against_narration_and_language() -> None:
    s = ClipScript(narration="He steals the car and runs", title="t", description="", hook="GTA mein car chura li",
                   keywords=[Keyword(word="banana", emoji="🔥"), Keyword(word="steal", emoji="🚗")])
    en = cst.check_llm_extras(s, "en", voiceover=True)
    assert en.keywords == [Keyword(word="steal", emoji="🚗")] and en.hook == s.hook
    hi = cst.check_llm_extras(s, "hi", voiceover=True)
    assert hi.hook == ""  # a Hindi hook must be in Devanagari
    meta = ClipScript(narration="", title="t", description="", hook="GTA में चोरी",
                      keywords=[Keyword(word="GTA"), Keyword(word="गाड़ी", emoji="🚗")])
    out = cst.check_llm_extras(meta, "hi", voiceover=False)
    assert out.hook == "GTA में चोरी" and out.keywords == [Keyword(word="गाड़ी", emoji="🚗")]
    assert cst.check_llm_extras(meta, "en", voiceover=False) is meta


def test_write_script_checks_keywords_after_shortening() -> None:
    from autoclipper.stages import script as script_stage

    class LLM:
        def write_script(self, **kw):
            return ClipScript(narration=" ".join(["clutch"] + ["word"] * 400), title="t", description="",
                              hook="Watch this", keywords=[Keyword(word="clutch"), Keyword(word="word")])

        def shorten(self, *, lang, narration, max_words):
            return "word " * 5

    from autoclipper.models import Transcript

    out = script_stage._generate_script(llm=LLM(), transcript=Transcript(language="en", duration=20, segments=[]),
                                        moment=Moment(start=0, end=20, hook="h", reason="r"), lang="en")
    assert out.keywords == [Keyword(word="word")]  # "clutch" was shortened away


def test_cross_field_check_only_for_an_enabled_hook_and_by_caption_top() -> None:
    s = Settings()
    s.captions.position_pct = 20  # pass-2 regression: this blocked every save
    assert s.cross_field_errors() == {}
    s.captions.position_pct = 12
    assert "hook.top_pct" in s.cross_field_errors()
    s.hook.enabled = False
    assert s.cross_field_errors() == {}
    b = Settings()
    b.captions.position_pct = None
    b.captions.margin_v = 1500  # bottom-anchored captions high up: now checked too
    b.hook.top_pct = 30
    assert "hook.top_pct" in b.cross_field_errors()
    b.captions.margin_v = 520
    assert b.cross_field_errors() == {}


def test_hook_layout_top_anchored_and_shrinks_above_the_captions() -> None:
    s = Settings()
    lay = cst.hook_layout("Steal a car in GTA, Dad gets it", s, width=1080, height=1920)
    assert lay.top_px == 154 and lay.font_size == 112 and lay.lines == 2 and lay.wrap_px == 900
    big = Settings()
    big.hook.font_size, big.hook.top_pct, big.captions.position_pct = 200, 30, 55
    long = "this is a very long hook line that wraps over many lines"
    lay = cst.hook_layout(long, big, width=1080, height=1920)
    limit = cst.caption_top_px(big.captions, 1920) - cst.HOOK_CAPTION_GAP * 1920
    assert lay.font_size < 200 and lay.top_px + lay.height_px <= limit
    assert lay.top_px >= 0  # grows downwards: never off the top
    # the ASS event uses the fitted size, the top anchor and the same wrap width
    ass = build_ass(W("a"), lang="en", settings=big.captions, hook=long, hook_settings=big.hook)
    style = next(ln for ln in ass.splitlines() if ln.startswith("Style: Hook"))
    assert style.split(",")[2] == str(lay.font_size) and "\\an8\\pos(540," in dialogues(ass, "Hook")[0]
    # Devanagari: marks take no extra width, the estimate stays sane
    assert cst.estimate_lines("गेम में चोरी, असली में पापा को गाड़ी", 112, 900) == 2


def test_hook_default_is_bigger_than_every_preset_caption() -> None:
    assert all(HookSettings().font_size > p["values"]["font_size"] for p in caption_presets.PRESETS.values())


def test_style_key_ignores_fields_that_do_not_change_this_render() -> None:
    s = Settings()
    sc = ClipScript(narration="n", title="t", description="")
    base = cst.style_key(s, sc, "en")
    s2 = s.model_copy(deep=True)
    s2.captions.font_hi = "Other Devanagari"
    s2.captions.emoji = False
    s2.captions.emphasis_color = "&H00010203"
    s2.captions.emphasis_scale = 1.7
    assert cst.style_key(s2, sc, "en") == base  # other language's font, emoji/emphasis with no keywords
    assert cst.style_key(s2, sc, "hi") != cst.style_key(s, sc, "hi")  # font_hi matters for hi
    kw = sc.model_copy(update={"keywords": [Keyword(word="n", emoji="🔥")]})
    s3 = s.model_copy(deep=True)
    s3.captions.emoji = False
    assert cst.style_key(s3, kw, "en") == cst.style_key(s, kw, "en")  # ASS never draws emoji
    s.captions.engine = s3.captions.engine = "hyperframes"
    assert cst.style_key(s3, kw, "en") != cst.style_key(s, kw, "en")


def test_ass_box_never_scales_words() -> None:
    cs = CaptionSettings(box=True, emphasis_scale=1.3, highlight_style="pop")
    ass = build_ass(W("this", "clutch", "wow"), lang="en", settings=cs, keywords=[Keyword(word="clutch")])
    assert "fscx" not in ass  # libass would draw a taller box behind a scaled word
    d = dialogues(ass)
    assert "{\\c&H00A5FF&}clutch{\\c&HFFFFFF&}" in d[0] and "{\\c&H00E5FF&}clutch" in d[1]


def test_karaoke_keyword_takes_the_fill_once_spoken() -> None:
    cs = CaptionSettings(highlight_style="karaoke")
    d = dialogues(build_ass(W("a", "clutch", "c"), lang="en", settings=cs, keywords=[Keyword(word="clutch")]))
    assert "{\\c&H00A5FF&\\fscx120" in d[0] and "{\\c&H00E5FF&\\fscx120" in d[2]


def test_hook_weight_follows_caption_bold_in_both_engines(tmp_path: Path) -> None:
    cs = CaptionSettings(bold=False)
    style = next(ln for ln in build_ass([], lang="en", settings=cs, hook="Hi there").splitlines()
                 if ln.startswith("Style: Hook"))
    assert style.split(",")[7] == "0"
    page = hf_template.render_html([], _params(bold=False, hook=_hook()))
    assert "font-weight: 400;" in page.split(".hook-in {")[1].split("}")[0]


def test_emoji_font_is_linked_not_copied_when_possible(tmp_path: Path) -> None:
    src = tmp_path / "NotoColorEmoji.ttf"
    src.write_bytes(b"x" * 1000)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert hf._copy_font(src, tmp_path / "a") == "NotoColorEmoji.ttf"
    hf._copy_font(src, tmp_path / "b")
    assert (tmp_path / "a" / "NotoColorEmoji.ttf").stat().st_ino == src.stat().st_ino
    assert (tmp_path / "b" / "NotoColorEmoji.ttf").read_bytes() == src.read_bytes()
