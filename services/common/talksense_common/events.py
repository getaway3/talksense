"""Асинхронные события: единый конверт, шина (RabbitMQ / in-memory), outbox, дедупликация.

Топология RabbitMQ (совпадает с api/asyncapi.yaml):
  exchange  talksense.events (topic)            — сюда публикуются все события
  queue     <service>.events                    — очередь подписчика, привязана к нужным routing key
  queue     <service>.events.retry              — отложенные повторы (TTL), затем обратно в <service>.events
  queue     <service>.events.dlq                — сообщения, которые не удалось обработать (dead letter)
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

from .context import correlation_id
from .db import Database, dumps

log = logging.getLogger("talksense.events")
EXCHANGE = "talksense.events"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Envelope(BaseModel):
    eventId: str = Field(default_factory=lambda: str(uuid.uuid4()))
    eventType: str
    schemaVersion: int = 1
    occurredAt: str = Field(default_factory=now_iso)
    correlationId: str
    producer: str
    payload: dict


class RetryLater(Exception):
    """Временная ошибка: обработать сообщение повторно позже (с задержкой)."""


Handler = Callable[[Envelope, int], Awaitable[None]]  # (событие, номер попытки с 1)


class EventBus:
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def publish(self, routing_key: str, body: str) -> None: raise NotImplementedError
    async def subscribe(self, service: str, routing_keys: list[str], handler: Handler) -> None:
        raise NotImplementedError


# --------------------------------------------------------------------------- in-memory
class InMemoryBus(EventBus):
    """Брокер в памяти для тестов: детерминированная доставка через drain().

    Повторяет семантику RabbitMQ-топологии: подписка по routing key,
    повтор при RetryLater, DLQ после max_attempts, at-least-once (можно
    сымитировать повторную доставку через redeliver_last).
    """

    def __init__(self, max_attempts: int = 5):
        self.subs: list[tuple[str, list[str], Handler]] = []
        self.queue: deque[tuple[str, str, Handler, int]] = deque()
        self.published: list[tuple[str, dict]] = []
        self.dlq: list[tuple[str, dict]] = []
        self.max_attempts = max_attempts

    async def publish(self, routing_key: str, body: str) -> None:
        self.published.append((routing_key, json.loads(body)))
        for _service, keys, handler in self.subs:
            if any(_match(k, routing_key) for k in keys):
                self.queue.append((routing_key, body, handler, 1))

    async def subscribe(self, service: str, routing_keys: list[str], handler: Handler) -> None:
        self.subs.append((service, routing_keys, handler))

    async def drain(self, limit: int = 1000) -> int:
        n = 0
        while self.queue and n < limit:
            rk, body, handler, attempt = self.queue.popleft()
            n += 1
            try:
                env = Envelope.model_validate_json(body)
                correlation_id.set(env.correlationId)
                await handler(env, attempt)
            except RetryLater:
                if attempt >= self.max_attempts:
                    self.dlq.append((rk, json.loads(body)))
                else:
                    self.queue.append((rk, body, handler, attempt + 1))
            except Exception:  # noqa: BLE001 — как в брокере: ошибка обработчика -> DLQ
                log.exception("handler failed, message -> DLQ")
                self.dlq.append((rk, json.loads(body)))
        return n

    def events(self, routing_key: str | None = None) -> list[dict]:
        return [b for rk, b in self.published if routing_key is None or rk == routing_key]


def _match(pattern: str, key: str) -> bool:
    p, k = pattern.split("."), key.split(".")
    if "#" in p:
        return True
    return len(p) == len(k) and all(a in ("*", b) for a, b in zip(p, k, strict=True))


# --------------------------------------------------------------------------- RabbitMQ
class RabbitBus(EventBus):
    def __init__(self, url: str, retry_delays_ms: list[int] | None = None, max_attempts: int = 5):
        self.url = url
        self.retry_delays_ms = retry_delays_ms or [2000, 5000, 10000]
        self.max_attempts = max_attempts
        self._conn = None
        self._channel = None
        self._exchange = None

    async def start(self) -> None:
        import aio_pika

        for i in range(30):  # брокер может стартовать позже сервиса
            try:
                self._conn = await aio_pika.connect_robust(self.url)
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("RabbitMQ недоступен (%s), попытка %s", exc, i + 1)
                await asyncio.sleep(2)
        else:
            raise RuntimeError("RabbitMQ недоступен")
        self._channel = await self._conn.channel(publisher_confirms=True)
        await self._channel.set_qos(prefetch_count=10)
        self._exchange = await self._channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    async def publish(self, routing_key: str, body: str) -> None:
        import aio_pika

        env = json.loads(body)
        msg = aio_pika.Message(body.encode(), content_type="application/json",
                               delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                               message_id=env["eventId"], correlation_id=env["correlationId"],
                               type=env["eventType"])
        await self._exchange.publish(msg, routing_key=routing_key)

    async def subscribe(self, service: str, routing_keys: list[str], handler: Handler) -> None:
        import aio_pika

        ch = self._channel
        qname = f"{service}.events"
        await ch.declare_queue(f"{qname}.dlq", durable=True)
        queue = await ch.declare_queue(qname, durable=True, arguments={
            "x-dead-letter-exchange": "", "x-dead-letter-routing-key": f"{qname}.dlq"})
        # очередь отложенных повторов: по истечении TTL сообщение возвращается в основную очередь
        retry = await ch.declare_queue(f"{qname}.retry", durable=True, arguments={
            "x-dead-letter-exchange": "", "x-dead-letter-routing-key": qname})
        for key in routing_keys:
            await queue.bind(self._exchange, routing_key=key)

        async def on_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
            attempt = int((message.headers or {}).get("x-attempt", 1))
            try:
                env = Envelope.model_validate_json(message.body)
                correlation_id.set(env.correlationId)
                log.info("получено %s (попытка %s)", env.eventType, attempt)
                await handler(env, attempt)
                await message.ack()
            except RetryLater as exc:
                if attempt >= self.max_attempts:
                    log.error("исчерпаны повторы %s -> DLQ", message.message_id)
                    await message.reject(requeue=False)
                    return
                delay = self.retry_delays_ms[min(attempt - 1, len(self.retry_delays_ms) - 1)]
                log.warning("повтор %s через %s мс: %s", message.message_id, delay, exc)
                await ch.default_exchange.publish(
                    aio_pika.Message(message.body, headers={"x-attempt": attempt + 1}, expiration=delay / 1000,
                                     content_type="application/json", message_id=message.message_id,
                                     correlation_id=message.correlation_id, type=message.type,
                                     delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                    routing_key=retry.name)
                await message.ack()
            except Exception:  # noqa: BLE001
                log.exception("ошибка обработки %s -> DLQ", message.message_id)
                await message.reject(requeue=False)

        await queue.consume(on_message)


def make_bus(url: str | None) -> EventBus:
    return RabbitBus(url) if url else InMemoryBus()


# --------------------------------------------------------------------------- outbox
class Outbox:
    """Transactional Outbox: событие пишется в БД сервиса в той же транзакции,
    что и бизнес-изменение, а затем досылается в брокер. Если брокер недоступен,
    событие не теряется — его отправит фоновый цикл."""

    def __init__(self, db: Database, bus: EventBus, producer: str):
        self.db, self.bus, self.producer = db, bus, producer
        self._task: asyncio.Task | None = None
        self._flush_lock = asyncio.Lock()

    def add(self, conn, event_type: str, correlation_id: str, payload: dict) -> Envelope:
        env = Envelope(eventType=event_type, correlationId=correlation_id, producer=self.producer, payload=payload)
        conn.execute("INSERT INTO outbox (routing_key, body) VALUES (?, ?)", (event_type, env.model_dump_json()))
        return env

    async def flush(self) -> int:
        async with self._flush_lock:
            rows = self.db.query("SELECT id, routing_key, body FROM outbox WHERE published_at IS NULL ORDER BY id")
            sent = 0
            for r in rows:
                try:
                    await self.bus.publish(r["routing_key"], r["body"])
                    log.info("опубликовано %s", r["routing_key"])
                except Exception as exc:  # noqa: BLE001
                    log.warning("outbox: брокер недоступен (%s), повторим позже", exc)
                    break
                self.db.execute("UPDATE outbox SET published_at = datetime('now') WHERE id = ?", (r["id"],))
                sent += 1
            return sent

    def start(self, interval: float = 0.5) -> None:
        async def loop():
            while True:
                await asyncio.sleep(interval)
                try:
                    await self.flush()
                except Exception:  # noqa: BLE001
                    log.exception("outbox relay error")

        self._task = asyncio.create_task(loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()


def already_processed(conn, event_id: str) -> bool:
    """Дедупликация at-least-once доставки. Вызывать внутри транзакции обработчика:
    если событие уже обрабатывалось — вернуть True и ничего не делать."""
    cur = conn.execute("INSERT OR IGNORE INTO processed_events (event_id) VALUES (?)", (event_id,))
    return cur.rowcount == 0


__all__ = ["Envelope", "EventBus", "InMemoryBus", "RabbitBus", "Outbox", "RetryLater", "already_processed",
           "make_bus", "now_iso", "dumps"]
