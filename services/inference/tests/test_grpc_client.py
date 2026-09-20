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

import threading
import time
from collections.abc import Iterator
from concurrent import futures
from types import SimpleNamespace

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
        self.metadata_seen: list[dict[str, str]] = []

    def StreamDetections(
        self,
        request_iterator: Iterator[adapter_pb2.StreamDetectionsRequest],
        context: grpc.ServicerContext,
    ) -> adapter_pb2.StreamDetectionsResponse:
        received = [request.detection for request in request_iterator]
        self.calls.append(received)
        self.metadata_seen.append(dict(context.invocation_metadata()))
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

    def test_internal_token_is_sent_as_call_metadata(self, running_server):
        # The match engine's InternalTokenInterceptor reads `x-internal-token`
        # off invocation metadata; a worker that does not send it gets every
        # batch rejected once that gate is armed.
        servicer, address = running_server
        client = MatchEngineClient(
            DetectorSettings(match_engine_grpc=address), internal_token="tok-1"
        )

        client.send_detections([_detection("D1")])

        assert servicer.metadata_seen[0]["x-internal-token"] == "tok-1"

    def test_no_token_sends_no_credential_metadata(self, running_server):
        servicer, address = running_server
        client = MatchEngineClient(DetectorSettings(match_engine_grpc=address))

        client.send_detections([_detection("D1")])

        assert "x-internal-token" not in servicer.metadata_seen[0]

    def test_a_batch_survives_no_server_listening(self):
        # An address nothing is bound to -- the RPC must fail fast (channel
        # connects but the peer refuses) rather than the default multi-minute
        # gRPC deadline, and the caller must get None back, not an exception.
        client = MatchEngineClient(DetectorSettings(match_engine_grpc="127.0.0.1:1"))

        ack = client.send_detections([_detection("D1")])

        assert ack is None

    def test_the_configured_deadline_is_passed_to_the_stub(self, running_server):
        # A fake stub in place of the real one: what matters here is that
        # `timeout=` leaves `DetectorSettings.grpc_timeout_s` and reaches the
        # call — an unwired default of None is exactly the hang this exists
        # to prevent.
        _servicer, address = running_server
        client = MatchEngineClient(DetectorSettings(match_engine_grpc=address, grpc_timeout_s=3.25))

        calls: list[dict] = []

        class _RecordingStub:
            def StreamDetections(self, request_iterator, timeout=None):
                calls.append({"timeout": timeout})
                list(request_iterator)
                return SimpleNamespace(ack=adapter_pb2.IngestAck(accepted=1, rejected=0, detail=""))

        client._stub = _RecordingStub()
        client.send_detections([_detection("D1")])

        assert calls == [{"timeout": 3.25}]

    def test_a_stalled_server_drops_the_batch_at_the_deadline_not_forever(self):
        """The failure this whole setting exists for: a match engine that
        accepts the stream and never answers. With no `timeout=` this call
        hangs forever and wedges the calling pump/flush thread; with it, the
        batch is dropped at the deadline and the worker moves on."""

        class _StallingServicer(adapter_pb2_grpc.MetadataIngestServiceServicer):
            def __init__(self) -> None:
                self.release = threading.Event()

            def StreamDetections(self, request_iterator, context):
                for _ in request_iterator:
                    pass
                self.release.wait(timeout=30)  # the wedge: answers only if released
                return adapter_pb2.StreamDetectionsResponse(
                    ack=adapter_pb2.IngestAck(accepted=0, rejected=0, detail="")
                )

        servicer = _StallingServicer()
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
        adapter_pb2_grpc.add_MetadataIngestServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            client = MatchEngineClient(
                DetectorSettings(match_engine_grpc=f"127.0.0.1:{port}", grpc_timeout_s=0.3)
            )

            started = time.monotonic()
            ack = client.send_detections([_detection("D1")])
            elapsed = time.monotonic() - started

            assert ack is None
            assert elapsed < 10.0, (
                "send_detections blocked past the configured deadline — "
                "StreamDetections is running without a timeout"
            )
        finally:
            servicer.release.set()  # let the handler return so teardown is quick
            server.stop(grace=None)


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
