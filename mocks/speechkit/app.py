"""Мок Yandex SpeechKit v1 (POST /speech/v1/stt:recognize → {"result": "..."}).

Как «распознаёт»: демо-аудиофайлы (demo/audio/*.ogg) содержат заголовок TSDEMO и текст сценария —
мок возвращает этот текст. Для любого другого файла — короткую фразу-заглушку.
Режимы для демонстрации сбоев: POST /__admin/mode {"mode": "ok" | "fail" | "bad_audio"}.
"""
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="speechkit-mock")
state = {"mode": "ok", "calls": 0, "failed": 0}
MAGIC = b"TSDEMO\n"


@app.post("/speech/v1/stt:recognize")
async def recognize(request: Request, lang: str = "ru-RU", format: str = "oggopus",
                    authorization: str | None = Header(None)):
    state["calls"] += 1
    if not (authorization or "").startswith("Api-Key "):
        return JSONResponse({"error_code": "UNAUTHORIZED", "error_message": "Api-Key required"}, status_code=401)
    if state["mode"] == "fail":
        state["failed"] += 1
        return JSONResponse({"error_code": "INTERNAL", "error_message": "service temporarily unavailable"},
                            status_code=503)
    audio = await request.body()
    if state["mode"] == "bad_audio" or not audio:
        return JSONResponse({"error_code": "BAD_REQUEST", "error_message": "audio is corrupted"}, status_code=400)
    if audio.startswith(MAGIC):
        text = audio[len(MAGIC):].decode("utf-8", errors="replace").strip()
    else:
        text = "S1: Добрый день. S2: Здравствуйте."
    return {"result": text}


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
