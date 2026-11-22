"""Uniform JSON errors for the web API (CONTRACT_WEB section 3).

Every non-2xx response body is ``{"message": str, "errors": {"dotted.path": [str, ...]}}``. Validation
errors never echo the raw input, and unexpected exceptions never leak a traceback.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("autoclipper.web")

INTERNAL_ERROR = "internal error (see server log)"
VALIDATION_FAILED = "validation failed"
_LOC_PREFIXES = {"body", "query", "path", "header", "cookie"}


class APIError(Exception):
    def __init__(self, status: int, message: str, errors: dict[str, list[str]] | None = None,
                 headers: Mapping[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.errors = errors or {}
        self.headers = dict(headers) if headers else None


def error_body(message: str, errors: Mapping[str, list[str]] | None = None) -> dict[str, Any]:
    return {"message": message, "errors": dict(errors or {})}


def error_response(status: int, message: str, errors: Mapping[str, list[str]] | None = None,
                   headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(error_body(message, errors), status_code=status, headers=dict(headers or {}))


def validation_errors(errs: Iterable[Mapping[str, Any]], prefix: str = "") -> dict[str, list[str]]:
    """pydantic/FastAPI error list -> {dotted.loc: [msg]} without the body/query/path prefix or input."""
    out: dict[str, list[str]] = {}
    for err in errs:
        loc = [str(x) for x in (err.get("loc") or ())]
        etype = str(err.get("type", ""))
        if loc and loc[0] in _LOC_PREFIXES:
            head = loc.pop(0)
            if head == "body" and etype == "json_invalid":
                loc = []  # loc is the byte offset of the syntax error: report it on the body
            if not loc:
                loc = ["body"] if head == "body" else loc
        key = ".".join(loc) if loc else (prefix or "body")
        if prefix and loc:
            key = f"{prefix}.{key}"
        msg = str(err.get("msg") or "invalid value")
        if len(msg) > 300:
            msg = msg[:297] + "..."
        out.setdefault(key, []).append(msg)
    return out


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(_request: Request, exc: APIError) -> JSONResponse:
        return error_response(exc.status, exc.message, exc.errors, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _request_validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(422, VALIDATION_FAILED, validation_errors(exc.errors()))

    @app.exception_handler(ValidationError)
    async def _pydantic_validation(_request: Request, exc: ValidationError) -> JSONResponse:
        return error_response(422, VALIDATION_FAILED, validation_errors(exc.errors()))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "request failed"
        if exc.status_code == 400 and detail == "There was an error parsing the body":
            # json.loads raised something other than JSONDecodeError (a 5000-digit int, very deep nesting):
            # still malformed JSON for the client (API-2).
            return error_response(422, VALIDATION_FAILED, {"body": ["invalid JSON"]})
        if exc.status_code == 404 and detail == "Not Found":
            detail = "not found"
        elif exc.status_code == 405:
            detail = "method not allowed"
        return error_response(exc.status_code, detail, {}, getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return error_response(500, INTERNAL_ERROR)
