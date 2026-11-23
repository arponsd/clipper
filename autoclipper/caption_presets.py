"""Caption style presets (G5): named bundles of caption style fields.

A preset only fills fields of ``CaptionSettings`` (size, weight, case, colours, outline/shadow, box, highlight
style, emphasis, words per line); fonts, the position and the engine are never touched. After a preset is
applied every field can still be changed by hand: the render only reads the fields themselves, and
``captions.preset`` just remembers which bundle was applied last (``custom`` = none).
"""
from __future__ import annotations

from typing import Any

from .config import CaptionSettings, Settings

# Colours are ASS &HAABBGGRR (BGR order, AA 00 = opaque).
PRESETS: dict[str, dict[str, Any]] = {
    "clean": {
        "name": "Clean",
        "description": "Regular weight, thin outline, soft yellow highlight. Calm talking-head look.",
        "values": {"font_size": 70, "bold": False, "uppercase": False, "words_per_line": 4,
                   "primary_color": "&H00FFFFFF", "highlight_color": "&H0080F0FF",
                   "outline_color": "&H00000000", "outline_px": 3, "shadow_px": 1,
                   "box": False, "box_color": "&H40000000", "highlight_style": "color",
                   "emphasis_color": "&H0066D9FF", "emphasis_scale": 1.0},
    },
    "bold_pop": {
        "name": "Bold pop",
        "description": "Big upper-case words, two per line; the spoken word pops in yellow.",
        "values": {"font_size": 92, "bold": True, "uppercase": True, "words_per_line": 2,
                   "primary_color": "&H00FFFFFF", "highlight_color": "&H0000E5FF",
                   "outline_color": "&H00000000", "outline_px": 7, "shadow_px": 3,
                   "box": False, "box_color": "&H40000000", "highlight_style": "pop",
                   "emphasis_color": "&H0000FF7F", "emphasis_scale": 1.25},
    },
    "karaoke": {
        "name": "Karaoke",
        "description": "Four words per line that fill with gold as they are spoken.",
        "values": {"font_size": 80, "bold": True, "uppercase": False, "words_per_line": 4,
                   "primary_color": "&H00FFFFFF", "highlight_color": "&H0000D7FF",
                   "outline_color": "&H00000000", "outline_px": 5, "shadow_px": 2,
                   "box": False, "box_color": "&H40000000", "highlight_style": "karaoke",
                   "emphasis_color": "&H00FFB040", "emphasis_scale": 1.15},
    },
    "boxed": {
        "name": "Boxed",
        "description": "White text on a dark box: readable over any background.",
        "values": {"font_size": 74, "bold": True, "uppercase": False, "words_per_line": 3,
                   "primary_color": "&H00FFFFFF", "highlight_color": "&H0000E5FF",
                   "outline_color": "&H00000000", "outline_px": 0, "shadow_px": 0,
                   "box": True, "box_color": "&H30000000", "highlight_style": "color",
                   "emphasis_color": "&H00FFFF00", "emphasis_scale": 1.0},  # 1.0: libass boxes stay even
    },
    "gaming_neon": {
        "name": "Gaming neon",
        "description": "Upper case with a purple glow outline and a neon-green pop for gaming clips.",
        "values": {"font_size": 88, "bold": True, "uppercase": True, "words_per_line": 2,
                   "primary_color": "&H00FFFFFF", "highlight_color": "&H0014FF39",
                   "outline_color": "&H00D2008C", "outline_px": 7, "shadow_px": 4,
                   "box": False, "box_color": "&H40000000", "highlight_style": "pop",
                   "emphasis_color": "&H00FFFF00", "emphasis_scale": 1.3},
    },
}

PRESET_IDS = tuple(PRESETS)


class PresetError(ValueError):
    """Unknown preset name."""


def preset_values(name: str) -> dict[str, Any]:
    try:
        return dict(PRESETS[name]["values"])
    except KeyError:
        raise PresetError(f"unknown caption preset {name!r}; choose one of: {', '.join(PRESET_IDS)}") from None


def apply_preset(settings: Settings, name: str) -> Settings:
    """A copy of ``settings`` with the preset's fields filled into ``captions`` and captions.preset = name."""
    values = preset_values(name)
    data = settings.captions.model_dump()
    data.update(values)
    data["preset"] = name
    return settings.model_copy(update={"captions": CaptionSettings.model_validate(data)})


def matches_preset(captions: CaptionSettings, name: str) -> bool:
    """True when every field of the preset still has the preset's value (no hand override)."""
    if name not in PRESETS:
        return False
    return all(getattr(captions, k) == v for k, v in PRESETS[name]["values"].items())


def presets_payload() -> list[dict[str, Any]]:
    """GET /api/caption-presets: [{id, name, description, values}]."""
    return [{"id": pid, "name": p["name"], "description": p["description"], "values": dict(p["values"])}
            for pid, p in PRESETS.items()]


# Every preset must be a valid CaptionSettings (checked at import: a typo fails the test suite at once).
for _pid in PRESETS:
    CaptionSettings.model_validate({**CaptionSettings().model_dump(), **PRESETS[_pid]["values"]})
