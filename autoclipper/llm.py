"""Claude client: structured JSON output via the official anthropic SDK.

`AnthropicLLM` implements `interfaces.LLM`. All HTTP goes through `complete_json`, which sends
`output_config` (effort + json_schema format), optionally through the server-side refusal fallback
beta, checks `stop_reason` before reading content and parses the joined text blocks as JSON.

Tests inject a fake `client` (anything exposing `.messages.create` / `.beta.messages.create`).
"""
from __future__ import annotations

import json
import os
from typing import Any

from .config import LLMSettings
from .models import ClipScript, Moment

FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_MAX_TOKENS = 16000
SHORTEN_MAX_TOKENS = 8000
# The SDK refuses non-streaming calls it expects to take longer than ~10 min (max_tokens above
# about 21333) unless an explicit timeout is passed.
_NON_STREAMING_TOKEN_LIMIT = 21333
_LONG_REQUEST_TIMEOUT = 1800.0  # seconds

LANGUAGE_NAMES = {"en": "English", "hi": "Hindi"}


class LLMError(RuntimeError):
    """Any failure talking to the LLM or interpreting its answer."""


class LLMRefusal(LLMError):
    """The model declined the request (stop_reason == "refusal")."""


class LLMTruncated(LLMError):
    """The answer hit max_tokens before the JSON was complete."""


# --------------------------------------------------------------------------- schemas
# Structured output: every object has additionalProperties false and a full required list.

MOMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number", "description": "start time in seconds"},
                    "end": {"type": "number", "description": "end time in seconds"},
                    "hook": {"type": "string", "description": "the attention-grabbing hook line"},
                    "reason": {"type": "string", "description": "why this moment can go viral"},
                    "score": {"type": "number", "description": "virality score 0..1"},
                },
                "required": ["start", "end", "hook", "reason", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}

SCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "narration": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["narration", "title", "description", "hashtags"],
    "additionalProperties": False,
}

SHORTEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"narration": {"type": "string"}},
    "required": ["narration"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- prompts

MOMENTS_SYSTEM = (
    "You are a senior short-form video editor who finds the most viral moments in long videos "
    "for YouTube Shorts, Instagram Reels and Facebook Reels. You pick self-contained moments with a "
    "strong hook in the first seconds, a clear payoff, emotion, surprise, humor, a bold claim or "
    "useful insight. A moment must make sense without the rest of the video. Use only timestamps "
    "that exist in the transcript."
)

SCRIPT_SYSTEM = (
    "You write voice-over NARRATION for vertical short videos. The narration is NEW commentary "
    "spoken over the clip by a narrator: it hooks the viewer in the first sentence, explains or "
    "reacts to what is happening and ends with a punchy line. It is NOT a translation, dub or "
    "transcript of what the people in the clip say. Write plain spoken text only: no stage "
    "directions, no emojis, no speaker labels, no hashtags inside the narration."
)

SHORTEN_SYSTEM = (
    "You tighten voice-over narration for short videos. Keep the language, script, meaning, hook "
    "and tone; remove filler. Return plain spoken text only."
)


LENGTHEN_SYSTEM = (
    "You expand voice-over narration for short videos that is too short for its clip. Keep the "
    "language, script, meaning, hook, tone and ending; add vivid, relevant detail or context about "
    "the moment instead of filler or repetition. Return plain spoken text only."
)


def language_rules(lang: str) -> str:
    if lang == "hi":
        return (
            "Write everything (narration, title, description) in natural, conversational Hindi as "
            "people actually speak it, in Devanagari script. Common English loanwords are fine but "
            "must also be written in Devanagari. Never use romanized Hindi (Hinglish in Latin letters). "
            "Use standard Hindi spelling (for example अट्ठाईस, not अठ्ठाईस), and do not add Urdu "
            "nuktas (क़, ज़, फ़) unless the word is normally written with them."
        )
    name = LANGUAGE_NAMES.get(lang, lang)
    return f"Write everything (narration, title, description) in natural, conversational {name}."


# --------------------------------------------------------------------------- client


def _stop_details_text(details: Any) -> str:
    if details is None:
        return ""
    parts = []
    for attr in ("category", "explanation"):
        val = getattr(details, attr, None)
        if val is None and isinstance(details, dict):
            val = details.get(attr)
        if val:
            parts.append(f"{attr}: {val}")
    return "; ".join(parts)


def has_credentials(client: Any) -> bool:
    """True when the SDK client resolved an API key, auth token, or an `ant auth login` profile."""
    return bool(getattr(client, "api_key", None) or getattr(client, "auth_token", None)
                or getattr(client, "credentials", None) is not None)


def build_client(settings: LLMSettings) -> Any:
    """Anthropic client from the env key named by settings.api_key_env, else the `ant auth login` profile."""
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover - dependency is declared
        raise LLMError("the 'anthropic' package is not installed: run `uv sync`") from e
    api_key = os.environ.get(settings.api_key_env, "").strip()
    # Zero-arg construction lets the SDK resolve an `ant auth login` profile.
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    if not has_credentials(client):
        raise LLMError(f"no Anthropic credentials: run `ant auth login`, "
                       f"or put {settings.api_key_env}=... in .env")
    return client


class AnthropicLLM:
    """`interfaces.LLM` backed by Claude with structured JSON output."""

    def __init__(self, settings: LLMSettings, client: Any | None = None):
        self.settings = settings
        if client is None:
            client = build_client(settings)
        self.client = client

    # ---- low level

    @property
    def max_tokens(self) -> int:
        """Token budget for moments/scripts: llm.max_tokens from settings (default 16000)."""
        return int(getattr(self.settings, "max_tokens", DEFAULT_MAX_TOKENS) or DEFAULT_MAX_TOKENS)

    def complete_json(self, *, system: str, user: str, schema: dict,
                      max_tokens: int | None = None) -> dict:
        if max_tokens is None:
            max_tokens = self.max_tokens
        kwargs: dict[str, Any] = dict(
            model=self.settings.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.settings.effort,
                           "format": {"type": "json_schema", "schema": schema}},
        )
        if max_tokens > _NON_STREAMING_TOKEN_LIMIT:
            kwargs["timeout"] = _LONG_REQUEST_TIMEOUT
        try:
            if self.settings.use_fallbacks:
                resp = self.client.beta.messages.create(**kwargs, betas=[FALLBACK_BETA],
                                                        fallbacks="default")
            else:
                resp = self.client.messages.create(**kwargs)
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001 - mapped to typed errors below
            raise _map_sdk_error(e, self.settings) from e

        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            detail = _stop_details_text(getattr(resp, "stop_details", None))
            raise LLMRefusal("Claude declined this request" + (f" ({detail})" if detail else "")
                             + "; try a different video or moment")
        if stop == "max_tokens":
            raise LLMTruncated(f"Claude's answer was cut off at max_tokens={max_tokens}; raise it with "
                               "`autoclipper settings set llm.max_tokens=32000` or lower llm.effort")

        text = "".join(getattr(b, "text", "") or "" for b in (getattr(resp, "content", None) or [])
                       if getattr(b, "type", None) == "text").strip()
        if not text:
            raise LLMError(f"Claude returned no text content (stop_reason={stop!r})")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"Claude returned invalid JSON: {text[:200]!r}") from e
        if not isinstance(data, dict):
            raise LLMError(f"Claude returned JSON that is not an object: {text[:200]!r}")
        return data

    # ---- interfaces.LLM

    def pick_moments(self, transcript_text: str, *, count: int, min_seconds: float,
                     max_seconds: float, video_duration: float) -> list[Moment]:
        user = (
            f"Video duration: {video_duration:.1f} seconds.\n"
            f"Find the {count} most viral moments. Each moment must be between {min_seconds:g} and "
            f"{max_seconds:g} seconds long, must not overlap another moment, and should start and "
            "end on sentence boundaries from the transcript. For each give start and end in seconds, "
            "the hook (the line that grabs attention), the reason it works, and a score from 0 to 1. "
            "Order by score, best first.\n\n"
            "Transcript, one segment per line as [start-end] text:\n"
            f"<transcript>\n{transcript_text}\n</transcript>"
        )
        data = self.complete_json(system=MOMENTS_SYSTEM, user=user, schema=MOMENTS_SCHEMA)
        out: list[Moment] = []
        for item in data.get("moments") or []:
            try:
                out.append(Moment.model_validate(item))
            except Exception:  # noqa: BLE001 - skip malformed items; validation stage filters
                continue
        return out

    def write_script(self, *, lang: str, clip_transcript: str, moment: Moment,
                     duration: float, target_words: int,
                     target_seconds: float | None = None) -> ClipScript:
        user = (
            f"{language_rules(lang)}\n\n"
            f"{_length_instruction(lang, duration, target_words, target_seconds)}\n"
            "Also write: a title of at most 70 characters that makes people click, a 1-3 sentence "
            "description, and 3 to 8 relevant hashtags (each starting with #, no spaces).\n\n"
            f"Why this moment was picked: {moment.reason or '-'}\n"
            f"Hook: {moment.hook or '-'}\n\n"
            "What is said in the clip (for context only; do not translate or repeat it):\n"
            f"<clip_transcript>\n{clip_transcript}\n</clip_transcript>"
        )
        data = self.complete_json(system=SCRIPT_SYSTEM, user=user, schema=SCRIPT_SCHEMA)
        try:
            return ClipScript.model_validate(data)
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Claude returned an invalid script: {str(data)[:200]}") from e

    def shorten(self, *, lang: str, narration: str, max_words: int) -> str:
        user = (
            f"{language_rules(lang)}\n\n"
            f"Rewrite this narration so it has at most {max_words} words "
            f"(it currently has {len(narration.split())}).\n\n"
            f"<narration>\n{narration}\n</narration>"
        )
        data = self.complete_json(system=SHORTEN_SYSTEM, user=user, schema=SHORTEN_SCHEMA,
                                  max_tokens=min(SHORTEN_MAX_TOKENS, self.max_tokens))
        text = str(data.get("narration") or "").strip()
        if not text:
            raise LLMError("Claude returned an empty shortened narration")
        return text


    def lengthen(self, *, lang: str, narration: str, target_words: int) -> str:
        """Optional `interfaces.Lengthener`: a longer narration of about target_words words."""
        lo, hi = word_range(target_words)
        user = (
            f"{language_rules(lang)}\n\n"
            f"This narration is too short for its clip: it has {len(narration.split())} words. "
            f"Rewrite it so it has between {lo} and {hi} words (target {target_words}). Keep the same "
            "hook and ending line.\n\n"
            f"<narration>\n{narration}\n</narration>"
        )
        data = self.complete_json(system=LENGTHEN_SYSTEM, user=user, schema=SHORTEN_SCHEMA,
                                  max_tokens=min(SHORTEN_MAX_TOKENS, self.max_tokens))
        text = str(data.get("narration") or "").strip()
        if not text:
            raise LLMError("Claude returned an empty lengthened narration")
        return text


def word_range(target_words: int) -> tuple[int, int]:
    """Accepted word range around the target: round(target*0.95) .. round(target*1.05)."""
    return max(1, round(target_words * 0.95)), max(1, round(target_words * 1.05))


def _length_instruction(lang: str, duration: float, target_words: int,
                        target_seconds: float | None) -> str:
    if target_seconds is None or target_seconds <= 0:  # step-1 wording
        return (f"Clip length: {duration:.1f} seconds. Write a narration of about {target_words} words "
                f"(never more than {int(target_words * 1.1)}) so it fits the clip when read aloud at a "
                "natural pace.")
    lo, hi = word_range(target_words)
    wps = target_words / target_seconds
    name = LANGUAGE_NAMES.get(lang, lang)
    return (f"Clip length: {duration:.1f} seconds. The narration must be spoken for about "
            f"{target_seconds:.1f} seconds, i.e. it fills most of the clip and ends just before it does. "
            f"The {name} narrator speaks about {wps:.2f} words per second, so write between {lo} and "
            f"{hi} words (target {target_words}). Count the words: too short leaves the end of the "
            "clip silent, too long gets cut.")


def _map_sdk_error(e: Exception, settings: LLMSettings) -> LLMError:
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return LLMError(f"LLM request failed: {e}")
    if isinstance(e, anthropic.AuthenticationError):
        return LLMError(f"Anthropic rejected the credentials ({settings.api_key_env} in .env, "
                        f"or the `ant auth login` profile); check them or run `ant auth login` again")
    if isinstance(e, anthropic.PermissionDeniedError):
        return LLMError(f"Anthropic denied access to model {settings.model!r}: {e}")
    if isinstance(e, anthropic.NotFoundError):
        return LLMError(f"model {settings.model!r} not found; check llm.model in settings")
    if isinstance(e, anthropic.RateLimitError):
        return LLMError("Anthropic rate limit reached; wait a minute and rerun the job (it resumes)")
    if isinstance(e, anthropic.BadRequestError):
        return LLMError(f"Anthropic rejected the request (400): {e}")
    if isinstance(e, anthropic.APIStatusError):
        return LLMError(f"Anthropic API error {getattr(e, 'status_code', '?')}: {e}")
    if isinstance(e, anthropic.APITimeoutError):
        return LLMError("Anthropic API request timed out; rerun the job (it resumes)")
    if isinstance(e, anthropic.APIConnectionError):
        return LLMError("could not connect to the Anthropic API; check your internet connection")
    return LLMError(f"LLM request failed: {type(e).__name__}: {e}")
