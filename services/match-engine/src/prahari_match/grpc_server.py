"""`MetadataIngestService`, the high-rate worker -> match-engine link
(adapter.proto). Client-streaming: a worker keeps one connection open for the
lifetime of its process and pushes one message per detection or heartbeat: a
single `IngestAck` per RPC call, once the stream closes.

This module is the one place detections become alerts. `StreamDetections`
runs the full pipeline -- match, dedup, publish -- per message; everything
else (`matcher.py`, `dedup.py`, `alerts.py`) is pure and testable without gRPC
at all. Keeping the servicer this thin is why those modules can be tested
directly.

`StreamHealth` deliberately does nothing but count and acknowledge.
CLAUDE.md: "workers observe; the registry decides" -- computing a health
verdict from these events is the registry's `camera_current` view, not this
service. Reinterpreting a `HealthEvent` here would create the exact
two-services-with-contradictory-verdicts failure that invariant exists to
prevent.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from concurrent import futures
from datetime import UTC

import grpc
from prahari.v1 import adapter_pb2, adapter_pb2_grpc

from .alerts import AlertBuilder, AlertPublisher
from .config import MatchSettings
from .dedup import Deduper
from .detections import DetectionPublisher, NullDetectionPublisher
from .matcher import WatchlistStore, match
from .metrics import (
    ALERTS_EMITTED,
    DEDUP_SUPPRESSED,
    DETECTIONS_ACCEPTED,
    DETECTIONS_REJECTED,
    GRPC_ACTIVE_STREAMS,
    METRICS,
)

__all__ = ["MetadataIngestServicer", "serve"]

log = logging.getLogger(__name__)


class MetadataIngestServicer(adapter_pb2_grpc.MetadataIngestServiceServicer):
    def __init__(
        self,
        store: WatchlistStore,
        deduper: Deduper,
        publisher: AlertPublisher,
        settings: MatchSettings,
        alert_builder: AlertBuilder | None = None,
        detection_publisher: DetectionPublisher | None = None,
    ) -> None:
        self._store = store
        self._deduper = deduper
        self._publisher = publisher
        self._settings = settings
        self._alert_builder = alert_builder or AlertBuilder()
        self._detection_publisher = detection_publisher or NullDetectionPublisher()
        self._warned_missing_wall_clock = False

    def StreamDetections(
        self,
        request_iterator: Iterator[adapter_pb2.StreamDetectionsRequest],
        context: grpc.ServicerContext,
    ) -> adapter_pb2.StreamDetectionsResponse:
        accepted = 0
        rejected = 0
        first_error = ""

        METRICS.add_gauge(GRPC_ACTIVE_STREAMS, 1)
        try:
            for request in request_iterator:
                detection = request.detection
                try:
                    self._handle_detection(detection)
                    accepted += 1
                    METRICS.inc(DETECTIONS_ACCEPTED)
                except Exception as exc:  # noqa: BLE001 - one bad message must not drop the stream
                    rejected += 1
                    METRICS.inc(DETECTIONS_REJECTED)
                    # `detail` keeps the FIRST failure plus a count, not the
                    # last one -- a flood of identical rejections would
                    # otherwise overwrite the original cause with its 500th
                    # copy.
                    if rejected == 1:
                        first_error = str(exc) or type(exc).__name__
                    log.exception("failed to process detection %s", detection.detection_id)
        finally:
            METRICS.add_gauge(GRPC_ACTIVE_STREAMS, -1)

        detail = first_error
        if rejected > 1:
            detail = f"{first_error} (+{rejected - 1} more rejected)"

        return adapter_pb2.StreamDetectionsResponse(
            ack=adapter_pb2.IngestAck(accepted=accepted, rejected=rejected, detail=detail)
        )

    def StreamHealth(
        self,
        request_iterator: Iterator[adapter_pb2.StreamHealthRequest],
        context: grpc.ServicerContext,
    ) -> adapter_pb2.StreamHealthResponse:
        accepted = 0
        for _request in request_iterator:
            # No decision made here -- see module docstring. Accepting and
            # counting is the entire contract this service owes a heartbeat.
            accepted += 1

        return adapter_pb2.StreamHealthResponse(
            ack=adapter_pb2.IngestAck(accepted=accepted, rejected=0, detail="")
        )

    def _check_bounds(self, detection) -> None:  # noqa: ANN001 - adapter_pb2.VehicleDetection-shaped
        """Reject malformed input at the boundary, before it costs pipeline
        work. A `ValueError` here lands in `StreamDetections`'s catch-all and
        is counted in `IngestAck.rejected` -- the message is skipped, the
        stream stays up."""
        if not detection.camera_id:
            raise ValueError("detection has an empty camera_id")
        if detection.HasField("plate"):
            if len(detection.plate.raw_text) > self._settings.max_raw_text_chars:
                raise ValueError(
                    f"plate raw_text is {len(detection.plate.raw_text)} chars "
                    f"(limit {self._settings.max_raw_text_chars})"
                )
            if len(detection.plate.char_confidence) > self._settings.max_char_confidences:
                raise ValueError(
                    f"plate char_confidence has {len(detection.plate.char_confidence)} "
                    f"entries (limit {self._settings.max_char_confidences})"
                )

    def _handle_detection(self, detection) -> None:  # noqa: ANN001 - adapter_pb2.VehicleDetection-shaped
        self._check_bounds(detection)

        # Every detection reaches the detections bus, watchlist hit or not -- see
        # detections.py's module docstring for why (DAY3-DESIGN.md §2). This must run
        # before the plate/match early-returns below, which are about *alerting*, not
        # about whether the sighting is worth keeping as evidence.
        self._detection_publisher.publish(detection)

        if not detection.HasField("plate"):
            return  # no plate legible on this detection -- nothing to match

        result = match(detection.plate, self._store, self._settings)
        if not result.matched:
            return

        if detection.observed_at.HasField("wall_clock"):
            wall_clock_s = detection.observed_at.wall_clock.ToDatetime(tzinfo=UTC).timestamp()
        else:
            # An unset `wall_clock` parses as the epoch, not an error --
            # `floor(0 / bucket_s) == 0` for every such detection, which
            # collapses dedup to "alert once, ever" for that (camera, plate)
            # pair. Fall back to receipt time so bucketing stays sane; logged
            # once per servicer lifetime, not per detection, since a
            # misbehaving adapter will trigger this on every message.
            if not self._warned_missing_wall_clock:
                log.warning(
                    "detection %s has no wall_clock; falling back to receipt time for dedup",
                    detection.detection_id,
                )
                self._warned_missing_wall_clock = True
            wall_clock_s = time.time()
        if not self._deduper.should_alert(detection.camera_id, result.entry.plate, wall_clock_s):
            METRICS.inc(DEDUP_SUPPRESSED)
            return  # same vehicle, same dwell -- already alerted this bucket

        dedup_key = self._deduper.key_for(detection.camera_id, result.entry.plate, wall_clock_s)
        alert = self._alert_builder.build(detection, result, dedup_key)
        self._publisher.publish(alert)
        METRICS.inc(ALERTS_EMITTED)


def serve(
    store: WatchlistStore,
    deduper: Deduper,
    publisher: AlertPublisher,
    settings: MatchSettings,
    detection_publisher: DetectionPublisher | None = None,
) -> grpc.Server:
    """Build and start the gRPC server. Returns the (already-started) server
    so a caller controls its own shutdown -- this function does not block,
    matching how `app.py`'s lifespan needs to run it alongside uvicorn rather
    than instead of it."""
    # `grpc_max_workers` bounds the threads doing the work; the options bound
    # what a client can ask of us. `max_concurrent_streams` sits well above
    # the thread count on purpose -- an open-but-idle stream is cheap, and it
    # is in-flight *messages* that occupy handler threads.
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=settings.grpc_max_workers),
        options=[
            ("grpc.max_receive_message_length", settings.grpc_max_receive_message_bytes),
            ("grpc.max_concurrent_streams", settings.grpc_max_concurrent_streams),
        ],
    )
    servicer = MetadataIngestServicer(
        store, deduper, publisher, settings, detection_publisher=detection_publisher
    )
    adapter_pb2_grpc.add_MetadataIngestServiceServicer_to_server(servicer, server)
    server.add_insecure_port(f"{settings.grpc_host}:{settings.grpc_port}")
    server.start()
    log.info("MetadataIngestService listening on %s:%d", settings.grpc_host, settings.grpc_port)
    return server
