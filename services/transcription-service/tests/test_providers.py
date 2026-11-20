"""Unit-тесты адаптера SpeechKit: разбор ответа и классификация ошибок."""
import httpx
import pytest

from transcription_service.providers import BadAudio, ProviderUnavailable, YandexSpeechKit, to_segments


def test_segments_by_speaker_labels():
    segs = to_segments("S1: А раф есть?\nS2: Закончился.", duration_sec=10)
    assert [(s["speaker"], s["text"]) for s in segs] == [("S1", "А раф есть?"), ("S2", "Закончился.")]
    assert segs[0]["startMs"] == 0 and segs[-1]["endMs"] == 10000


def test_plain_text_without_labels_is_one_segment():
    segs = to_segments("добрый день капучино пожалуйста")
    assert len(segs) == 1 and segs[0]["speaker"] == "S1"


def sk(handler):
    return YandexSpeechKit("http://sk", "key", "folder", transport=httpx.MockTransport(handler))


async def test_request_follows_speechkit_contract():
    seen = {}

    def handler(req: httpx.Request):
        seen.update(path=req.url.path, auth=req.headers["authorization"], params=dict(req.url.params))
        return httpx.Response(200, json={"result": "S1: привет"})

    assert await sk(handler).recognize(b"ogg") == "S1: привет"
    assert seen == {"path": "/speech/v1/stt:recognize", "auth": "Api-Key key",
                    "params": {"lang": "ru-RU", "format": "oggopus", "folderId": "folder"}}


@pytest.mark.parametrize("status, exc", [(503, ProviderUnavailable), (429, ProviderUnavailable), (400, BadAudio)])
async def test_errors_are_classified(status, exc):
    with pytest.raises(exc):
        await sk(lambda r: httpx.Response(status, json={})).recognize(b"x")


async def test_network_error_is_retryable():
    def boom(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderUnavailable):
        await sk(boom).recognize(b"x")
