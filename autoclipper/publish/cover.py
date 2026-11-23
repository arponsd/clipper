"""G7 cover frame picker: cover.jpg (1080x1920) per clip-lang, from a frame of final.mp4 or an uploaded image,
with an optional title line burned in (libass, complex shaping so Devanagari is correct).

Files in `clips/NN/<lang>/`:
- `cover.jpg`            the cover sent to the platforms (JPEG, 1080x1920, <= 2 MB)
- `cover.json`           {"source": "frame"|"upload", "time": float|null, "title": str, "width", "height",
                          "size", "sha256", "final_sha256", "created_at"}
- `cover_upload.<ext>`   the uploaded source image (jpg/png/webp), kept so the title can be changed later

How each platform uses it (docs/CONTRACT_STEP4.md section 16): YouTube `thumbnails.set`, Facebook
`POST /{video-id}/thumbnails`, Instagram `thumb_offset` (frame source only; `cover_url` needs a public URL).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .. import ffmpeg
from ..config import FONTS_DIR
from ..jobstore import jobs_root
from ..stages._atomic import write_text_atomic
from . import records

COVER_W, COVER_H = 1080, 1920
PREVIEW_W, PREVIEW_H = 270, 480
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_COVER_BYTES = 2 * 1024 * 1024
MIN_IMAGE_SIDE, MAX_IMAGE_SIDE = 64, 8192
MAX_TITLE_CHARS = 100
UPLOAD_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
COVER_FILES = ("cover.jpg", "cover.json", *(f"cover_upload{ext}" for ext in UPLOAD_TYPES.values()))


class CoverError(Exception):
    """Bad input (-> 422) unless `status` says otherwise. `field` is the errors key."""

    def __init__(self, message: str, *, field: str = "body", status: int = 422):
        super().__init__(message)
        self.message = message
        self.field = field
        self.status = status


def clip_dir(job_id: str, clip: int, lang: str) -> Path:
    return jobs_root() / job_id / "clips" / f"{clip:02d}" / lang


def cover_path(job_id: str, clip: int, lang: str) -> Path:
    return clip_dir(job_id, clip, lang) / "cover.jpg"


def info_path(job_id: str, clip: int, lang: str) -> Path:
    return clip_dir(job_id, clip, lang) / "cover.json"


def upload_path(job_id: str, clip: int, lang: str) -> Path | None:
    d = clip_dir(job_id, clip, lang)
    for ext in UPLOAD_TYPES.values():
        p = d / f"cover_upload{ext}"
        if p.is_file():
            return p
    return None


def read_info(job_id: str, clip: int, lang: str) -> dict | None:
    """cover.json when cover.jpg exists too, else None."""
    try:
        data = json.loads(info_path(job_id, clip, lang).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not cover_path(job_id, clip, lang).is_file():
        return None
    return data


def public_info(job_id: str, clip: int, lang: str, final_sha256: str | None) -> dict | None:
    """cover.json + `outdated` (made from an earlier render) + `url`, for the API."""
    info = read_info(job_id, clip, lang)
    if info is None:
        return None
    out = {k: info.get(k) for k in ("source", "time", "title", "width", "height", "size", "sha256",
                                    "final_sha256", "created_at")}
    # only a frame depends on the render; an uploaded image never goes out of date
    out["outdated"] = bool(info.get("source") == "frame" and final_sha256 and info.get("final_sha256")
                           and info["final_sha256"] != final_sha256)
    out["thumb_offset_ms"] = thumb_offset_ms(info)
    out["url"] = f"/api/jobs/{job_id}/clips/{clip}/{lang}/cover.jpg"
    return out


def thumb_offset_ms(info: dict) -> int | None:
    t = info.get("time")
    if info.get("source") == "frame" and isinstance(t, (int, float)):
        return max(0, int(round(float(t) * 1000)))
    return None


def item_cover(job_id: str, clip: int, lang: str, final_sha256: str,
               duration: float | None = None) -> dict | None:
    """Cover facts for a PublishItem (None = no cover). Includes the absolute `file` for the uploaders.
    A frame offset past the end of the current render (`duration`) is dropped (Instagram thumb_offset)."""
    info = read_info(job_id, clip, lang)
    if info is None:
        return None
    p = cover_path(job_id, clip, lang)
    try:
        size = p.stat().st_size
    except OSError:
        return None
    offset = thumb_offset_ms(info)
    past_end = offset is not None and duration is not None and offset > int(max(0.0, duration - 0.05) * 1000)
    return {"path": f"clips/{clip:02d}/{lang}/cover.jpg", "file": str(p), "size": size,
            "sha256": records.file_sha256(p), "source": info.get("source"), "time": info.get("time"),
            "title": info.get("title") or "", "thumb_offset_ms": None if past_end else offset,
            "past_end": past_end,
            "outdated": bool(info.get("source") == "frame" and info.get("final_sha256")
                             and info.get("final_sha256") != final_sha256)}


# ---------------------------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------------------------
def check_time(t: Any, duration: float) -> float:
    if isinstance(t, bool) or not isinstance(t, (int, float)):
        raise CoverError("time must be a number of seconds", field="time")
    t = float(t)
    if t != t or t in (float("inf"), float("-inf")):
        raise CoverError("time must be a finite number of seconds", field="time")
    if t < 0:
        raise CoverError("time must be 0 or more seconds", field="time")
    # the last decodable frame starts a little before the end
    last = max(0.0, duration - 0.05)
    if t > last:
        raise CoverError(f"time {t:.2f} s is outside the clip (0 - {last:.2f} s)", field="time")
    return t


def check_title(title: Any) -> str:
    if title is None:
        return ""
    if not isinstance(title, str):
        raise CoverError("title must be text", field="title")
    text = " ".join(title.split())
    if len(text) > MAX_TITLE_CHARS:
        raise CoverError(f"title is longer than {MAX_TITLE_CHARS} characters", field="title")
    if any(ord(c) < 32 for c in text):
        raise CoverError("title has control characters", field="title")
    return text


def sniff_type(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# ---------------------------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------------------------
def _fonts_dir() -> Path | None:
    d = FONTS_DIR
    try:
        if d.is_dir() and any(p.suffix.lower() in ffmpeg.FONT_SUFFIXES for p in d.rglob("*") if p.is_file()):
            return d
    except OSError:
        pass
    return None


def _ass_text(text: str) -> str:
    # ASS override blocks start with "{" and "\N"-style escapes with "\": keep them out of user text
    return text.replace("\\", "⧵").replace("{", "(").replace("}", ")")


def title_ass(title: str, font: str) -> str:
    return (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cover,{font},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,"
        "8,70,70,230,1\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,9:00:00.00,Cover,,0,0,0,,{_ass_text(title)}\n"
    )


def _fill_filter(w: int, h: int) -> str:
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"


def render_frame(src: Path, t: float | None, dst: Path, *, width: int, height: int,
                 ass: Path | None = None, quality: int = 3) -> None:
    vf = _fill_filter(width, height)
    if ass is not None:
        vf += "," + ffmpeg.ass_filter(ass, _fonts_dir())
    args: list[str] = []
    if t is not None:
        args += ["-ss", f"{t:.3f}"]
    args += ["-i", str(src), "-frames:v", "1", "-vf", vf, "-q:v", str(quality), "-f", "image2",
             "-c:v", "mjpeg", str(dst)]
    ffmpeg.run(args, desc="cover frame")


def preview_frame(final: Path, t: float, work_dir: Path) -> bytes:
    """A small JPEG of the frame at t (for the scrubber); nothing is kept on disk."""
    tmp = work_dir / f".cover-preview-{uuid.uuid4().hex}.jpg"
    try:
        render_frame(final, t, tmp, width=PREVIEW_W, height=PREVIEW_H, quality=5)
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def image_size(path: Path) -> tuple[int, int]:
    try:
        info = ffmpeg.probe(path)
    except ffmpeg.FFmpegError:
        raise CoverError("the image could not be read (is it a valid JPEG, PNG or WebP?)") from None
    if not info.width or not info.height:
        raise CoverError("the image could not be read (is it a valid JPEG, PNG or WebP?)")
    return int(info.width), int(info.height)


# ---------------------------------------------------------------------------------------------
# writes (callers hold the worker's exclusive_publish for the clip-lang)
# ---------------------------------------------------------------------------------------------
def save_upload(job_id: str, clip: int, lang: str, data: bytes, content_type: str) -> dict:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype not in UPLOAD_TYPES:
        raise CoverError("upload a JPEG, PNG or WebP image (Content-Type image/jpeg, image/png or image/webp)",
                         field="Content-Type", status=415)
    if not data:
        raise CoverError("the image is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise CoverError("the image is larger than 10 MB", status=413)
    if sniff_type(data) != ctype:
        raise CoverError(f"the file is not a {ctype.split('/')[1].upper()} image")
    d = clip_dir(job_id, clip, lang)
    tmp = d / f".cover-upload-{uuid.uuid4().hex}{UPLOAD_TYPES[ctype]}"
    try:
        tmp.write_bytes(data)
        w, h = image_size(tmp)
        if min(w, h) < MIN_IMAGE_SIDE or max(w, h) > MAX_IMAGE_SIDE:
            raise CoverError(f"the image is {w}x{h}; use {MIN_IMAGE_SIDE}-{MAX_IMAGE_SIDE} pixels per side")
        for ext in UPLOAD_TYPES.values():
            (d / f"cover_upload{ext}").unlink(missing_ok=True)
        dst = d / f"cover_upload{UPLOAD_TYPES[ctype]}"
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return {"type": ctype, "width": w, "height": h, "size": len(data)}


def make_cover(job_id: str, clip: int, lang: str, *, source: str, t: float | None, title: str, final: Path,
               final_sha256: str, font: str) -> dict:
    """Write cover.jpg + cover.json (atomic). `t` is checked by the caller (check_time)."""
    d = clip_dir(job_id, clip, lang)
    if source == "frame":
        src: Path = final
    elif source == "upload":
        up = upload_path(job_id, clip, lang)
        if up is None:
            raise CoverError("upload an image first", field="source", status=409)
        src, t = up, None
    else:
        raise CoverError("source must be frame or upload", field="source")
    tag = uuid.uuid4().hex
    ass = d / f".cover-title-{tag}.ass" if title else None
    tmp = d / f".cover-{tag}.jpg"
    try:
        if ass is not None:
            write_text_atomic(ass, title_ass(title, font))
        for q in (3, 6, 10):
            render_frame(src, t, tmp, width=COVER_W, height=COVER_H, ass=ass, quality=q)
            if tmp.stat().st_size <= MAX_COVER_BYTES:
                break
        else:
            raise CoverError("the cover is larger than 2 MB even at low quality", status=409)
        os.replace(tmp, d / "cover.jpg")
    except ffmpeg.FFmpegError as e:
        raise CoverError(f"could not make the cover: {str(e).splitlines()[0][:200]}", status=500) from None
    finally:
        tmp.unlink(missing_ok=True)
        if ass is not None:
            ass.unlink(missing_ok=True)
    p = d / "cover.jpg"
    info = {"source": source, "time": round(t, 3) if t is not None else None, "title": title,
            "width": COVER_W, "height": COVER_H, "size": p.stat().st_size, "sha256": records.file_sha256(p),
            "final_sha256": final_sha256, "created_at": time.time()}
    write_text_atomic(d / "cover.json", json.dumps(info, ensure_ascii=False, indent=2))
    return info


def delete_cover(job_id: str, clip: int, lang: str) -> bool:
    d = clip_dir(job_id, clip, lang)
    found = False
    for name in COVER_FILES:
        p = d / name
        if p.exists():
            found = True
            p.unlink(missing_ok=True)
    return found


__all__ = ["CoverError", "make_cover", "save_upload", "preview_frame", "public_info", "item_cover",
           "delete_cover", "check_time", "check_title"]
