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
from prahari_common.internal_auth import expected_token_ok, provided_token

from .alerts import AlertBuilder, AlertPublisher
from .config import MatchSettings
from .dedup import Deduper
from .detections import DetectionPublisher, NullDetectionPublisher
from .matcher import WatchlistStore, match

__all__ = ["InternalTokenInterceptor", "MetadataIngestServicer", "serve"]

log = logging.getLogger(__name__)


class InternalTokenInterceptor(grpc.ServerInterceptor):
    """Rejects calls that do not carry the shared `x-internal-token` metadata.

    The gRPC twin of `app.py`'s `require_internal_token` middleware: an open
    `MetadataIngestService` lets anything that can reach the pod inject
    detections — and therefore alerts — straight into the evidence trail, so
    it is gated by the same credential as the HTTP surface.

    `serve()` installs it only when `MatchSettings.internal_token` is set;
    empty means the gate is off (`expected_token_ok`), so the interceptor is
    simply never built rather than passing everything through a disabled
    check on every call.
    """

    def __init__(self, expected_token: str) -> None:
        self._expected = expected_token

    def intercept_service(self, continuation, handler_call_details):  # noqa: ANN001, ANN202 - grpc's own signature
        handler = continuation(handler_call_details)
        provided = provided_token(dict(handler_call_details.invocation_metadata or ()))
        if handler is None or expected_token_ok(provided, self._expected):
            return handler

        def reject(_request_or_iterator, context):  # noqa: ANN001, ANN202
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "internal token required")

        # Return a rejecting handler of the same cardinality as the real one —
        # the four method types differ in request/response streaming shape, and
        # answering a client-streaming call with a unary handler would fail the
        # RPC before `reject` ever ran, with the wrong status code.
        if handler.stream_unary:
            return grpc.stream_unary_rpc_method_handler(reject)
        if handler.stream_stream:
            return grpc.stream_stream_rpc_method_handler(reject)
        if handler.unary_stream:
            return grpc.unary_stream_rpc_method_handler(reject)
        return grpc.unary_unary_rpc_method_handler(reject)


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
        detail = ""

        for request in request_iterator:
            detection = request.detection
            try:
                self._handle_detection(detection)
                accepted += 1
            except Exception as exc:  # noqa: BLE001 - one bad message must not drop the stream
                rejected += 1
                detail = str(exc)
                log.exception("failed to process detection %s", detection.detection_id)

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

    def _handle_detection(self, detection) -> None:  # noqa: ANN001 - adapter_pb2.VehicleDetection-shaped
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
            return  # same vehicle, same dwell -- already alerted this bucket

        dedup_key = self._deduper.key_for(detection.camera_id, result.entry.plate, wall_clock_s)
        alert = self._alert_builder.build(detection, result, dedup_key)
        self._publisher.publish(alert)


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
    interceptors = (
        (InternalTokenInterceptor(settings.internal_token),) if settings.internal_token else ()
    )
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=settings.grpc_max_workers),
        interceptors=interceptors,
    )
    servicer = MetadataIngestServicer(
        store, deduper, publisher, settings, detection_publisher=detection_publisher
    )
    adapter_pb2_grpc.add_MetadataIngestServiceServicer_to_server(servicer, server)
    server.add_insecure_port(f"{settings.grpc_host}:{settings.grpc_port}")
    server.start()
    log.info("MetadataIngestService listening on %s:%d", settings.grpc_host, settings.grpc_port)
    return server
