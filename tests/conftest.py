"""Тестовый стенд: все 6 сервисов + моки SpeechKit/GigaChat в одном процессе.

* брокер — InMemoryBus (та же семантика подписок, повторов и DLQ, что у RabbitBus);
* синхронные вызовы между сервисами идут через httpx.ASGITransport — настоящие HTTP-запросы
  к настоящим FastAPI-приложениям, только без сети;
* у каждого сервиса своя БД SQLite во временном каталоге.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

os.environ["TALKSENSE_NO_AUTOAPP"] = "1"
os.environ.setdefault("SERVICE_TOKEN", "test-service-token")
ROOT = Path(__file__).resolve().parents[1]
for p in ["services/common", *[f"services/{s}-service" for s in
                               ("venue", "device", "recording", "transcription", "analysis", "report")]]:
    sys.path.insert(0, str(ROOT / p))

import httpx  # noqa: E402
import pytest  # noqa: E402

from talksense_common.events import InMemoryBus  # noqa: E402


def _load_mock(name: str):
    spec = importlib.util.spec_from_file_location(f"mock_{name}", ROOT / "mocks" / name / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class System:
    async def settle(self) -> None:
        """Доставить все события, включая порождённые обработчиками."""
        while await self.bus.drain():
            pass

    def demo_audio(self, script: str) -> bytes:
        return b"TSDEMO\n" + (ROOT / "demo" / "scripts" / script).read_bytes()

    async def upload(self, script: str, chunk_id: str, token: str | None = None, started: str = "2026-11-20T10:15:30Z"):
        from device_service.main import DEMO_TOKEN
        return await self.recording.post(
            "/device/v1/recordings",
            headers={"X-Device-Token": token or DEMO_TOKEN, "Idempotency-Key": chunk_id},
            files={"audio": (f"{chunk_id}.ogg", self.demo_audio(script), "audio/ogg")},
            data={"startedAt": started, "durationSec": "30"})


@pytest.fixture
async def system(tmp_path):
    from analysis_service import main as analysis
    from device_service import main as device
    from recording_service import main as recording
    from report_service import main as report
    from transcription_service import main as transcription
    from venue_service import main as venue

    sk, gc = _load_mock("speechkit"), _load_mock("gigachat")
    sk.state.update(mode="ok", calls=0, failed=0)
    gc.state.update(mode="ok", calls=0)
    T = httpx.ASGITransport
    bus = InMemoryBus(max_attempts=5)

    s = System()
    s.bus, s.speechkit, s.gigachat = bus, sk.state, gc.state
    s.venue_ctx = venue.Ctx(str(tmp_path / "venue.db"), bus)
    s.venue_ctx.seed_demo()
    s.venue_app = venue.create_app(s.venue_ctx)
    s.device_ctx = device.Ctx(str(tmp_path / "device.db"), bus, venue_transport=T(s.venue_app))
    s.device_ctx.seed_demo()
    s.device_app = device.create_app(s.device_ctx)
    s.recording_ctx = recording.Ctx(str(tmp_path / "recording.db"), bus, device_transport=T(s.device_app),
                                    audio_dir=str(tmp_path / "audio"))
    s.recording_app = recording.create_app(s.recording_ctx)
    s.transcription_ctx = transcription.Ctx(str(tmp_path / "tr.db"), bus, recording_transport=T(s.recording_app),
                                            speechkit_transport=T(sk.app))
    s.transcription_app = transcription.create_app(s.transcription_ctx)
    s.analysis_ctx = analysis.Ctx(str(tmp_path / "an.db"), bus, transcription_transport=T(s.transcription_app),
                                  venue_transport=T(s.venue_app), llm_transport=T(gc.app))
    s.analysis_app = analysis.create_app(s.analysis_ctx)
    s.report_ctx = report.Ctx(str(tmp_path / "report.db"), bus, venue_transport=T(s.venue_app))
    s.report_app = report.create_app(s.report_ctx)
    for ctx in (s.venue_ctx, s.device_ctx, s.recording_ctx, s.transcription_ctx, s.analysis_ctx, s.report_ctx):
        await ctx.start(relay=False)

    clients = []
    for name in ("venue", "device", "recording", "transcription", "analysis", "report"):
        c = httpx.AsyncClient(transport=T(getattr(s, f"{name}_app")), base_url=f"http://{name}-service")
        setattr(s, name, c)
        clients.append(c)
    yield s
    for c in clients:
        await c.aclose()
