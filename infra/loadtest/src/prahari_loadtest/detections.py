"""Synthetic `VehicleDetection` injection over the real gRPC ingest link.

This drives `MetadataIngestService.StreamDetections` on the match engine —
the same client-streaming RPC the inference workers use (`adapter.proto`).
Generated detections are honest about being synthetic: `evidence_ref` is
tagged `loadtest://` and the plates are either random non-watchlist strings
or real plates drawn from `data/watchlist/` so a hit exercises the actual
Bloom → fuzzy-match → dedup → alert path.

Watchlist-hit detections are tracked by `detection_id`; an alert poller on the
match engine's `/api/v1/alerts` surface (the RecentAlertsPublisher ring
buffer) matches them back, giving an end-to-end inject→alert latency sample.
The floor on that measurement is `alert_poll_s` — it measures "alert became
visible", not "matcher scored", and the summary says so.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import random
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import grpc
import httpx

from .config import INTERNAL_TOKEN_HEADER
from .farm import FarmCamera
from .report import Samples

log = logging.getLogger(__name__)

GRPC_IMPORT_ERROR: str | None = None
try:
    from prahari.v1 import adapter_pb2, adapter_pb2_grpc, common_pb2, events_pb2
except ImportError as exc:  # stubs are gitignored; `make proto` first
    GRPC_IMPORT_ERROR = str(exc)
    adapter_pb2 = adapter_pb2_grpc = common_pb2 = events_pb2 = None  # type: ignore[assignment]

_VEHICLE_CLASSES = ["car", "truck", "bus", "two-wheeler", "auto"]


def load_watchlist_plates(watchlist_dir: str) -> list[str]:
    """Plates from the repo's synthetic watchlist snapshots (json + csv).

    Read, not parsed through the matcher: a hit needs the plate the *watchlist*
    holds, and the match engine normalises on load, so sending the raw plate
    string is the faithful simulation of OCR seeing that vehicle.
    """
    plates: list[str] = []
    root = Path(watchlist_dir)
    if not root.is_dir():
        return plates
    for path in sorted(root.iterdir()):
        try:
            if path.suffix == ".json":
                for entry in json.loads(path.read_text()):
                    if entry.get("plate"):
                        plates.append(entry["plate"])
            elif path.suffix == ".csv":
                for entry in csv.DictReader(path.read_text().splitlines()):
                    if entry.get("plate"):
                        plates.append(entry["plate"])
        except (json.JSONDecodeError, OSError):
            log.warning("could not parse watchlist file %s; skipping", path)
    return plates


def _random_plate(rng: random.Random) -> str:
    """A STANDARD-format Gujarat plate: `GJ<dd><LL><dddd>`."""
    return (
        f"GJ{rng.randint(1, 38):02d}"
        f"{chr(65 + rng.randint(0, 25))}{chr(65 + rng.randint(0, 25))}"
        f"{rng.randint(0, 9999):04d}"
    )


def _build_detection(
    cam: FarmCamera,
    plate: str | None,
    rng: random.Random,
    seq: int,
) -> Any:
    det = events_pb2.VehicleDetection(
        detection_id=f"lt-{seq:012d}-{rng.getrandbits(32):08x}",
        camera_id=cam.id,
        observed_at=common_pb2.StreamTime(pts_ms=seq * 500),
        vehicle_box=common_pb2.BoundingBox(x_min=0.2, y_min=0.4, x_max=0.6, y_max=0.8),
        vehicle_class=rng.choice(_VEHICLE_CLASSES),
        vehicle_confidence=round(rng.uniform(0.6, 0.98), 3),
        evidence_ref=f"loadtest://synthetic/{cam.external_id}",
    )
    det.observed_at.wall_clock.GetCurrentTime()
    if plate is not None:
        det.plate.raw_text = plate
        det.plate.normalised_text = plate
        det.plate.char_confidence.extend([round(rng.uniform(0.7, 0.99), 3) for _ in plate])
        det.plate.format = events_pb2.PLATE_FORMAT_STANDARD
        det.plate.plate_box.CopyFrom(
            common_pb2.BoundingBox(x_min=0.3, y_min=0.6, x_max=0.5, y_max=0.7)
        )
    return det


async def _request_stream(
    cameras: list[FarmCamera],
    watchlist: list[str],
    rate_per_camera_s: float,
    hit_rate: float,
    deadline: float,
    rng: random.Random,
    samples: Samples,
    phase: str,
    pending_hits: dict[str, float],
) -> AsyncIterator[Any]:
    """Yield detection requests at the configured aggregate rate.

    Per-camera Poisson-ish emission is approximated by an aggregate stream:
    each tick picks a random camera, so over time every camera emits at
    `rate_per_camera_s`. Timestamps come from the wall clock, not frame
    counts — same rule the workers follow (PTS-derived wall_clock).
    """
    total_rate = rate_per_camera_s * len(cameras)
    interval = 1.0 / total_rate if total_rate > 0 else 1.0
    seq = 0
    while time.monotonic() < deadline:
        cam = rng.choice(cameras)
        is_hit = watchlist and rng.random() < hit_rate
        plate = rng.choice(watchlist) if is_hit else _random_plate(rng)
        det = _build_detection(cam, plate, rng, seq)
        seq += 1
        sent_at = time.time()
        if is_hit:
            pending_hits[det.detection_id] = sent_at
        samples.add("detection.sent", 1.0, phase=phase)
        yield adapter_pb2.StreamDetectionsRequest(detection=det)
        await asyncio.sleep(interval)


async def stream_detections(
    grpc_target: str,
    cameras: list[FarmCamera],
    watchlist: list[str],
    rate_per_camera_s: float,
    hit_rate: float,
    duration_s: float,
    n_streams: int,
    internal_token: str,
    samples: Samples,
    phase: str,
    pending_hits: dict[str, float],
    seed: int = 7,
) -> dict[str, int]:
    """Open `n_streams` client streams and push detections for `duration_s`.

    Returns the server's aggregate IngestAck {accepted, rejected} across all
    streams — the match engine's own verdict on what it took. A stream that
    dies mid-phase reconnects with a 2 s backoff (the workers' reconnect
    discipline), so a flaky target produces `grpc.stream_error` samples in the
    data rather than a silently shortened phase.
    """
    if GRPC_IMPORT_ERROR:
        raise RuntimeError(
            f"prahari-proto stubs unavailable ({GRPC_IMPORT_ERROR}); run `make proto` first"
        )
    deadline = time.monotonic() + duration_s

    async def one_stream(idx: int) -> dict[str, int] | None:
        rng = random.Random(seed + idx)
        metadata = ((INTERNAL_TOKEN_HEADER, internal_token),) if internal_token else ()
        totals = {"accepted": 0, "rejected": 0}
        connected_once = False
        async with grpc.aio.insecure_channel(grpc_target) as channel:
            stub = adapter_pb2_grpc.MetadataIngestServiceStub(channel)
            while time.monotonic() < deadline:
                t0 = time.perf_counter()
                try:
                    response = await stub.StreamDetections(
                        _request_stream(
                            cameras,
                            watchlist,
                            rate_per_camera_s / n_streams,
                            hit_rate,
                            deadline,
                            rng,
                            samples,
                            phase,
                            pending_hits,
                        ),
                        metadata=metadata,
                    )
                except grpc.aio.AioRpcError as exc:
                    samples.add("grpc.stream_error", 1.0, phase=phase, code=str(exc.code()))
                    await asyncio.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
                    continue
                totals["accepted"] += response.ack.accepted
                totals["rejected"] += response.ack.rejected
                connected_once = True
                samples.add("grpc.stream_open_s", time.perf_counter() - t0, phase=phase)
                break  # stream ran to the deadline and closed cleanly
        return totals if connected_once else None

    results = await asyncio.gather(*(one_stream(i) for i in range(n_streams)))
    return {
        "accepted": sum(r["accepted"] for r in results if r is not None),
        "rejected": sum(r["rejected"] for r in results if r is not None),
        "streams_failed": sum(1 for r in results if r is None),
    }


async def alert_latency_probe(
    match_http: str,
    internal_token: str,
    pending_hits: dict[str, float],
    samples: Samples,
    phase: str,
    poll_s: float,
    stop: asyncio.Event,
) -> None:
    """Poll `/api/v1/alerts` until each pending watchlist detection appears.

    Measures inject→alert-visible latency. Poll interval is the resolution
    floor; summary.md reports it alongside the numbers.
    """
    headers = {INTERNAL_TOKEN_HEADER: internal_token} if internal_token else {}
    async with httpx.AsyncClient(base_url=match_http, headers=headers, timeout=10.0) as http:
        while pending_hits and not stop.is_set():
            try:
                resp = await http.get("/api/v1/alerts", params={"limit": 500})
                if resp.status_code == 200:
                    now = time.time()
                    for alert in resp.json():
                        det_id = (alert.get("detection") or {}).get("detection_id")
                        if det_id in pending_hits:
                            samples.add(
                                "e2e.alert_latency_ms",
                                (now - pending_hits.pop(det_id)) * 1000,
                                phase=phase,
                            )
            except httpx.HTTPError:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_s)
            except TimeoutError:
                pass
