"""Credential store: `data/secrets/` (CONTRACT_STEP4 section 4).

Files are JSON, written atomically with mode 0600 (dir 0700). On Windows chmod only toggles the read-only
bit and the data dir may sit anywhere (e.g. C:\\code\\auto-clipper, whose inherited ACL can grant
"Authenticated Users"), so the secrets dir gets an explicit owner-only ACL via icacls: inheritance removed,
full control for the current user only; new files inherit it. Nothing here ever logs a file's contents.
Every secret value that passes through `read_secret`/`write_secret` is remembered in-process so `redact()`
can mask it in any text (error messages, log lines) before it leaves the publish package.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable

from ..config import data_dir

log = logging.getLogger("autoclipper.publish.secrets")

# Fixed allowlist of secret names (+ the internal resume records, one per job/clip/lang/platform).
_NAMES = {"google_client", "youtube_en", "youtube_hi", "meta_en", "meta_hi"}
_RESUME_RE = re.compile(r"\Aresume_[A-Za-z0-9][A-Za-z0-9_-]{0,63}_[0-9]{2,3}_(en|hi)_(youtube|facebook|instagram)\Z")
# keys whose string values are secrets wherever they appear
SECRET_KEYS = ("client_secret", "refresh_token", "access_token", "page_token", "ig_token", "session_uri",
               "token", "code", "code_verifier", "id_token")
_MIN_SECRET_LEN = 8

_known: set[str] = set()
_known_lock = threading.Lock()


_IS_WINDOWS = os.name == "nt"
_acl_done: set[str] = set()
_acl_lock = threading.Lock()


def _windows_owner_only(d: Path) -> None:
    """Replace the dir's ACL with "current user: full control" (inherited by every file created in it).
    Once per process per dir; a failure is logged loudly (the README tells how to check with icacls)."""
    key = str(d)
    with _acl_lock:
        if key in _acl_done:
            return
        user = os.environ.get("USERNAME", "")
        domain = os.environ.get("USERDOMAIN", "")
        if not user:
            log.warning("cannot restrict %s: USERNAME is not set; secrets may be readable by other users", d)
            return
        who = f"{domain}\\{user}" if domain else user
        try:
            r = subprocess.run(["icacls", str(d), "/inheritance:r", "/grant:r", f"{who}:(OI)(CI)F"],
                               capture_output=True, text=True, timeout=30)
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            _acl_done.add(key)
        else:
            log.warning("could not set an owner-only ACL on %s; secrets may be readable by other users - "
                        "run: icacls \"%s\" /inheritance:r /grant:r \"%%USERNAME%%:(OI)(CI)F\"", d, d)


def secrets_dir() -> Path:
    d = data_dir() / "secrets"
    d.mkdir(parents=True, exist_ok=True)
    if _IS_WINDOWS:
        _windows_owner_only(d)
    else:
        try:
            os.chmod(d, 0o700)
        except OSError:  # pragma: no cover - foreign owner
            pass
    return d


def _check_name(name: str) -> None:
    if name not in _NAMES and not _RESUME_RE.match(name or ""):
        raise ValueError(f"unknown secret name {name!r}")


def secret_path(name: str) -> Path:
    _check_name(name)
    return secrets_dir() / f"{name}.json"


def remember(*values: Any) -> None:
    """Register secret values so `redact` masks them."""
    with _known_lock:
        for v in values:
            if isinstance(v, str) and len(v) >= _MIN_SECRET_LEN:
                _known.add(v)


def _collect(data: Any) -> Iterable[str]:
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, str) and k in SECRET_KEYS:
                yield v
            elif isinstance(v, (dict, list)):
                yield from _collect(v)
    elif isinstance(data, list):
        for v in data:
            yield from _collect(v)


def write_secret(name: str, data: dict) -> Path:
    """Atomic write with mode 0600: O_EXCL tmp in the same dir, fsync, os.replace, chmod."""
    path = secret_path(name)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    remember(*_collect(data))
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover
        pass
    return path


def read_secret(name: str) -> dict | None:
    path = secret_path(name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("secret file %s is unreadable (%s); treating it as missing", path.name, type(e).__name__)
        return None
    if not isinstance(data, dict):
        log.warning("secret file %s is not a JSON object; treating it as missing", path.name)
        return None
    remember(*_collect(data))
    return data


def delete_secret(name: str) -> bool:
    try:
        secret_path(name).unlink()
        return True
    except FileNotFoundError:
        return False


_PATTERNS = [
    # a token after Bearer/OAuth: 8+ token chars incl. at least one digit or punctuation (so prose such as
    # "Google OAuth client" is left alone)
    re.compile(r"\b(Bearer|OAuth)\s+(?=[A-Za-z]*[0-9._~+/=-])[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"(?i)\b(access_token|refresh_token|client_secret|page_token|ig_token|code_verifier|"
               r"upload_id|fb_exchange_token|input_token)=([^&\s\"']+)"),
    re.compile(r"(?i)(\"(?:access_token|refresh_token|client_secret|page_token|ig_token|id_token)\"\s*:\s*\")"
               r"[^\"]*(\")"),
]


def redact(text: str) -> str:
    """Mask every known secret value and anything that looks like a bearer token / token parameter."""
    if not isinstance(text, str) or not text:
        return text
    with _known_lock:
        known = sorted(_known, key=len, reverse=True)
    for v in known:
        if v in text:
            text = text.replace(v, "***")
    text = _PATTERNS[0].sub(lambda m: f"{m.group(1)} ***", text)
    text = _PATTERNS[1].sub(lambda m: f"{m.group(1)}=***", text)
    text = _PATTERNS[2].sub(lambda m: f"{m.group(1)}***{m.group(2)}", text)
    return text
