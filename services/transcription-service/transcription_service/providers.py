"""Провайдеры распознавания речи.

YandexSpeechKit — REST API SpeechKit v1 (синхронное распознавание коротких фрагментов ogg/opus,
POST https://stt.api.cloud.yandex.net/speech/v1/stt:recognize, авторизация Api-Key).
В ЛР4 вместо облака используется мок с тем же контрактом (mocks/speechkit) — адрес задаётся
переменной SPEECHKIT_URL, поэтому код адаптера один и тот же.
"""
from __future__ import annotations

import re

import httpx


class ProviderUnavailable(Exception):
    """Провайдер временно недоступен (5xx, таймаут) — имеет смысл повторить позже."""


class BadAudio(Exception):
    """Провайдер не смог распознать аудио (4xx) — повтор не поможет."""


SPEAKER_RE = re.compile(r"^\s*(S\d+)\s*:\s*(.*)$")


def to_segments(text: str, duration_sec: int | None = None) -> list[dict]:
    """Ответ SpeechKit v1 — сплошной текст. Если в нём есть метки дикторов «S1: …»
    (их добавляет диаризация в v3 и мок), разбиваем на сегменты по строкам."""
    lines = [ln for ln in text.splitlines() if ln.strip()] or [text]
    total_ms = (duration_sec or max(1, len(lines) * 3)) * 1000
    step = total_ms // max(1, len(lines))
    segments = []
    for i, ln in enumerate(lines):
        m = SPEAKER_RE.match(ln)
        speaker, txt = (m.group(1), m.group(2)) if m else ("S1", ln.strip())
        segments.append({"startMs": i * step, "endMs": (i + 1) * step, "speaker": speaker, "text": txt,
                         "confidence": 0.9})
    return segments


class YandexSpeechKit:
    def __init__(self, base_url: str, api_key: str, folder_id: str | None = None, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        self.api_key, self.folder_id = api_key, folder_id

    async def recognize(self, audio: bytes, fmt: str = "OGG_OPUS") -> str:
        params = {"lang": "ru-RU", "format": "oggopus" if fmt == "OGG_OPUS" else "lpcm"}
        if self.folder_id:
            params["folderId"] = self.folder_id
        try:
            resp = await self.client.post("/speech/v1/stt:recognize", params=params, content=audio,
                                          headers={"Authorization": f"Api-Key {self.api_key}"})
        except httpx.TransportError as exc:
            raise ProviderUnavailable(str(exc)) from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise ProviderUnavailable(f"SpeechKit HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise BadAudio(f"SpeechKit HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json().get("result", "")
