"""Собственное хранилище сервиса: SQLite (по одному файлу на сервис).

В проекте (ЛР3) у сервисов PostgreSQL; методичка ЛР4 разрешает SQLite,
чтобы не поднимать шесть СУБД. Схема и запросы переносимы на PostgreSQL.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

COMMON_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    routing_key   TEXT NOT NULL,
    body          TEXT NOT NULL,
    published_at  TEXT
);
CREATE TABLE IF NOT EXISTS processed_events (
    event_id      TEXT PRIMARY KEY,
    processed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS idempotency (
    scope         TEXT NOT NULL,
    key           TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    status_code   INTEGER NOT NULL,
    response      TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (scope, key)
);
"""


class Database:
    def __init__(self, path: str, schema: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._conn.executescript(COMMON_SCHEMA + schema)

    @contextmanager
    def tx(self):
        """Транзакция: бизнес-изменения и запись в outbox фиксируются атомарно."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def page(items: list, page_no: int, size: int, total: int) -> dict:
    return {"items": items, "page": {"page": page_no, "size": size, "totalElements": total,
                                     "totalPages": (total + size - 1) // size if size else 0}}
