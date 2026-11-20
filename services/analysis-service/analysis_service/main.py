"""analysis-service — ИИ-анализ расшифровок (контракт: api/analysis-service.yaml).

Подписан на transcription.completed. Синхронно вызывает transcription-service (S4) и venue-service (S5),
затем GigaChat. Публикует analysis.completed / analysis.discarded / analysis.failed.
"""
from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import Query
from pydantic import BaseModel, Field

from talksense_common.db import Database, page
from talksense_common.events import Envelope, EventBus, Outbox, RetryLater, already_processed, make_bus, now_iso
from talksense_common.http import DependencyUnavailable, ServiceClient
from talksense_common.problems import ProblemError
from talksense_common.service import SERVICE_TOKEN, create_base_app

from . import logic
from .providers import GigaChat, LlmUnavailable

SERVICE = "analysis-service"
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))
log = logging.getLogger(SERVICE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY, recording_id TEXT NOT NULL, venue_id TEXT NOT NULL, type TEXT NOT NULL,
    occurred_at TEXT NOT NULL, summary TEXT NOT NULL, menu_item_id TEXT, requested_item TEXT, quote TEXT,
    severity TEXT NOT NULL, confidence REAL NOT NULL, review_status TEXT NOT NULL DEFAULT 'NEW', review_comment TEXT
);
CREATE INDEX IF NOT EXISTS ix_inc_venue_day ON incidents (venue_id, occurred_at);
CREATE TABLE IF NOT EXISTS analysis_runs (
    recording_id TEXT PRIMARY KEY, transcript_id TEXT NOT NULL, result TEXT NOT NULL, relevant_share REAL,
    created_at TEXT NOT NULL
);
"""


def inc_json(r) -> dict:
    return {"id": r["id"], "recordingId": r["recording_id"], "venueId": r["venue_id"], "type": r["type"],
            "occurredAt": r["occurred_at"], "summary": r["summary"], "menuItemId": r["menu_item_id"],
            "requestedItem": r["requested_item"], "quote": r["quote"], "severity": r["severity"],
            "confidence": r["confidence"], "reviewStatus": r["review_status"]}


class ReviewIncidentRequest(BaseModel):
    reviewStatus: str = Field(pattern="^(CONFIRMED|FALSE_POSITIVE)$")
    comment: str | None = Field(default=None, max_length=500)


class Ctx:
    def __init__(self, db_path: str | None = None, bus: EventBus | None = None, transcription_transport=None,
                 venue_transport=None, llm_transport=None):
        self.db = Database(db_path or os.getenv("DB_PATH", "data/analysis.db"), SCHEMA)
        self.bus = bus or make_bus(os.getenv("RABBITMQ_URL"))
        self.outbox = Outbox(self.db, self.bus, SERVICE)
        self.transcription = ServiceClient("transcription-service",
                                           os.getenv("TRANSCRIPTION_URL", "http://transcription-service:8000"),
                                           SERVICE_TOKEN, transport=transcription_transport)
        self.venue = ServiceClient("venue-service", os.getenv("VENUE_URL", "http://venue-service:8000"),
                                   SERVICE_TOKEN, transport=venue_transport)
        self.llm = GigaChat(os.getenv("GIGACHAT_URL", "http://gigachat-mock:8000"),
                            os.getenv("GIGACHAT_AUTH_URL", "http://gigachat-mock:8000"),
                            os.getenv("GIGACHAT_AUTH_KEY", "mock-key"), transport=llm_transport,
                            verify=os.getenv("GIGACHAT_CA_BUNDLE", "true") != "false")

    async def start(self, relay: bool = True) -> None:
        await self.bus.start()
        await self.bus.subscribe(SERVICE, ["transcription.completed"], self.on_transcribed)
        if relay:
            self.outbox.start()
            await self.outbox.flush()

    def _publish_once(self, env: Envelope, event_type: str, payload: dict) -> None:
        with self.db.tx() as c:
            if already_processed(c, env.eventId):
                return
            self.outbox.add(c, event_type, env.correlationId, payload)

    async def on_transcribed(self, env: Envelope, attempt: int) -> None:
        p = env.payload
        rec_id, venue_id = p["recordingId"], p["venueId"]
        if self.db.one("SELECT 1 FROM processed_events WHERE event_id = ?", (env.eventId,)):
            return
        try:
            # S4: текст расшифровки
            tr = await self.transcription.get(f"/internal/v1/transcripts/{p['transcriptId']}",
                                              correlation_id=env.correlationId)
            if tr.status_code == 404:
                self._publish_once(env, "analysis.failed", {"recordingId": rec_id, "transcriptId": p["transcriptId"],
                                                            "reason": "DEPENDENCY_UNAVAILABLE", "attempts": attempt})
                await self.outbox.flush()
                return
            # S5: меню заведения (для сопоставления позиций)
            mr = await self.venue.get(f"/internal/v1/venues/{venue_id}/menu", correlation_id=env.correlationId)
            if mr.status_code in (404, 409):  # точка удалена или отключена — анализ не нужен
                self._publish_once(env, "analysis.discarded", {"recordingId": rec_id,
                                                               "transcriptId": p["transcriptId"],
                                                               "reason": "VENUE_INACTIVE"})
                await self.outbox.flush()
                return
            segments, menu = tr.json()["segments"], mr.json()
            content = await self.llm.chat(logic.build_messages(segments, menu))
            answer = logic.parse_llm_answer(content)
        except (DependencyUnavailable, LlmUnavailable, logic.BadLlmAnswer) as exc:
            if attempt < MAX_ATTEMPTS:
                log.warning("анализ %s: попытка %s/%s не удалась (%s)", rec_id, attempt, MAX_ATTEMPTS, exc)
                raise RetryLater(str(exc)) from exc
            reason = "LLM_BAD_RESPONSE" if isinstance(exc, logic.BadLlmAnswer) else (
                "LLM_UNAVAILABLE" if isinstance(exc, LlmUnavailable) else "DEPENDENCY_UNAVAILABLE")
            self._publish_once(env, "analysis.failed", {"recordingId": rec_id, "transcriptId": p["transcriptId"],
                                                        "reason": reason, "attempts": attempt})
            await self.outbox.flush()
            return

        result = logic.finalize(answer, menu)
        occurred = p.get("startedAt") or now_iso()
        with self.db.tx() as c:
            if already_processed(c, env.eventId):
                return
            # повторный анализ той же записи (reprocess) заменяет прежние инциденты
            c.execute("DELETE FROM incidents WHERE recording_id = ?", (rec_id,))
            c.execute("INSERT OR REPLACE INTO analysis_runs VALUES (?,?,?,?,?)",
                      (rec_id, p["transcriptId"], answer.model_dump_json(), answer.relevantShare, now_iso()))
            if result["discard"]:
                self.outbox.add(c, "analysis.discarded", env.correlationId, {
                    "recordingId": rec_id, "transcriptId": p["transcriptId"], "reason": result["reason"]})
            else:
                short = []
                for inc in result["incidents"]:
                    iid = str(uuid.uuid4())
                    c.execute("INSERT INTO incidents (id, recording_id, venue_id, type, occurred_at, summary, "
                              "menu_item_id, requested_item, quote, severity, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              (iid, rec_id, venue_id, inc["type"], occurred, inc["summary"], inc["menuItemId"],
                               inc["requestedItem"], inc["quote"], inc["severity"], inc["confidence"]))
                    short.append({"incidentId": iid, "type": inc["type"], "severity": inc["severity"],
                                  "menuItemId": inc["menuItemId"], "requestedItem": inc["requestedItem"],
                                  "summary": inc["summary"]})
                self.outbox.add(c, "analysis.completed", env.correlationId, {
                    "recordingId": rec_id, "venueId": venue_id, "date": occurred[:10], "incidents": short,
                    "relevantShare": result["relevantShare"], "occurredAt": occurred})
        await self.outbox.flush()


def create_app(ctx: Ctx | None = None):
    ctx = ctx or Ctx()

    @asynccontextmanager
    async def lifespan(_app):
        await ctx.start()
        yield
        await ctx.outbox.stop()
        await ctx.bus.close()

    app = create_base_app(SERVICE, "ИИ-анализ расшифровок (GigaChat)", lifespan)
    app.state.ctx = ctx
    db = ctx.db

    @app.get("/api/v1/incidents", tags=["public"])
    async def list_incidents(venueId: uuid.UUID, from_: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
                             to: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"), type: str | None = None,
                             severity: str | None = Query(None, pattern="^(LOW|MEDIUM|HIGH)$"),
                             page_no: int = Query(0, alias="page", ge=0), size: int = Query(20, ge=1, le=100)):
        if from_ > to:
            raise ProblemError(400, "from должен быть не позже to", errors=[{"field": "from", "message": "> to"}])
        where = "venue_id = ? AND substr(occurred_at, 1, 10) BETWEEN ? AND ?"
        params: list = [str(venueId), from_, to]
        if type:
            types = [t.strip().upper() for t in type.split(",") if t.strip()]
            bad = [t for t in types if t not in logic.INCIDENT_TYPES]
            if bad:
                raise ProblemError(400, f"Неизвестный тип: {', '.join(bad)}", errors=[{"field": "type", "message": "enum"}])
            where += f" AND type IN ({','.join('?' * len(types))})"
            params += types
        if severity:
            where += " AND severity = ?"
            params.append(severity)
        total = db.one(f"SELECT count(*) AS c FROM incidents WHERE {where}", tuple(params))["c"]
        rows = db.query(f"SELECT * FROM incidents WHERE {where} ORDER BY occurred_at LIMIT ? OFFSET ?",
                        (*params, size, page_no * size))
        return page([inc_json(r) for r in rows], page_no, size, total)

    @app.get("/api/v1/incidents/{incident_id}", tags=["public"])
    async def get_incident(incident_id: str):
        r = db.one("SELECT * FROM incidents WHERE id = ?", (incident_id,))
        if not r:
            raise ProblemError(404, f"Инцидент {incident_id} не найден")
        return inc_json(r)

    @app.patch("/api/v1/incidents/{incident_id}", tags=["public"])
    async def review(incident_id: str, body: ReviewIncidentRequest):
        if not db.one("SELECT 1 FROM incidents WHERE id = ?", (incident_id,)):
            raise ProblemError(404, f"Инцидент {incident_id} не найден")
        db.execute("UPDATE incidents SET review_status = ?, review_comment = ? WHERE id = ?",
                   (body.reviewStatus, body.comment, incident_id))
        return inc_json(db.one("SELECT * FROM incidents WHERE id = ?", (incident_id,)))

    return app


app = create_app() if os.getenv("TALKSENSE_NO_AUTOAPP") != "1" else None
