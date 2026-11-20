"""Бизнес-логика анализа расшифровки (без ввода-вывода — покрыта unit-тестами).

1. build_messages — промпт для GigaChat: расшифровка + меню, строгий JSON на выходе.
2. parse_llm_answer — валидация ответа модели по схеме (модель может вернуть мусор).
3. match_menu_item — сопоставление «как сказал гость» с позицией меню (название + синонимы).
4. finalize — правила поверх ответа модели:
   * позиция есть в меню → это OUT_OF_STOCK, даже если модель решила MENU_REQUEST, и наоборот;
   * нет инцидентов и мало полезного текста → запись отбрасывается (личный разговор / шум).
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher

from pydantic import BaseModel, Field, ValidationError, field_validator

INCIDENT_TYPES = ("CONFLICT", "OUT_OF_STOCK", "MENU_REQUEST", "COMPLAINT", "PRAISE", "SERVICE_ISSUE")
SEVERITIES = ("LOW", "MEDIUM", "HIGH")
DISCARD_THRESHOLD = 0.2  # доля полезного текста, ниже которой запись без инцидентов отбрасывается

SYSTEM_PROMPT = """Ты анализируешь расшифровку разговора у кассы кофейни или пекарни.
Задача: отбросить личные разговоры сотрудников и «воду», выделить только события, полезные владельцу бизнеса.
Типы событий: CONFLICT (конфликт, повышенный тон), OUT_OF_STOCK (гость спросил позицию, её нет в наличии),
MENU_REQUEST (гость просит то, чего нет в меню), COMPLAINT (жалоба), PRAISE (благодарность),
SERVICE_ISSUE (нарушение стандарта обслуживания).
Не включай имена, телефоны и другие персональные данные — цитаты обезличивай.
Ответь строго JSON без пояснений:
{"relevantShare": число 0..1, "incidents": [{"type": "...", "severity": "LOW|MEDIUM|HIGH",
 "summary": "кратко по-русски", "requestedItem": "название позиции или null", "quote": "короткая цитата",
 "confidence": число 0..1}]}"""


class LlmIncident(BaseModel):
    type: str
    severity: str = "MEDIUM"
    summary: str = Field(min_length=1, max_length=500)
    requestedItem: str | None = None
    quote: str | None = Field(default=None, max_length=300)
    confidence: float = Field(default=0.7, ge=0, le=1)

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        v = v.upper()
        if v not in INCIDENT_TYPES:
            raise ValueError(f"неизвестный тип {v}")
        return v

    @field_validator("severity")
    @classmethod
    def _sev(cls, v: str) -> str:
        v = v.upper()
        return v if v in SEVERITIES else "MEDIUM"


class LlmAnswer(BaseModel):
    relevantShare: float = Field(ge=0, le=1)
    incidents: list[LlmIncident] = []


class BadLlmAnswer(Exception):
    pass


def build_messages(segments: list[dict], menu: list[dict]) -> list[dict]:
    menu_short = [{"name": m["name"], "aliases": m.get("aliases", []), "available": m["available"]} for m in menu]
    dialog = "\n".join(f'{s["speaker"]}: {s["text"]}' for s in segments)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"МЕНЮ:\n{json.dumps(menu_short, ensure_ascii=False)}\n\nРАСШИФРОВКА:\n{dialog}"},
    ]


def parse_llm_answer(content: str) -> LlmAnswer:
    """Модели любят оборачивать JSON в ```json …``` — вырезаем и валидируем."""
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise BadLlmAnswer("в ответе нет JSON")
    try:
        return LlmAnswer.model_validate_json(m.group(0))
    except ValidationError as exc:
        raise BadLlmAnswer(f"ответ не соответствует схеме: {exc.errors()[:2]}") from exc


def _norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[^\w\s-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


STOP_WORDS = {"а", "и", "на", "с", "со", "в", "у", "вас", "есть", "пожалуйста", "ещё", "еще", "один", "одну"}


def _stems(s: str) -> set[str]:
    """Грубый стемминг для русского: первые 5 букв слова (лавандовый/лавандовым → «лаван»)."""
    return {w[:5] for w in s.split() if w not in STOP_WORDS and len(w) > 2}


def match_menu_item(requested: str | None, menu: list[dict], threshold: float = 0.72) -> dict | None:
    """Ищем позицию по названию и синонимам: вхождение строки, совпадение основ слов
    в любом порядке («миндальный круассан» = «Круассан миндальный») или нечёткое сходство."""
    if not requested:
        return None
    req = _norm(requested)
    req_stems = _stems(req)
    best, best_score = None, 0.0
    for item in menu:
        for name in [item["name"], *item.get("aliases", [])]:
            n = _norm(name)
            if not n:
                continue
            if n == req or n in req or req in n:
                return item
            if req_stems and req_stems == _stems(n):
                return item
            score = SequenceMatcher(None, n, req).ratio()
            if score > best_score:
                best, best_score = item, score
    return best if best_score >= threshold else None


def finalize(answer: LlmAnswer, menu: list[dict]) -> dict:
    """Итог анализа: {"discard": bool, "reason": ..., "relevantShare": .., "incidents": [...]}"""
    incidents = []
    for inc in answer.incidents:
        item = match_menu_item(inc.requestedItem, menu) if inc.type in ("OUT_OF_STOCK", "MENU_REQUEST") else None
        t = inc.type
        if t == "MENU_REQUEST" and item is not None:
            t = "MENU_REQUEST"   # позиция в меню есть — значит, её не было в наличии
        elif t == "OUT_OF_STOCK" and item is None and inc.requestedItem:
            t = "MENU_REQUEST"   # такой позиции в меню нет — это запрос на добавление
        incidents.append({**inc.model_dump(), "type": t, "menuItemId": item["id"] if item else None,
                          "requestedItem": item["name"] if item else inc.requestedItem})
    if not incidents and answer.relevantShare < DISCARD_THRESHOLD:
        return {"discard": True, "reason": "PERSONAL_ONLY" if answer.relevantShare > 0 else "NOISE",
                "relevantShare": answer.relevantShare, "incidents": []}
    return {"discard": False, "reason": None, "relevantShare": answer.relevantShare, "incidents": incidents}
