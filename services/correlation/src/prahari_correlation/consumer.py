"""Background consumer driving `prahari:detections` into the `DetectionStore`
and (when configured) `correlation_sightings` in Postgres.

Two deliberate differences from the pre-durability version:

* It runs as an **asyncio task on the app event loop**, not a thread. The old
  thread existed because `RedisStreamConsumer.poll()` was a blocking XREAD on
  a sync client; `redis.asyncio`'s `xreadgroup` blocks by *awaiting*, so the
  loop stays free and the same asyncpg pool serves the writer (here) and the
  readers (route queries) — no cross-loop plumbing.
* It consumes through the **consumer group `prahari-correlation`** instead of
  a private `$` cursor. Redis tracks what has been delivered-but-not-acked
  per consumer, so a restart resumes where the group left off rather than
  skipping every detection that arrived while the pod was down — and the
  group is what `inference.yaml`'s KEDA `redis-streams` trigger measures.

Ack discipline is the durability contract: an entry is XACKed only after it
has been indexed by the store AND (when Postgres is configured) inserted —
at-least-once delivery, deduplicated at the store's `_seen_ids` and at the
table's `detection_id` primary key. Entries that fail to persist stay pending
and are re-read (own pending via a `"0"` read; orphaned pending from a dead
replica via XAUTOCLAIM). Entries that can never succeed — undecodable
payloads, detections the store rejects — are acked anyway: redelivery cannot
fix them, and leaving them pending would wedge the group on a poison pill.

Ordering note for the route builder: XREADGROUP preserves stream order within
a batch, but pending re-reads and claimed orphans interleave with new entries
across polls. Nothing downstream depends on delivery order — the store keys
by plate skeleton and `build_route` sorts every sighting by
`observed_at.wall_clock` — so the weaker ordering is stated here, not worked
around.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Protocol

from prahari.v1 import events_pb2

from .metrics import Metrics
from .store import AddOutcome, DetectionStore

if TYPE_CHECKING:
    from .db import PostgresSightings

__all__ = ["CONSUMER_GROUP", "DetectionConsumer"]

log = logging.getLogger(__name__)

CONSUMER_GROUP = "prahari-correlation"
"""Must match `consumerGroup:` in `infra/helm/prahari/templates/inference.yaml`'s
KEDA trigger — that scaler reads this group's pending-entries count as the
pipeline-backlog signal. Renaming one without the other silently zeroes
autoscaling. Deliberately a constant, not a settings field: a knob that can be
set to a value the chart does not know is a profile switch that does not
switch."""

_CLAIM_MIN_IDLE_MS = 60_000
"""How long a pending entry must sit before XAUTOCLAIM lets another group
member adopt it. Long enough that a slow-but-alive consumer never loses its
batch mid-processing; short enough that a killed pod's backlog is re-read
within about a minute."""

_RETRY_DELAY_S = 1.0
"""Pause after a poll iteration that left entries unacked or raised — a
hard-down Postgres/Redis must not spin the loop hot."""


class _PingableRedis(Protocol):
    def ping(self) -> bool: ...


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


class DetectionConsumer:
    def __init__(
        self,
        redis_url: str | None,
        stream_key: str,
        store: DetectionStore,
        *,
        db: PostgresSightings | None = None,
        metrics: Metrics | None = None,
        ping_client: _PingableRedis | None = None,
        client: Any | None = None,
        consumer_name: str | None = None,
        block_ms: int = 5000,
        count: int = 100,
    ) -> None:
        self._redis_url = redis_url
        self._stream_key = stream_key
        self._store = store
        self._db = db
        self._metrics = metrics if metrics is not None else Metrics()
        self._stop = threading.Event()
        self._task: asyncio.Task | None = None
        self._ping_client = ping_client
        # `client` injects an async-redis double in tests; production builds
        # the real one lazily on first poll, on the loop that runs the task.
        self._client = client
        # The pod name in-cluster — a stable per-replica consumer identity.
        self._consumer_name = consumer_name or socket.gethostname()
        self._block_ms = block_ms
        self._count = count
        self._group_ready = False
        self._own_pending = False
        """True when entries were delivered to this consumer but left unacked
        (persist failure / lost ack). They are only re-readable via a
        non-">" XREADGROUP, so the next poll must read our pending list."""

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Must be called from inside a running event loop (the lifespan does)."""
        if self._redis_url is None:
            log.warning(
                "PRAHARI_CORRELATION_REDIS_URL not set; detection consumer disabled -- "
                "/api/v1/routes serves only what Postgres already holds (if configured)"
            )
            return
        self._task = asyncio.create_task(self._run(), name="detection-consumer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._client is not None:
            aclose = getattr(self._client, "aclose", None)
            if aclose is not None:
                with suppress(Exception):
                    await aclose()
        close = getattr(self._ping_client, "close", None)
        if close is not None:
            with suppress(Exception):
                close()

    # --- the poll loop --------------------------------------------------------

    async def _client_or_connect(self):
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.Redis.from_url(self._redis_url)
        return self._client

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Same rule as before: a Redis blip must not kill the poll
                # loop — a dead task with a reachable Redis is the "looks
                # healthy, silently stopped consuming" failure is_connected()
                # exists to catch.
                log.exception("detection poll failed; retrying")
                await asyncio.sleep(_RETRY_DELAY_S)

    async def _ensure_group(self, client) -> None:  # noqa: ANN001 -- redis client or test double
        """XGROUP CREATE ... MKSTREAM, idempotent. `id="0"`, not `$`: a freshly
        created group replays the stream's retained history, which is exactly
        the backfill Postgres wants on first deploy (the stream is MAXLEN-
        bounded, so the replay is bounded). An existing group is a BUSYGROUP
        error, treated as success — the group's last-delivered position is
        the resume point either way."""
        if self._group_ready:
            return
        try:
            await client.xgroup_create(self._stream_key, CONSUMER_GROUP, id="0", mkstream=True)
            log.info(
                "created consumer group %s on %s (from 0: replaying retained history)",
                CONSUMER_GROUP,
                self._stream_key,
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    async def _read_entries(self, client, last_id: str) -> list:  # noqa: ANN001, ANN202
        """One XREADGROUP. `last_id=">"` reads new entries (and may block);
        any concrete id — `"0"` included — reads *this consumer's* pending
        list instead, which is how unacked entries are re-fetched."""
        response = await client.xreadgroup(
            CONSUMER_GROUP,
            self._consumer_name,
            {self._stream_key: last_id},
            count=self._count,
            block=self._block_ms if last_id == ">" else None,
        )
        entries: list = []
        for _stream_name, stream_entries in response or []:
            entries.extend(stream_entries)
        return entries

    async def _claim_idle(self, client) -> list:  # noqa: ANN001, ANN202
        """Adopt pending entries that have been idle past `_CLAIM_MIN_IDLE_MS`
        — the backlog of a consumer that died (a rescheduled pod keeps its
        pending list under its old consumer name, and nobody re-reads it
        until another member claims it). Claimed entries then go through the
        normal process+ack path."""
        try:
            result = await client.xautoclaim(
                self._stream_key,
                CONSUMER_GROUP,
                self._consumer_name,
                min_idle_time=_CLAIM_MIN_IDLE_MS,
                start_id="0-0",
                count=self._count,
            )
        except Exception:
            log.exception("xautoclaim on %s failed", self._stream_key)
            return []
        # redis-py: [next_start_id, [(entry_id, fields), ...], [deleted_ids]]
        if isinstance(result, (list, tuple)) and len(result) >= 2:
            return list(result[1] or [])
        return []

    async def _poll_once(self) -> int:
        """One poll iteration. Returns how many entries were processed —
        mostly so tests can drive the loop a step at a time."""
        client = await self._client_or_connect()
        await self._ensure_group(client)

        if self._own_pending:
            entries = await self._read_entries(client, "0")
            if entries:
                await self._process(client, entries)
                # A full page means there may be MORE pending beyond it —
                # keep the flag so the next iteration reads "0" again.
                if len(entries) >= self._count:
                    self._own_pending = True
                return len(entries)
            self._own_pending = False

        claimed = await self._claim_idle(client)
        if claimed:
            await self._process(client, claimed)
            return len(claimed)

        entries = await self._read_entries(client, ">")
        if entries:
            await self._process(client, entries)
            return len(entries)
        return 0

    async def _process(self, client, entries: list) -> None:  # noqa: ANN001
        """Store + persist a batch, then XACK what succeeded. Persist comes
        BEFORE the ack: a sighting is durable before it is acknowledged."""
        ack_ids: list = []
        unacked = 0
        for entry_id, fields in entries:
            eid = _text(entry_id)
            raw = fields.get(b"detection") or fields.get("detection")
            if raw is None:
                self._metrics.inc("detections_dropped_no_field")
                log.warning(
                    "stream %s entry %s has no 'detection' field; acking",
                    self._stream_key,
                    eid,
                )
                ack_ids.append(entry_id)
                continue
            try:
                detection = events_pb2.VehicleDetection.FromString(raw)
            except Exception:
                # A poison pill: redelivery decodes to the same failure, so it
                # is acked and counted rather than allowed to wedge the group.
                self._metrics.inc("detections_dropped_undecodable")
                log.exception("failed to decode stream %s entry %s; acking", self._stream_key, eid)
                ack_ids.append(entry_id)
                continue

            try:
                outcome = self._store.add(detection)
            except Exception:
                # A store bug is deterministic — redelivery hits it again —
                # so the entry is acked and counted like a poison pill.
                self._metrics.inc("detections_dropped_store_error")
                log.exception("failed to store detection %s", detection.detection_id)
                ack_ids.append(entry_id)
                continue

            if self._db is not None and outcome != AddOutcome.REJECTED:
                try:
                    await self._db.insert_detection(detection, eid)
                except Exception:
                    # Transient until proven otherwise (a down Postgres is the
                    # common case): leave the entry PENDING so it is retried,
                    # and make the failure visible rather than silently
                    # shrinking to memory-only.
                    self._metrics.inc("sightings_persist_failed")
                    log.exception(
                        "failed to persist detection %s (stream %s); left pending",
                        detection.detection_id,
                        eid,
                    )
                    unacked += 1
                    continue
            ack_ids.append(entry_id)

        if ack_ids:
            try:
                await client.xack(self._stream_key, CONSUMER_GROUP, *ack_ids)
            except Exception:
                # The work may be done but the ack lost — the entries stay
                # pending and redeliver into the dedup gates. Count it and
                # take the pending path next iteration.
                self._own_pending = True
                raise
        # A fully-acked batch clears the flag — including the pending re-read
        # that set it; only a still-unacked residue keeps it True.
        self._own_pending = unacked > 0
        if unacked:
            await asyncio.sleep(_RETRY_DELAY_S)

    # --- readiness / metrics ---------------------------------------------------

    def is_connected(self) -> bool:
        """Used by `/readyz` -- a correlation service that silently stopped
        consuming looks healthy and returns empty routes forever, same
        reasoning as match-engine's watchlist-empty check. Checks two
        independent things: the poll task is still running (a dead task
        leaves Redis itself perfectly reachable, so a `PING`-only check would
        report ready forever), and a live `PING` (the URL being set does not
        mean Redis is actually reachable right now)."""
        if self._redis_url is None:
            return False
        if self._task is not None and self._task.done():
            return False
        try:
            client = self._ping_client_or_connect()
            return bool(client.ping())
        except Exception:
            log.exception("detection consumer readiness ping failed")
            return False

    def stream_length(self) -> int | None:
        """XLEN of the detections stream, or `None` when it cannot be known
        (no Redis configured, Redis unreachable, a ping-only test double).
        Exposed as `detections_stream_length` — total retained history, NOT
        consumer lag; `pending_count` is the lag signal."""
        if self._redis_url is None:
            return None
        try:
            client = self._ping_client_or_connect()
            xlen = getattr(client, "xlen", None)
            if xlen is None:
                return None
            return int(xlen(self._stream_key))
        except Exception:
            return None

    def pending_count(self) -> int | None:
        """Entries delivered to the `prahari-correlation` group but not yet
        acked — the real consumer-lag signal a group gives us (and the number
        the KEDA trigger scales on). `None` when unknown, same contract as
        `stream_length`."""
        if self._redis_url is None:
            return None
        try:
            client = self._ping_client_or_connect()
            xpending = getattr(client, "xpending", None)
            if xpending is None:
                return None
            summary = xpending(self._stream_key, CONSUMER_GROUP)
            if isinstance(summary, dict):
                return int(summary.get("pending", 0))
            return None
        except Exception:
            return None

    def _ping_client_or_connect(self) -> _PingableRedis:
        if self._ping_client is None:
            import redis

            self._ping_client = redis.Redis.from_url(self._redis_url)
        return self._ping_client
