"""`python -m prahari_loadtest` — the harness CLI.

Subcommands:

  run            a stepped load run against a live platform (or fakes)
  selftest       bring up the in-process fakes and run a tiny run against them
  fake-registry  serve the stub registry standalone, for manual poking

Every flag has an env-var twin (`PRAHARI_*`) so a run's full config can live
in a shell snippet pasted into docs/SCALE-80K.md.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .config import Endpoints, RunConfig
from .driver import run_sync
from .fakes import FakeRegistry, make_fake_ingest_server

log = logging.getLogger("prahari_loadtest")

DEFAULT_RUNS_DIR = Path(__file__).resolve().parents[2] / "runs"


def _parse_steps(text: str) -> list[int]:
    steps = [int(x) for x in text.split(",") if x.strip()]
    if not steps or steps != sorted(steps) or steps[0] < 1:
        raise argparse.ArgumentTypeError(
            "--cameras must be a sorted list of positive ints, e.g. 5,50,500"
        )
    return steps


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--registry-url",
        default=None,
        help="registry base URL (env: PRAHARI_REGISTRY_URL, default http://localhost:8000)",
    )
    p.add_argument(
        "--match-grpc",
        default=None,
        help="match-engine gRPC host:port (env: PRAHARI_MATCH_GRPC, default localhost:9001)",
    )
    p.add_argument(
        "--match-http",
        default=None,
        help="match-engine HTTP base URL (env: PRAHARI_MATCH_HTTP, default http://localhost:8001)",
    )
    p.add_argument(
        "--correlation-url",
        default=None,
        help="correlation base URL; empty disables route sampling "
        "(env: PRAHARI_CORRELATION_URL, default http://localhost:8002)",
    )
    p.add_argument(
        "--internal-token",
        default=None,
        help="X-Internal-Token for internal APIs / gRPC metadata (env: PRAHARI_INTERNAL_TOKEN)",
    )
    p.add_argument(
        "--runs-dir",
        default=str(DEFAULT_RUNS_DIR),
        help="where runs/<timestamp>/ lands (default: infra/loadtest/runs)",
    )
    p.add_argument("--label", default="", help="suffix on the run directory name")


def _endpoints_from(args: argparse.Namespace) -> Endpoints:
    ep = Endpoints.from_env()
    if args.registry_url is not None:
        ep.registry_url = args.registry_url
    if args.match_grpc is not None:
        ep.match_grpc = args.match_grpc
    if args.match_http is not None:
        ep.match_http = args.match_http
    if args.correlation_url is not None:
        ep.correlation_url = args.correlation_url
    if args.internal_token is not None:
        ep.internal_token = args.internal_token
    return ep


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prahari_loadtest",
        description="PRAHARI load-test harness — see infra/loadtest/README.md",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="a stepped load run against a live platform")
    _add_common(run_p)
    run_p.add_argument("--mode", choices=["simulate", "live"], default="simulate")
    run_p.add_argument(
        "--cameras", type=_parse_steps, default="5", help="comma-separated step list, e.g. 5,50,500"
    )
    run_p.add_argument("--duration-s", type=float, default=60.0, help="seconds per step")
    run_p.add_argument("--heartbeat-interval-s", type=float, default=10.0)
    run_p.add_argument("--detections-per-camera-s", type=float, default=0.2)
    run_p.add_argument("--watchlist-hit-rate", type=float, default=0.01)
    run_p.add_argument(
        "--watchlist-dir",
        default="data/watchlist",
        help="where to draw hit plates from (repo-relative)",
    )
    run_p.add_argument("--grpc-streams", type=int, default=4)
    run_p.add_argument("--alert-poll-s", type=float, default=0.5)
    run_p.add_argument("--sample-interval-s", type=float, default=15.0)
    run_p.add_argument(
        "--streams-live", type=int, default=0, help="live mode: ffmpeg→MediaMTX streams to publish"
    )
    run_p.add_argument(
        "--mediamtx-publish-url",
        default=None,
        help="live mode: where ffmpeg pushes (env: PRAHARI_MEDIAMTX_PUBLISH_URL)",
    )
    run_p.add_argument(
        "--camera-rtsp-template",
        default=None,
        help="live mode: rtsp_url template stored per camera, {i:05d} formatted "
        "(env: PRAHARI_CAMERA_RTSP_TEMPLATE)",
    )
    run_p.add_argument(
        "--no-cleanup",
        action="store_true",
        help="leave seeded cameras registered (default: decommission at end)",
    )

    st = sub.add_parser("selftest", help="run a tiny load run against in-process fakes")
    _add_common(st)
    st.add_argument("--duration-s", type=float, default=6.0)
    st.add_argument("--cameras", type=_parse_steps, default="5")

    fr = sub.add_parser("fake-registry", help="serve the stub registry standalone")
    fr.add_argument("--port", type=int, default=18000)
    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    endpoints = _endpoints_from(args)
    if args.mediamtx_publish_url:
        endpoints.mediamtx_publish_url = args.mediamtx_publish_url
    if args.camera_rtsp_template:
        endpoints.camera_rtsp_template = args.camera_rtsp_template
    config = RunConfig(
        mode=args.mode,
        cameras=args.cameras if isinstance(args.cameras, list) else [args.cameras],
        duration_s=args.duration_s,
        heartbeat_interval_s=args.heartbeat_interval_s,
        detections_per_camera_s=args.detections_per_camera_s,
        watchlist_hit_rate=args.watchlist_hit_rate,
        watchlist_dir=args.watchlist_dir,
        grpc_streams=args.grpc_streams,
        alert_poll_s=args.alert_poll_s,
        sample_interval_s=args.sample_interval_s,
        streams_live=args.streams_live,
        cleanup=not args.no_cleanup,
        label=args.label,
    )
    path = run_sync(config, endpoints, Path(args.runs_dir))
    print(f"run captured: {path}")
    print(f"  summary: {path / 'summary.md'}")
    return 0


def _cmd_selftest(args: argparse.Namespace) -> int:
    """Verify harness plumbing end-to-end against the fakes.

    Checks: cameras registered and listed, heartbeats accepted, gRPC
    detections acked (or honestly skipped when stubs are absent), samples
    written, cleanup done. Exit 0 on pass.
    """
    endpoints = _endpoints_from(args)
    checks: list[tuple[str, bool, str]] = []

    with FakeRegistry() as registry:
        endpoints.registry_url = registry.url
        endpoints.correlation_url = ""  # no fake correlation — route sampling off
        endpoints.match_http = registry.url  # probe will 404 gracefully; noted below

        grpc_ok = False
        server = None
        try:
            server, port, counters = make_fake_ingest_server()
            endpoints.match_grpc = f"127.0.0.1:{port}"
            grpc_ok = True
        except RuntimeError as exc:
            log.warning("gRPC leg skipped: %s", exc)
            counters = {"accepted": 0}

        cameras = args.cameras if isinstance(args.cameras, list) else [args.cameras]
        config = RunConfig(
            mode="simulate",
            cameras=cameras,
            duration_s=args.duration_s,
            heartbeat_interval_s=1.0,  # fast ticks so a 6 s selftest sees several
            detections_per_camera_s=1.0,
            watchlist_hit_rate=0.0,  # fake ingest produces no alerts; assert none expected
            grpc_streams=2,
            alert_poll_s=0.5,
            sample_interval_s=2.0,
            cleanup=True,
            label="selftest",
        )
        # watchlist_hit_rate is 0, so the default watchlist_dir is never read.

        path = run_sync(config, endpoints, Path(args.runs_dir))
        if server:
            server.stop(2)

        checks.append(
            (
                "cameras seeded into fake registry",
                len(registry.state.cameras) == cameras[-1],
                f"{len(registry.state.cameras)} vs {cameras[-1]}",
            ),
        )
        checks.append(
            (
                "heartbeats accepted by fake registry",
                len(registry.state.heartbeats) > 0,
                f"{len(registry.state.heartbeats)} heartbeats",
            ),
        )
        if grpc_ok:
            checks.append(
                (
                    "gRPC detections acked by fake ingest",
                    counters["accepted"] > 0,
                    f"{counters['accepted']} accepted",
                ),
            )
        else:
            checks.append(("gRPC detections (skipped — no stubs)", True, "skipped"))
        checks.append(
            (
                "samples.jsonl written",
                (path / "samples.jsonl").exists() and (path / "samples.jsonl").stat().st_size > 0,
                str(path),
            ),
        )
        checks.append(
            ("summary.md written", (path / "summary.md").exists(), str(path)),
        )
        checks.append(
            (
                "cleanup decommissioned cameras",
                all(
                    c.get("lifecycle") == "decommissioned" for c in registry.state.cameras.values()
                ),
                f"{len(registry.state.cameras)} rows all decommissioned",
            ),
        )

    print("\nselftest results:")
    ok = True
    for name, passed, detail in checks:
        mark = "PASS" if passed else "FAIL"
        ok = ok and passed
        print(f"  [{mark}] {name} — {detail}")
    print(f"\nrun dir: {path}")
    return 0 if ok else 1


def _cmd_fake_registry(args: argparse.Namespace) -> int:
    with FakeRegistry(port=args.port) as registry:
        print(f"fake registry on {registry.url} — ctrl-C to stop")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "selftest":
        return _cmd_selftest(args)
    if args.command == "fake-registry":
        return _cmd_fake_registry(args)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
