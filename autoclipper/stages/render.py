"""Render stage: cut.mp4 + voice + burned captions -> final.mp4, plus per-language metadata.json."""
from __future__ import annotations

import json
from pathlib import Path

from .. import config, ffmpeg
from ..config import AudioSettings
from ..interfaces import ProgressFn
from ..models import ClipScript

_FONT_SUFFIXES = {".ttf", ".otf", ".ttc"}


class RenderError(RuntimeError):
    """Final render failed before ffmpeg ran (missing inputs)."""


def resolve_fonts_dir() -> Path | None:
    """assets/fonts if it holds any font file, else None (libass then uses system fontconfig)."""
    d = Path(config.FONTS_DIR)
    if not d.is_dir():
        return None
    if any(p.is_file() and p.suffix.lower() in _FONT_SUFFIXES for p in d.rglob("*")):
        return d
    return None


def render_final(*, cut: Path, voice: Path | None, ass: Path | None, dst: Path,
                 audio: AudioSettings, on_progress: ProgressFn | None = None) -> Path:
    cut = Path(cut)
    if not cut.is_file():
        raise RenderError(f"cut clip not found: {cut} (run the cut stage first)")
    for label, p in (("voice", voice), ("captions", ass)):
        if p is not None and not Path(p).is_file():
            raise RenderError(f"{label} file not found: {p}")
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    return ffmpeg.mix_burn(
        cut, dst,
        voice=Path(voice) if voice is not None else None,
        ass=Path(ass) if ass is not None else None,
        original_volume=audio.original_volume, voice_volume=audio.voice_volume,
        fonts_dir=resolve_fonts_dir(), duration=ffmpeg.probe(cut).duration,
        on_progress=on_progress)


def write_metadata(script: ClipScript, lang: str, dst: Path) -> Path:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = {"language": lang, "title": script.title, "description": script.description,
            "hashtags": list(script.hashtags)}
    dst.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return dst
