"""The Day 2 link: sampled frames reaching the detection cascade and, from
there, the match engine -- `_pump` -> `CrossCameraBatcher` -> `_handle_batch`
-> `DetectionPipeline` -> `MatchEngineClient`.

Kept separate from `test_worker.py`: every capture double there deliberately
yields no frames (`StubCapture`, `DyingCapture`, ...), since that suite is
about the health-reporting contract, not the cascade. Proving the cascade
wiring needs a double that actually produces frames, which is this file's
only addition.
"""

from __future__ import annotations

import time

import numpy as np

from prahari_inference.capture import Frame
from prahari_inference.config import DetectorSettings, IngestSettings
from prahari_inference.timing import FrameTiming
from prahari_inference.worker import CameraAssignment, IngestWorker

SETTINGS = IngestSettings(max_active_cameras=3, heartbeat_interval_s=0.01)


class FrameYieldingCapture:
    """A `StreamCapture` stand-in that actually decodes something: four
    frames, each its own `loop_epoch` so `SampleGate` admits every one of
    them regardless of `sample_fps`, then the generator ends -- like a clip
    that finishes, not a camera that dies (I1's restart backoff plays no
    part in what this file is testing)."""

    def __init__(self, entry, *, ingest, use_hls=False, url=None) -> None:
        self.closed = False
        self.connected = True
        self.consecutive_failures = 0
        self.last_error: str | None = None
        self.measured_fps = 30.0

    def frames(self):
        for i in range(4):
            yield Frame(
                image=np.zeros((2, 2, 3), dtype=np.uint8),
                timing=FrameTiming(
                    pts_ms=float(i * 500),
                    delta_ms=None if i == 0 else 500.0,
                    wall_clock=time.time(),
                    loop_epoch=i,
                    replaying=False,
                    discontinuity=(i == 0),
                ),
                camera_id="cam-1",
            )

    def request_stop(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakePipeline:
    """Records every batch handed to it and returns one placeholder "result"
    per frame -- what a result actually contains is `DetectionPipeline`'s own
    concern, already covered by `test_pipeline.py`; this double only needs to
    prove `_handle_batch` calls `process_batch` then `to_protobuf` on each
    result it gets back."""

    def __init__(self) -> None:
        self.batches: list[list] = []

    def process_batch(self, frames):
        self.batches.append(list(frames))
        return list(frames)  # one placeholder "result" per input frame

    def to_protobuf(self, result):
        from prahari.v1 import events_pb2

        return [events_pb2.VehicleDetection(detection_id=f"det-{id(result)}", camera_id="cam-1")]


class FakeMatchClient:
    def __init__(self) -> None:
        self.sent: list[list] = []
        self.closed = False

    def send_detections(self, detections):
        self.sent.append(list(detections))
        return None

    def close(self) -> None:
        self.closed = True


def _worker(**detect_overrides) -> tuple[IngestWorker, FakePipeline, FakeMatchClient]:
    pipeline = FakePipeline()
    match_client = FakeMatchClient()
    assignment = CameraAssignment(camera_id="cam-1", url="rtsp://prahari-mediamtx:8554/cam-1")
    worker = IngestWorker(
        [assignment],
        settings=SETTINGS,
        detect_settings=DetectorSettings(**detect_overrides),
        pipeline=pipeline,
        match_client=match_client,
    )
    return worker, pipeline, match_client


def test_sampled_frames_reach_the_pipeline_and_the_match_client(monkeypatch):
    """The whole point of Day 2's wiring: frames flowing through `_pump`
    reach `DetectionPipeline.process_batch` and the resulting protobufs reach
    `MatchEngineClient.send_detections`, with no gRPC socket or model weights
    involved."""
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", FrameYieldingCapture)
    worker, pipeline, match_client = _worker(batch_size=4, batch_timeout_ms=5000)
    assignment = worker._assignments["cam-1"]

    try:
        worker._start_pump(assignment)
        worker._pump_threads["cam-1"].join(timeout=5.0)

        assert len(pipeline.batches) == 1
        assert len(pipeline.batches[0]) == 4
        assert len(match_client.sent) == 1
        assert len(match_client.sent[0]) == 4
    finally:
        worker._batcher.close()
        worker._match_client.close()


def test_a_full_batch_flushes_before_the_clip_ends(monkeypatch):
    """`batch_size=2` against four frames must flush twice, synchronously, on
    the pump thread's own submissions -- not wait for the fourth frame or a
    timeout, per `CrossCameraBatcher.submit`'s size-trigger contract."""
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", FrameYieldingCapture)
    worker, pipeline, match_client = _worker(batch_size=2, batch_timeout_ms=5000)
    assignment = worker._assignments["cam-1"]

    try:
        worker._start_pump(assignment)
        worker._pump_threads["cam-1"].join(timeout=5.0)

        assert [len(b) for b in pipeline.batches] == [2, 2]
        assert [len(s) for s in match_client.sent] == [2, 2]
    finally:
        worker._batcher.close()
        worker._match_client.close()


def test_publish_disabled_still_runs_the_cascade_but_sends_nothing(monkeypatch):
    """`publish_enabled=False` is for offline throughput profiling: the
    cascade must still run (that is the thing being profiled), only the
    network hop to the match engine is skipped."""
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", FrameYieldingCapture)
    worker, pipeline, match_client = _worker(
        batch_size=4, batch_timeout_ms=5000, publish_enabled=False
    )
    assignment = worker._assignments["cam-1"]

    try:
        worker._start_pump(assignment)
        worker._pump_threads["cam-1"].join(timeout=5.0)

        assert len(pipeline.batches) == 1
        assert match_client.sent == []
    finally:
        worker._batcher.close()
        worker._match_client.close()
