"""metadata.json -> per-platform payloads (CONTRACT_STEP4 section 6). Pure functions, no I/O."""
from __future__ import annotations

import re
from typing import Any

YT_TITLE_MAX = 100
YT_DESC_MAX_BYTES = 5000
YT_TAGS_MAX = 500
FB_TITLE_MAX = 255
IG_CAPTION_MAX = 2200
IG_MAX_HASHTAGS = 30
ELLIPSIS = "…"

_WS = re.compile(r"\s+")


def normalize_hashtags(tags: Any) -> list[str]:
    """`#` + text without spaces/`#`; empty and duplicate (case-insensitive) tags dropped, order kept."""
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(tags, list):
        return out
    for t in tags:
        if not isinstance(t, str):
            continue
        body = _WS.sub("", t).replace("#", "")
        if not body:
            continue
        key = body.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append("#" + body)
    return out


def _s(meta: dict, key: str) -> str:
    v = meta.get(key) if isinstance(meta, dict) else None
    return v.strip() if isinstance(v, str) else ""


def _no_angle(text: str) -> str:
    return text.replace("<", "").replace(">", "")


def cut_chars(text: str, limit: int) -> str:
    """At most `limit` characters; cut on a word boundary and end with an ellipsis when shortened."""
    if len(text) <= limit:
        return text
    room = limit - len(ELLIPSIS)
    head = text[:room]
    sp = head.rfind(" ")
    if sp >= room // 2:
        head = head[:sp]
    return head.rstrip() + ELLIPSIS


def cut_bytes(text: str, limit: int) -> str:
    """At most `limit` UTF-8 bytes, cut on a code-point boundary."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore")


def youtube_tag_cost(tag: str) -> int:
    return len(tag) + (2 if " " in tag else 0)


def youtube_tags(hashtags: list[str]) -> list[str]:
    """Tags without `#`, kept in order while the YouTube count (len + commas + quotes) stays <= 500."""
    out: list[str] = []
    total = 0
    for h in hashtags:
        tag = h.lstrip("#")
        if not tag:
            continue
        cost = youtube_tag_cost(tag) + (1 if out else 0)
        if total + cost > YT_TAGS_MAX:
            continue
        out.append(tag)
        total += cost
    return out


def youtube_payload(meta: dict, lang: str, target: Any) -> dict:
    tags = normalize_hashtags(meta.get("hashtags") if isinstance(meta, dict) else None)
    title = cut_chars(_WS.sub(" ", _no_angle(_s(meta, "title"))).strip(), YT_TITLE_MAX)
    desc_tags = list(tags)
    if getattr(target, "add_shorts_hashtag", True) and not any(t.casefold() == "#shorts" for t in desc_tags):
        desc_tags.append("#Shorts")
    description = _s(meta, "description")
    if desc_tags:
        description = (description + "\n\n" if description else "") + " ".join(desc_tags)
    description = cut_bytes(_no_angle(description), YT_DESC_MAX_BYTES)
    return {
        "snippet": {
            "title": title,
            "description": description,
            "tags": youtube_tags(tags),
            "categoryId": getattr(target, "category_id", "22"),
            "defaultLanguage": lang,
            "defaultAudioLanguage": lang,
        },
        "status": {
            "privacyStatus": getattr(target, "privacy", "private"),
            "selfDeclaredMadeForKids": bool(getattr(target, "made_for_kids", False)),
            "containsSyntheticMedia": bool(getattr(target, "contains_synthetic_media", True)),
        },
    }


def facebook_payload(meta: dict) -> dict:
    tags = normalize_hashtags(meta.get("hashtags") if isinstance(meta, dict) else None)
    description = _s(meta, "description")
    if tags:
        description = (description + "\n\n" if description else "") + " ".join(tags)
    return {"title": cut_chars(_WS.sub(" ", _s(meta, "title")).strip(), FB_TITLE_MAX), "description": description}


def instagram_payload(meta: dict, target: Any) -> dict:
    tags = normalize_hashtags(meta.get("hashtags") if isinstance(meta, dict) else None)[:IG_MAX_HASHTAGS]
    title = _s(meta, "title")
    description = _s(meta, "description")
    tag_line = " ".join(tags)

    def build(desc: str) -> str:
        return "\n\n".join(p for p in (title, desc, tag_line) if p)

    caption = build(description)
    if len(caption) > IG_CAPTION_MAX:  # cut the description first, keep the title and the hashtags
        fixed = len(build(""))
        room = IG_CAPTION_MAX - fixed - (2 if description else 0)
        description = cut_chars(description, room) if room > len(ELLIPSIS) else ""
        caption = build(description)
    if len(caption) > IG_CAPTION_MAX:  # title + hashtags alone too long (pathological)
        caption = cut_chars(caption, IG_CAPTION_MAX)
    return {"caption": caption, "share_to_feed": bool(getattr(target, "share_to_feed", True))}


def count_hashtags(text: str) -> int:
    return len(re.findall(r"#\w", text))
