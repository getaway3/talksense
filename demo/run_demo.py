"""Демонстрация сценариев ЛР4 через API Gateway.

    python demo/run_demo.py [--gateway http://localhost:8080] [--speechkit http://localhost:8091]

Сценарии:
  1. Успешный сквозной сценарий: 5 фрагментов разговоров → распознавание → анализ → отчёт за день.
  2. Сбой SpeechKit → повторы через очередь → FAILED_TRANSCRIPTION (аудио сохранено) → reprocess → ANALYZED.
  3. Идемпотентность загрузки: повтор с тем же Idempotency-Key не создаёт дубль.
  4. Internal-эндпоинты не доступны через шлюз; ошибки — в формате Problem Details.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Консоль Windows (cp1251) не умеет печатать «→» и кириллицу в UTF-8 — не падаем на выводе
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
VENUE = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
DEVICE_TOKEN = "dvt_demo_5f8c1e0a9b7d4c2e8f6a3b1d"
FINAL = {"ANALYZED", "DISCARDED", "FAILED_TRANSCRIPTION", "FAILED_ANALYSIS"}
RUN = datetime.now().strftime("%H%M%S")


def title(text: str) -> None:
    print("\n" + "=" * 78 + f"\n{text}\n" + "=" * 78)


def audio(script: str) -> bytes:
    return b"TSDEMO\n" + (HERE / "scripts" / script).read_bytes()


def upload(gw: httpx.Client, script: str, chunk: str) -> httpx.Response:
    return gw.post("/device/v1/recordings",
                   headers={"X-Device-Token": DEVICE_TOKEN, "Idempotency-Key": chunk},
                   files={"audio": (f"{chunk}.ogg", audio(script), "audio/ogg")},
                   data={"startedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "durationSec": "30"})


def wait(gw: httpx.Client, rec_id: str, until: set[str] = FINAL, timeout: float = 60) -> dict:
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        rec = gw.get(f"/api/v1/recordings/{rec_id}").json()
        if rec["status"] != last:
            print(f"    {time.time() - t0:5.1f} с  {rec['status']}" + (f"  ({rec['statusReason']})"
                                                                     if rec.get("statusReason") else ""))
            last = rec["status"]
        if rec["status"] in until:
            return rec
        time.sleep(0.5)
    raise TimeoutError(f"запись {rec_id} не дошла до {until}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://localhost:8080")
    ap.add_argument("--speechkit", default="http://localhost:8091")
    args = ap.parse_args()
    gw = httpx.Client(base_url=args.gateway, timeout=15)
    sk = httpx.Client(base_url=args.speechkit, timeout=5)
    sk.post("/__admin/mode", json={"mode": "ok"})
    today = datetime.now(timezone.utc).date().isoformat()

    title("1. Успешный сценарий: фрагменты с устройства → отчёт за день")
    ids = {}
    for script in ["01_out_of_stock.txt", "02_menu_request.txt", "03_conflict.txt", "04_personal.txt",
                   "05_praise.txt"]:
        r = upload(gw, script, f"demo-{RUN}-{script[:2]}")
        print(f"  POST /device/v1/recordings  {script:<22} -> {r.status_code}  id={r.json()['id'][:8]}…  "
              f"cid={r.headers.get('x-correlation-id', '')[:8]}")
        ids[script] = r.json()["id"]
    for script, rid in ids.items():
        print(f"  {script}:")
        rec = wait(gw, rid)
        print(f"    итог: {rec['status']}, инцидентов: {rec['incidentsCount']}, аудио удалено: {rec['audioPurged']}")
    inc = gw.get("/api/v1/incidents", params={"venueId": VENUE, "from": today, "to": today, "size": 100}).json()
    print(f"\n  GET /api/v1/incidents → {inc['page']['totalElements']} инцидентов:")
    for i in inc["items"][-4:]:
        print(f"    {i['type']:<13} {i['severity']:<6} {i['summary']}")
    rep = gw.get(f"/api/v1/reports/daily/{VENUE}/{today}").json()
    print(f"\n  GET /api/v1/reports/daily/{{venue}}/{today}:")
    print("  " + json.dumps({k: rep[k] for k in ("venueName", "recordingsAnalyzed", "totals", "outOfStock",
                                                 "menuRequests", "highlights")},
                            ensure_ascii=False, indent=2).replace("\n", "\n  "))

    title("2. Сбой SpeechKit → компенсация → перезапуск")
    sk.post("/__admin/mode", json={"mode": "fail"})
    print("  speechkit-mock переведён в режим fail (HTTP 503)")
    rid = upload(gw, "01_out_of_stock.txt", f"demo-{RUN}-fail").json()["id"]
    rec = wait(gw, rid, until={"FAILED_TRANSCRIPTION"})
    print(f"    аудио сохранено для повтора: audioPurged={rec['audioPurged']}")
    sk.post("/__admin/mode", json={"mode": "ok"})
    print("  speechkit-mock снова ok; POST /api/v1/recordings/{id}/reprocess")
    r = gw.post(f"/api/v1/recordings/{rid}/reprocess", headers={"Idempotency-Key": f"re-{RUN}"})
    print(f"    -> {r.status_code}, attempt={r.json()['attempt']}")
    rec = wait(gw, rid)
    print(f"    итог: {rec['status']}, аудио удалено: {rec['audioPurged']}")

    title("3. Идемпотентность загрузки")
    a = upload(gw, "05_praise.txt", f"demo-{RUN}-idem")
    b = upload(gw, "05_praise.txt", f"demo-{RUN}-idem")
    print(f"  первая отправка -> {a.status_code}, повтор -> {b.status_code} "
          f"(Idempotent-Replayed: {b.headers.get('idempotent-replayed')}), id совпадает: {a.json()['id'] == b.json()['id']}")

    title("4. Шлюз и единая модель ошибок")
    r = gw.get(f"/internal/v1/venues/{VENUE}")
    print(f"  GET /internal/v1/venues/… через шлюз -> {r.status_code} {r.headers['content-type']}")
    r = gw.post("/device/v1/recordings", headers={"X-Device-Token": "dvt_stolen", "Idempotency-Key": "x"},
                files={"audio": ("a.ogg", b"x", "audio/ogg")}, data={"startedAt": today + "T10:00:00Z",
                                                                     "durationSec": "5"})
    print(f"  загрузка с чужим токеном -> {r.status_code}\n  " + json.dumps(r.json(), ensure_ascii=False))
    print("\nГотово.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
