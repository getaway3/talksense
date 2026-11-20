"""Мок GigaChat API с тем же контрактом: POST /api/v2/oauth и POST /api/v1/chat/completions.

Вместо языковой модели — простые правила по ключевым словам. Мок нужен, чтобы детерминированно
воспроизводить сценарии (конфликт, нет в наличии, просьба добавить в меню, личный разговор) без ключей и
расходов на реальный GigaChat. Режимы: POST /__admin/mode {"mode": "ok" | "fail" | "bad_json"}.
"""
import json
import re
import time
import uuid

from fastapi import FastAPI, Form, Header
from fastapi.responses import JSONResponse

app = FastAPI(title="gigachat-mock")
state = {"mode": "ok", "calls": 0}

CONFLICT = ("сколько можно", "безобраз", "позовите управляющ", "хамите", "верните деньги", "это издевательство")
COMPLAINT = ("холодный", "невкусн", "пересолен", "грязн", "долго жд", "чёрствый", "черствый")
PRAISE = ("очень вкусно", "спасибо большое", "лучший кофе", "вы молодцы")
NO_STOCK = ("закончил", "нет в наличии", "не осталось", "закончился", "закончилась")
NO_MENU = ("нет в меню", "такого нет", "не готовим", "у нас такого нет")
ASK_ITEM = re.compile(r"(?:а\s+)?(?:у вас\s+)?(?:есть\s+)?([а-яёa-z\- ]{3,40}?)\s+(?:есть|бывает)\??", re.I)


def analyze(dialog: str) -> dict:
    lines = [ln for ln in dialog.splitlines() if ln.strip()]
    incidents, useful = [], set()

    def add(i, **inc):
        useful.add(i)
        incidents.append(inc)

    for i, ln in enumerate(lines):
        low = ln.lower()
        if any(k in low for k in CONFLICT):
            add(i, type="CONFLICT", severity="HIGH", summary="Конфликт с гостем: недовольство и повышенный тон",
                requestedItem=None, quote="«" + ln.split(":", 1)[-1].strip()[:80] + "»", confidence=0.86)
            break
    for i, ln in enumerate(lines):
        m = ASK_ITEM.search(ln.split(":", 1)[-1])
        if not m:
            continue
        item = re.sub(r"^(?:а|ну|а у вас|у вас|скажите)\s+", "", m.group(1).strip(), flags=re.I).strip()
        reply = " ".join(lines[i + 1:i + 3]).lower()
        if any(k in reply for k in NO_STOCK):
            add(i, type="OUT_OF_STOCK", severity="MEDIUM", summary=f"Гость спросил «{item}» — нет в наличии",
                requestedItem=item, quote=None, confidence=0.84)
        elif any(k in reply for k in NO_MENU) or "добавьте" in reply:
            add(i, type="MENU_REQUEST", severity="LOW", summary=f"Гость просит добавить в меню «{item}»",
                requestedItem=item, quote=None, confidence=0.8)
    for i, ln in enumerate(lines):
        low = ln.lower()
        if any(k in low for k in COMPLAINT) and not any(k in low for k in CONFLICT):
            add(i, type="COMPLAINT", severity="MEDIUM", summary="Жалоба гостя на качество или ожидание",
                requestedItem=None, quote=None, confidence=0.75)
        if any(k in low for k in PRAISE):
            add(i, type="PRAISE", severity="LOW", summary="Гость поблагодарил", requestedItem=None, quote=None,
                confidence=0.8)
    share = round(min(1.0, (len(useful) * 2 + 0.1) / max(1, len(lines))), 2) if incidents else (
        0.05 if lines else 0.0)
    return {"relevantShare": share, "incidents": incidents}


@app.post("/api/v2/oauth")
async def oauth(scope: str = Form(...), authorization: str | None = Header(None)):
    if not (authorization or "").startswith("Basic "):
        return JSONResponse({"code": 6, "message": "Credentials doesn't match db data"}, status_code=401)
    return {"access_token": "mock-" + uuid.uuid4().hex, "expires_at": int((time.time() + 1800) * 1000)}


@app.post("/api/v1/chat/completions")
async def chat(body: dict, authorization: str | None = Header(None)):
    state["calls"] += 1
    if not (authorization or "").startswith("Bearer mock-"):
        return JSONResponse({"status": 401, "message": "Unauthorized"}, status_code=401)
    if state["mode"] == "fail":
        return JSONResponse({"status": 503, "message": "Service Unavailable"}, status_code=503)
    user = next((m["content"] for m in body.get("messages", []) if m["role"] == "user"), "")
    dialog = user.split("РАСШИФРОВКА:", 1)[-1]
    content = "Извините, не могу помочь." if state["mode"] == "bad_json" else (
        "```json\n" + json.dumps(analyze(dialog), ensure_ascii=False) + "\n```")
    return {"choices": [{"message": {"role": "assistant", "content": content}, "index": 0, "finish_reason": "stop"}],
            "created": int(time.time()), "model": body.get("model", "GigaChat"), "object": "chat.completion",
            "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": 120, "total_tokens": len(user) // 4 + 120}}


@app.post("/__admin/mode")
async def set_mode(body: dict):
    state["mode"] = body.get("mode", "ok")
    return state


@app.get("/__admin/state")
async def get_state():
    return state


@app.get("/health")
async def health():
    return {"status": "ok"}
