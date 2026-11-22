"""Lead sign-off regression: Hindi captions must be burned with complex (HarfBuzz) shaping.

The real run showed भविष्य burned as "भवष्ि्य" and खत्म as "खत्‌म" (simple shaping: the i-matra is
not reordered and the virama stays visible). ffmpeg.ass_filter now asks libass for shaping=complex.
This test burns a Devanagari line with real ffmpeg and checks the pixels match an explicit
shaping=complex render and differ from a shaping=simple one.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from autoclipper import ffmpeg
from autoclipper.config import CaptionSettings
from autoclipper.models import Word
from autoclipper.stages.captions import build_ass


def _devanagari_font_available() -> bool:
    if shutil.which("fc-list") is None:
        return False
    out = subprocess.run(["fc-list"], capture_output=True, text=True).stdout
    return "Noto Sans Devanagari" in out


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or not _devanagari_font_available(),
    reason="needs ffmpeg with libass and Noto Sans Devanagari installed system-wide")


def _burn(ass: Path, vf: str, dst: Path) -> bytes:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=1",
         "-vf", vf, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", str(dst)],
        check=True)
    return dst.read_bytes()


def test_hindi_burn_uses_complex_shaping(tmp_path: Path) -> None:
    words = [Word(start=0.0, end=0.5, text="भविष्य"), Word(start=0.5, end=1.0, text="खत्म")]
    ass = tmp_path / "hi.ass"
    ass.write_text(build_ass(words, lang="hi", settings=CaptionSettings()), encoding="utf-8")

    ours = _burn(ass, ffmpeg.ass_filter(ass, None), tmp_path / "ours.raw")
    esc = ffmpeg.escape_filter_path(ass.resolve())
    complex_ = _burn(ass, f"ass=filename={esc}:shaping=complex", tmp_path / "complex.raw")
    simple = _burn(ass, f"ass=filename={esc}:shaping=simple", tmp_path / "simple.raw")

    assert any(ours), "nothing was burned"
    assert ours == complex_
    assert ours != simple, "Devanagari burned with simple shaping (broken conjuncts / matras)"
