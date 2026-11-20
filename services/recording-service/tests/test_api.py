"""Тесты эндпоинтов recording-service с моком device-service (httpx.MockTransport)."""
import httpx
import pytest

from talksense_common.events import InMemoryBus
from recording_service import main

GOOD = "dvt_good"
VENUE = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def device_mock(request: httpx.Request) -> httpx.Response:
    """Мок device-service по контракту POST /internal/v1/devices/verify."""
    import json
    assert request.headers["authorization"] == "Bearer test-service-token"
    token = json.loads(request.content)["deviceToken"]
    if token == GOOD:
        return httpx.Response(200, json={"deviceId": "d1", "venueId": VENUE, "status": "ACTIVE"})
    if token == "dvt_revoked":
        return httpx.Response(403, json={"status": 403, "title": "Forbidden", "detail": "Устройство отозвано"})
    if token == "dvt_down":
        return httpx.Response(503, json={})
    return httpx.Response(401, json={"status": 401, "title": "Unauthorized", "detail": "Неизвестный токен"})


@pytest.fixture
async def client(tmp_path):
    bus = InMemoryBus()
    ctx = main.Ctx(str(tmp_path / "r.db"), bus, device_transport=httpx.MockTransport(device_mock),
                   audio_dir=str(tmp_path / "audio"))
    app = main.create_app(ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://rec") as c:
        c.bus, c.ctx = bus, ctx
        yield c


def upload(c, token=GOOD, key="k1", data=b"OggS-audio"):
    return c.post("/device/v1/recordings", headers={"X-Device-Token": token, "Idempotency-Key": key},
                  files={"audio": ("a.ogg", data, "audio/ogg")},
                  data={"startedAt": "2026-11-20T10:00:00Z", "durationSec": "12"})


async def test_upload_publishes_event_via_outbox(client):
    r = await upload(client)
    assert r.status_code == 201
    ev = client.bus.events("recording.uploaded")
    assert len(ev) == 1 and ev[0]["payload"]["recordingId"] == r.json()["id"]
    assert ev[0]["payload"]["venueId"] == VENUE
    assert client.ctx.db.one("SELECT count(*) c FROM outbox WHERE published_at IS NULL")["c"] == 0


@pytest.mark.parametrize("token, status", [("dvt_bad", 401), ("dvt_revoked", 403), ("dvt_down", 503)])
async def test_upload_rejected(client, token, status):
    r = await upload(client, token=token)
    assert r.status_code == status
    assert r.headers["content-type"].startswith("application/problem+json")
    assert client.bus.events() == []


async def test_upload_requires_idempotency_key(client):
    r = await client.post("/device/v1/recordings", headers={"X-Device-Token": GOOD},
                          files={"audio": ("a.ogg", b"x", "audio/ogg")},
                          data={"startedAt": "2026-11-20T10:00:00Z", "durationSec": "12"})
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "Idempotency-Key"


async def test_audio_url_is_signed_and_expires(client):
    rec = (await upload(client)).json()
    noauth = await client.get(f"/internal/v1/recordings/{rec['id']}/audio-url")
    assert noauth.status_code == 401
    r = await client.get(f"/internal/v1/recordings/{rec['id']}/audio-url",
                         headers={"Authorization": "Bearer test-service-token"})
    url = httpx.URL(r.json()["url"])
    ok = await client.get(url.path, params=dict(url.params))
    assert ok.status_code == 200 and ok.content == b"OggS-audio"
    forged = await client.get(url.path, params={**dict(url.params), "sig": "0" * 64})
    assert forged.status_code == 403


async def test_reprocess_only_from_failed(client):
    rec = (await upload(client)).json()
    r = await client.post(f"/api/v1/recordings/{rec['id']}/reprocess", headers={"Idempotency-Key": "x"})
    assert r.status_code == 409


async def test_list_pagination(client):
    for i in range(3):
        await upload(client, key=f"k{i}")
    r = (await client.get("/api/v1/recordings", params={"venueId": VENUE, "date": "2026-11-20", "size": 2})).json()
    assert r["page"] == {"page": 0, "size": 2, "totalElements": 3, "totalPages": 2}
    assert len(r["items"]) == 2
    other_day = (await client.get("/api/v1/recordings", params={"venueId": VENUE, "date": "2026-11-21"})).json()
    assert other_day["page"]["totalElements"] == 0
