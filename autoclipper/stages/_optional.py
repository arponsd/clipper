"""Private helper: pass newer optional keyword arguments only to callables that accept them.

LLM implementations written before an argument existed (older fakes, third-party clients) keep working:
the argument is simply not passed to them.
"""
from __future__ import annotations

import inspect
from typing import Any, Callable


def accepts(fn: Callable[..., Any], name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    # only a parameter declared by name: a **kwargs proxy may forward to an older implementation
    p = params.get(name)
    return p is not None and p.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)


def call_with_optional(fn: Callable[..., Any], *args: Any, optional: dict[str, Any] | None = None,
                       **kwargs: Any) -> Any:
    """``fn(*args, **kwargs, **<the optional ones fn takes>)``; optional values that are None or "" are
    never passed."""
    extra = {k: v for k, v in (optional or {}).items() if v not in (None, "") and accepts(fn, k)}
    return fn(*args, **kwargs, **extra)
