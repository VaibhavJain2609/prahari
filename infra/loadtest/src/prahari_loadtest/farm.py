"""The synthetic camera farm.

Registers N cameras through the registry's real REST API — `POST
/api/v1/cameras`, the same path a manual (Model 2, direct-connect) camera
arrives by — and optionally emits worker-shaped heartbeats against
`POST /api/v1/cameras/{id}/heartbeat`.

Mode honesty:

* `simulate` — the farm emits heartbeats itself. This measures registry write
  throughput, health-verdict computation, and read-path latency under load.
  It does NOT measure decode, inference, or MediaMTX — no pixels move.
* `live` — cameras carry a real `rtsp_url`, ffmpeg publishes real streams into
  MediaMTX, and the deployed inference workers emit the heartbeats. The farm
  only registers; `--with-sim-heartbeats` exists for the degenerate case of
  live-mode-without-workers, and is recorded in the run config when used.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass

import httpx

from .config import DISTRICTS, LAT_MAX, LAT_MIN, LON_MAX, LON_MIN
from .report import Samples


@dataclass
class FarmCamera:
    id: str
    external_id: str
    district: str
    latitude: float
    longitude: float


def _camera_payload(external_id: str, i: int, rng: random.Random, rtsp_url: str | None) -> dict:
    return {
        "source": "loadtest",
        "external_id": external_id,
        "adapter": "manual",
        "camera_type": "ip",
        "site_name": f"Loadtest junction {i:05d}",
        "district": DISTRICTS[i % len(DISTRICTS)],
        "department": "loadtest",
        "owner": "loadtest-harness",
        "declared_fps": 15.0,
        "codec": "h264",
        "native_width": 640,
        "native_height": 480,
        "location": {
            "latitude": round(rng.uniform(LAT_MIN, LAT_MAX), 6),
            "longitude": round(rng.uniform(LON_MIN, LON_MAX), 6),
        },
        **({"rtsp_url": rtsp_url} if rtsp_url else {}),
    }


async def seed_cameras(
    client: httpx.AsyncClient,
    count: int,
    prefix: str,
    samples: Samples,
    phase: str,
    *,
    rtsp_template: str | None = None,
    concurrency: int = 32,
    seed: int = 1234,
) -> list[FarmCamera]:
    """Register `count` cameras; records per-request latency under `api.camera_create`.

    `prefix` must be unique per run — the registry rejects a duplicate
    (source, external_id) with 409, which this surfaces as a failure rather
    than silently reusing a previous run's rows.
    """
    rng = random.Random(seed)
    cameras: list[FarmCamera] = []
    errors: list[str] = []
    sem = asyncio.Semaphore(concurrency)

    async def register(i: int) -> None:
        external_id = f"{prefix}-{i:05d}"
        rtsp_url = rtsp_template.format(i=i) if rtsp_template else None
        payload = _camera_payload(external_id, i, rng, rtsp_url)
        async with sem:
            t0 = time.perf_counter()
            try:
                resp = await client.post("/api/v1/cameras", json=payload)
                elapsed_ms = (time.perf_counter() - t0) * 1000
                samples.add(
                    "api.camera_create_ms", elapsed_ms, phase=phase, status=resp.status_code
                )
                if resp.status_code == 201:
                    body = resp.json()
                    cameras.append(
                        FarmCamera(
                            id=body["id"],
                            external_id=external_id,
                            district=payload["district"],
                            latitude=payload["location"]["latitude"],
                            longitude=payload["location"]["longitude"],
                        )
                    )
                else:
                    errors.append(f"{external_id}: HTTP {resp.status_code} {resp.text[:200]}")
            except httpx.HTTPError as exc:
                samples.add(
                    "api.camera_create_ms", (time.perf_counter() - t0) * 1000, phase=phase, status=0
                )
                errors.append(f"{external_id}: {exc}")

    await asyncio.gather(*(register(i) for i in range(count)))
    if errors:
        # Registration failures are run-corrupting: a farm that thinks it has
        # N cameras but has fewer understates every per-camera rate. Fail the
        # step loudly instead of measuring a partial estate as if it were full.
        raise RuntimeError(
            f"camera seeding: {len(errors)}/{count} registrations failed; first: {errors[0]}"
        )
    return cameras


async def heartbeat_emitters(
    client: httpx.AsyncClient,
    cameras: list[FarmCamera],
    interval_s: float,
    samples: Samples,
    phase: str,
    stop: asyncio.Event,
) -> None:
    """One emitter task per camera, staggered across the interval.

    Each camera posts at `interval_s` cadence with plausible worker payloads —
    measured_fps jittering around the declared rate, cumulative frame counts,
    a loop_epoch that rolls every ~3 intervals. Staggering avoids a thundering
    herd that would measure burst absorption instead of steady-state load.
    """
    rng = random.Random(0)

    async def emit(cam: FarmCamera, offset_s: float) -> None:
        await asyncio.sleep(offset_s)
        frames = 0
        epoch = 0
        while not stop.is_set():
            t0 = time.perf_counter()
            payload = {
                "worker_id": "loadtest-farm",
                "connected": True,
                "measured_fps": round(15.0 + rng.uniform(-0.5, 0.5), 2),
                "frames_decoded": frames,
                "consecutive_failures": 0,
                "black_frame_ratio": 0.0,
                "tamper_suspected": False,
                "loop_epoch": epoch,
                "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "last_frame_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            try:
                resp = await client.post(f"/api/v1/cameras/{cam.id}/heartbeat", json=payload)
                samples.add(
                    "heartbeat.post_ms",
                    (time.perf_counter() - t0) * 1000,
                    phase=phase,
                    status=resp.status_code,
                )
            except httpx.HTTPError:
                samples.add(
                    "heartbeat.post_ms",
                    (time.perf_counter() - t0) * 1000,
                    phase=phase,
                    status=0,
                )
            frames += int(15 * interval_s)
            epoch += 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except TimeoutError:
                pass

    step = interval_s / max(len(cameras), 1)
    await asyncio.gather(*(emit(cam, i * step) for i, cam in enumerate(cameras)))


async def decommission(
    client: httpx.AsyncClient, cameras: list[FarmCamera], concurrency: int = 32
) -> int:
    """Soft-delete every seeded camera. Returns the count decommissioned."""
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def one(cam: FarmCamera) -> None:
        nonlocal done
        async with sem:
            try:
                resp = await client.delete(f"/api/v1/cameras/{cam.id}")
                if resp.status_code == 200:
                    done += 1
            except httpx.HTTPError:
                pass

    await asyncio.gather(*(one(c) for c in cameras))
    return done


async def reconcile_streams(client: httpx.AsyncClient) -> dict:
    """Ask the registry to reconcile MediaMTX paths for the seeded cameras."""
    resp = await client.post("/api/v1/streams/reconcile")
    resp.raise_for_status()
    return resp.json()
