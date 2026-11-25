"""Settings: defaults, load/save to data/settings.json. Every field is editable later from the UI."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

from .publish.settings import PublishSettings

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = PROJECT_ROOT / "assets" / "fonts"


def data_dir() -> Path:
    d = Path(os.environ.get("AUTOCLIPPER_DATA_DIR", PROJECT_ROOT / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


class LLMSettings(BaseModel):
    provider: Literal["anthropic"] = "anthropic"
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Server-side refusal fallback (routes a declined request to another model).
    use_fallbacks: bool = True
    api_key_env: str = "ANTHROPIC_API_KEY"
    # Output token budget per LLM call. Long (2 h+) sources at high effort may need 32000+.
    max_tokens: int = Field(16000, ge=1024, le=64000)


class WhisperSettings(BaseModel):
    model: str = "small"  # tiny | base | small | medium | large-v3
    device: str = "auto"  # auto | cpu | cuda
    compute_type: str = "auto"  # auto -> int8 on CPU, float16 on CUDA
    language: str | None = None  # None = auto-detect source language


class VoiceProfile(BaseModel):
    profile_id: str = ""
    engine: str = "chatterbox"


class VoiceboxSettings(BaseModel):
    url: str = "http://127.0.0.1:17493"
    poll_interval: float = Field(1.0, ge=0.1, le=10)
    timeout_seconds: float = Field(600.0, ge=10, le=7200)
    # The optional "lengthen" retry (a too-short take regenerated with a longer narration) is a nice-to-have:
    # it gets its own, shorter limit so a slow Voicebox does not hold a clip for timeout_seconds again.
    # The original take is kept when it runs out. Capped at timeout_seconds.
    lengthen_timeout_seconds: float = Field(240.0, ge=10, le=7200)
    # M2: generate the narration sentence by sentence (one Voicebox call per chunk, joined with short gaps).
    # Chatterbox stops early on long input (a 115-word Hindi text came back as 10 s); short chunks do not.
    chunking: bool = True
    # Longer sentences are split at commas, then words. Chatterbox skipped a clause in a 155-character chunk.
    chunk_max_chars: int = Field(150, ge=40, le=2000)
    chunk_min_chars: int = Field(60, ge=0, le=500)  # shorter pieces are merged with a neighbour (Chatterbox
    # babbles on very short input: a 6-word Hindi sentence came back as 12.7 s)
    chunk_gap_seconds: float = Field(0.2, ge=0.0, le=1.5)  # silence between chunks
    # Voicebox wait per chunk, and for the one retry of a chunk that came back cut short (both capped at
    # timeout_seconds). A chunk that still fails stops the voice stage with an error (resume retries it).
    chunk_timeout_seconds: float = Field(240.0, ge=10, le=3600)
    chunk_retry_timeout_seconds: float = Field(120.0, ge=10, le=3600)
    # Same seed for every chunk so the voice stays consistent; None = derived from the profile + language.
    seed: int | None = Field(None, ge=0, le=2**31 - 1)
    english: VoiceProfile = Field(default_factory=VoiceProfile)
    # Hindi must use Chatterbox Multilingual.
    hindi: VoiceProfile = Field(default_factory=lambda: VoiceProfile(engine="chatterbox"))


class ClipSettings(BaseModel):
    count: int = Field(5, ge=1, le=30)
    min_seconds: float = Field(20, ge=5, le=180)
    max_seconds: float = Field(60, ge=10, le=180)
    width: int = Field(1080, ge=240, le=3840)
    height: int = Field(1920, ge=240, le=3840)
    # "crop" = center crop to 9:16; "blur" = fit whole frame over a blurred fill
    reframe: Literal["crop", "blur"] = "crop"

    @field_validator("width", "height")
    @classmethod
    def _even(cls, v: int) -> int:
        if v % 2:
            raise ValueError("must be an even number (yuv420p video)")
        return v


class SignalSettings(BaseModel):
    """Local audio/video signals for moment picking (README "Moment picking for low-speech videos"). Computed
    once per job (signals.json) with ffmpeg + numpy; used for videos with little speech."""
    enabled: bool = True
    video: bool = True  # scene cuts, motion and static (menu/map) detection; off = audio loudness only
    sample_fps: float = Field(4.0, ge=0.5, le=10)  # frames per second sampled for the video analysis
    sample_width: int = Field(96, ge=32, le=320)  # analysis frame width in px (height = 9/16 of it)
    window_seconds: float = Field(10.0, ge=5, le=30)  # one line per window in the table given to the LLM
    # A video with at least this share of speech is "speech-rich": the transcript alone picks the moments
    # (as before); below it the LLM also gets the signal table and moments need not be speech-bounded.
    speech_rich_coverage: float = Field(0.4, ge=0.0, le=1.0)
    # A second is "static" when less than this share of the pixels changes (for >= 2 s in a row)
    static_motion: float = Field(0.02, ge=0.0, le=0.5)
    # A moment that is static for at least this share and mostly without speech is rejected (menu, map,
    # loading screen); less static moments of low-speech videos are ranked down by their static share.
    reject_static_fraction: float = Field(0.5, ge=0.1, le=1.0)
    cut_threshold: float = Field(0.35, ge=0.05, le=1.0)  # picture change score (0..1) for a scene cut
    # A second is "menu-like" (map, menu, pause blur, fade) when at least this share of the analysis frame is
    # flat (no texture) for >= 2 s in a row; treated like static.
    menu_flatness: float = Field(0.6, ge=0.2, le=1.0)
    timeout_seconds: float = Field(900.0, ge=30, le=7200)


class VisionSettings(BaseModel):
    """Vision check of moment candidates (README "Vision check"): a few small frames per candidate are sent to
    Claude, which describes what is visible, labels each frame (action, driving, menu/map, ...), scores the
    candidate and may tighten it. Results are cached per job in vision.json."""
    # "auto" = only for low-speech videos (speech below signals.speech_rich_coverage); True = every video
    # (extra cost); False = never.
    enabled: Literal["auto", True, False] = "auto"
    frames_per_moment: int = Field(6, ge=2, le=12)  # evenly spaced, the first and last near the moment's ends
    frame_width: int = Field(512, ge=256, le=1024)  # px; the height follows the video's aspect ratio
    # Most candidates checked per job (the moment picker is asked for extra candidates, up to this many).
    max_candidates: int = Field(12, ge=1, le=30)
    timeout_seconds: float = Field(300.0, ge=10, le=1800)  # frame extraction + each Claude request

    @field_validator("enabled", mode="before")
    @classmethod
    def _enabled_value(cls, v: object) -> object:
        # Literal[True] would also take 1 / 0; only "auto", true or false are meant
        if not (isinstance(v, bool) or v == "auto"):
            raise ValueError('must be "auto", true or false')
        return v


class ContextSettings(BaseModel):
    """Source context for the LLM (README "Video context for titles and descriptions")."""
    enabled: bool = True  # give the moment picker and the script/metadata writer the video's page info + notes
    credit_source: bool = True  # make sure each description credits the original creator and links the source
    description_chars: int = Field(1000, ge=0, le=1000)  # how much of the page description is kept


class AudioSettings(BaseModel):
    # Voice-over mix (see README "Audio mix"):
    # "loudness" = measure the voice and the background (EBU R128) and set their levels from that: the voice
    #   is normalised to voice_lufs and the background sits background_db_below_voice under it while the
    #   narration plays, however loud or quiet the source is.
    # "gain" = the old fixed gains (original_volume under the voice, original_volume_after outside it).
    mix: Literal["loudness", "gain"] = "loudness"
    voice_lufs: float = Field(-16.0, ge=-30, le=-8)  # loudness mix: voice level before the final loudnorm
    # loudness mix: background level under the narration, in dB below the voice
    background_db_below_voice: float = Field(18.0, ge=0, le=40)
    # gain mix only: original audio level WHILE the voice plays (ducked)
    original_volume: float = Field(0.15, ge=0, le=1)
    # Original audio level when no voice plays (before / after the narration, and the whole clip with the
    # voice-over off). gain mix: a plain factor. loudness mix: relative to the voice level (1.0 = as loud as
    # the voice, 0.5 = 6 dB under it).
    original_volume_after: float = Field(1.0, ge=0, le=2)
    duck_ramp_seconds: float = Field(0.4, ge=0, le=2)  # ramp time between the two levels
    voice_volume: float = Field(1.0, ge=0, le=2)  # gain mix: factor; loudness mix: trim on voice_lufs
    # A narration that ends before short_voice_below x the clip length: "trim_clip" ends the video at the
    # narration end + short_voice_tail seconds (never under 3 s); "keep" keeps the whole clip (the original
    # audio comes back after the narration).
    short_voice: Literal["trim_clip", "keep"] = "trim_clip"
    short_voice_below: float = Field(0.6, ge=0.1, le=0.95)
    short_voice_tail: float = Field(1.0, ge=0, le=5)
    max_speedup: float = Field(1.25, ge=1.0, le=2.0)  # cap on atempo to fit voice into clip
    loudnorm: bool = True  # single-pass loudnorm on the final mix
    target_lufs: float = Field(-14.0, ge=-30, le=-8)  # loudnorm integrated loudness target


class ScriptSettings(BaseModel):
    # Words per second of the TTS voice, measured on chatterbox in the sample job (EN ~3.0, HI ~3.36).
    # M2: re-measured on complete sentence-by-sentence Hindi takes (speech only, gaps excluded; every chunk
    # checked by ASR): 3.40 and 3.28 on the gaming clip, 3.07 on the Reagan clip -> HI 3.25.
    wps_en: float = Field(3.0, ge=1.0, le=6.0)
    wps_hi: float = Field(3.25, ge=1.0, le=6.0)
    # Narration target = clip length x fill_ratio (the LLM overshoots the word count by ~5 %).
    fill_ratio: float = Field(0.88, ge=0.5, le=1.0)
    # A voice shorter than this fraction of the clip gets one "lengthen" retry (new jobs only).
    lengthen_below: float = Field(0.75, ge=0.3, le=0.95)

    def wps(self, lang: str) -> float:
        return self.wps_hi if lang == "hi" else self.wps_en


class CaptionSettings(BaseModel):
    # G5 style preset last applied ("custom" = none; see caption_presets.py). Informational: the render uses
    # the style fields themselves, so every field can still be changed by hand after a preset.
    preset: Literal["custom", "clean", "bold_pop", "karaoke", "boxed", "gaming_neon"] = "custom"
    font_en: str = "Noto Sans"
    font_hi: str = "Noto Sans Devanagari"
    font_size: int = Field(78, ge=20, le=200)
    words_per_line: int = Field(3, ge=1, le=8)
    primary_color: str = "&H00FFFFFF"  # ASS BGR: white
    highlight_color: str = "&H0000E5FF"  # ASS BGR: yellow
    # Vertical CENTER of the caption line in % of the height from the top (72 = below a centred face).
    # None -> legacy bottom-anchored placement with margin_v.
    position_pct: float | None = Field(72.0, ge=5, le=95)
    margin_v: int = 520  # distance from bottom in px (1920 tall); used only when position_pct is None
    # "ass" = libass burn-in (always available); "hyperframes" = animated HTML captions rendered by the
    # optional HyperFrames CLI (Node 22+), falling back to ASS per clip when it is missing or fails.
    engine: Literal["ass", "hyperframes"] = "ass"
    # ---- style (G5): a preset fills the fields below; each one can still be changed by hand. The defaults
    # are the step 1-4 look, so older settings files render exactly as before.
    bold: bool = True
    uppercase: bool = False  # Latin/other cased scripts only; Devanagari has no case
    outline_color: str = "&H00000000"  # ASS &HAABBGGRR
    outline_px: int = Field(5, ge=0, le=20)
    shadow_px: int = Field(2, ge=0, le=20)
    box: bool = False  # a filled box behind each caption line (box_color); the outline is not drawn then
    box_color: str = "&H40000000"
    # How the spoken word is shown: color = it turns the highlight colour; pop = colour + it grows;
    # karaoke = spoken words stay highlighted; none = no highlight. None = the engine default
    # (ASS: color, HyperFrames: hyperframes.style).
    highlight_style: Literal["color", "pop", "karaoke", "none"] | None = None
    # Keyword emphasis: the 1-3 key words per line the LLM marks in script.json "keywords" are drawn in
    # emphasis_color, emphasis_scale times bigger (both engines).
    emphasis: bool = True
    emphasis_color: str = "&H0000A5FF"  # ASS BGR: orange
    emphasis_scale: float = Field(1.2, ge=1.0, le=2.0)
    emphasis_max_per_line: int = Field(2, ge=1, le=3)
    # The emoji the LLM may give a keyword is drawn after it - HyperFrames engine only (libass has no colour
    # emoji), with Noto Color Emoji (assets/fonts or the system font).
    emoji: bool = True

    @field_validator("outline_color", "box_color", "emphasis_color")
    @classmethod
    def _ass_color(cls, v: str) -> str:
        if not isinstance(v, str) or not ASS_COLOR_RE.match(v):
            raise ValueError("must be an ASS colour like &H00FFFFFF (&H + 8 hex digits, AABBGGRR)")
        return v


ASS_COLOR_RE = re.compile(r"^&H[0-9A-Fa-f]{8}\Z")  # \Z: "$" would accept a trailing newline


class HookSettings(BaseModel):
    """Hook text overlay (G4): the short hook line from script.json "hook", shown big over the first seconds
    of every clip, separate from the captions. An empty hook shows nothing. See README "Hook text"."""
    enabled: bool = True
    duration: float = Field(2.5, ge=1.0, le=5.0)  # seconds from the start of the clip
    # TOP EDGE of the hook (its box) in % of the height from the top; the hook grows downwards from here,
    # so it never leaves the frame. At render time a hook that would reach the captions is drawn smaller
    # (caption_style.hook_layout); on save the top must lie above the captions (cross_field_errors).
    top_pct: float = Field(8.0, ge=2, le=40)
    style: Literal["box", "plain"] = "box"  # box = filled box behind the text; plain = outlined text
    # ASS font size (like captions.font_size): the largest size; a long hook is drawn smaller to fit.
    font_size: int = Field(112, ge=30, le=200)
    uppercase: bool = False
    text_color: str = "&H00FFFFFF"  # ASS &HAABBGGRR: white
    box_color: str = "&H002A1AE8"  # box style: red (ASS BGR)
    outline_color: str = "&H00000000"  # plain style: text outline
    # HyperFrames engine only (ASS always fades in and out): pop = scales in, slide = slides down in,
    # fade = fades in.
    animation: Literal["pop", "slide", "fade"] = "pop"

    @field_validator("text_color", "box_color", "outline_color")
    @classmethod
    def _ass_color(cls, v: str) -> str:
        if not isinstance(v, str) or not ASS_COLOR_RE.match(v):
            raise ValueError("must be an ASS colour like &H00FFFFFF (&H + 8 hex digits, AABBGGRR)")
        return v


_PATH_MAX = 500


class HyperFramesSettings(BaseModel):
    """Optional HyperFrames caption engine (captions.engine = "hyperframes"). See CONTRACT_STEP3."""
    # Folder that holds node / node.exe ("" = use the `node` on PATH). Also accepts <dir>/bin/node.
    node_bin_dir: str = "~/tools/node22/bin"
    # Folder given to `npm --prefix ... install hyperframes`.
    package_dir: str = "~/tools/hyperframes"
    style: Literal["pop", "karaoke"] = "pop"
    fps: int | None = Field(None, ge=12, le=60)  # None = the cut clip's frame rate
    format: Literal["webm", "mov"] = "webm"  # overlay codec: VP9+alpha (small) or ProRes 4444 (big)
    workers: int = Field(0, ge=0, le=8)  # 0 = HyperFrames "auto"
    timeout_seconds: float = Field(300.0, ge=30, le=3600)  # one clip-language overlay render

    @field_validator("node_bin_dir", "package_dir")
    @classmethod
    def _path_text(cls, v: str) -> str:
        if len(v) > _PATH_MAX:
            raise ValueError(f"must be at most {_PATH_MAX} characters")
        if any(ch in v for ch in ("\x00", "\n", "\r")):
            raise ValueError("must not contain NUL or line breaks")
        return v


class SeparationSettings(BaseModel):
    """Vocal separation with Demucs (optional, voice-over mode only). See README "Vocal separation"."""
    # Background under the narration: "original" = the clip's own audio; "no_vocals" = the clip with its
    # speech removed by Demucs (falls back to the original, with a warning, when Demucs is missing or fails).
    background: Literal["original", "no_vocals"] = "original"
    # Python of the venv Demucs is installed in (Windows: ~/tools/demucs/Scripts/python.exe).
    python: str = "~/tools/demucs/bin/python"
    model: str = "htdemucs"  # Demucs model name (-n); htdemucs = 4-stem hybrid transformer
    # Seconds of audio processed at once: lower = less memory. Transformer models (htdemucs*) allow at most
    # 7 (Demucs refuses more than 7.8); other models up to 60.
    segment: int = Field(7, ge=1, le=60)
    threads: int = Field(4, ge=0, le=64)  # CPU threads for torch (0 = torch's default: all cores)
    timeout_seconds: float = Field(900.0, ge=30, le=7200)  # one clip
    # Where Demucs keeps its downloaded model (TORCH_HOME); "" = torch's default (~/.cache/torch).
    models_dir: str = "~/tools/demucs/models"

    @field_validator("python", "models_dir")
    @classmethod
    def _path_text(cls, v: str) -> str:
        if len(v) > _PATH_MAX:
            raise ValueError(f"must be at most {_PATH_MAX} characters")
        if any(ch in v for ch in ("\x00", "\n", "\r")):
            raise ValueError("must not contain NUL or line breaks")
        return v

    @field_validator("model")
    @classmethod
    def _model_name(cls, v: str) -> str:
        if not _MODEL_RE.match(v):
            raise ValueError("must be 1-64 letters, digits, '_' or '-' (e.g. htdemucs)")
        return v

    @model_validator(mode="after")
    def _segment_fits_model(self) -> "SeparationSettings":
        if self.model.lower().startswith("htdemucs") and self.segment > 7:
            raise ValueError(f"separation.segment must be at most 7 for the transformer model {self.model} "
                             f"(Demucs refuses longer segments); got {self.segment}")
        return self


_MODEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
_EXT_RE = re.compile(r"^\.[a-z0-9]{1,9}\Z")  # \Z: "$" would accept ".mp4\n"


class WebSettings(BaseModel):
    max_upload_mb: int = Field(4096, ge=1, le=102400)
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ogv"],
        min_length=1, max_length=30)

    @field_validator("allowed_extensions")
    @classmethod
    def _check_extensions(cls, v: list[str]) -> list[str]:
        for ext in v:
            if not isinstance(ext, str) or not _EXT_RE.match(ext):
                raise ValueError(f"invalid extension {ext!r}: use lower-case like '.mp4' "
                                 "(a dot then 1-9 letters/digits)")
        if len(set(v)) != len(v):
            raise ValueError("duplicate extensions")
        return v


class Settings(BaseModel):
    languages: list[Literal["en", "hi"]] = ["en", "hi"]
    # Voice-over narration. False = captions only: no narration script, no Voicebox; the clip keeps its
    # original audio and the captions are made from the original speech (translated for other languages).
    voiceover: bool = True
    llm: LLMSettings = Field(default_factory=LLMSettings)
    whisper: WhisperSettings = Field(default_factory=WhisperSettings)
    voicebox: VoiceboxSettings = Field(default_factory=VoiceboxSettings)
    clips: ClipSettings = Field(default_factory=ClipSettings)
    signals: SignalSettings = Field(default_factory=SignalSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    vision: VisionSettings = Field(default_factory=VisionSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    captions: CaptionSettings = Field(default_factory=CaptionSettings)
    hook: HookSettings = Field(default_factory=HookSettings)
    script: ScriptSettings = Field(default_factory=ScriptSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    hyperframes: HyperFramesSettings = Field(default_factory=HyperFramesSettings)
    separation: SeparationSettings = Field(default_factory=SeparationSettings)
    publish: PublishSettings = Field(default_factory=PublishSettings)

    def voice_for(self, lang: str) -> VoiceProfile:
        return self.voicebox.hindi if lang == "hi" else self.voicebox.english

    def cross_field_errors(self) -> dict[str, list[str]]:
        """Checks across sections, run when settings are saved (web 422, CLI error). Not run on load, so an
        older settings file never stops loading. Only a hook that is turned on is checked, and only for
        starting above the captions: a hook too big for the space left is drawn smaller at render time."""
        errors: dict[str, list[str]] = {}
        vb = self.voicebox
        if vb.chunk_min_chars >= vb.chunk_max_chars:
            errors["voicebox.chunk_min_chars"] = [
                f"must be smaller than voicebox.chunk_max_chars ({vb.chunk_max_chars})"]
        if self.hook.enabled:
            from .stages.caption_style import caption_top_px

            cap_top = caption_top_px(self.captions, self.clips.height) / self.clips.height * 100
            limit = cap_top - HOOK_MIN_SPACE_PCT
            if self.hook.top_pct > limit:
                where = (f"captions.position_pct {self.captions.position_pct:g}"
                         if self.captions.position_pct is not None else f"captions.margin_v {self.captions.margin_v}")
                errors["hook.top_pct"] = [
                    f"must be at most {max(0.0, limit):.0f} so the hook starts above the captions ({where}); "
                    f"move the hook up, the captions down, or turn the hook off"]
        return errors


HOOK_MIN_SPACE_PCT = 6.0  # % of the height: the least room between the hook top and the caption top


def expand_path(value: str) -> Path:
    """User path setting -> Path: environment variables, then ``~`` (``%USERPROFILE%`` on Windows)."""
    return Path(os.path.expandvars(value)).expanduser()


def settings_path() -> Path:
    return data_dir() / "settings.json"


def load_settings() -> Settings:
    p = settings_path()
    if p.exists():
        return Settings.model_validate_json(p.read_text(encoding="utf-8"))
    s = Settings()
    save_settings(s)
    return s


def save_settings(s: Settings) -> None:
    settings_path().write_text(json.dumps(s.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8")
