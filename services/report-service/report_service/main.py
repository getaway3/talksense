"""report-service — ежедневные выгрузки (контракт: api/report-service.yaml).

Подписан на analysis.completed, синхронно получает название и часовой пояс точки у venue-service (S6).
Хранит отчёт за день как документ (JSON) + построчно инциденты для экспорта CSV.
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import Query
from fastapi.responses import Response

from talksense_common.db import Database, page
from talksense_common.events import Envelope, EventBus, Outbox, RetryLater, already_processed, make_bus, now_iso
from talksense_common.http import DependencyUnavailable, ServiceClient
from talksense_common.problems import ProblemError
from talksense_common.service import SERVICE_TOKEN, create_base_app

from . import aggregate

SERVICE = "report-service"

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_reports (
    venue_id TEXT NOT NULL, date TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (venue_id, date)
);
CREATE TABLE IF NOT EXISTS applied_recordings (recording_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS report_incidents (
    incident_id TEXT PRIMARY KEY, venue_id TEXT NOT NULL, date TEXT NOT NULL, occurred_at TEXT, type TEXT NOT NULL,
    severity TEXT NOT NULL, requested_item TEXT, summary TEXT NOT NULL
);
"""


class Ctx:
    def __init__(self, db_path: str | None = None, bus: EventBus | None = None, venue_transport=None):
        self.db = Database(db_path or os.getenv("DB_PATH", "data/report.db"), SCHEMA)
        self.bus = bus or make_bus(os.getenv("RABBITMQ_URL"))
        self.outbox = Outbox(self.db, self.bus, SERVICE)
        self.venue = ServiceClient("venue-service", os.getenv("VENUE_URL", "http://venue-service:8000"),
                                   SERVICE_TOKEN, transport=venue_transport)
        self.venue_names: dict[str, str] = {}  # кэш (в проекте — Redis)

    async def start(self, relay: bool = True) -> None:
        await self.bus.start()
        await self.bus.subscribe(SERVICE, ["analysis.completed"], self.on_analysis)
        if relay:
            self.outbox.start()

    async def venue_name(self, venue_id: str, cid: str) -> str | None:
        if venue_id not in self.venue_names:
            try:
                r = await self.venue.get(f"/internal/v1/venues/{venue_id}", correlation_id=cid)  # S6
            except DependencyUnavailable as exc:
                raise RetryLater(str(exc)) from exc
            if r.status_code == 200:
                self.venue_names[venue_id] = r.json()["name"]
            else:
                return None
        return self.venue_names[venue_id]

    async def on_analysis(self, env: Envelope, attempt: int) -> None:
        p = env.payload
        name = await self.venue_name(p["venueId"], env.correlationId)
        with self.db.tx() as c:
            if already_processed(c, env.eventId):
                return
            # одна запись учитывается в отчёте ровно один раз, даже если анализ повторили
            if c.execute("INSERT OR IGNORE INTO applied_recordings VALUES (?)", (p["recordingId"],)).rowcount == 0:
                return
            row = c.execute("SELECT body FROM daily_reports WHERE venue_id = ? AND date = ?",
                            (p["venueId"], p["date"])).fetchone()
            report = json.loads(row["body"]) if row else aggregate.empty_report(p["venueId"], p["date"], name)
            report["venueName"] = report.get("venueName") or name
            aggregate.apply_analysis(report, p, now_iso())
            c.execute("INSERT OR REPLACE INTO daily_reports VALUES (?,?,?)",
                      (p["venueId"], p["date"], json.dumps(report, ensure_ascii=False)))
            for inc in p.get("incidents", []):
                c.execute("INSERT OR IGNORE INTO report_incidents VALUES (?,?,?,?,?,?,?,?)",
                          (inc["incidentId"], p["venueId"], p["date"], p.get("occurredAt"), inc["type"],
                           inc["severity"], inc.get("requestedItem"), inc["summary"]))


def create_app(ctx: Ctx | None = None):
    ctx = ctx or Ctx()

    @asynccontextmanager
    async def lifespan(_app):
        await ctx.start()
        yield
        await ctx.outbox.stop()
        await ctx.bus.close()

    app = create_base_app(SERVICE, "Ежедневные выгрузки для сайта", lifespan)
    app.state.ctx = ctx
    db = ctx.db
    DATE = r"^\d{4}-\d{2}-\d{2}$"

    def get_report(venue_id: str, date: str) -> dict:
        r = db.one("SELECT body FROM daily_reports WHERE venue_id = ? AND date = ?", (venue_id, date))
        if not r:
            raise ProblemError(404, f"Отчёта за {date} нет — за этот день ещё нет проанализированных записей")
        return json.loads(r["body"])

    @app.get("/api/v1/reports/daily", tags=["public"])
    async def list_reports(venueId: uuid.UUID, from_: str = Query(..., alias="from", pattern=DATE),
                           to: str = Query(..., pattern=DATE), page_no: int = Query(0, alias="page", ge=0),
                           size: int = Query(20, ge=1, le=100)):
        args = (str(venueId), from_, to)
        total = db.one("SELECT count(*) AS c FROM daily_reports WHERE venue_id = ? AND date BETWEEN ? AND ?", args)["c"]
        rows = db.query("SELECT body FROM daily_reports WHERE venue_id = ? AND date BETWEEN ? AND ? "
                        "ORDER BY date DESC LIMIT ? OFFSET ?", (*args, size, page_no * size))
        return page([json.loads(r["body"]) for r in rows], page_no, size, total)

    @app.get("/api/v1/reports/daily/{venue_id}/{date}", tags=["public"])
    async def daily(venue_id: str, date: str):
        return get_report(venue_id, date)

    @app.get("/api/v1/reports/daily/{venue_id}/{date}/export", tags=["public"])
    async def export(venue_id: str, date: str):
        report = get_report(venue_id, date)
        rows = db.query("SELECT * FROM report_incidents WHERE venue_id = ? AND date = ? ORDER BY occurred_at",
                        (venue_id, date))
        incidents = [{"occurredAt": r["occurred_at"], "type": r["type"], "severity": r["severity"],
                      "requestedItem": r["requested_item"], "summary": r["summary"]} for r in rows]
        return Response(aggregate.to_csv(report, incidents), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="report-{date}.csv"'})

    return app


app = create_app() if os.getenv("TALKSENSE_NO_AUTOAPP") != "1" else None
