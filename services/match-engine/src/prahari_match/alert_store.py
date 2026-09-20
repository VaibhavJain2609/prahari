"""Alert history: the durable record behind `GET /api/v1/alerts`.

Three pieces:

* `AlertRecord` — the row shape both stores return. Columns mirror the `Alert`
  proto where a query needs them (see `migrations/0001_alerts.sql`); `payload`
  carries the whole message so a reader gets back the exact alert the bus saw.
* `AlertStore` — the async `Protocol` the HTTP surface reads through.
  `PostgresAlertStore` is the real one; `MemoryAlertStore` is the
  `database_url`-unset fallback and the in-test implementation — same contract,
  so an endpoint never has to know which it is holding.
* `AlertStorePublisher` — the bridge onto the *write* path. `publish()` is
  sync (the `AlertPublisher` contract, invoked from gRPC handler threads); the
  insert itself runs on the uvicorn loop via `run_coroutine_threadsafe`.

Ordering trade-off, deliberate: the insert is *submitted* before the Redis
XADD — the store publisher sits ahead of `RedisStreamPublisher` in the
`FanOutPublisher` list — but it completes asynchronously, alongside the stream
publish rather than blocking it. A failed insert logs and counts
`prahari_match_alert_persist_failures_total` without touching the stream:
the stream is the live relay, and alert delivery must not die because history
is down. The cost is a history gap during a Postgres outage (visible in the
metric and in `/readyz`'s `persistence` field) and a crash window between XADD
and INSERT-commit where a row can be lost. The reverse ordering — stream
first, insert synchronously awaited — has the same window with worse failure
semantics: a hung Postgres would stall the gRPC handler threads behind a
cross-loop wait.

Acknowledgement is the only lifecycle (audit decision: no assignment
workflow). It is idempotent and first-write-wins: a second ack returns the
existing record unchanged rather than overwriting the original actor, because
`acknowledged_by` is the audit trail of *who* cleared it.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from google.protobuf.json_format import MessageToDict
from prahari.v1 import events_pb2

from .metrics import ALERT_PERSIST_FAILURES, ALERTS_PERSISTED, METRICS

__all__ = [
    "AlertRecord",
    "AlertStore",
    "AlertStorePublisher",
    "MemoryAlertStore",
    "PostgresAlertStore",
    "alert_to_record",
]

log = logging.getLogger(__name__)


@dataclass
class AlertRecord:
    """One persisted alert. `id`/`created_at` are assigned by the store
    (Postgres `bigserial`/`now()`, or the memory store's stand-ins); everything
    else is extracted from the `Alert` proto by `alert_to_record`."""

    id: int | None
    alert_id: str
    dedup_key: str
    plate: str  # observed plate — what an officer searches for
    camera_id: str
    confidence: float  # MatchExplanation.final_score
    priority: int
    band: int
    explanation: dict
    payload: dict  # whole Alert as MessageToDict — the response body
    occurred_at: datetime
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    created_at: datetime | None = None

    def to_dict(self) -> dict:
        """Response shape: the full alert payload (so the BFF's existing
        `detection.camera_id` scope filter keeps working unchanged) plus the
        lifecycle columns on top."""
        return {
            **self.payload,
            "id": self.id,
            "occurred_at": self.occurred_at.isoformat(),
            "acknowledged_at": (self.acknowledged_at.isoformat() if self.acknowledged_at else None),
            "acknowledged_by": self.acknowledged_by,
        }


def _aware(dt: datetime) -> datetime:
    """A naive timestamp is interpreted as UTC — the service runs UTC and a
    naive `?since=` from a client should mean UTC, not raise."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def alert_to_record(alert: events_pb2.Alert) -> AlertRecord:
    """Extract the query columns from the proto, keeping the whole message in
    `payload`. `occurred_at` is the *sighting* time (the detection's wall
    clock), not the engine's `raised_at` — history answers "when was this
    vehicle seen". Falls back to `raised_at`, then to now, for workers that
    send no wall clock (the same fallback dedup uses in grpc_server.py)."""
    detection = alert.detection
    explanation = alert.explanation
    plate = (
        explanation.observed_plate or detection.plate.normalised_text or detection.plate.raw_text
    )
    if detection.HasField("observed_at") and detection.observed_at.HasField("wall_clock"):
        occurred_at = detection.observed_at.wall_clock.ToDatetime(tzinfo=UTC)
    elif alert.HasField("raised_at"):
        occurred_at = alert.raised_at.ToDatetime(tzinfo=UTC)
    else:
        occurred_at = datetime.now(UTC)
    return AlertRecord(
        id=None,
        alert_id=alert.alert_id,
        dedup_key=alert.dedup_key,
        plate=plate,
        camera_id=detection.camera_id,
        confidence=explanation.final_score,
        priority=alert.priority,
        band=alert.band,
        explanation=MessageToDict(explanation, preserving_proto_field_name=True),
        payload=MessageToDict(alert, preserving_proto_field_name=True),
        occurred_at=occurred_at,
    )


class AlertStore(Protocol):
    """What the HTTP surface reads and the write path persists through.
    Async even for the memory implementation so an endpoint's `await` is
    correct regardless of which store is configured."""

    async def record(self, alert: events_pb2.Alert) -> None: ...

    async def get(self, alert_id: str) -> AlertRecord | None: ...

    async def list(
        self,
        *,
        since: datetime | None = None,
        camera_id: str | None = None,
        plate: str | None = None,
        acknowledged: bool | None = None,
        limit: int = 50,
    ) -> list[AlertRecord]: ...

    async def acknowledge(self, alert_id: str, by: str) -> AlertRecord | None: ...


class MemoryAlertStore:
    """Bounded in-memory store — the fallback when `PRAHARI_MATCH_DATABASE_URL`
    is unset and what tests exercise the endpoints against. Bounded the same
    way the old ring buffer was (`recent_alerts_size`): this is the degraded
    path, not the system of record, and a long-running process must not grow
    it without limit.

    Also an `AlertPublisher`: `publish()` records synchronously (a deque
    append is cheap and needs no event loop), so the store can sit directly
    in the `FanOutPublisher` list where `RecentAlertsPublisher` used to."""

    def __init__(self, max_size: int) -> None:
        self._records: deque[AlertRecord] = deque(maxlen=max_size)
        self._ids = itertools.count(1)
        # gRPC handler threads (publish) and the event loop (list/ack) share
        # this deque — same threading split as metrics.py.
        self._lock = threading.Lock()

    def publish(self, alert: events_pb2.Alert) -> None:
        self._insert(alert)

    def _insert(self, alert: events_pb2.Alert) -> AlertRecord:
        rec = alert_to_record(alert)
        with self._lock:
            rec.id = next(self._ids)
            rec.created_at = datetime.now(UTC)
            self._records.append(rec)
        return rec

    async def record(self, alert: events_pb2.Alert) -> None:
        self._insert(alert)

    async def get(self, alert_id: str) -> AlertRecord | None:
        with self._lock:
            for rec in self._records:
                if rec.alert_id == alert_id:
                    return rec
        return None

    async def list(
        self,
        *,
        since: datetime | None = None,
        camera_id: str | None = None,
        plate: str | None = None,
        acknowledged: bool | None = None,
        limit: int = 50,
    ) -> list[AlertRecord]:
        with self._lock:
            items = list(self._records)
        if since is not None:
            since = _aware(since)
            items = [r for r in items if r.occurred_at >= since]
        if camera_id:
            items = [r for r in items if r.camera_id == camera_id]
        if plate:
            items = [
                r for r in items if r.plate == plate or r.explanation.get("matched_plate") == plate
            ]
        if acknowledged is True:
            items = [r for r in items if r.acknowledged_at is not None]
        elif acknowledged is False:
            items = [r for r in items if r.acknowledged_at is None]
        # Newest first, matching the Postgres ORDER BY and the old ring
        # buffer's read order.
        items.sort(key=lambda r: (r.occurred_at, r.id or 0), reverse=True)
        return items[:limit]

    async def acknowledge(self, alert_id: str, by: str) -> AlertRecord | None:
        """First write wins — see the module docstring."""
        with self._lock:
            for rec in self._records:
                if rec.alert_id == alert_id:
                    if rec.acknowledged_at is None:
                        rec.acknowledged_at = datetime.now(UTC)
                        rec.acknowledged_by = by
                    return rec
        return None


_COLUMNS = (
    "id, alert_id, dedup_key, plate, camera_id, confidence, priority, band, "
    "explanation, payload, occurred_at, acknowledged_at, acknowledged_by, created_at"
)

_INSERT_SQL = """
    INSERT INTO alerts
        (alert_id, dedup_key, plate, camera_id, confidence, priority, band,
         explanation, payload, occurred_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10)
    ON CONFLICT (alert_id) DO NOTHING
"""
# ON CONFLICT DO NOTHING: a replayed detection that slipped past dedup, or a
# retried publish, must not error the write path — the first stored row is the
# record.


def _row_to_record(row: Any) -> AlertRecord:
    """`row` is an asyncpg Record — `row["col"]` — or a plain dict (the test
    fake). jsonb columns arrive as dicts via the codec registered in
    `db.create_pool`."""
    return AlertRecord(
        id=row["id"],
        alert_id=row["alert_id"],
        dedup_key=row["dedup_key"],
        plate=row["plate"],
        camera_id=row["camera_id"],
        confidence=row["confidence"],
        priority=row["priority"],
        band=row["band"],
        explanation=row["explanation"],
        payload=row["payload"],
        occurred_at=row["occurred_at"],
        acknowledged_at=row["acknowledged_at"],
        acknowledged_by=row["acknowledged_by"],
        created_at=row["created_at"],
    )


class PostgresAlertStore:
    """Alert history in Postgres — the system of record once
    `PRAHARI_MATCH_DATABASE_URL` is set. Plain SQL, same style as the
    registry's repository: the query surface is small enough that an ORM would
    only obscure it. `pool` is anything with asyncpg's `fetch`/`fetchrow`/
    `execute` surface, so tests can drive the SQL through a fake."""

    def __init__(self, pool) -> None:  # noqa: ANN001 - asyncpg.Pool, duck-typed for tests
        self._pool = pool

    async def record(self, alert: events_pb2.Alert) -> None:
        rec = alert_to_record(alert)
        await self._pool.execute(
            _INSERT_SQL,
            rec.alert_id,
            rec.dedup_key,
            rec.plate,
            rec.camera_id,
            rec.confidence,
            rec.priority,
            rec.band,
            rec.explanation,
            rec.payload,
            rec.occurred_at,
        )

    async def get(self, alert_id: str) -> AlertRecord | None:
        row = await self._pool.fetchrow(
            f"SELECT {_COLUMNS} FROM alerts WHERE alert_id = $1", alert_id
        )
        return _row_to_record(row) if row is not None else None

    async def list(
        self,
        *,
        since: datetime | None = None,
        camera_id: str | None = None,
        plate: str | None = None,
        acknowledged: bool | None = None,
        limit: int = 50,
    ) -> list[AlertRecord]:
        clauses: list[str] = []
        args: list[Any] = []

        def param(value: Any) -> str:
            args.append(value)
            return f"${len(args)}"

        if since is not None:
            clauses.append(f"occurred_at >= {param(_aware(since))}")
        if camera_id:
            clauses.append(f"camera_id = {param(camera_id)}")
        if plate:
            # Match the observed plate or the watchlist plate it matched — an
            # officer may search either.
            ph = param(plate)
            clauses.append(f"(plate = {ph} OR explanation->>'matched_plate' = {ph})")
        if acknowledged is True:
            clauses.append("acknowledged_at IS NOT NULL")
        elif acknowledged is False:
            clauses.append("acknowledged_at IS NULL")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT {_COLUMNS} FROM alerts {where} "
            f"ORDER BY occurred_at DESC, id DESC LIMIT {param(limit)}"
        )
        rows = await self._pool.fetch(sql, *args)
        return [_row_to_record(row) for row in rows]

    async def acknowledge(self, alert_id: str, by: str) -> AlertRecord | None:
        # First write wins: the UPDATE only touches unacknowledged rows, so a
        # repeat ack is a no-op that returns the original record rather than
        # rewriting the audit trail. The fallback SELECT is what makes the
        # endpoint idempotent — it distinguishes "already acked" from "no such
        # alert" without a second round-trip in the common case.
        row = await self._pool.fetchrow(
            f"UPDATE alerts SET acknowledged_at = now(), acknowledged_by = $2 "
            f"WHERE alert_id = $1 AND acknowledged_at IS NULL RETURNING {_COLUMNS}",
            alert_id,
            by,
        )
        if row is None:
            row = await self._pool.fetchrow(
                f"SELECT {_COLUMNS} FROM alerts WHERE alert_id = $1", alert_id
            )
        return _row_to_record(row) if row is not None else None


class AlertStorePublisher:
    """`AlertPublisher` that persists each alert through an async `AlertStore`.

    `publish()` runs on a gRPC handler thread; the asyncpg pool lives on the
    uvicorn event loop, so the insert is handed over with
    `run_coroutine_threadsafe` and this method returns without waiting — see
    the module docstring for why the stream publish is never blocked on it.
    Failure surfaces in `prahari_match_alert_persist_failures_total` and the
    log, never in the publish path.
    """

    def __init__(self, store: AlertStore, loop: asyncio.AbstractEventLoop) -> None:
        self._store = store
        self._loop = loop

    def publish(self, alert: events_pb2.Alert) -> None:
        coro = self._store.record(alert)
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError:
            # Loop already closed — a publish racing shutdown. Close the
            # coroutine so it does not warn as never-awaited.
            coro.close()
            METRICS.inc(ALERT_PERSIST_FAILURES)
            log.error("alert %s not persisted: event loop is closed", alert.alert_id)
            return
        future.add_done_callback(self._settle)

    @staticmethod
    def _settle(future: asyncio.Future) -> None:
        try:
            future.result()
        except asyncio.CancelledError:
            METRICS.inc(ALERT_PERSIST_FAILURES)
            log.warning("alert insert cancelled during shutdown")
        except Exception:
            METRICS.inc(ALERT_PERSIST_FAILURES)
            log.exception(
                "alert insert failed — the live relay is unaffected, but this "
                "alert is absent from history until Postgres recovers"
            )
        else:
            METRICS.inc(ALERTS_PERSISTED)
