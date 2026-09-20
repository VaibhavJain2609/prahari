"""Read-path latency sampling and host resource capture.

The sampler periodically times the endpoints an operator console actually
hits — camera list, summary, geojson, and a correlation route build — while
the farm and injector put the write path under load. Resource usage is
sampled from `docker stats` (or `kubectl top` when the platform is in k3d);
when neither is available the run records that honestly rather than dropping
the column silently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time

import httpx

from .config import INTERNAL_TOKEN_HEADER
from .report import Samples

log = logging.getLogger(__name__)


async def sample_endpoint(
    client: httpx.AsyncClient,
    samples: Samples,
    name: str,
    path: str,
    phase: str,
    params: dict | None = None,
) -> None:
    t0 = time.perf_counter()
    try:
        resp = await client.get(path, params=params or {})
        elapsed_ms = (time.perf_counter() - t0) * 1000
        samples.add(
            name,
            elapsed_ms,
            phase=phase,
            status=resp.status_code,
            bytes=len(resp.content),
        )
    except httpx.HTTPError:
        samples.add(name, (time.perf_counter() - t0) * 1000, phase=phase, status=0)


async def rest_sampler(
    registry: httpx.AsyncClient,
    correlation: httpx.AsyncClient | None,
    watchlist_plates: list[str],
    interval_s: float,
    samples: Samples,
    phase: str,
    stop: asyncio.Event,
) -> None:
    """Round-robin timing of the console's read endpoints for `duration`."""
    import random

    rng = random.Random(0)
    plate = rng.choice(watchlist_plates) if watchlist_plates else "GJ01AB1234"
    while not stop.is_set():
        await sample_endpoint(
            registry,
            samples,
            "api.cameras_list_ms",
            "/api/v1/cameras",
            phase,
            params={"limit": 5000},
        )
        await sample_endpoint(
            registry, samples, "api.cameras_summary_ms", "/api/v1/cameras/summary", phase
        )
        await sample_endpoint(
            registry, samples, "api.cameras_geojson_ms", "/api/v1/cameras/geojson", phase
        )
        if correlation is not None:
            await sample_endpoint(
                correlation,
                samples,
                "api.route_build_ms",
                f"/api/v1/routes/{plate}",
                phase,
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            pass


def docker_stats() -> list[dict]:
    """One `docker stats --no-stream` snapshot of prahari-* containers.

    Returns [] when docker or the daemon is absent — the caller records the
    gap rather than the harness pretending to have resource numbers.
    """
    if not shutil.which("docker"):
        return []
    try:
        out = subprocess.run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                '{"name":"{{.Name}}","cpu":"{{.CPUPerc}}","mem":"{{.MemUsage}}","net":"{{.NetIO}}","pids":"{{.PIDs}}"}',
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    rows = []
    for line in out.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "prahari" in row.get("name", ""):
            rows.append(row)
    return rows


def kubectl_top() -> list[dict]:
    """`kubectl top pods -n prahari` — needs metrics-server; [] otherwise."""
    if not shutil.which("kubectl"):
        return []
    try:
        out = subprocess.run(
            ["kubectl", "top", "pods", "-n", "prahari", "--no-headers"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if out.returncode != 0:
        return []
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            rows.append({"pod": parts[0], "cpu": parts[1], "mem": parts[2]})
    return rows


async def resource_sampler(
    interval_s: float, samples: Samples, phase: str, stop: asyncio.Event
) -> None:
    """Snapshot container CPU/mem every `interval_s`; records the source used."""
    while not stop.is_set():
        rows = await asyncio.to_thread(docker_stats)
        source = "docker"
        if not rows:
            rows = await asyncio.to_thread(kubectl_top)
            source = "kubectl"
        for row in rows:
            samples.add("resource.sample", 0.0, phase=phase, source=source, **row)
        if not rows:
            samples.add("resource.unavailable", 1.0, phase=phase)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            pass


def make_client(base_url: str, internal_token: str, timeout: float = 30.0) -> httpx.AsyncClient:
    headers = {INTERNAL_TOKEN_HEADER: internal_token} if internal_token else {}
    return httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=timeout,
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
    )
