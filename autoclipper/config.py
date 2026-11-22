"""Settings: defaults, load/save to data/settings.json. Every field is editable later from the UI."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

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
    poll_interval: float = 1.0
    timeout_seconds: float = 600.0
    english: VoiceProfile = Field(default_factory=VoiceProfile)
    # Hindi must use Chatterbox Multilingual.
    hindi: VoiceProfile = Field(default_factory=lambda: VoiceProfile(engine="chatterbox"))


class ClipSettings(BaseModel):
    count: int = Field(5, ge=1, le=30)
    min_seconds: float = Field(20, ge=5, le=180)
    max_seconds: float = Field(60, ge=10, le=180)
    width: int = 1080
    height: int = 1920
    # "crop" = center crop to 9:16; "blur" = fit whole frame over a blurred fill
    reframe: Literal["crop", "blur"] = "crop"


class AudioSettings(BaseModel):
    original_volume: float = Field(0.15, ge=0, le=1)  # original audio kept quietly under voice
    voice_volume: float = Field(1.0, ge=0, le=2)
    max_speedup: float = Field(1.25, ge=1.0, le=2.0)  # cap on atempo to fit voice into clip


class CaptionSettings(BaseModel):
    font_en: str = "Noto Sans"
    font_hi: str = "Noto Sans Devanagari"
    font_size: int = 78
    words_per_line: int = 3
    primary_color: str = "&H00FFFFFF"  # ASS BGR: white
    highlight_color: str = "&H0000E5FF"  # ASS BGR: yellow
    margin_v: int = 520  # distance from bottom in px (1920 tall)


class Settings(BaseModel):
    languages: list[Literal["en", "hi"]] = ["en", "hi"]
    llm: LLMSettings = Field(default_factory=LLMSettings)
    whisper: WhisperSettings = Field(default_factory=WhisperSettings)
    voicebox: VoiceboxSettings = Field(default_factory=VoiceboxSettings)
    clips: ClipSettings = Field(default_factory=ClipSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    captions: CaptionSettings = Field(default_factory=CaptionSettings)

    def voice_for(self, lang: str) -> VoiceProfile:
        return self.voicebox.hindi if lang == "hi" else self.voicebox.english


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
