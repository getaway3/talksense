"""Unit-тесты машины состояний саги и подписи ссылок на аудио."""
import time

import pytest

from recording_service import saga
from recording_service.main import sign


@pytest.mark.parametrize("current, event, expected", [
    ("UPLOADED", "transcription.completed", "TRANSCRIBED"),
    ("UPLOADED", "transcription.failed", "FAILED_TRANSCRIPTION"),
    ("TRANSCRIBED", "analysis.completed", "ANALYZED"),
    ("UPLOADED", "analysis.completed", "ANALYZED"),          # события пришли не по порядку
    ("TRANSCRIBED", "analysis.discarded", "DISCARDED"),
    ("TRANSCRIBED", "analysis.failed", "FAILED_ANALYSIS"),
    ("ANALYZED", "transcription.completed", None),           # устаревшее событие игнорируется
    ("ANALYZED", "analysis.failed", None),
    ("DISCARDED", "analysis.completed", None),
    ("UPLOADED", "unknown.event", None),
])
def test_next_status(current, event, expected):
    assert saga.next_status(current, event) == expected


@pytest.mark.parametrize("status, purged, ok", [
    ("FAILED_TRANSCRIPTION", False, True), ("FAILED_ANALYSIS", False, True),
    ("FAILED_ANALYSIS", True, False), ("ANALYZED", False, False), ("UPLOADED", False, False),
])
def test_can_reprocess(status, purged, ok):
    assert saga.can_reprocess(status, purged) is ok


def test_audio_purged_only_after_final_analysis():
    assert saga.PURGE_AUDIO_ON == {"analysis.completed", "analysis.discarded"}


def test_signature_depends_on_record_and_expiry():
    exp = int(time.time()) + 60
    assert sign("a", exp) == sign("a", exp)
    assert sign("a", exp) != sign("b", exp)
    assert sign("a", exp) != sign("a", exp + 1)
