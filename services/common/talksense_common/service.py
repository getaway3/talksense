"""Сборка FastAPI-приложения сервиса: настройки, логирование с correlationId,
проверка сервисного токена для internal-эндпоинтов, health."""
from __future__ import annotations

import logging
import os
import uuid

from fastapi import FastAPI, Header, Request

from . import problems
from .problems import ProblemError

from .context import correlation_id  # noqa: E402


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id.get()
        return True


def setup_logging(service: str) -> None:
    root = logging.getLogger()
    if getattr(root, "_talksense", False):
        return
    handler = logging.StreamHandler()
    handler.addFilter(_CorrelationFilter())
    handler.setFormatter(logging.Formatter(
        f"%(asctime)s %(levelname)-7s {service} [cid=%(correlation_id)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    root._talksense = True  # type: ignore[attr-defined]
    logging.getLogger("aio_pika").setLevel(logging.WARNING)
    logging.getLogger("aiormq").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


SERVICE_TOKEN = os.getenv("SERVICE_TOKEN", "dev-service-token")


def create_base_app(service: str, title: str, lifespan=None) -> FastAPI:
    setup_logging(service)
    app = FastAPI(title=service, summary=title, version="1.0.0", lifespan=lifespan)
    problems.install(app)

    @app.middleware("http")
    async def _cid(request: Request, call_next):
        cid = request.headers.get("x-correlation-id") or str(uuid.uuid4())
        token = correlation_id.set(cid)
        try:
            response = await call_next(request)
        finally:
            correlation_id.reset(token)
        response.headers["X-Correlation-Id"] = cid
        return response

    @app.get("/health", tags=["ops"])
    async def health():
        return {"status": "ok", "service": service}

    return app


def require_service_token(authorization: str | None = Header(default=None)) -> None:
    """Internal-эндпоинты доступны только другим сервисам (в проде — JWT client credentials Keycloak,
    здесь — общий сервисный токен из переменной SERVICE_TOKEN)."""
    if authorization != f"Bearer {SERVICE_TOKEN}":
        raise ProblemError(401, "Нужен сервисный токен для internal-эндпоинта")
