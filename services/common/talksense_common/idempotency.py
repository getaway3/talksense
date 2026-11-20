"""Idempotency-Key для повторяемых операций (создание, загрузка, перезапуск).

Повтор с тем же ключом и тем же телом возвращает сохранённый ответ,
повтор с тем же ключом и другим телом — 422 (Problem).
"""
from __future__ import annotations

import hashlib
import json

from fastapi.responses import JSONResponse

from .db import Database, dumps
from .problems import ProblemError


def request_hash(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else json.dumps(p, sort_keys=True, default=str).encode())
    return h.hexdigest()


def require_key(key: str | None) -> str:
    if not key:
        raise ProblemError(400, "Заголовок Idempotency-Key обязателен для этой операции",
                           errors=[{"field": "Idempotency-Key", "message": "required"}])
    return key


def lookup(db: Database, scope: str, key: str, req_hash: str, replay_status: int | None = None) -> JSONResponse | None:
    row = db.one("SELECT request_hash, status_code, response FROM idempotency WHERE scope = ? AND key = ?",
                 (scope, key))
    if row is None:
        return None
    if row["request_hash"] != req_hash:
        raise ProblemError(422, "Idempotency-Key уже использован с другим содержимым запроса")
    return JSONResponse(json.loads(row["response"]), status_code=replay_status or row["status_code"],
                        headers={"Idempotent-Replayed": "true"})


def store(conn, scope: str, key: str, req_hash: str, status_code: int, response: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO idempotency (scope, key, request_hash, status_code, response) "
                 "VALUES (?, ?, ?, ?, ?)", (scope, key, req_hash, status_code, dumps(response)))
