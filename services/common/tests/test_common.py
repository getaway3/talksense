"""Unit-тесты общей библиотеки: маршрутизация шины, повторы/DLQ, outbox, идемпотентность."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SERVICE_TOKEN", "test-service-token")

import pytest  # noqa: E402

from talksense_common import idempotency as idem  # noqa: E402
from talksense_common.db import Database  # noqa: E402
from talksense_common.events import InMemoryBus, Outbox, RetryLater, _match, already_processed  # noqa: E402
from talksense_common.problems import ProblemError  # noqa: E402


@pytest.mark.parametrize("pattern, key, ok", [
    ("analysis.completed", "analysis.completed", True), ("analysis.*", "analysis.failed", True),
    ("analysis.*", "transcription.failed", False), ("#", "anything.here", True),
])
def test_topic_match(pattern, key, ok):
    assert _match(pattern, key) is ok


async def test_retry_then_dlq():
    bus, calls = InMemoryBus(max_attempts=3), []

    async def handler(env, attempt):
        calls.append(attempt)
        raise RetryLater("down")

    await bus.subscribe("s", ["a.b"], handler)
    db = Database(":memory:", "")
    ob = Outbox(db, bus, "test")
    with db.tx() as c:
        ob.add(c, "a.b", "cid", {"x": 1})
    assert await ob.flush() == 1 and await ob.flush() == 0
    await bus.drain()
    assert calls == [1, 2, 3] and len(bus.dlq) == 1


async def test_outbox_keeps_event_when_broker_down():
    class DownBus(InMemoryBus):
        async def publish(self, rk, body):
            raise ConnectionError("broker down")

    db = Database(":memory:", "")
    ob = Outbox(db, DownBus(), "test")
    with db.tx() as c:
        ob.add(c, "a.b", "cid", {})
    assert await ob.flush() == 0
    assert db.one("SELECT count(*) c FROM outbox WHERE published_at IS NULL")["c"] == 1


def test_outbox_rolls_back_with_business_change():
    db = Database(":memory:", "CREATE TABLE t (x INTEGER);")
    ob = Outbox(db, InMemoryBus(), "test")
    with pytest.raises(RuntimeError):
        with db.tx() as c:
            c.execute("INSERT INTO t VALUES (1)")
            ob.add(c, "a.b", "cid", {})
            raise RuntimeError("fail in the middle")
    assert db.one("SELECT count(*) c FROM outbox")["c"] == 0 and db.one("SELECT count(*) c FROM t")["c"] == 0


def test_already_processed():
    db = Database(":memory:", "")
    with db.tx() as c:
        assert already_processed(c, "e1") is False
        assert already_processed(c, "e1") is True


def test_idempotency_lookup():
    db = Database(":memory:", "")
    h = idem.request_hash({"a": 1})
    assert idem.lookup(db, "op", "k", h) is None
    with db.tx() as c:
        idem.store(c, "op", "k", h, 201, {"id": 1})
    assert idem.lookup(db, "op", "k", h).status_code == 201
    with pytest.raises(ProblemError) as e:
        idem.lookup(db, "op", "k", idem.request_hash({"a": 2}))
    assert e.value.status == 422
