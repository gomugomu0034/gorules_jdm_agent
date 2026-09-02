"""Structured errors shared by every route."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """Raised by route handlers to produce the standard error envelope."""

    def __init__(self, code: str, message: str, status: int = 400, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail


def not_found(what: str, ident: str) -> ApiError:
    return ApiError(f"{what.upper()}_NOT_FOUND", f"No {what.replace('_', ' ')} with id {ident!r}.", 404)


def _envelope(request: Request, code: str, message: str, detail: Any = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "detail": detail,
            "request_id": getattr(request.state, "request_id", None),
        }
    }


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status,
            content=_envelope(request, exc.code, exc.message, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=_envelope(
                request,
                "VALIDATION_ERROR",
                "The request body failed validation.",
                [
                    {"path": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                    for e in exc.errors()
                ],
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 429: "RATE_LIMITED"}
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(request, codes.get(exc.status_code, "HTTP_ERROR"), str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=_envelope(request, "INTERNAL", "An unexpected error occurred."),
        )
