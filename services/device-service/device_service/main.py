"""device-service — реестр устройств-регистраторов (контракт: api/device-service.yaml).

Вызывает venue-service синхронно (S1) при регистрации, подписан на venue.deactivated.
Статус «в сети» в проекте хранится в Redis с TTL; здесь — колонка last_seen_at
(онлайн, если heartbeat был не позже ONLINE_TTL секунд назад).
"""
from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, Query
from pydantic import BaseModel, Field

from talksense_common import idempotency as idem
from talksense_common.db import Database, page
from talksense_common.events import Envelope, EventBus, Outbox, already_processed, make_bus, now_iso
from talksense_common.http import DependencyUnavailable, ServiceClient
from talksense_common.problems import ProblemError
from talksense_common.service import SERVICE_TOKEN, create_base_app, require_service_token

SERVICE = "device-service"
ONLINE_TTL = int(os.getenv("ONLINE_TTL", "120"))
DEMO_DEVICE = "3b241101-e2bb-4255-8caf-4136c566a962"
DEMO_TOKEN = "dvt_demo_5f8c1e0a9b7d4c2e8f6a3b1d"

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY, serial_number TEXT NOT NULL UNIQUE, venue_id TEXT NOT NULL, placement TEXT,
    status TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, firmware_version TEXT, last_seen_at TEXT,
    created_at TEXT NOT NULL
);
"""


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RegisterDeviceRequest(BaseModel):
    serialNumber: str = Field(pattern=r"^TS-REC-[0-9]{6}$")
    venueId: uuid.UUID
    placement: str | None = Field(default=None, max_length=100)


class Heartbeat(BaseModel):
    firmwareVersion: str
    freeStorageMb: int | None = None
    bufferedChunks: int | None = None
    micLevelDb: float | None = None


class VerifyDeviceRequest(BaseModel):
    deviceToken: str


def is_online(last_seen: str | None) -> bool:
    if not last_seen:
        return False
    seen = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - seen <= timedelta(seconds=ONLINE_TTL)


def device_json(r) -> dict:
    return {"id": r["id"], "serialNumber": r["serial_number"], "venueId": r["venue_id"], "placement": r["placement"],
            "status": r["status"], "online": r["status"] == "ACTIVE" and is_online(r["last_seen_at"]),
            "firmwareVersion": r["firmware_version"], "lastSeenAt": r["last_seen_at"], "createdAt": r["created_at"]}


class Ctx:
    def __init__(self, db_path: str | None = None, bus: EventBus | None = None, venue_transport=None):
        self.db = Database(db_path or os.getenv("DB_PATH", "data/device.db"), SCHEMA)
        self.bus = bus or make_bus(os.getenv("RABBITMQ_URL"))
        self.outbox = Outbox(self.db, self.bus, SERVICE)
        self.venue = ServiceClient("venue-service", os.getenv("VENUE_URL", "http://venue-service:8000"),
                                   SERVICE_TOKEN, transport=venue_transport)

    async def start(self, relay: bool = True) -> None:
        await self.bus.start()
        await self.bus.subscribe(SERVICE, ["venue.deactivated"], self.on_venue_deactivated)
        if relay:
            self.outbox.start()

    async def on_venue_deactivated(self, env: Envelope, attempt: int) -> None:
        """A7: закрыли точку — отзываем все её устройства (идемпотентно)."""
        with self.db.tx() as c:
            if already_processed(c, env.eventId):
                return
            c.execute("UPDATE devices SET status = 'REVOKED' WHERE venue_id = ? AND status != 'REVOKED'",
                      (env.payload["venueId"],))

    def seed_demo(self) -> None:
        if self.db.one("SELECT 1 FROM devices WHERE id = ?", (DEMO_DEVICE,)):
            return
        with self.db.tx() as c:
            c.execute("INSERT INTO devices VALUES (?,?,?,?,?,?,?,?,?)",
                      (DEMO_DEVICE, "TS-REC-000123", "7c9e6679-7425-40de-944b-e07fc1f90ae7", "Касса №1",
                       "ACTIVE", token_hash(DEMO_TOKEN), "1.4.2", None, now_iso()))


def create_app(ctx: Ctx | None = None):
    ctx = ctx or Ctx()
    if os.getenv("SEED_DEMO", "0") == "1":
        ctx.seed_demo()

    @asynccontextmanager
    async def lifespan(_app):
        await ctx.start()
        yield
        await ctx.outbox.stop()
        await ctx.bus.close()

    app = create_base_app(SERVICE, "Реестр устройств-регистраторов", lifespan)
    app.state.ctx = ctx
    db = ctx.db

    def get_device(device_id: str):
        r = db.one("SELECT * FROM devices WHERE id = ?", (device_id,))
        if not r:
            raise ProblemError(404, f"Устройство {device_id} не найдено")
        return r

    def by_token(token: str | None):
        if not token:
            raise ProblemError(401, "Нет заголовка X-Device-Token")
        r = db.one("SELECT * FROM devices WHERE token_hash = ?", (token_hash(token),))
        if not r:
            raise ProblemError(401, "Неизвестный токен устройства")
        if r["status"] != "ACTIVE":
            raise ProblemError(403, "Устройство отозвано")
        return r

    @app.get("/api/v1/devices", tags=["public"])
    async def list_devices(venueId: uuid.UUID, page_no: int = Query(0, alias="page", ge=0),
                           size: int = Query(20, ge=1, le=100)):
        total = db.one("SELECT count(*) AS c FROM devices WHERE venue_id = ?", (str(venueId),))["c"]
        rows = db.query("SELECT * FROM devices WHERE venue_id = ? ORDER BY created_at LIMIT ? OFFSET ?",
                        (str(venueId), size, page_no * size))
        return page([device_json(r) for r in rows], page_no, size, total)

    @app.post("/api/v1/devices", status_code=201, tags=["public"])
    async def register(body: RegisterDeviceRequest, idempotency_key: str | None = Header(None)):
        key = idem.require_key(idempotency_key)
        h = idem.request_hash(body.model_dump(mode="json"))
        if (replay := idem.lookup(db, "register_device", key, h)) is not None:
            return replay
        # S1: синхронно проверяем, что заведение существует и активно
        try:
            resp = await ctx.venue.get(f"/internal/v1/venues/{body.venueId}")
        except DependencyUnavailable as exc:
            raise ProblemError(503, f"venue-service недоступен: {exc}") from exc
        if resp.status_code == 404:
            raise ProblemError(404, f"Заведение {body.venueId} не найдено")
        if resp.status_code != 200:
            raise ProblemError(503, f"venue-service ответил {resp.status_code}")
        if resp.json().get("status") != "ACTIVE":
            raise ProblemError(409, "Заведение отключено — нельзя привязать устройство")
        if db.one("SELECT 1 FROM devices WHERE serial_number = ? AND status != 'REVOKED'", (body.serialNumber,)):
            raise ProblemError(409, f"Устройство {body.serialNumber} уже активно")
        token = "dvt_" + secrets.token_hex(16)
        did = str(uuid.uuid4())
        with db.tx() as c:
            c.execute("DELETE FROM devices WHERE serial_number = ? AND status = 'REVOKED'", (body.serialNumber,))
            c.execute("INSERT INTO devices VALUES (?,?,?,?,?,?,?,?,?)",
                      (did, body.serialNumber, str(body.venueId), body.placement, "ACTIVE", token_hash(token),
                       None, None, now_iso()))
            result = {"device": device_json(c.execute("SELECT * FROM devices WHERE id = ?", (did,)).fetchone()),
                      "deviceToken": token}
            idem.store(c, "register_device", key, h, 201, result)
        return result

    @app.get("/api/v1/devices/{device_id}", tags=["public"])
    async def get_device_ep(device_id: str):
        return device_json(get_device(device_id))

    @app.post("/api/v1/devices/{device_id}/revoke", tags=["public"])
    async def revoke(device_id: str):
        get_device(device_id)
        db.execute("UPDATE devices SET status = 'REVOKED' WHERE id = ?", (device_id,))
        return device_json(get_device(device_id))

    @app.post("/device/v1/heartbeat", status_code=204, tags=["public"])
    async def heartbeat(body: Heartbeat, x_device_token: str | None = Header(None)):
        d = by_token(x_device_token)
        db.execute("UPDATE devices SET last_seen_at = ?, firmware_version = ? WHERE id = ?",
                   (now_iso(), body.firmwareVersion, d["id"]))

    @app.post("/internal/v1/devices/verify", tags=["internal"], dependencies=[Depends(require_service_token)])
    async def verify(body: VerifyDeviceRequest):
        d = by_token(body.deviceToken)
        return {"deviceId": d["id"], "venueId": d["venue_id"], "status": d["status"]}

    return app


app = create_app() if os.getenv("TALKSENSE_NO_AUTOAPP") != "1" else None
