"""Тесты device-service с моком venue-service."""
import httpx
import pytest

from talksense_common.events import Envelope, InMemoryBus
from device_service import main

VENUE = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
CLOSED = "11111111-1111-1111-1111-111111111111"


def venue_mock(req: httpx.Request) -> httpx.Response:
    vid = req.url.path.rsplit("/", 1)[-1]
    if vid == VENUE:
        return httpx.Response(200, json={"id": VENUE, "status": "ACTIVE"})
    if vid == CLOSED:
        return httpx.Response(200, json={"id": CLOSED, "status": "DEACTIVATED"})
    return httpx.Response(404, json={"status": 404})


@pytest.fixture
async def client(tmp_path):
    ctx = main.Ctx(str(tmp_path / "d.db"), InMemoryBus(), venue_transport=httpx.MockTransport(venue_mock))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(main.create_app(ctx)), base_url="http://d") as c:
        c.ctx = ctx
        yield c


def register(c, serial="TS-REC-000001", venue=VENUE, key="k"):
    return c.post("/api/v1/devices", headers={"Idempotency-Key": key}, json={"serialNumber": serial, "venueId": venue})


async def test_register_heartbeat_verify(client):
    r = (await register(client)).json()
    token, did = r["deviceToken"], r["device"]["id"]
    assert r["device"]["online"] is False
    hb = await client.post("/device/v1/heartbeat", headers={"X-Device-Token": token}, json={"firmwareVersion": "1.5.0"})
    assert hb.status_code == 204
    d = (await client.get(f"/api/v1/devices/{did}")).json()
    assert d["online"] is True and d["firmwareVersion"] == "1.5.0"
    v = await client.post("/internal/v1/devices/verify", json={"deviceToken": token},
                          headers={"Authorization": "Bearer test-service-token"})
    assert v.json() == {"deviceId": did, "venueId": VENUE, "status": "ACTIVE"}


async def test_token_is_stored_hashed(client):
    token = (await register(client)).json()["deviceToken"]
    assert client.ctx.db.one("SELECT count(*) c FROM devices WHERE token_hash = ?", (token,))["c"] == 0


@pytest.mark.parametrize("venue, status", [(CLOSED, 409), ("22222222-2222-2222-2222-222222222222", 404)])
async def test_register_checks_venue(client, venue, status):
    assert (await register(client, venue=venue)).status_code == status


async def test_bad_serial_is_validation_error(client):
    r = await register(client, serial="ABC")
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "serialNumber"


async def test_duplicate_active_serial_conflict(client):
    await register(client, key="a")
    assert (await register(client, key="b")).status_code == 409


async def test_revoked_device_cannot_heartbeat(client):
    r = (await register(client)).json()
    await client.post(f"/api/v1/devices/{r['device']['id']}/revoke")
    hb = await client.post("/device/v1/heartbeat", headers={"X-Device-Token": r["deviceToken"]},
                           json={"firmwareVersion": "1"})
    assert hb.status_code == 403


async def test_venue_deactivated_event_revokes_and_is_idempotent(client):
    r = (await register(client)).json()
    env = Envelope(eventType="venue.deactivated", correlationId=VENUE, producer="venue-service",
                   payload={"venueId": VENUE})
    await client.ctx.on_venue_deactivated(env, 1)
    await client.ctx.on_venue_deactivated(env, 1)  # повторная доставка
    d = (await client.get(f"/api/v1/devices/{r['device']['id']}")).json()
    assert d["status"] == "REVOKED"
