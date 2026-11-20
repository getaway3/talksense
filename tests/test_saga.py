"""Интеграционные тесты: сквозной сценарий «Обработка записи» через 6 сервисов и брокер."""
from __future__ import annotations

VENUE = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
DATE = "2026-11-20"


async def status_of(system, rec_id: str) -> dict:
    r = await system.recording.get(f"/api/v1/recordings/{rec_id}")
    assert r.status_code == 200
    return r.json()


async def test_happy_path_out_of_stock(system):
    r = await system.upload("01_out_of_stock.txt", "chunk-001")
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["status"] == "UPLOADED"
    assert [e["eventType"] for e in system.bus.events()] == ["recording.uploaded"]

    await system.settle()

    rec = await status_of(system, rec["id"])
    assert rec["status"] == "ANALYZED"
    assert rec["incidentsCount"] == 1
    assert rec["audioPurged"] is True, "после анализа исходное аудио удаляется"
    assert [rk for rk, _ in system.bus.published] == [
        "recording.uploaded", "transcription.completed", "analysis.completed"]
    # у всех событий саги один correlationId = recordingId
    assert {e["correlationId"] for e in system.bus.events()} == {rec["id"]}

    inc = (await system.analysis.get("/api/v1/incidents",
                                     params={"venueId": VENUE, "from": DATE, "to": DATE})).json()
    assert inc["page"]["totalElements"] == 1
    item = inc["items"][0]
    assert item["type"] == "OUT_OF_STOCK"
    assert item["requestedItem"] == "Раф лавандовый", "реплика гостя сопоставлена с позицией меню"
    assert item["menuItemId"]

    rep = (await system.report.get(f"/api/v1/reports/daily/{VENUE}/{DATE}")).json()
    assert rep["venueName"] == "Кофейня «Зерно» на Садовой"
    assert rep["totals"]["OUT_OF_STOCK"] == 1
    assert rep["outOfStock"] == [{"name": "Раф лавандовый", "menuItemId": item["menuItemId"], "count": 1}]


async def test_full_day_report(system):
    for i, script in enumerate(["01_out_of_stock.txt", "02_menu_request.txt", "03_conflict.txt",
                                "04_personal.txt", "05_praise.txt", "01_out_of_stock.txt"]):
        assert (await system.upload(script, f"day-{i}", started=f"{DATE}T1{i}:00:00Z")).status_code == 201
    await system.settle()

    rep = (await system.report.get(f"/api/v1/reports/daily/{VENUE}/{DATE}")).json()
    assert rep["recordingsAnalyzed"] == 5, "личный разговор отброшен и в отчёт не попал"
    assert rep["totals"] == {"CONFLICT": 1, "OUT_OF_STOCK": 2, "MENU_REQUEST": 1, "COMPLAINT": 0, "PRAISE": 1,
                             "SERVICE_ISSUE": 0}
    assert rep["outOfStock"][0]["count"] == 2
    assert rep["menuRequests"][0]["name"] == "матча на кокосовом"
    assert any("Конфликт" in h for h in rep["highlights"])

    csv = await system.report.get(f"/api/v1/reports/daily/{VENUE}/{DATE}/export")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert "OUT_OF_STOCK" in csv.text and "CONFLICT" in csv.text


async def test_speechkit_failure_then_reprocess(system):
    """Сбой на шаге распознавания → FAILED_TRANSCRIPTION (аудио сохраняется) → reprocess → ANALYZED."""
    system.speechkit["mode"] = "fail"
    rec = (await system.upload("01_out_of_stock.txt", "chunk-fail")).json()
    await system.settle()

    rec = await status_of(system, rec["id"])
    assert rec["status"] == "FAILED_TRANSCRIPTION"
    assert rec["statusReason"].startswith("PROVIDER_UNAVAILABLE")
    assert rec["audioPurged"] is False, "компенсация: аудио не удаляется, чтобы можно было повторить"
    assert system.speechkit["failed"] == 3, "3 попытки (1 + 2 повтора через очередь)"
    failed = system.bus.events("transcription.failed")
    assert failed and failed[0]["payload"]["attempts"] == 3

    system.speechkit["mode"] = "ok"
    r = await system.recording.post(f"/api/v1/recordings/{rec['id']}/reprocess",
                                    headers={"Idempotency-Key": "re-1"})
    assert r.status_code == 202 and r.json()["attempt"] == 2
    # повтор с тем же ключом не запускает обработку второй раз
    r2 = await system.recording.post(f"/api/v1/recordings/{rec['id']}/reprocess",
                                     headers={"Idempotency-Key": "re-1"})
    assert r2.status_code == 202 and r2.headers.get("Idempotent-Replayed") == "true"
    assert len(system.bus.events("recording.uploaded")) == 2

    await system.settle()
    assert (await status_of(system, rec["id"]))["status"] == "ANALYZED"


async def test_gigachat_failure_then_reprocess_reuses_transcript(system):
    system.gigachat["mode"] = "fail"
    rec = (await system.upload("03_conflict.txt", "chunk-llm")).json()
    await system.settle()
    rec = await status_of(system, rec["id"])
    assert rec["status"] == "FAILED_ANALYSIS"
    assert rec["statusReason"] == "LLM_UNAVAILABLE"
    assert rec["audioPurged"] is False

    system.gigachat["mode"] = "ok"
    calls_before = system.speechkit["calls"]
    await system.recording.post(f"/api/v1/recordings/{rec['id']}/reprocess", headers={"Idempotency-Key": "re-2"})
    await system.settle()
    assert (await status_of(system, rec["id"]))["status"] == "ANALYZED"
    assert system.speechkit["calls"] == calls_before, "платный SpeechKit повторно не вызывается"


async def test_bad_llm_json_is_retried_then_failed(system):
    system.gigachat["mode"] = "bad_json"
    rec = (await system.upload("03_conflict.txt", "chunk-badjson")).json()
    await system.settle()
    rec = await status_of(system, rec["id"])
    assert rec["status"] == "FAILED_ANALYSIS" and rec["statusReason"] == "LLM_BAD_RESPONSE"


async def test_personal_talk_is_discarded_and_purged(system):
    """Компенсация приватности: личный разговор → аудио и расшифровка удаляются."""
    rec = (await system.upload("04_personal.txt", "chunk-personal")).json()
    await system.settle()
    rec = await status_of(system, rec["id"])
    assert rec["status"] == "DISCARDED"
    assert rec["statusReason"] == "PERSONAL_ONLY"
    assert rec["audioPurged"] is True
    tr = await system.transcription.get(f"/api/v1/transcripts/by-recording/{rec['id']}")
    assert tr.status_code == 404, "расшифровка удалена"
    assert (await system.report.get(f"/api/v1/reports/daily/{VENUE}/{DATE}")).status_code == 404


async def test_duplicate_event_delivery_is_ignored(system):
    await system.upload("01_out_of_stock.txt", "chunk-dup")
    await system.settle()
    # брокер доставил analysis.completed повторно (at-least-once)
    from talksense_common.events import Envelope
    dup = system.bus.events("analysis.completed")[0]
    await system.report_ctx.on_analysis(Envelope(**dup), 1)
    await system.recording_ctx.on_saga_event(Envelope(**dup), 1)
    rep = (await system.report.get(f"/api/v1/reports/daily/{VENUE}/{DATE}")).json()
    assert rep["recordingsAnalyzed"] == 1 and rep["totals"]["OUT_OF_STOCK"] == 1


async def test_upload_is_idempotent(system):
    r1 = await system.upload("01_out_of_stock.txt", "chunk-same")
    r2 = await system.upload("01_out_of_stock.txt", "chunk-same")
    assert r1.status_code == 201 and r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    assert len(system.bus.events("recording.uploaded")) == 1


async def test_unknown_device_rejected(system):
    r = await system.upload("01_out_of_stock.txt", "chunk-x", token="dvt_wrong")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 401 and body["title"] == "Unauthorized" and "traceId" in body


async def test_venue_deactivation_revokes_devices(system):
    """venue.deactivated → device-service отзывает устройства → загрузка отклоняется 403."""
    r = await system.venue.post(f"/api/v1/venues/{VENUE}/deactivate")
    assert r.status_code == 200 and r.json()["status"] == "DEACTIVATED"
    await system.settle()
    dev = (await system.device.get("/api/v1/devices", params={"venueId": VENUE})).json()["items"][0]
    assert dev["status"] == "REVOKED"
    assert (await system.upload("01_out_of_stock.txt", "chunk-after")).status_code == 403


async def test_register_device_checks_venue(system):
    ok = await system.device.post("/api/v1/devices", headers={"Idempotency-Key": "k1"},
                                  json={"serialNumber": "TS-REC-000777", "venueId": VENUE, "placement": "Витрина"})
    assert ok.status_code == 201 and ok.json()["deviceToken"].startswith("dvt_")
    again = await system.device.post("/api/v1/devices", headers={"Idempotency-Key": "k1"},
                                     json={"serialNumber": "TS-REC-000777", "venueId": VENUE, "placement": "Витрина"})
    assert again.status_code == 201 and again.json()["deviceToken"] == ok.json()["deviceToken"]
    other = await system.device.post("/api/v1/devices", headers={"Idempotency-Key": "k1"},
                                     json={"serialNumber": "TS-REC-000778", "venueId": VENUE})
    assert other.status_code == 422, "тот же ключ с другим телом"
    missing = await system.device.post("/api/v1/devices", headers={"Idempotency-Key": "k2"},
                                       json={"serialNumber": "TS-REC-000779",
                                             "venueId": "00000000-0000-0000-0000-000000000000"})
    assert missing.status_code == 404
    # новое устройство может грузить записи
    r = await system.upload("05_praise.txt", "chunk-new", token=ok.json()["deviceToken"])
    assert r.status_code == 201


async def test_internal_endpoints_require_service_token(system):
    r = await system.venue.get(f"/internal/v1/venues/{VENUE}")
    assert r.status_code == 401 and r.json()["type"].endswith("/unauthorized")


async def test_validation_errors_use_problem_model(system):
    r = await system.analysis.get("/api/v1/incidents", params={"venueId": "not-a-uuid", "from": DATE, "to": DATE})
    assert r.status_code == 400
    body = r.json()
    assert body["title"] == "Validation error" and body["errors"][0]["field"] == "venueId"
