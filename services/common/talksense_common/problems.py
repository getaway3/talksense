"""Единая модель ошибок: Problem Details (RFC 9457 / RFC 7807)."""
from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("talksense.problems")
BASE = "https://talksense.local/problems/"

TITLES = {
    400: "Validation error", 401: "Unauthorized", 403: "Forbidden", 404: "Not found",
    405: "Method not allowed", 409: "Conflict", 410: "Gone", 413: "Payload too large",
    422: "Idempotency key reuse", 500: "Internal error", 503: "Service unavailable",
}


class ProblemError(Exception):
    """Бросается из кода сервиса, превращается в application/problem+json."""

    def __init__(self, status: int, detail: str, title: str | None = None, errors: list | None = None,
                 type_: str | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.title = title or TITLES.get(status, "Error")
        self.errors = errors
        self.type = type_ or BASE + self.title.lower().replace(" ", "-")


def problem_body(request: Request, status: int, title: str, detail: str, type_: str | None = None,
                 errors: list | None = None) -> dict:
    body = {
        "type": type_ or BASE + title.lower().replace(" ", "-"),
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "traceId": request.headers.get("x-correlation-id") or uuid.uuid4().hex,
    }
    if errors:
        body["errors"] = errors
    return body


def problem_response(request: Request, status: int, title: str, detail: str, **kw) -> JSONResponse:
    return JSONResponse(problem_body(request, status, title, detail, **kw), status_code=status,
                        media_type="application/problem+json")


def install(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(request: Request, exc: ProblemError):
        return problem_response(request, exc.status, exc.title, exc.detail, type_=exc.type, errors=exc.errors)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errors = [{"field": ".".join(str(p) for p in e["loc"] if p not in ("body", "query", "path", "header")),
                   "message": e["msg"]} for e in exc.errors()]
        return problem_response(request, 400, "Validation error", "Запрос не прошёл валидацию", errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        title = TITLES.get(exc.status_code, "Error")
        return problem_response(request, exc.status_code, title, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        log.exception("unhandled error")
        return problem_response(request, 500, "Internal error", "Внутренняя ошибка сервиса")
