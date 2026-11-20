"""recording-service — приём аудио и состояние саги (контракт: api/recording-service.yaml).

Хранилище аудио: в проекте MinIO; здесь — каталог AUDIO_DIR (мок объектного хранилища),
а «presigned URL» реализован как ссылка с HMAC-подписью и сроком действия.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, File, Form, Header, Query, UploadFile
from fastapi.responses import FileResponse

from talksense_common import idempotency as idem
from talksense_common.db import Database, page
from talksense_common.events import Envelope, EventBus, Outbox, already_processed, make_bus, now_iso
from talksense_common.http import DependencyUnavailable, ServiceClient
from talksense_common.problems import ProblemError
from talksense_common.service import SERVICE_TOKEN, create_base_app, require_service_token

from . import saga

SERVICE = "recording-service"
MAX_BYTES = 20 * 1024 * 1024
URL_TTL = 15 * 60
SIGN_KEY = os.getenv("AUDIO_SIGN_KEY", "dev-audio-sign-key").encode()
STATUSES = ("UPLOADED", "TRANSCRIBED", "ANALYZED", "DISCARDED", "FAILED_TRANSCRIPTION", "FAILED_ANALYSIS")

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id TEXT PRIMARY KEY, device_id TEXT NOT NULL, venue_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
    started_at TEXT NOT NULL, duration_sec INTEGER NOT NULL, format TEXT NOT NULL, size_bytes INTEGER NOT NULL,
    status TEXT NOT NULL, status_reason TEXT, audio_path TEXT, audio_purged INTEGER NOT NULL DEFAULT 0,
    incidents_count INTEGER NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
    UNIQUE (device_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS ix_rec_venue_day ON recordings (venue_id, started_at);
"""


def sign(rec_id: str, exp: int) -> str:
    return hmac.new(SIGN_KEY, f"{rec_id}:{exp}".encode(), hashlib.sha256).hexdigest()


def rec_json(r) -> dict:
    return {"id": r["id"], "deviceId": r["device_id"], "venueId": r["venue_id"], "chunkId": r["chunk_id"],
            "startedAt": r["started_at"], "durationSec": r["duration_sec"], "format": r["format"],
            "sizeBytes": r["size_bytes"], "status": r["status"], "statusReason": r["status_reason"],
            "audioPurged": bool(r["audio_purged"]), "incidentsCount": r["incidents_count"],
            "attempt": r["attempt"], "updatedAt": r["updated_at"]}


class Ctx:
    def __init__(self, db_path: str | None = None, bus: EventBus | None = None, device_transport=None,
                 audio_dir: str | None = None):
        self.db = Database(db_path or os.getenv("DB_PATH", "data/recording.db"), SCHEMA)
        self.bus = bus or make_bus(os.getenv("RABBITMQ_URL"))
        self.outbox = Outbox(self.db, self.bus, SERVICE)
        self.audio_dir = Path(audio_dir or os.getenv("AUDIO_DIR", "data/audio"))
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.self_url = os.getenv("SELF_URL", "http://recording-service:8000")
        self.device = ServiceClient("device-service", os.getenv("DEVICE_URL", "http://device-service:8000"),
                                    SERVICE_TOKEN, transport=device_transport)

    async def start(self, relay: bool = True) -> None:
        await self.bus.start()
        await self.bus.subscribe(SERVICE, ["transcription.completed", "transcription.failed", "analysis.completed",
                                           "analysis.discarded", "analysis.failed"], self.on_saga_event)
        if relay:
            self.outbox.start()
            await self.outbox.flush()

    async def on_saga_event(self, env: Envelope, attempt: int) -> None:
        """Подписка на события конвейера: переводим запись по машине состояний."""
        rec_id = env.payload["recordingId"]
        purge_path = None
        with self.db.tx() as c:
            if already_processed(c, env.eventId):
                return
            r = c.execute("SELECT * FROM recordings WHERE id = ?", (rec_id,)).fetchone()
            if r is None:
                return
            new = saga.next_status(r["status"], env.eventType)
            if new is None:
                return  # устаревшее/повторное событие для этого статуса
            reason = env.payload.get("reason")
            detail = env.payload.get("detail")
            incidents = len(env.payload.get("incidents", [])) if env.eventType == "analysis.completed" else r["incidents_count"]
            purge = env.eventType in saga.PURGE_AUDIO_ON
            c.execute("UPDATE recordings SET status=?, status_reason=?, incidents_count=?, audio_purged=?, "
                      "updated_at=? WHERE id=?",
                      (new, f"{reason}: {detail}" if detail else reason, incidents,
                       1 if purge else r["audio_purged"], now_iso(), rec_id))
            if purge:
                purge_path = r["audio_path"]
        if purge_path:  # компенсация приватности / политика хранения: удаляем исходное аудио
            Path(purge_path).unlink(missing_ok=True)


def create_app(ctx: Ctx | None = None):
    ctx = ctx or Ctx()

    @asynccontextmanager
    async def lifespan(_app):
        await ctx.start()
        yield
        await ctx.outbox.stop()
        await ctx.bus.close()

    app = create_base_app(SERVICE, "Приём аудио и состояние саги обработки", lifespan)
    app.state.ctx = ctx
    db = ctx.db

    def get_rec(rec_id: str):
        r = db.one("SELECT * FROM recordings WHERE id = ?", (rec_id,))
        if not r:
            raise ProblemError(404, f"Запись {rec_id} не найдена")
        return r

    @app.post("/device/v1/recordings", status_code=201, tags=["public"])
    async def upload(audio: UploadFile = File(...), startedAt: datetime = Form(...),
                     durationSec: int = Form(..., ge=1, le=600), x_device_token: str | None = Header(None),
                     idempotency_key: str | None = Header(None)):
        chunk_id = idem.require_key(idempotency_key)
        if not x_device_token:
            raise ProblemError(401, "Нет заголовка X-Device-Token")
        # S2: проверка токена устройства в device-service
        try:
            resp = await ctx.device.post("/internal/v1/devices/verify", json={"deviceToken": x_device_token})
        except DependencyUnavailable as exc:
            raise ProblemError(503, f"device-service недоступен, повторите отправку: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ProblemError(resp.status_code, resp.json().get("detail", "Устройство не авторизовано"))
        ident = resp.json()
        # идемпотентность: (устройство, chunkId) уникальны — повтор вернёт ту же запись
        existing = db.one("SELECT * FROM recordings WHERE device_id = ? AND chunk_id = ?",
                          (ident["deviceId"], chunk_id))
        if existing:
            from fastapi.responses import JSONResponse
            return JSONResponse(rec_json(existing), status_code=200, headers={"Idempotent-Replayed": "true"})
        data = await audio.read()
        if len(data) > MAX_BYTES:
            raise ProblemError(413, "Максимальный размер фрагмента — 20 МБ")
        if not data:
            raise ProblemError(400, "Пустой аудиофайл", errors=[{"field": "audio", "message": "empty"}])
        rec_id = str(uuid.uuid4())
        fmt = "WAV" if (audio.filename or "").lower().endswith(".wav") else "OGG_OPUS"
        path = ctx.audio_dir / f"{rec_id}.{'wav' if fmt == 'WAV' else 'ogg'}"
        path.write_bytes(data)  # «MinIO»
        started = startedAt.isoformat().replace("+00:00", "Z")
        try:
            with db.tx() as c:
                c.execute("INSERT INTO recordings (id, device_id, venue_id, chunk_id, started_at, duration_sec, "
                          "format, size_bytes, status, audio_path, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (rec_id, ident["deviceId"], ident["venueId"], chunk_id, started, durationSec, fmt,
                           len(data), "UPLOADED", str(path), now_iso()))
                # Transactional Outbox: событие фиксируется в той же транзакции, что и запись
                ctx.outbox.add(c, "recording.uploaded", rec_id, {
                    "recordingId": rec_id, "venueId": ident["venueId"], "deviceId": ident["deviceId"],
                    "startedAt": started, "durationSec": durationSec, "format": fmt, "attempt": 1})
        except Exception:
            path.unlink(missing_ok=True)
            raise
        await ctx.outbox.flush()
        return rec_json(get_rec(rec_id))

    @app.get("/api/v1/recordings", tags=["public"])
    async def list_recordings(venueId: uuid.UUID, date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
                              status: str | None = Query(None, pattern="^(" + "|".join(STATUSES) + ")$"),
                              page_no: int = Query(0, alias="page", ge=0), size: int = Query(20, ge=1, le=100)):
        where, params = "venue_id = ? AND substr(started_at, 1, 10) = ?", [str(venueId), date]
        if status:
            where += " AND status = ?"
            params.append(status)
        total = db.one(f"SELECT count(*) AS c FROM recordings WHERE {where}", tuple(params))["c"]
        rows = db.query(f"SELECT * FROM recordings WHERE {where} ORDER BY started_at LIMIT ? OFFSET ?",
                        (*params, size, page_no * size))
        return page([rec_json(r) for r in rows], page_no, size, total)

    @app.get("/api/v1/recordings/{rec_id}", tags=["public"])
    async def get_recording(rec_id: str):
        return rec_json(get_rec(rec_id))

    @app.post("/api/v1/recordings/{rec_id}/reprocess", status_code=202, tags=["public"])
    async def reprocess(rec_id: str, idempotency_key: str | None = Header(None)):
        key = idem.require_key(idempotency_key)
        h = idem.request_hash(rec_id)
        if (replay := idem.lookup(db, "reprocess", key, h)) is not None:
            return replay
        r = get_rec(rec_id)
        if not saga.can_reprocess(r["status"], bool(r["audio_purged"])):
            raise ProblemError(409, f"Перезапуск возможен только из FAILED_* и пока аудио не удалено "
                                    f"(сейчас {r['status']})")
        with db.tx() as c:
            attempt = r["attempt"] + 1
            c.execute("UPDATE recordings SET status='UPLOADED', status_reason=NULL, attempt=?, updated_at=? "
                      "WHERE id=?", (attempt, now_iso(), rec_id))
            ctx.outbox.add(c, "recording.uploaded", rec_id, {
                "recordingId": rec_id, "venueId": r["venue_id"], "deviceId": r["device_id"],
                "startedAt": r["started_at"], "durationSec": r["duration_sec"], "format": r["format"],
                "attempt": attempt})
            result = rec_json(c.execute("SELECT * FROM recordings WHERE id = ?", (rec_id,)).fetchone())
            idem.store(c, "reprocess", key, h, 202, result)
        await ctx.outbox.flush()
        return result

    @app.get("/internal/v1/recordings/{rec_id}/audio-url", tags=["internal"],
             dependencies=[Depends(require_service_token)])
    async def audio_url(rec_id: str):
        r = get_rec(rec_id)
        if r["audio_purged"] or not r["audio_path"] or not Path(r["audio_path"]).exists():
            raise ProblemError(410, "Аудио удалено по политике хранения")
        exp = int(time.time()) + URL_TTL
        return {"url": f"{ctx.self_url}/internal/v1/recordings/{rec_id}/audio?exp={exp}&sig={sign(rec_id, exp)}",
                "expiresAt": datetime.fromtimestamp(exp, timezone.utc).isoformat().replace("+00:00", "Z"), "format": r["format"]}

    @app.get("/internal/v1/recordings/{rec_id}/audio", tags=["internal"], include_in_schema=False)
    async def audio_download(rec_id: str, exp: int, sig: str):
        """Аналог presigned URL MinIO: доступ по подписи, без сервисного токена."""
        if exp < time.time() or not hmac.compare_digest(sig, sign(rec_id, exp)):
            raise ProblemError(403, "Ссылка недействительна или истекла")
        r = get_rec(rec_id)
        if r["audio_purged"] or not Path(r["audio_path"]).exists():
            raise ProblemError(410, "Аудио удалено")
        return FileResponse(r["audio_path"], media_type="audio/ogg")

    return app


app = create_app() if os.getenv("TALKSENSE_NO_AUTOAPP") != "1" else None
