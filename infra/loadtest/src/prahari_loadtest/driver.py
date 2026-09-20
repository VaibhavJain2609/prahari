"""The stepped run orchestrator.

A run is a list of camera-count steps (`--cameras 5,50,500`). Each step:
seed the additional cameras, run the workload for `duration_s`, sample the
read path and resources throughout, then move to the next step without
tearing down — a ramp, not a series of cold starts. At the end every seeded
camera is decommissioned (soft delete) unless `--no-cleanup`.

What a simulate-mode step measures:
  * camera registration latency (`api.camera_create_ms`)
  * heartbeat ingestion (`heartbeat.post_ms` + achieved rate)
  * detection ingest (`detection.sent` rate, gRPC ack accepted/rejected)
  * detection→alert-visible latency (`e2e.alert_latency_ms`, floored at
    `alert_poll_s`)
  * read-path latency under load (`api.cameras_*_ms`, `api.route_build_ms`)
  * per-service CPU/mem (`resource.sample`)
"""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import platform
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from . import detections, farm, sampler
from .config import Endpoints, RunConfig
from .live import MediaFarm
from .report import RunDir, Samples, describe

log = logging.getLogger(__name__)


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except OSError:
        return "unknown"


def _environment() -> dict[str, Any]:
    return {
        "git_sha": _git_sha(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "user": getpass.getuser(),
        "grpc_stubs": detections.GRPC_IMPORT_ERROR is None,
    }


async def _wait_ready(client: httpx.AsyncClient, timeout_s: float = 10.0) -> bool:
    """Probe /healthz; False means the run proceeds anyway and records zeros."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            resp = await client.get("/healthz")
            if resp.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.5)
    return False


async def run(config: RunConfig, endpoints: Endpoints, runs_base: Path) -> Path:
    """Execute a full stepped run; returns the run directory."""
    samples = Samples()
    run_dir = RunDir(runs_base, config.label or config.mode)
    started = time.time()

    env = _environment()
    run_dir.write_config(
        {
            "config": asdict(config),
            "endpoints": {k: v for k, v in asdict(endpoints).items() if k != "internal_token"},
            "internal_token_set": bool(endpoints.internal_token),
            "environment": env,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        }
    )

    registry = sampler.make_client(endpoints.registry_url, endpoints.internal_token)
    correlation = (
        sampler.make_client(endpoints.correlation_url, endpoints.internal_token)
        if endpoints.correlation_url
        else None
    )
    media = (
        MediaFarm(publish_base=endpoints.mediamtx_publish_url) if config.mode == "live" else None
    )
    seeded: list[farm.FarmCamera] = []
    watchlist = detections.load_watchlist_plates(config.watchlist_dir)
    pending_hits: dict[str, float] = {}
    phase_results: list[dict[str, Any]] = []

    registry_ok = await _wait_ready(registry)
    if not registry_ok:
        log.error(
            "registry not reachable at %s — run will record failures, not throughput",
            endpoints.registry_url,
        )
        samples.add("preflight.registry_unreachable", 1.0)

    try:
        if media is not None and config.streams_live > 0:
            media.start_mediamtx()
            media.publish_streams(config.streams_live)
            samples.add("live.streams_published", float(config.streams_live))

        prev = 0
        for step_n in config.cameras:
            phase = f"n={step_n}"
            new_count = step_n - prev
            if new_count > 0:
                rtsp_template = endpoints.camera_rtsp_template if config.mode == "live" else None
                batch = await farm.seed_cameras(
                    registry,
                    new_count,
                    prefix=f"lt-{int(started)}",
                    samples=samples,
                    phase=phase,
                    rtsp_template=rtsp_template,
                )
                seeded.extend(batch)
            prev = step_n

            if config.mode == "live":
                try:
                    result = await farm.reconcile_streams(registry)
                    samples.add("live.reconcile_ok", 1.0, phase=phase)
                    log.info("mediamtx reconcile: %s", result)
                except httpx.HTTPError as exc:
                    samples.add("live.reconcile_ok", 0.0, phase=phase)
                    log.warning("mediamtx reconcile failed: %s", exc)

            stop = asyncio.Event()
            tasks = [
                asyncio.create_task(
                    sampler.rest_sampler(
                        registry,
                        correlation,
                        watchlist,
                        config.sample_interval_s,
                        samples,
                        phase,
                        stop,
                    )
                ),
                asyncio.create_task(sampler.resource_sampler(15.0, samples, phase, stop)),
            ]
            # simulate mode: the farm emits heartbeats. live mode: real workers
            # do — emitting them here too would pollute the measurement.
            if config.mode == "simulate":
                tasks.append(
                    asyncio.create_task(
                        farm.heartbeat_emitters(
                            registry,
                            seeded,
                            config.heartbeat_interval_s,
                            samples,
                            phase,
                            stop,
                        )
                    )
                )

            grpc_ok = detections.GRPC_IMPORT_ERROR is None
            if grpc_ok:
                tasks.append(
                    asyncio.create_task(
                        detections.alert_latency_probe(
                            endpoints.match_http,
                            endpoints.internal_token,
                            pending_hits,
                            samples,
                            phase,
                            config.alert_poll_s,
                            stop,
                        )
                    )
                )
                detect_task = asyncio.create_task(
                    detections.stream_detections(
                        endpoints.match_grpc,
                        seeded,
                        watchlist,
                        config.detections_per_camera_s,
                        config.watchlist_hit_rate,
                        config.duration_s,
                        config.grpc_streams,
                        endpoints.internal_token,
                        samples,
                        phase,
                        pending_hits,
                    )
                )
            else:
                samples.add("grpc.unavailable", 1.0, phase=phase)
                detect_task = None

            t0 = time.time()
            # The phase runs `duration_s` regardless of the detection leg's
            # fate — a dead match engine must produce stream_error samples for
            # the full window, not truncate the phase's heartbeat/read load.
            if detect_task is not None:
                ack = (await asyncio.gather(detect_task, asyncio.sleep(config.duration_s)))[0]
            else:
                await asyncio.sleep(config.duration_s)
                ack = {"accepted": 0, "rejected": 0, "streams_failed": 0}
            elapsed = time.time() - t0

            # Let the alert poller drain for a grace window so late alerts get
            # matched back before the step closes.
            drain = min(config.alert_poll_s * 4, 10.0)
            await asyncio.sleep(drain if pending_hits else 0)
            stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)

            sent = samples.count("detection.sent", phase)
            phase_results.append(
                {
                    "phase": phase,
                    "cameras": step_n,
                    "duration_s": round(elapsed, 1),
                    "detections_sent": sent,
                    "detections_accepted": ack["accepted"],
                    "detections_rejected": ack["rejected"],
                    "streams_failed": ack["streams_failed"],
                    "heartbeats": samples.count("heartbeat.post_ms", phase),
                    "unmatched_hits": len(pending_hits),
                }
            )
    finally:
        if media is not None:
            media.stop()
        if config.cleanup and seeded:
            removed = await farm.decommission(registry, seeded)
            log.info("decommissioned %d/%d seeded cameras", removed, len(seeded))
        await registry.aclose()
        if correlation is not None:
            await correlation.aclose()

    finished = time.time()
    run_dir.write_samples(samples)
    summary = _render_summary(config, env, phase_results, samples, started, finished)
    run_dir.write_summary(summary)
    return run_dir.path


def _render_summary(
    config: RunConfig,
    env: dict[str, Any],
    phases: list[dict[str, Any]],
    samples: Samples,
    started: float,
    finished: float,
) -> str:
    lines = [
        f"# Load-test run — {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(started))}",
        "",
        f"- mode: `{config.mode}`",
        f"- steps: {config.cameras}",
        f"- duration/step: {config.duration_s}s",
        f"- heartbeat interval: {config.heartbeat_interval_s}s",
        f"- detection rate: {config.detections_per_camera_s}/camera/s "
        f"({config.watchlist_hit_rate:.1%} watchlist hits)",
        f"- alert poll interval (e2e latency floor): {config.alert_poll_s}s",
        f"- git: `{env['git_sha']}` · python {env['python']} · {env['platform']}",
        f"- gRPC stubs available: {env['grpc_stubs']}",
        "",
        "## Phase results",
        "",
        "| phase | cameras | detections sent | accepted | rejected | heartbeats | unmatched hits |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in phases:
        lines.append(
            f"| {p['phase']} | {p['cameras']} | {p['detections_sent']} | "
            f"{p['detections_accepted']} | {p['detections_rejected']} | "
            f"{p['heartbeats']} | {p['unmatched_hits']} |"
        )

    # Latency table: only timing metrics. Count/gauge metrics (detection.sent,
    # resource.*, live.*, *.unavailable) live in the phase table or the raw
    # samples — rendering a count beside a p95 would read as a latency.
    metrics = sorted(
        {r["metric"] for r in samples.rows}
        - {
            "detection.sent",
            "resource.sample",
            "resource.unavailable",
            "live.streams_published",
            "live.reconcile_ok",
            "grpc.unavailable",
            "grpc.stream_error",
            "preflight.registry_unreachable",
        }
    )
    lines += [
        "",
        "## Latency / rate samples (ms unless noted)",
        "",
        "| metric | phase | n | p50 | p95 | p99 | mean | max |",
        "|---|---|---|---|---|---|---|---|",
    ]
    phases_seen = sorted({r.get("phase", "") for r in samples.rows})
    for metric in metrics:
        for phase in phases_seen:
            stats = describe(samples.by_metric(metric, phase))
            if stats["n"] == 0:
                continue
            lines.append(
                f"| {metric} | {phase or '-'} | {stats['n']} | {stats.get('p50', '-')} | "
                f"{stats.get('p95', '-')} | {stats.get('p99', '-')} | "
                f"{stats.get('mean', '-')} | {stats.get('max', '-')} |"
            )

    res = [r for r in samples.rows if r["metric"] == "resource.sample"]
    if res:
        lines += [
            "",
            "## Resource samples",
            "",
            "| source | name | cpu | mem |",
            "|---|---|---|---|",
        ]
        for r in res[:200]:
            name = r.get("name") or r.get("pod", "?")
            lines.append(
                f"| {r.get('source', '?')} | {name} | {r.get('cpu', '-')} | {r.get('mem', '-')} |"
            )
    else:
        lines += [
            "",
            "## Resource samples",
            "",
            "_None captured — docker/kubectl unavailable or no prahari-* containers._",
        ]

    lines += [
        "",
        "## Honesty notes",
        "",
        "- `simulate` mode measures registry/match-engine/correlation throughput "
        "and latency. It does NOT measure video decode or inference — no pixels move.",
        "- `e2e.alert_latency_ms` is floored at the alert poll interval; it measures "
        '"alert visible via /api/v1/alerts", not matcher scoring time.',
        "- `live` mode measures real streams only when inference workers are "
        "deployed and pointed at this MediaMTX; check `live.streams_published` and "
        "the reconcile result before citing numbers from it.",
        "- `detection.sent` counts messages yielded to gRPC, not acks; compare "
        "against `accepted`/`rejected` for the engine's own verdict.",
    ]
    return "\n".join(lines) + "\n"


def run_sync(config: RunConfig, endpoints: Endpoints, runs_base: Path) -> Path:
    return asyncio.run(run(config, endpoints, runs_base))


def config_as_json(config: RunConfig) -> str:
    return json.dumps(asdict(config), indent=2)
