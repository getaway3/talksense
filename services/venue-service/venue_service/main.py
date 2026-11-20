"""venue-service — заведения и меню (контракт: api/venue-service.yaml)."""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, Header, Query
from pydantic import BaseModel, Field

from talksense_common.db import Database, page
from talksense_common.events import EventBus, Outbox, make_bus, now_iso
from talksense_common import idempotency as idem
from talksense_common.problems import ProblemError
from talksense_common.service import create_base_app, require_service_token

SERVICE = "venue-service"
VENUE_TYPES = ("COFFEE_SHOP", "BAKERY", "RESTAURANT", "SHOP", "OTHER")

SCHEMA = """
CREATE TABLE IF NOT EXISTS venues (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, name TEXT NOT NULL, address TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'OTHER', timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
    working_hours TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS menu_items (
    id TEXT PRIMARY KEY, venue_id TEXT NOT NULL REFERENCES venues(id), name TEXT NOT NULL,
    category TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]', price REAL, available INTEGER NOT NULL DEFAULT 1
);
"""

DEMO_ORG = "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"
DEMO_VENUE = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


class CreateVenueRequest(BaseModel):
    organizationId: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(min_length=1, max_length=500)
    type: str = Field(default="OTHER", pattern="^(" + "|".join(VENUE_TYPES) + ")$")
    timezone: str = "Europe/Moscow"
    workingHours: str | None = None


class UpsertMenuItemRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=100)
    aliases: list[str] = []
    price: float | None = Field(default=None, ge=0)
    available: bool = True


def venue_json(r) -> dict:
    return {"id": r["id"], "organizationId": r["organization_id"], "name": r["name"], "address": r["address"],
            "type": r["type"], "timezone": r["timezone"], "workingHours": r["working_hours"],
            "status": r["status"], "createdAt": r["created_at"]}


def item_json(r) -> dict:
    import json
    return {"id": r["id"], "name": r["name"], "category": r["category"], "aliases": json.loads(r["aliases"]),
            "price": r["price"], "available": bool(r["available"])}


class Ctx:
    def __init__(self, db_path: str | None = None, bus: EventBus | None = None):
        self.db = Database(db_path or os.getenv("DB_PATH", "data/venue.db"), SCHEMA)
        self.bus = bus or make_bus(os.getenv("RABBITMQ_URL"))
        self.outbox = Outbox(self.db, self.bus, SERVICE)

    async def start(self, relay: bool = True) -> None:
        await self.bus.start()
        if relay:
            self.outbox.start()
            await self.outbox.flush()

    def seed_demo(self) -> None:
        import json
        if self.db.one("SELECT 1 FROM venues WHERE id = ?", (DEMO_VENUE,)):
            return
        with self.db.tx() as c:
            c.execute("INSERT INTO venues VALUES (?,?,?,?,?,?,?,?,?)",
                      (DEMO_VENUE, DEMO_ORG, "Кофейня «Зерно» на Садовой", "Ростов-на-Дону, ул. Большая Садовая, 10",
                       "COFFEE_SHOP", "Europe/Moscow", "08:00-22:00", "ACTIVE", now_iso()))
            for name, cat, aliases, price, avail in [
                ("Раф лавандовый", "Кофе", ["лавандовый раф", "раф с лавандой"], 320, 0),
                ("Капучино", "Кофе", ["капучинка"], 220, 1),
                ("Круассан миндальный", "Выпечка", ["миндальный круассан"], 190, 1),
                ("Чизкейк Сан-Себастьян", "Десерты", ["баскский чизкейк", "чизкейк"], 290, 1),
            ]:
                c.execute("INSERT INTO menu_items VALUES (?,?,?,?,?,?,?)",
                          (str(uuid.uuid4()), DEMO_VENUE, name, cat, json.dumps(aliases, ensure_ascii=False),
                           price, avail))


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

    app = create_base_app(SERVICE, "Заведения и меню", lifespan)
    app.state.ctx = ctx
    db = ctx.db

    def get_venue(venue_id: str):
        r = db.one("SELECT * FROM venues WHERE id = ?", (venue_id,))
        if not r:
            raise ProblemError(404, f"Заведение {venue_id} не найдено")
        return r

    # ------------------------------------------------------------------ public
    @app.get("/api/v1/venues", tags=["public"])
    async def list_venues(organizationId: uuid.UUID, status: str | None = Query(None, pattern="^(ACTIVE|DEACTIVATED)$"),
                          page_no: int = Query(0, alias="page", ge=0), size: int = Query(20, ge=1, le=100)):
        where, params = "organization_id = ?", [str(organizationId)]
        if status:
            where += " AND status = ?"
            params.append(status)
        total = db.one(f"SELECT count(*) AS c FROM venues WHERE {where}", tuple(params))["c"]
        rows = db.query(f"SELECT * FROM venues WHERE {where} ORDER BY created_at LIMIT ? OFFSET ?",
                        (*params, size, page_no * size))
        return page([venue_json(r) for r in rows], page_no, size, total)

    @app.post("/api/v1/venues", status_code=201, tags=["public"])
    async def create_venue(body: CreateVenueRequest, idempotency_key: str | None = Header(None)):
        key = idem.require_key(idempotency_key)
        h = idem.request_hash(body.model_dump(mode="json"))
        if (replay := idem.lookup(db, "create_venue", key, h)) is not None:
            return replay
        vid = str(uuid.uuid4())
        with db.tx() as c:
            c.execute("INSERT INTO venues VALUES (?,?,?,?,?,?,?,?,?)",
                      (vid, str(body.organizationId), body.name, body.address, body.type, body.timezone,
                       body.workingHours, "ACTIVE", now_iso()))
            result = venue_json(c.execute("SELECT * FROM venues WHERE id = ?", (vid,)).fetchone())
            idem.store(c, "create_venue", key, h, 201, result)
        return result

    @app.get("/api/v1/venues/{venue_id}", tags=["public"])
    async def get_venue_ep(venue_id: str):
        return venue_json(get_venue(venue_id))

    @app.post("/api/v1/venues/{venue_id}/deactivate", tags=["public"])
    async def deactivate(venue_id: str):
        v = get_venue(venue_id)
        if v["status"] == "DEACTIVATED":
            raise ProblemError(409, "Заведение уже отключено")
        with db.tx() as c:
            c.execute("UPDATE venues SET status = 'DEACTIVATED' WHERE id = ?", (venue_id,))
            ctx.outbox.add(c, "venue.deactivated", venue_id,
                           {"venueId": venue_id, "organizationId": v["organization_id"], "reason": "Точка закрыта"})
        await ctx.outbox.flush()
        return venue_json(get_venue(venue_id))

    @app.get("/api/v1/venues/{venue_id}/menu", tags=["public"])
    async def get_menu(venue_id: str):
        get_venue(venue_id)
        return [item_json(r) for r in db.query("SELECT * FROM menu_items WHERE venue_id = ? ORDER BY category, name",
                                                (venue_id,))]

    @app.post("/api/v1/venues/{venue_id}/menu", status_code=201, tags=["public"])
    async def add_menu_item(venue_id: str, body: UpsertMenuItemRequest):
        import json
        get_venue(venue_id)
        iid = str(uuid.uuid4())
        with db.tx() as c:
            c.execute("INSERT INTO menu_items VALUES (?,?,?,?,?,?,?)",
                      (iid, venue_id, body.name, body.category, json.dumps(body.aliases, ensure_ascii=False),
                       body.price, int(body.available)))
        return item_json(db.one("SELECT * FROM menu_items WHERE id = ?", (iid,)))

    @app.put("/api/v1/venues/{venue_id}/menu/{item_id}", tags=["public"])
    async def update_menu_item(venue_id: str, item_id: str, body: UpsertMenuItemRequest):
        import json
        if not db.one("SELECT 1 FROM menu_items WHERE id = ? AND venue_id = ?", (item_id, venue_id)):
            raise ProblemError(404, f"Позиция {item_id} не найдена")
        with db.tx() as c:
            c.execute("UPDATE menu_items SET name=?, category=?, aliases=?, price=?, available=? WHERE id=?",
                      (body.name, body.category, json.dumps(body.aliases, ensure_ascii=False), body.price,
                       int(body.available), item_id))
        return item_json(db.one("SELECT * FROM menu_items WHERE id = ?", (item_id,)))

    # ------------------------------------------------------------------ internal
    @app.get("/internal/v1/venues/{venue_id}", tags=["internal"], dependencies=[Depends(require_service_token)])
    async def internal_venue(venue_id: str):
        return venue_json(get_venue(venue_id))

    @app.get("/internal/v1/venues/{venue_id}/menu", tags=["internal"], dependencies=[Depends(require_service_token)])
    async def internal_menu(venue_id: str):
        v = get_venue(venue_id)
        if v["status"] != "ACTIVE":
            raise ProblemError(409, "Заведение отключено — анализ записей не выполняется")
        return [item_json(r) for r in db.query("SELECT * FROM menu_items WHERE venue_id = ?", (venue_id,))]

    return app


app = create_app() if os.getenv("TALKSENSE_NO_AUTOAPP") != "1" else None
