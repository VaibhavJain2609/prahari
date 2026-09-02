"""gRPC client for `MetadataIngestService`, the worker -> match-engine link
(adapter.proto). See `services/match-engine/src/prahari_match/grpc_server.py`
for the servicer this talks to.

One `grpc.Channel` per worker process, held for the worker's lifetime — HTTP/2
multiplexes many RPC calls onto a single channel, so opening a fresh one per
batch would pay a connection-setup cost the design explicitly avoids (see
adapter.proto's comment on why this link is gRPC at all). Each batch flush
instead opens its own client-streaming `StreamDetections` call: every
detection produced by that batch goes out as one message, the request stream
closes, and the single `IngestAck` that comes back covers exactly that batch.

`grpcio` is a normal, non-CUDA dependency — unlike `ultralytics`/`paddleocr`
this has no lazy-import requirement and is imported at module load like any
other library.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import grpc
from prahari.v1 import adapter_pb2, adapter_pb2_grpc, events_pb2

from .config import DetectorSettings, detector_settings

__all__ = ["MatchEngineClient"]

log = logging.getLogger(__name__)


class MatchEngineClient:
    """Thin wrapper around the `MetadataIngestService` stub.

    Construction never blocks or connects — `grpc.insecure_channel` builds
    lazily, and the first real network activity happens on the first
    `send_detections` call. That matters on startup order: the worker must
    not fail to construct because the match-engine pod has not been
    scheduled yet, since a Kubernetes cluster gives no guarantee about which
    of the two comes up first.
    """

    def __init__(
        self,
        settings: DetectorSettings | None = None,
        *,
        channel: grpc.Channel | None = None,
    ) -> None:
        self._s = settings or detector_settings()
        self._channel = channel or grpc.insecure_channel(self._s.match_engine_grpc)
        self._stub = adapter_pb2_grpc.MetadataIngestServiceStub(self._channel)

    def send_detections(
        self, detections: Iterable[events_pb2.VehicleDetection]
    ) -> adapter_pb2.IngestAck | None:
        """Stream one batch of detections through a fresh `StreamDetections`
        call and return the resulting ack.

        Returns `None`, opening no RPC at all, for an empty batch —
        `DetectionPipeline.process_batch` produces a `DetectionResult` for
        every sampled frame, including ones the motion gate skipped or that
        had no legible plate, so a batch that detected nothing worth sending
        is the common case, not the exception.

        A gRPC failure (peer not up yet, deadline exceeded) is caught and
        logged rather than raised: one bad batch must not take down the pump
        or batch-flush thread that called this, the same reasoning `_pump`'s
        own top-level `except` in `worker.py` already applies to decode
        failures. The detections in a failed batch are not retried — by the
        time the next batch flushes, the vehicles in this one are gone from
        frame, so replaying it later would attach a stale timestamp to
        evidence instead of dropping it, which is the worse failure mode.
        """
        requests = [adapter_pb2.StreamDetectionsRequest(detection=d) for d in detections]
        if not requests:
            return None
        try:
            response = self._stub.StreamDetections(iter(requests))
        except grpc.RpcError as exc:
            log.warning("StreamDetections failed (%d detections dropped): %s", len(requests), exc)
            return None
        return response.ack

    def close(self) -> None:
        self._channel.close()
