"""Settings: defaults, load/save to data/settings.json. Every field is editable later from the UI."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

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


class AudioSettings(BaseModel):
    original_volume: float = Field(0.15, ge=0, le=1)  # original audio level WHILE the voice plays (ducked)
    # original audio level when no voice plays (before / after the narration)
    original_volume_after: float = Field(1.0, ge=0, le=2)
    duck_ramp_seconds: float = Field(0.4, ge=0, le=2)  # ramp time between the two levels
    voice_volume: float = Field(1.0, ge=0, le=2)
    max_speedup: float = Field(1.25, ge=1.0, le=2.0)  # cap on atempo to fit voice into clip
    loudnorm: bool = True  # single-pass loudnorm on the final mix
    target_lufs: float = Field(-14.0, ge=-30, le=-8)  # loudnorm integrated loudness target


class ScriptSettings(BaseModel):
    # Words per second of the TTS voice, measured on chatterbox in the sample job (EN ~3.0, HI ~3.36).
    wps_en: float = Field(3.0, ge=1.0, le=6.0)
    wps_hi: float = Field(3.35, ge=1.0, le=6.0)
    # Narration target = clip length x fill_ratio (the LLM overshoots the word count by ~5 %).
    fill_ratio: float = Field(0.88, ge=0.5, le=1.0)
    # A voice shorter than this fraction of the clip gets one "lengthen" retry (new jobs only).
    lengthen_below: float = Field(0.75, ge=0.3, le=0.95)

    def wps(self, lang: str) -> float:
        return self.wps_hi if lang == "hi" else self.wps_en


class CaptionSettings(BaseModel):
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
    llm: LLMSettings = Field(default_factory=LLMSettings)
    whisper: WhisperSettings = Field(default_factory=WhisperSettings)
    voicebox: VoiceboxSettings = Field(default_factory=VoiceboxSettings)
    clips: ClipSettings = Field(default_factory=ClipSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    captions: CaptionSettings = Field(default_factory=CaptionSettings)
    script: ScriptSettings = Field(default_factory=ScriptSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    hyperframes: HyperFramesSettings = Field(default_factory=HyperFramesSettings)

    def voice_for(self, lang: str) -> VoiceProfile:
        return self.voicebox.hindi if lang == "hi" else self.voicebox.english


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
