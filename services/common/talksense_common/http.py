"""Межсервисные синхронные вызовы: таймауты, повторы для GET, простой circuit breaker,
проброс X-Correlation-Id и сервисного токена."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger("talksense.http")


class DependencyUnavailable(Exception):
    """Сервис-зависимость не ответил (таймаут, отказ соединения, 5xx, открыт breaker)."""


class CircuitBreaker:
    def __init__(self, threshold: int = 5, reset_after: float = 15.0):
        self.threshold, self.reset_after = threshold, reset_after
        self.failures, self.opened_at = 0, 0.0

    def allow(self) -> bool:
        if self.failures < self.threshold:
            return True
        if time.monotonic() - self.opened_at > self.reset_after:  # half-open: пропускаем пробный запрос
            return True
        return False

    def success(self) -> None:
        self.failures = 0

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()


class ServiceClient:
    def __init__(self, name: str, base_url: str, service_token: str, timeout: float = 2.0,
                 transport: httpx.AsyncBaseTransport | None = None, retries: int = 2):
        self.name = name
        self.retries = retries
        self.breaker = CircuitBreaker()
        self.client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport,
                                        headers={"Authorization": f"Bearer {service_token}"})

    async def request(self, method: str, url: str, correlation_id: str | None = None, **kw) -> httpx.Response:
        if not self.breaker.allow():
            raise DependencyUnavailable(f"{self.name}: circuit breaker открыт")
        headers = kw.pop("headers", {}) or {}
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id
        attempts = 1 + (self.retries if method == "GET" else 0)  # повторяем только безопасные запросы
        last: Exception | None = None
        for i in range(attempts):
            try:
                resp = await self.client.request(method, url, headers=headers, **kw)
                if resp.status_code >= 500:
                    raise DependencyUnavailable(f"{self.name}: HTTP {resp.status_code}")
                self.breaker.success()
                return resp
            except (httpx.TransportError, DependencyUnavailable) as exc:
                last = exc
                log.warning("%s %s %s: %s (попытка %s/%s)", self.name, method, url, exc, i + 1, attempts)
                if i + 1 < attempts:
                    await asyncio.sleep(0.2 * (i + 1))
        self.breaker.failure()
        raise DependencyUnavailable(str(last))

    async def get(self, url: str, **kw) -> httpx.Response:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw) -> httpx.Response:
        return await self.request("POST", url, **kw)

    async def aclose(self) -> None:
        await self.client.aclose()
