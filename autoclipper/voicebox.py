"""REST client for a local Voicebox TTS server (plain ``httpx``, not the SDK's ``httpx2``).

API used:
    POST /generate        {text, profile_id, language, engine} -> {"id"|"generation_id": ...}
    GET  /history/{id}    -> {"status": "pending|generating|completed|failed|error", ...}
    GET  /audio/{id}      -> audio bytes
    GET  /profiles        -> [ {...}, ... ] or {"profiles": [...]}
    POST /generate/{id}/cancel  (best effort, on timeout or Ctrl+C while waiting)
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import VoiceboxSettings
from .interfaces import ProgressFn

DEFAULT_URL = "http://127.0.0.1:17493"
_DONE = "completed"
_FAILED = {"failed", "error"}


_CANCEL_TIMEOUT = 5.0


class VoiceboxError(RuntimeError):
    """Any Voicebox failure, with a message the user can act on."""


class VoiceboxTimeout(VoiceboxError):
    """A generation did not finish within the timeout (it was cancelled)."""


class _WaitTimeout(VoiceboxError):
    """Internal: polling exceeded the timeout (re-raised as a plain VoiceboxError after cancelling)."""


class VoiceboxClient:
    """Implements :class:`autoclipper.interfaces.VoiceboxAPI`.

    ``transport`` lets tests pass ``httpx.MockTransport``; ``sleep`` and ``clock`` make polling
    deterministic and instant in tests.
    """

    def __init__(self, url: str = DEFAULT_URL, *, poll_interval: float = 1.0,
                 timeout: float = 600.0, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.poll_interval = max(0.0, poll_interval)
        self.timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._http = httpx.Client(
            base_url=self.url,
            transport=transport,
            # Generation can be synchronous on some servers, so reads may take a while.
            timeout=httpx.Timeout(max(timeout, 1.0), connect=5.0),
        )

    @classmethod
    def from_settings(cls, s: VoiceboxSettings, **kw: Any) -> "VoiceboxClient":
        kw.setdefault("poll_interval", s.poll_interval)
        kw.setdefault("timeout", s.timeout_seconds)
        return cls(s.url, **kw)

    # -- lifecycle ---------------------------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "VoiceboxClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- low level ---------------------------------------------------------------------------
    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            resp = self._http.request(method, path, **kw)
        except httpx.ConnectError as e:
            raise VoiceboxError(f"Voicebox is not reachable at {self.url} - is it running?") from e
        except httpx.TimeoutException as e:
            raise VoiceboxError(f"Voicebox at {self.url} timed out on {method} {path}") from e
        except httpx.HTTPError as e:
            raise VoiceboxError(f"Voicebox request {method} {path} failed: {e}") from e
        if resp.status_code >= 400:
            raise VoiceboxError(
                f"Voicebox {method} {path} returned HTTP {resp.status_code}: {_error_detail(resp)}")
        return resp

    def _json(self, method: str, path: str, **kw: Any) -> Any:
        resp = self._request(method, path, **kw)
        try:
            return resp.json()
        except ValueError as e:
            raise VoiceboxError(
                f"Voicebox {method} {path} returned non-JSON: {resp.text[:200]!r}") from e

    # -- API ---------------------------------------------------------------------------------
    def health(self) -> None:
        self.list_profiles()

    def list_profiles(self) -> list[dict]:
        data = self._json("GET", "/profiles")
        if isinstance(data, dict):
            data = data.get("profiles", [])
        if not isinstance(data, list):
            raise VoiceboxError(f"unexpected /profiles response: {str(data)[:200]}")
        return [p for p in data if isinstance(p, dict)]

    def generate(self, text: str, *, profile_id: str, language: str, engine: str) -> str:
        if not text.strip():
            raise VoiceboxError("cannot generate voice for empty text")
        payload = {"text": text, "profile_id": profile_id, "language": language, "engine": engine}
        data = self._json("POST", "/generate", json=payload)
        gen_id: Any = None
        if isinstance(data, dict):
            gen_id = data.get("id")
            if gen_id in (None, ""):
                gen_id = data.get("generation_id")
        if gen_id in (None, ""):
            raise VoiceboxError(f"Voicebox /generate returned no id: {str(data)[:200]}")
        return str(gen_id)

    def cancel(self, gen_id: str) -> bool:
        """Ask Voicebox to stop a generation (POST /generate/{id}/cancel). Best effort: never raises."""
        try:
            resp = self._http.post(f"/generate/{gen_id}/cancel", timeout=_CANCEL_TIMEOUT)
            return resp.status_code < 400
        except Exception:  # noqa: BLE001 - the server may be gone; cancelling is only a courtesy
            return False

    def wait(self, gen_id: str, on_progress: ProgressFn | None = None, timeout: float | None = None) -> dict:
        """Poll /history/{id} until completed. On timeout or interrupt the generation is cancelled.

        Voicebox only reports generating/completed/failed, so the deadline runs from the start of
        waiting (right after POST /generate returned).
        """
        try:
            return self._wait(gen_id, on_progress, timeout)
        except _WaitTimeout as e:
            self.cancel(gen_id)
            raise VoiceboxTimeout(str(e)) from None
        except VoiceboxError:
            raise  # failed generation or transport error: nothing left to cancel
        except BaseException:
            self.cancel(gen_id)  # KeyboardInterrupt, progress callback errors, ...
            raise

    def _wait(self, gen_id: str, on_progress: ProgressFn | None, timeout: float | None = None) -> dict:
        limit = self.timeout if timeout is None else min(self.timeout, max(1.0, float(timeout)))
        deadline = self._clock() + limit
        while True:
            data = self._json("GET", f"/history/{gen_id}")
            if not isinstance(data, dict):
                raise VoiceboxError(f"unexpected /history/{gen_id} response: {str(data)[:200]}")
            status = str(data.get("status", "")).strip().lower()
            if status == _DONE:
                if on_progress:
                    on_progress(1.0, "voice generated")
                return data
            if status in _FAILED:
                msg = data.get("error") or data.get("message") or data.get("detail") or "no details"
                raise VoiceboxError(f"Voicebox generation {gen_id} failed: {msg}")
            if on_progress:
                on_progress(_fraction(data.get("progress")), status or "waiting")
            if self._clock() >= deadline:
                raise _WaitTimeout(
                    f"Voicebox generation {gen_id} did not finish within {limit:.0f}s "
                    f"(last status: {status or 'unknown'})")
            self._sleep(self.poll_interval)

    def download(self, gen_id: str, dst: Path) -> Path:
        resp = self._request("GET", f"/audio/{gen_id}")
        if not resp.content:
            raise VoiceboxError(f"Voicebox returned empty audio for generation {gen_id}")
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(resp.content)
        return dst

    def synthesize(self, text: str, *, profile_id: str, language: str, engine: str,
                   dst: Path, on_progress: ProgressFn | None = None, timeout: float | None = None) -> Path:
        """generate -> wait -> download. ``timeout`` (seconds, capped at the client's) limits this wait."""
        def sub(f: float, msg: str) -> None:
            if on_progress:
                on_progress(0.1 + 0.8 * f, msg)

        if on_progress:
            on_progress(0.0, "requesting voice")
        gen_id = self.generate(text, profile_id=profile_id, language=language, engine=engine)
        self.wait(gen_id, on_progress=sub, timeout=timeout)
        out = self.download(gen_id, dst)
        if on_progress:
            on_progress(1.0, "voice downloaded")
        return out


def _fraction(value: Any) -> float:
    """Server progress may be 0..1 or 0..100; anything else -> 0."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if f > 1.0:
        f /= 100.0
    return min(max(f, 0.0), 1.0)


def _error_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200] or resp.reason_phrase
    if isinstance(data, dict):
        for key in ("detail", "error", "message"):
            if data.get(key):
                return str(data[key])[:200]
    return str(data)[:200]
