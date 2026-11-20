"""Машина состояний саги «Обработка записи». Владелец состояния — recording-service.

    UPLOADED ──transcription.completed──► TRANSCRIBED ──analysis.completed──► ANALYZED
       │                                    ├──────────analysis.discarded──► DISCARDED
       └─transcription.failed─► FAILED_TRANSCRIPTION   └─analysis.failed──► FAILED_ANALYSIS
    FAILED_* ──reprocess──► UPLOADED
"""
from __future__ import annotations

TERMINAL = {"ANALYZED", "DISCARDED"}
FAILED = {"FAILED_TRANSCRIPTION", "FAILED_ANALYSIS"}

# событие -> (из каких статусов допустим переход, новый статус)
TRANSITIONS: dict[str, tuple[set[str], str]] = {
    "transcription.completed": ({"UPLOADED"}, "TRANSCRIBED"),
    "transcription.failed": ({"UPLOADED"}, "FAILED_TRANSCRIPTION"),
    # анализ может прийти раньше, чем recording обработал transcription.completed (порядок не гарантирован)
    "analysis.completed": ({"UPLOADED", "TRANSCRIBED", "FAILED_ANALYSIS"}, "ANALYZED"),
    "analysis.discarded": ({"UPLOADED", "TRANSCRIBED", "FAILED_ANALYSIS"}, "DISCARDED"),
    "analysis.failed": ({"UPLOADED", "TRANSCRIBED"}, "FAILED_ANALYSIS"),
}

# после каких событий исходное аудио больше не нужно (минимизация персональных данных)
PURGE_AUDIO_ON = {"analysis.completed", "analysis.discarded"}


def next_status(current: str, event_type: str) -> str | None:
    """Новый статус или None, если событие устарело/неприменимо (тогда оно игнорируется)."""
    rule = TRANSITIONS.get(event_type)
    if rule is None:
        return None
    allowed_from, target = rule
    return target if current in allowed_from else None


def can_reprocess(status: str, audio_purged: bool) -> bool:
    return status in FAILED and not audio_purged
