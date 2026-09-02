"""`MatchEngineClient`: the worker's half of `MetadataIngestService`.

Exercised against a real `grpc.server` on a loopback port, with a servicer
authored by this test rather than `prahari_match`'s real one -- inference and
match-engine are separate workspace members with disjoint ownership, and what
this suite needs to prove is the wire behaviour of the client (batching into
one `StreamDetections` call, surviving a peer that is not there), not the
match engine's own matching logic, which `prahari_match`'s own tests already
cover.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent import futures

import grpc
import pytest
from prahari.v1 import adapter_pb2, adapter_pb2_grpc, events_pb2

from prahari_inference.config import DetectorSettings
from prahari_inference.grpc_client import MatchEngineClient


class _RecordingServicer(adapter_pb2_grpc.MetadataIngestServiceServicer):
    """Records every detection it receives per `StreamDetections` call, and
    acks with counts a test can assert on."""

    def __init__(self) -> None:
        self.calls: list[list[events_pb2.VehicleDetection]] = []

    def StreamDetections(
        self,
        request_iterator: Iterator[adapter_pb2.StreamDetectionsRequest],
        context: grpc.ServicerContext,
    ) -> adapter_pb2.StreamDetectionsResponse:
        received = [request.detection for request in request_iterator]
        self.calls.append(received)
        return adapter_pb2.StreamDetectionsResponse(
            ack=adapter_pb2.IngestAck(accepted=len(received), rejected=0, detail="")
        )


@pytest.fixture
def running_server():
    servicer = _RecordingServicer()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    adapter_pb2_grpc.add_MetadataIngestServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield servicer, f"127.0.0.1:{port}"
    finally:
        server.stop(grace=None)


def _detection(detection_id: str) -> events_pb2.VehicleDetection:
    return events_pb2.VehicleDetection(detection_id=detection_id, camera_id="cam-1")


class TestSendDetections:
    def test_a_batch_is_sent_as_one_stream_detections_call(self, running_server):
        servicer, address = running_server
        client = MatchEngineClient(DetectorSettings(match_engine_grpc=address))

        ack = client.send_detections([_detection("D1"), _detection("D2")])

        assert ack is not None
        assert ack.accepted == 2
        assert ack.rejected == 0
        assert len(servicer.calls) == 1
        assert [d.detection_id for d in servicer.calls[0]] == ["D1", "D2"]

    def test_an_empty_batch_opens_no_rpc_at_all(self, running_server):
        servicer, address = running_server
        client = MatchEngineClient(DetectorSettings(match_engine_grpc=address))

        ack = client.send_detections([])

        assert ack is None
        assert servicer.calls == []

    def test_two_batches_are_two_independent_calls(self, running_server):
        servicer, address = running_server
        client = MatchEngineClient(DetectorSettings(match_engine_grpc=address))

        client.send_detections([_detection("D1")])
        client.send_detections([_detection("D2"), _detection("D3")])

        assert len(servicer.calls) == 2
        assert [d.detection_id for d in servicer.calls[0]] == ["D1"]
        assert [d.detection_id for d in servicer.calls[1]] == ["D2", "D3"]

    def test_a_batch_survives_no_server_listening(self):
        # An address nothing is bound to -- the RPC must fail fast (channel
        # connects but the peer refuses) rather than the default multi-minute
        # gRPC deadline, and the caller must get None back, not an exception.
        client = MatchEngineClient(DetectorSettings(match_engine_grpc="127.0.0.1:1"))

        ack = client.send_detections([_detection("D1")])

        assert ack is None


def test_close_closes_the_channel(running_server):
    _servicer, address = running_server
    client = MatchEngineClient(DetectorSettings(match_engine_grpc=address))

    client.close()

    # grpc raises ValueError -- not RpcError -- for a call on an
    # already-closed channel, since this is a lifecycle misuse rather than a
    # peer-unreachable failure, and send_detections only shields callers from
    # the latter (see its docstring). worker.stop() never triggers this: the
    # batcher drains synchronously, so every send_detections call finishes
    # before match_client.close() runs.
    with pytest.raises(ValueError, match="closed channel"):
        client.send_detections([_detection("D1")])
