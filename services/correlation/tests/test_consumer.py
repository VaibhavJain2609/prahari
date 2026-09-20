"""consumer.py: readiness probes and the consumer-group poll loop.

The group behaviour (XGROUP CREATE, XREADGROUP, XACK-after-persist, pending
re-read, XAUTOCLAIM) is exercised against `_FakeAsyncRedis`, a scripted async
double — the repo's established pattern is a hand-rolled fake rather than a
fakeredis dependency (see packages/prahari-common/tests/test_bus.py).
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from prahari.v1 import common_pb2, events_pb2
from redis.exceptions import ResponseError

import prahari_correlation.consumer as consumer_module
from prahari_correlation.consumer import CONSUMER_GROUP, DetectionConsumer
from prahari_correlation.metrics import Metrics
from prahari_correlation.store import AddOutcome, DetectionStore

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
        self.ack_failures = 0  # xack raises while > 0, simulating a lost ack
        self.create_error: Exception | None = None  # non-BUSYGROUP create failure

    async def xgroup_create(self, name, groupname, id=None, mkstream=False):  # noqa: ANN001, ANN202
        self.groups_created.append((name, groupname, id, mkstream))
        if self.create_error is not None:
            raise self.create_error
        if self.group_exists:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        self.group_exists = True
        return True

    async def xreadgroup(self, groupname, consumername, streams, count=None, block=None):  # noqa: ANN001, ANN202
        # The real client suspends on the socket here; the fake must yield too
        # or a consumer `_run` loop driven in-test never lets the loop schedule
        # `stop()` — a 100% CPU spin that only exists because the fake is faster
        # than a network.
        await asyncio.sleep(0)
        _key, last_id = next(iter(streams.items()))
        self.read_ids.append(last_id)
        if self.read_script:
            item = self.read_script.popleft()
            if isinstance(item, Exception):
                raise item
            return item
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
            item = self.claim_script.popleft()
            if isinstance(item, Exception):
                raise item
            return item
        return [b"0-0", [], []]

    async def xack(self, name, groupname, *ids):  # noqa: ANN001, ANN202
        if self.ack_failures:
            self.ack_failures -= 1
            raise ConnectionError("simulated lost ack")
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
    store=None,  # noqa: ANN001, ANN202 -- DetectionStore or a scripted double
    db: _FakeDb | None = None,
    metrics: Metrics | None = None,
    count: int = 100,
) -> DetectionConsumer:
    return DetectionConsumer(
        "redis://irrelevant",
        STREAM,
        store or DetectionStore(10, 10),
        db=db,
        metrics=metrics,
        client=client,
        consumer_name="test-consumer",
        block_ms=10,
        count=count,
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


async def test_empty_poll_returns_zero() -> None:
    consumer = _consumer(_FakeAsyncRedis())
    assert await consumer._poll_once() == 0  # noqa: SLF001


async def test_a_non_busygroup_create_error_propagates_and_stays_unready() -> None:
    # BUSYGROUP means "group already exists" (fine); anything else — NOAUTH,
    # a dead master — must surface, not be swallowed as success, or the
    # consumer would read against a group that was never created.
    client = _FakeAsyncRedis()
    client.create_error = ResponseError("NOAUTH this consumer has no permissions")

    consumer = _consumer(client)
    with pytest.raises(ResponseError, match="NOAUTH"):
        await consumer._poll_once()  # noqa: SLF001
    assert not consumer._group_ready  # noqa: SLF001 -- retried on the next poll


async def test_an_xautoclaim_failure_falls_through_to_the_new_entries_read() -> None:
    # XAUTOCLAIM is best-effort orphan adoption: its failure must not skip the
    # normal ">" read or the consumer stalls whenever the command errors.
    client = _FakeAsyncRedis()
    client.claim_script.append(ConnectionError("xautoclaim exploded"))
    client.read_script.append([(STREAM.encode(), [_entry(b"1-0", _detection())])])

    consumer = _consumer(client)
    assert await consumer._poll_once() == 1  # noqa: SLF001
    assert client.read_ids == [">"]
    assert client.acked == [(b"1-0",)]


async def test_a_malformed_xautoclaim_reply_is_treated_as_nothing_claimed() -> None:
    client = _FakeAsyncRedis()
    client.claim_script.append("not-a-triplet")  # len < 2 and not a sequence of entries
    client.read_script.append([(STREAM.encode(), [_entry(b"1-0", _detection())])])

    consumer = _consumer(client)
    assert await consumer._poll_once() == 1  # noqa: SLF001
    assert client.read_ids == [">"]


async def test_an_empty_pending_read_clears_the_flag_and_reads_new() -> None:
    client = _FakeAsyncRedis()
    consumer = _consumer(client)
    consumer._own_pending = True  # noqa: SLF001 -- as a lost ack would have left it

    client.read_script.append([])  # the "0" re-read: nothing left pending
    client.read_script.append([(STREAM.encode(), [_entry(b"1-0", _detection())])])

    assert await consumer._poll_once() == 1  # noqa: SLF001
    assert client.read_ids == ["0", ">"]
    assert not consumer._own_pending  # noqa: SLF001


async def test_a_full_pending_page_keeps_the_flag_so_the_next_poll_reads_pending_again() -> None:
    # count=1 with one pending entry: the page is full, so more pending may
    # exist beyond it — the flag must survive a fully-acked batch here, and
    # the next poll must read "0" again rather than jumping to ">".
    client = _FakeAsyncRedis()
    consumer = _consumer(client, count=1)
    consumer._own_pending = True  # noqa: SLF001

    client.read_script.append([(STREAM.encode(), [_entry(b"1-0", _detection("D1"))])])
    client.read_script.append([])  # second "0" read: drained

    assert await consumer._poll_once() == 1  # noqa: SLF001
    assert consumer._own_pending  # noqa: SLF001
    assert client.read_ids == ["0"]

    # Second poll re-reads pending ("0"); finding it drained, the same
    # iteration falls through to the new-entries read.
    assert await consumer._poll_once() == 0  # noqa: SLF001
    assert client.read_ids == ["0", "0", ">"]
    assert not consumer._own_pending  # noqa: SLF001


async def test_an_entry_with_no_detection_field_is_acked_and_counted() -> None:
    # Same poison-pill rule as an undecodable payload: redelivery can never
    # conjure the field, so the entry is acked and the drop is measured.
    client = _FakeAsyncRedis()
    metrics = Metrics()
    client.read_script.append([(STREAM.encode(), [(b"1-0", {b"not_detection": b"x"})])])

    consumer = _consumer(client, metrics=metrics)
    assert await consumer._poll_once() == 1  # noqa: SLF001
    assert client.acked == [(b"1-0",)]
    assert "prahari_correlation_detections_dropped_no_field 1" in metrics.render()


async def test_a_store_error_is_acked_like_a_poison_pill_not_left_pending() -> None:
    # A store bug is deterministic — redelivery hits it again — so the entry
    # is acked and counted rather than wedging the group.
    class _BrokenStore:
        def add(self, _detection):  # noqa: ANN001, ANN202
            raise RuntimeError("store bug")

    client = _FakeAsyncRedis()
    db = _FakeDb()
    metrics = Metrics()
    client.read_script.append([(STREAM.encode(), [_entry(b"1-0", _detection())])])

    consumer = _consumer(client, store=_BrokenStore(), db=db, metrics=metrics)
    assert await consumer._poll_once() == 1  # noqa: SLF001
    assert client.acked == [(b"1-0",)]
    assert db.inserted == []
    assert not consumer._own_pending  # noqa: SLF001
    assert "prahari_correlation_detections_dropped_store_error 1" in metrics.render()


async def test_a_lost_ack_marks_own_pending_and_propagates() -> None:
    # The work happened but the XACK did not: the entries stay pending and
    # MUST be re-read via "0" next poll — they redeliver into the dedup
    # gates, which is the at-least-once contract working as designed.
    client = _FakeAsyncRedis()
    client.ack_failures = 1
    client.read_script.append([(STREAM.encode(), [_entry(b"1-0", _detection())])])

    consumer = _consumer(client)
    with pytest.raises(ConnectionError):
        await consumer._poll_once()  # noqa: SLF001
    assert consumer._own_pending  # noqa: SLF001


async def test_a_rejected_detection_is_not_left_pending() -> None:
    # outcome == REJECTED skips the persist AND is still acked — covered
    # end-to-end above; here assert the metric-free path leaves no residue.
    class _RejectingStore:
        def add(self, _detection):  # noqa: ANN001, ANN202
            return AddOutcome.REJECTED

    client = _FakeAsyncRedis()
    db = _FakeDb()
    client.read_script.append([(STREAM.encode(), [_entry(b"1-0", _detection())])])

    consumer = _consumer(client, store=_RejectingStore(), db=db)
    assert await consumer._poll_once() == 1  # noqa: SLF001
    assert client.acked == [(b"1-0",)]
    assert db.inserted == []
    assert not consumer._own_pending  # noqa: SLF001


# --- the _run loop and lifecycle -------------------------------------------


async def test_start_spawns_the_poll_task_and_stop_cancels_and_closes() -> None:
    client = _FakeAsyncRedis()

    class _Ping:
        def __init__(self) -> None:
            self.closed = False

        def ping(self) -> bool:
            return True

        def close(self) -> None:
            self.closed = True

    ping = _Ping()
    consumer = _consumer(client)
    consumer._ping_client = ping  # noqa: SLF001

    consumer.start()  # inside the test's running loop, like the app lifespan
    assert consumer._task is not None  # noqa: SLF001
    assert consumer.is_connected()

    await asyncio.sleep(0.01)  # let the loop tick at least one poll
    await consumer.stop()

    assert consumer._task.done()  # noqa: SLF001
    assert client.closed
    assert ping.closed
    assert not consumer.is_connected()  # task done -> /readyz reports not-ready


async def test_stop_tolerates_client_close_errors() -> None:
    class _FailingClose:
        async def aclose(self) -> None:
            raise RuntimeError("already gone")

    class _FailingPingClose:
        def close(self) -> None:
            raise RuntimeError("already gone")

    consumer = _consumer(_FailingClose())  # type: ignore[arg-type]
    consumer._ping_client = _FailingPingClose()  # noqa: SLF001
    await consumer.stop()  # shutdown must not re-raise a dead client's error


async def test_the_run_loop_survives_a_poll_exception_and_retries(monkeypatch) -> None:
    # A Redis blip mid-poll must not kill the loop — a dead task with a
    # reachable Redis is the "looks healthy, silently stopped consuming"
    # failure is_connected() exists to catch.
    monkeypatch.setattr(consumer_module, "_RETRY_DELAY_S", 0)
    client = _FakeAsyncRedis()
    client.read_script.append(ConnectionError("redis blip"))
    client.read_script.append([(STREAM.encode(), [_entry(b"1-0", _detection())])])

    consumer = _consumer(client)
    task = asyncio.create_task(consumer._run())  # noqa: SLF001
    try:
        for _ in range(100):
            await asyncio.sleep(0.005)
            if client.acked:
                break
    finally:
        consumer._stop.set()  # noqa: SLF001
        await asyncio.wait_for(task, 2.0)

    assert client.acked == [(b"1-0",)]  # the retry recovered and processed


async def test_client_or_connect_builds_the_async_redis_lazily(monkeypatch) -> None:
    import redis.asyncio as aioredis

    sentinel = object()
    calls: list[str] = []
    monkeypatch.setattr(
        aioredis.Redis,
        "from_url",
        classmethod(lambda _cls, url: calls.append(url) or sentinel),
    )

    consumer = DetectionConsumer("redis://example:6379", STREAM, DetectionStore(10, 10))
    assert await consumer._client_or_connect() is sentinel  # noqa: SLF001
    assert await consumer._client_or_connect() is sentinel  # noqa: SLF001 -- cached
    assert calls == ["redis://example:6379"]


def test_ping_client_or_connect_builds_the_sync_redis_lazily(monkeypatch) -> None:
    import redis

    class _Probe:
        def ping(self) -> bool:
            return True

    calls: list[str] = []
    monkeypatch.setattr(
        redis.Redis, "from_url", classmethod(lambda _cls, url: calls.append(url) or _Probe())
    )

    consumer = DetectionConsumer("redis://example:6379", STREAM, DetectionStore(10, 10))
    assert consumer.is_connected()
    assert calls == ["redis://example:6379"]


# --- readiness probes --------------------------------------------------------


def test_stream_length_reports_xlen() -> None:
    class _Probe:
        def ping(self) -> bool:
            return True

        def xlen(self, key):  # noqa: ANN001, ANN202
            assert key == STREAM
            return 42

    consumer = DetectionConsumer(
        "redis://irrelevant", STREAM, DetectionStore(10, 10), ping_client=_Probe()
    )
    assert consumer.stream_length() == 42


def test_stream_length_is_none_without_redis() -> None:
    consumer = DetectionConsumer(None, STREAM, DetectionStore(10, 10))
    assert consumer.stream_length() is None


def test_stream_length_is_none_when_the_client_has_no_xlen() -> None:
    class _PingOnly:
        def ping(self) -> bool:
            return True

    consumer = DetectionConsumer(
        "redis://irrelevant", STREAM, DetectionStore(10, 10), ping_client=_PingOnly()
    )
    assert consumer.stream_length() is None


def test_stream_length_is_none_when_xlen_raises() -> None:
    class _Probe:
        def ping(self) -> bool:
            return True

        def xlen(self, _key):  # noqa: ANN001, ANN202
            raise ConnectionError("simulated redis outage")

    consumer = DetectionConsumer(
        "redis://irrelevant", STREAM, DetectionStore(10, 10), ping_client=_Probe()
    )
    assert consumer.stream_length() is None


def test_pending_count_is_none_for_a_non_dict_summary() -> None:
    class _Probe:
        def ping(self) -> bool:
            return True

        def xpending(self, _stream, _group):  # noqa: ANN001, ANN202
            return []  # redis-py range form, not the summary dict

    consumer = DetectionConsumer(
        "redis://irrelevant", STREAM, DetectionStore(10, 10), ping_client=_Probe()
    )
    assert consumer.pending_count() is None


def test_pending_count_is_none_when_xpending_raises() -> None:
    class _Probe:
        def ping(self) -> bool:
            return True

        def xpending(self, _stream, _group):  # noqa: ANN001, ANN202
            raise ConnectionError("simulated redis outage")

    consumer = DetectionConsumer(
        "redis://irrelevant", STREAM, DetectionStore(10, 10), ping_client=_Probe()
    )
    assert consumer.pending_count() is None


def test_pending_count_is_none_when_the_client_has_no_xpending() -> None:
    class _PingOnly:
        def ping(self) -> bool:
            return True

    consumer = DetectionConsumer(
        "redis://irrelevant", STREAM, DetectionStore(10, 10), ping_client=_PingOnly()
    )
    assert consumer.pending_count() is None
