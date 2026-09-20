"""consumer.py: readiness probes and the consumer-group poll loop.

The group behaviour (XGROUP CREATE, XREADGROUP, XACK-after-persist, pending
re-read, XAUTOCLAIM) is exercised against `_FakeAsyncRedis`, a scripted async
double — the repo's established pattern is a hand-rolled fake rather than a
fakeredis dependency (see packages/prahari-common/tests/test_bus.py).
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

from google.protobuf.timestamp_pb2 import Timestamp
from prahari.v1 import common_pb2, events_pb2
from redis.exceptions import ResponseError

from prahari_correlation.consumer import CONSUMER_GROUP, DetectionConsumer
from prahari_correlation.store import DetectionStore

STREAM = "prahari:detections"


def _detection(detection_id: str = "D1", plate: str = "GJ01AB1234") -> events_pb2.VehicleDetection:
    ts = Timestamp()
    ts.FromDatetime(datetime.fromtimestamp(1000.0, tz=UTC))
    return events_pb2.VehicleDetection(
        detection_id=detection_id,
        camera_id="CAM-A",
        observed_at=common_pb2.StreamTime(wall_clock=ts),
        plate=events_pb2.PlateReading(normalised_text=plate),
        evidence_ref=f"evidence/{detection_id}",
    )


def _entry(
    entry_id: bytes,
    detection: events_pb2.VehicleDetection | None = None,
    raw: bytes | None = None,
):
    payload = raw if raw is not None else detection.SerializeToString()
    return (entry_id, {b"detection": payload})


class _FakeAsyncRedis:
    """Scripted stand-in for `redis.asyncio.Redis`: queued xreadgroup/xautoclaim
    responses, recorded group-create/ack calls, optional BUSYGROUP."""

    def __init__(self) -> None:
        self.groups_created: list[tuple] = []
        self.read_ids: list[str] = []
        self.acked: list[tuple] = []
        self.claim_calls = 0
        self.read_script: deque = deque()
        self.claim_script: deque = deque()
        self.group_exists = False
        self.closed = False

    async def xgroup_create(self, name, groupname, id=None, mkstream=False):  # noqa: ANN001, ANN202
        self.groups_created.append((name, groupname, id, mkstream))
        if self.group_exists:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        self.group_exists = True
        return True

    async def xreadgroup(self, groupname, consumername, streams, count=None, block=None):  # noqa: ANN001, ANN202
        _key, last_id = next(iter(streams.items()))
        self.read_ids.append(last_id)
        if self.read_script:
            return self.read_script.popleft()
        return []

    async def xautoclaim(  # noqa: ANN202
        self,
        name,
        groupname,
        consumername,
        min_idle_time,
        start_id="0-0",
        count=None,  # noqa: ANN001
    ):
        self.claim_calls += 1
        if self.claim_script:
            return self.claim_script.popleft()
        return [b"0-0", [], []]

    async def xack(self, name, groupname, *ids):  # noqa: ANN001, ANN202
        self.acked.append(tuple(ids))
        return len(ids)

    async def aclose(self) -> None:
        self.closed = True


class _FakeDb:
    """Records `insert_detection` calls; `fail` simulates Postgres down."""

    def __init__(self, fail: bool = False) -> None:
        self.inserted: list[tuple[events_pb2.VehicleDetection, str]] = []
        self.fail = fail

    async def insert_detection(self, detection, stream_entry_id: str) -> None:
        if self.fail:
            raise ConnectionError("simulated postgres outage")
        self.inserted.append((detection, stream_entry_id))


def _consumer(
    client: _FakeAsyncRedis,
    store: DetectionStore | None = None,
    db: _FakeDb | None = None,
) -> DetectionConsumer:
    return DetectionConsumer(
        "redis://irrelevant",
        STREAM,
        store or DetectionStore(10, 10),
        db=db,
        client=client,
        consumer_name="test-consumer",
        block_ms=10,
    )


def test_no_redis_url_means_never_connected() -> None:
    consumer = DetectionConsumer(None, STREAM, DetectionStore(10, 10))
    assert not consumer.is_connected()


def test_start_with_no_redis_url_does_not_spawn_a_task() -> None:
    consumer = DetectionConsumer(None, STREAM, DetectionStore(10, 10))
    consumer.start()
    assert consumer._task is None  # noqa: SLF001 -- the only externally-checkable proof


def test_is_connected_reflects_a_successful_ping() -> None:
    class _OkPing:
        def ping(self) -> bool:
            return True

    consumer = DetectionConsumer(
        "redis://irrelevant", STREAM, DetectionStore(10, 10), ping_client=_OkPing()
    )
    assert consumer.is_connected()


def test_is_connected_is_false_when_ping_raises() -> None:
    class _BrokenPing:
        def ping(self) -> bool:
            raise ConnectionError("simulated redis outage")

    consumer = DetectionConsumer(
        "redis://irrelevant",
        STREAM,
        DetectionStore(10, 10),
        ping_client=_BrokenPing(),
    )
    assert not consumer.is_connected()


def test_is_connected_is_false_when_the_poll_task_has_died() -> None:
    # Redis itself is perfectly reachable here (ping succeeds) -- this is
    # the "poll task died, Redis did not" case /readyz exists to catch,
    # which a PING-only check would silently miss.
    class _OkPing:
        def ping(self) -> bool:
            return True

    class _DoneTask:
        def done(self) -> bool:
            return True

    consumer = DetectionConsumer(
        "redis://irrelevant", STREAM, DetectionStore(10, 10), ping_client=_OkPing()
    )
    consumer._task = _DoneTask()  # noqa: SLF001 -- simulating a poll task that has exited

    assert not consumer.is_connected()


async def test_stop_before_start_does_not_raise() -> None:
    consumer = DetectionConsumer(None, STREAM, DetectionStore(10, 10))
    await consumer.stop()  # must be a no-op, not an AttributeError on a never-started task


async def test_poll_once_creates_the_group_then_processes_and_acks() -> None:
    client = _FakeAsyncRedis()
    db = _FakeDb()
    store = DetectionStore(10, 10)
    client.read_script.append([(STREAM.encode(), [_entry(b"1-0", _detection())])])

    consumer = _consumer(client, store, db)
    assert await consumer._poll_once() == 1

    # Group created from "0" with mkstream: a fresh deploy replays the
    # stream's retained history, backfilling Postgres.
    assert client.groups_created == [(STREAM, CONSUMER_GROUP, "0", True)]
    assert len(store.by_plate("GJ01AB1234")) == 1
    assert [entry_id for _d, entry_id in db.inserted] == ["1-0"]
    assert client.acked == [(b"1-0",)]


async def test_busygroup_is_tolerated() -> None:
    client = _FakeAsyncRedis()
    client.group_exists = True  # group already there: the normal restart path
    client.read_script.append([(STREAM.encode(), [_entry(b"1-0", _detection())])])

    consumer = _consumer(client)
    assert await consumer._poll_once() == 1
    assert client.acked == [(b"1-0",)]


async def test_a_persist_failure_leaves_the_entry_pending_for_redelivery() -> None:
    client = _FakeAsyncRedis()
    db = _FakeDb(fail=True)
    store = DetectionStore(10, 10)
    client.read_script.append([(STREAM.encode(), [_entry(b"1-0", _detection())])])

    consumer = _consumer(client, store, db)
    assert await consumer._poll_once() == 1
    assert client.acked == []  # nothing acked — the entry stays pending
    assert consumer._own_pending  # noqa: SLF001

    # Postgres recovers; the next poll re-reads THIS consumer's pending list
    # ("0", not ">") and retries. The store reports the entry as a duplicate
    # — the persist must still be attempted (a duplicate is not proof of
    # durability).
    client.read_script.append([(STREAM.encode(), [_entry(b"1-0", _detection())])])
    db.fail = False
    assert await consumer._poll_once() == 1

    assert client.read_ids == [">", "0"]
    assert [entry_id for _d, entry_id in db.inserted] == ["1-0"]
    assert client.acked == [(b"1-0",)]
    assert not consumer._own_pending  # noqa: SLF001


async def test_an_undecodable_entry_is_acked_not_left_pending_forever() -> None:
    client = _FakeAsyncRedis()
    db = _FakeDb()
    client.read_script.append([(STREAM.encode(), [_entry(b"1-0", raw=b"not a proto")])])

    consumer = _consumer(client, db=db)
    assert await consumer._poll_once() == 1
    # A poison pill can never succeed on redelivery, so it is acked and
    # counted rather than allowed to wedge the group.
    assert client.acked == [(b"1-0",)]
    assert db.inserted == []
    assert not consumer._own_pending  # noqa: SLF001


async def test_claimed_orphaned_entries_are_processed_and_acked() -> None:
    # A dead replica's pending entries are adopted via XAUTOCLAIM once idle
    # past the claim threshold — the nothing-is-lost half of the group story.
    client = _FakeAsyncRedis()
    db = _FakeDb()
    client.claim_script.append([b"2-1", [_entry(b"9-9", _detection("D9"))], []])

    consumer = _consumer(client, db=db)
    assert await consumer._poll_once() == 1
    assert client.claim_calls == 1
    assert [entry_id for _d, entry_id in db.inserted] == ["9-9"]
    assert client.acked == [(b"9-9",)]


async def test_rejected_detections_are_acked_but_never_persisted() -> None:
    # A detection with no usable wall_clock is dropped by the store AND must
    # not reach Postgres — the two query paths must agree on what a route is.
    ts = Timestamp()  # unset: seconds=0
    detection = events_pb2.VehicleDetection(
        detection_id="D-NOTS",
        camera_id="CAM-A",
        observed_at=common_pb2.StreamTime(wall_clock=ts),
        plate=events_pb2.PlateReading(normalised_text="GJ01AB1234"),
    )
    client = _FakeAsyncRedis()
    db = _FakeDb()
    client.read_script.append([(STREAM.encode(), [_entry(b"1-0", detection)])])

    consumer = _consumer(client, db=db)
    assert await consumer._poll_once() == 1
    assert client.acked == [(b"1-0",)]
    assert db.inserted == []


def test_pending_count_reports_the_group_backlog() -> None:
    class _Probe:
        def ping(self) -> bool:
            return True

        def xpending(self, stream, group):  # noqa: ANN001, ANN202
            assert (stream, group) == (STREAM, CONSUMER_GROUP)
            return {"pending": 7}

    consumer = DetectionConsumer(
        "redis://irrelevant", STREAM, DetectionStore(10, 10), ping_client=_Probe()
    )
    assert consumer.pending_count() == 7


def test_pending_count_is_none_without_redis() -> None:
    consumer = DetectionConsumer(None, STREAM, DetectionStore(10, 10))
    assert consumer.pending_count() is None
