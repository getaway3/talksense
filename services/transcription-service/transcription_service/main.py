"""transcription-service — распознавание речи (контракт: api/transcription-service.yaml).

Подписан на recording.uploaded и analysis.discarded. Хранилище расшифровок в проекте — MongoDB
(документ с массивом сегментов); здесь SQLite с JSON-колонкой segments.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends

from talksense_common.db import Database
from talksense_common.events import Envelope, EventBus, Outbox, RetryLater, already_processed, make_bus, now_iso
from talksense_common.http import DependencyUnavailable, ServiceClient
from talksense_common.problems import ProblemError
from talksense_common.service import SERVICE_TOKEN, create_base_app, require_service_token

from .providers import BadAudio, ProviderUnavailable, YandexSpeechKit, to_segments

SERVICE = "transcription-service"
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))
log = logging.getLogger(SERVICE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    id TEXT PRIMARY KEY, recording_id TEXT NOT NULL UNIQUE, venue_id TEXT NOT NULL, started_at TEXT,
    language TEXT NOT NULL, provider TEXT NOT NULL, segments TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def tr_json(r) -> dict:
    return {"id": r["id"], "recordingId": r["recording_id"], "venueId": r["venue_id"], "language": r["language"],
            "provider": r["provider"], "segments": json.loads(r["segments"]), "createdAt": r["created_at"]}


class Ctx:
    def __init__(self, db_path: str | None = None, bus: EventBus | None = None, recording_transport=None,
                 speechkit_transport=None):
        self.db = Database(db_path or os.getenv("DB_PATH", "data/transcription.db"), SCHEMA)
        self.bus = bus or make_bus(os.getenv("RABBITMQ_URL"))
        self.outbox = Outbox(self.db, self.bus, SERVICE)
        self.recording = ServiceClient("recording-service",
                                       os.getenv("RECORDING_URL", "http://recording-service:8000"),
                                       SERVICE_TOKEN, transport=recording_transport)
        self.provider = YandexSpeechKit(os.getenv("SPEECHKIT_URL", "http://speechkit-mock:8000"),
                                        os.getenv("SPEECHKIT_API_KEY", "mock-key"), os.getenv("YC_FOLDER_ID"),
                                        transport=speechkit_transport)

    async def start(self, relay: bool = True) -> None:
        await self.bus.start()
        await self.bus.subscribe(SERVICE, ["recording.uploaded", "analysis.discarded"], self.on_event)
        if relay:
            self.outbox.start()
            await self.outbox.flush()

    async def on_event(self, env: Envelope, attempt: int) -> None:
        if env.eventType == "analysis.discarded":
            await self.on_discarded(env)
        else:
            await self.on_uploaded(env, attempt)

    async def on_discarded(self, env: Envelope) -> None:
        """Компенсация приватности: в записи не было полезного — удаляем расшифровку."""
        with self.db.tx() as c:
            if already_processed(c, env.eventId):
                return
            c.execute("DELETE FROM transcripts WHERE recording_id = ?", (env.payload["recordingId"],))

    def _fail(self, env: Envelope, reason: str, detail: str, attempts: int) -> None:
        with self.db.tx() as c:
            if already_processed(c, env.eventId):
                return
            self.outbox.add(c, "transcription.failed", env.correlationId, {
                "recordingId": env.payload["recordingId"], "reason": reason, "attempts": attempts,
                "detail": detail})

    async def on_uploaded(self, env: Envelope, attempt: int) -> None:
        p = env.payload
        rec_id = p["recordingId"]
        if self.db.one("SELECT 1 FROM processed_events WHERE event_id = ?", (env.eventId,)):
            return  # повторная доставка того же события
        existing = self.db.one("SELECT * FROM transcripts WHERE recording_id = ?", (rec_id,))
        if existing:
            # перезапуск после FAILED_ANALYSIS: расшифровка уже есть — платный SpeechKit не вызываем
            with self.db.tx() as c:
                if already_processed(c, env.eventId):
                    return
                self._completed(c, env, existing["id"], json.loads(existing["segments"]))
            await self.outbox.flush()
            return

        try:
            # S3: временная ссылка на аудио
            resp = await self.recording.get(f"/internal/v1/recordings/{rec_id}/audio-url",
                                            correlation_id=env.correlationId)
            if resp.status_code in (404, 410):
                self._fail(env, "AUDIO_GONE", "Аудио удалено или запись не найдена", attempt)
                await self.outbox.flush()
                return
            audio = await self.recording.client.get(resp.json()["url"])
            if audio.status_code != 200:
                raise DependencyUnavailable(f"скачивание аудио: HTTP {audio.status_code}")
            text = await self.provider.recognize(audio.content, p.get("format", "OGG_OPUS"))
        except (ProviderUnavailable, DependencyUnavailable) as exc:
            if attempt < MAX_ATTEMPTS:
                log.warning("распознавание %s: попытка %s/%s не удалась (%s)", rec_id, attempt, MAX_ATTEMPTS, exc)
                raise RetryLater(str(exc)) from exc
            self._fail(env, "PROVIDER_UNAVAILABLE", str(exc), attempt)
            await self.outbox.flush()
            return
        except BadAudio as exc:
            self._fail(env, "BAD_AUDIO", str(exc), attempt)
            await self.outbox.flush()
            return

        segments = to_segments(text, p.get("durationSec"))
        tid = str(uuid.uuid4())
        with self.db.tx() as c:
            if already_processed(c, env.eventId):
                return
            c.execute("INSERT INTO transcripts VALUES (?,?,?,?,?,?,?,?)",
                      (tid, rec_id, p["venueId"], p.get("startedAt"), "ru-RU", "yandex-speechkit",
                       json.dumps(segments, ensure_ascii=False), now_iso()))
            self._completed(c, env, tid, segments)
        await self.outbox.flush()

    def _completed(self, c, env: Envelope, tid: str, segments: list[dict]) -> None:
        p = env.payload
        self.outbox.add(c, "transcription.completed", env.correlationId, {
            "recordingId": p["recordingId"], "transcriptId": tid, "venueId": p["venueId"],
            "startedAt": p.get("startedAt"), "wordsCount": sum(len(s["text"].split()) for s in segments),
            "speakersCount": len({s["speaker"] for s in segments})})


def create_app(ctx: Ctx | None = None):
    ctx = ctx or Ctx()

    @asynccontextmanager
    async def lifespan(_app):
        await ctx.start()
        yield
        await ctx.outbox.stop()
        await ctx.bus.close()

    app = create_base_app(SERVICE, "Распознавание речи (Yandex SpeechKit)", lifespan)
    app.state.ctx = ctx
    db = ctx.db

    @app.get("/api/v1/transcripts/by-recording/{rec_id}", tags=["public"])
    async def by_recording(rec_id: str):
        r = db.one("SELECT * FROM transcripts WHERE recording_id = ?", (rec_id,))
        if not r:
            raise ProblemError(404, f"Расшифровка записи {rec_id} не найдена")
        return tr_json(r)

    @app.get("/internal/v1/transcripts/{tid}", tags=["internal"], dependencies=[Depends(require_service_token)])
    async def internal_get(tid: str):
        r = db.one("SELECT * FROM transcripts WHERE id = ?", (tid,))
        if not r:
            raise ProblemError(404, f"Расшифровка {tid} не найдена")
        return tr_json(r)

    return app


app = create_app() if os.getenv("TALKSENSE_NO_AUTOAPP") != "1" else None
