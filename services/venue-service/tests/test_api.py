"""Тесты venue-service: CRUD, пагинация, идемпотентность, событие venue.deactivated."""
import httpx
import pytest

from talksense_common.events import InMemoryBus
from venue_service import main

ORG = "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"
SVC = {"Authorization": "Bearer test-service-token"}


@pytest.fixture
async def client(tmp_path):
    bus = InMemoryBus()
    ctx = main.Ctx(str(tmp_path / "v.db"), bus)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(main.create_app(ctx)), base_url="http://v") as c:
        c.bus = bus
        yield c


def new_venue(c, key, name="Пекарня"):
    return c.post("/api/v1/venues", headers={"Idempotency-Key": key},
                  json={"organizationId": ORG, "name": name, "address": "Ростов", "type": "BAKERY"})


async def test_create_is_idempotent(client):
    a, b = await new_venue(client, "k"), await new_venue(client, "k")
    assert a.status_code == b.status_code == 201
    assert a.json()["id"] == b.json()["id"] and b.headers["Idempotent-Replayed"] == "true"
    assert (await new_venue(client, "k", name="Другая")).status_code == 422


async def test_pagination(client):
    for i in range(5):
        await new_venue(client, f"k{i}", name=f"Точка {i}")
    r = (await client.get("/api/v1/venues", params={"organizationId": ORG, "page": 1, "size": 2})).json()
    assert r["page"] == {"page": 1, "size": 2, "totalElements": 5, "totalPages": 3}
    assert [v["name"] for v in r["items"]] == ["Точка 2", "Точка 3"]


async def test_menu_and_availability(client):
    vid = (await new_venue(client, "k")).json()["id"]
    item = (await client.post(f"/api/v1/venues/{vid}/menu",
                              json={"name": "Багет", "category": "Хлеб", "aliases": ["французский"]})).json()
    assert item["available"] is True
    upd = await client.put(f"/api/v1/venues/{vid}/menu/{item['id']}",
                           json={"name": "Багет", "category": "Хлеб", "available": False})
    assert upd.json()["available"] is False
    internal = await client.get(f"/internal/v1/venues/{vid}/menu", headers=SVC)
    assert internal.status_code == 200 and internal.json()[0]["aliases"] == []


async def test_deactivate_publishes_event_and_blocks_analysis(client):
    vid = (await new_venue(client, "k")).json()["id"]
    r = await client.post(f"/api/v1/venues/{vid}/deactivate")
    assert r.json()["status"] == "DEACTIVATED"
    ev = client.bus.events("venue.deactivated")
    assert len(ev) == 1 and ev[0]["payload"]["venueId"] == vid and ev[0]["producer"] == "venue-service"
    assert (await client.post(f"/api/v1/venues/{vid}/deactivate")).status_code == 409
    assert (await client.get(f"/internal/v1/venues/{vid}/menu", headers=SVC)).status_code == 409


async def test_not_found_problem(client):
    r = await client.get("/api/v1/venues/nope")
    assert r.status_code == 404 and r.json()["instance"] == "/api/v1/venues/nope"
