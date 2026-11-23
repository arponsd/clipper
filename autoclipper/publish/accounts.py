"""Connected accounts: read the secret files and expose only safe views (connected / id / name)."""
from __future__ import annotations

from typing import Any

from . import LANG_NAMES, LANGS, PLATFORMS
from .secrets import delete_secret, read_secret, write_secret

_HIDE = ("token", "secret", "session_uri", "verifier")


class Account(dict):
    """Account data incl. tokens (internal). repr/str never show a token."""

    def __repr__(self) -> str:
        safe = {k: ("***" if any(h in k for h in _HIDE) and v else v) for k, v in self.items()}
        return f"Account({safe!r})"

    __str__ = __repr__

    @property
    def public(self) -> dict:
        return {"id": self.get("id"), "name": self.get("name")}


# ---- google client ---------------------------------------------------------------------------
def google_client() -> dict | None:
    data = read_secret("google_client")
    if not data or not data.get("client_id") or not data.get("client_secret"):
        return None
    return Account(data)


def client_id_hint(client_id: str | None) -> str | None:
    if not client_id:
        return None
    suffix = "apps.googleusercontent.com"
    head = client_id[:8]
    if client_id.endswith(suffix) and len(client_id) > len(suffix) + 8:
        return f"{head}…{suffix}"
    return f"{head}…" if len(client_id) > 8 else head


# ---- per platform accounts -------------------------------------------------------------------
def youtube_account(lang: str) -> Account | None:
    data = read_secret(f"youtube_{lang}")
    if not data or not data.get("refresh_token") or not data.get("channel_id"):
        return None
    acc = Account(data)
    acc["id"] = data.get("channel_id")
    acc["name"] = data.get("channel_title") or data.get("channel_id")
    return acc


def meta_record(lang: str) -> dict | None:
    return read_secret(f"meta_{lang}")


def facebook_account(lang: str) -> Account | None:
    data = meta_record(lang)
    if not data or not data.get("page_id") or not data.get("page_token"):
        return None
    acc = Account(data)
    acc["id"] = data.get("page_id")
    acc["name"] = data.get("page_name") or data.get("page_id")
    acc["token"] = data.get("page_token")
    return acc


def instagram_account(lang: str) -> Account | None:
    data = meta_record(lang)
    if not data or not data.get("ig_user_id"):
        return None
    token = data.get("ig_token") or data.get("page_token")
    if not token:
        return None
    acc = Account(data)
    acc["id"] = data.get("ig_user_id")
    acc["name"] = data.get("ig_username") or data.get("ig_user_id")
    acc["token"] = token
    return acc


def account(lang: str, platform: str) -> Account | None:
    if platform == "youtube":
        return youtube_account(lang)
    if platform == "facebook":
        return facebook_account(lang)
    if platform == "instagram":
        return instagram_account(lang)
    raise ValueError(platform)


ID_FIELD = {"youtube": "channel_id", "facebook": "page_id", "instagram": "ig_user_id"}


def settings_id(target: Any, platform: str) -> str:
    return getattr(target, ID_FIELD[platform])


def other_lang(lang: str) -> str:
    return "hi" if lang == "en" else "en"


def mapping_problems(ps: Any, lang: str, platform: str, acc: Account | None) -> list[str]:
    """Account-level problems (no clip involved): mismatch with settings, same account as the other lang."""
    problems: list[str] = []
    target = ps.target(lang, platform)
    sid = settings_id(target, platform)
    what = {"youtube": "channel", "facebook": "Page", "instagram": "Instagram account"}[platform]
    if acc is not None:
        if not sid:
            problems.append(f"no {what} selected in settings for {LANG_NAMES[lang]} - connect again")
        elif sid != acc.get("id"):
            problems.append(f"settings {what} differs from the connected one")
    if platform == "youtube" and acc is not None and google_client() is None:
        problems.append("Google OAuth client not configured - upload client_secret.json")
    if not ps.allow_same_account:
        mine = {x for x in (sid, acc.get("id") if acc else None) if x}
        o = other_lang(lang)
        oacc = account(o, platform)
        theirs = {x for x in (settings_id(ps.target(o, platform), platform), oacc.get("id") if oacc else None) if x}
        if mine & theirs:
            problems.append(f"same {what} as {o} (English and Hindi must use different accounts; "
                            "set allow_same_account to permit it)")
    return problems


def account_status(ps: Any, lang: str, platform: str) -> dict:
    acc = account(lang, platform)
    target = ps.target(lang, platform)
    return {
        "connected": acc is not None,
        "enabled": bool(target.enabled),
        "account_id": acc.get("id") if acc else None,
        "account_name": acc.get("name") if acc else None,
        "connected_at": acc.get("connected_at") if acc else None,
        "problems": mapping_problems(ps, lang, platform, acc),
    }


def all_status(ps: Any) -> dict:
    gc = google_client()
    return {
        "dry_run": bool(ps.dry_run),
        "google_client": {"configured": gc is not None, "client_id_hint": client_id_hint(gc.get("client_id")) if gc else None},
        "langs": {lang: {p: account_status(ps, lang, p) for p in PLATFORMS} for lang in LANGS},
    }


__all__ = ["Account", "google_client", "youtube_account", "facebook_account", "instagram_account", "account",
           "account_status", "all_status", "mapping_problems", "write_secret", "delete_secret"]
