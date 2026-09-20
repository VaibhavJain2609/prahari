"""`live` mode: real streams through MediaMTX + ffmpeg.

The harness publishes synthetic video (`testsrc2`, plus a `drawtext` plate
banner when a usable font is found) into MediaMTX under `lt-src/<i>`, and the
farm registers each camera's `rtsp_url` pointing at those paths. The
registry's normal reconcile then creates the `cam-<id>` pull paths and
inference workers fan out from `fanout_rtsp_url` — exactly the production
topology, with the government gateway replaced by an ffmpeg publisher.

This mode measures the full pipeline: decode → detect → OCR → gRPC → match →
alert → correlation. It does NOT measure per-GPU capacity — that needs the
`profile=gpu` deployment and is the Day 4 measurement this harness exists to
produce, not to fake.

Honesty note the README repeats: synthetic video mostly contains no vehicles,
so the gRPC detection injector (still active in live mode unless
`--detections-per-camera-s 0`) is what exercises the metadata plane in a live
run unless the ffmpeg filter actually produces OCR-able plates.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

MEDIAMTX_IMAGE = "bluenviron/mediamtx:1.9.3"
MEDIAMTX_CONTAINER = "prahari-loadtest-mediamtx"


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


@dataclass
class MediaFarm:
    """Owns the local MediaMTX container and the ffmpeg publisher processes.

    Lifecycle is explicit — `start()` raises if prerequisites are missing,
    `stop()` always cleans up. Nothing here pretends to be the government
    gateway's control API; we only publish RTSP, which is the one direction
    the real feeds also allow a consumer to avoid (we *are* the source here,
    which is precisely what "synthetic" means).
    """

    publish_base: str = "rtsp://localhost:8554"
    api_port: int = 9997
    rtsp_port: int = 8554
    processes: list[subprocess.Popen] = field(default_factory=list)
    mediamtx_started_by_us: bool = False

    def start_mediamtx(self) -> None:
        """Start a local MediaMTX container if one isn't already answering."""
        if not shutil.which("docker"):
            raise RuntimeError("live mode needs docker for MediaMTX; not found on PATH")
        probe = _run(
            [
                "docker",
                "ps",
                "--filter",
                f"name={MEDIAMTX_CONTAINER}",
                "--format",
                "{{.Names}}",
            ]
        )
        if probe.returncode == 0 and MEDIAMTX_CONTAINER in probe.stdout:
            log.info("MediaMTX container %s already running; reusing", MEDIAMTX_CONTAINER)
            return
        res = _run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                MEDIAMTX_CONTAINER,
                "-p",
                f"{self.rtsp_port}:8554",
                "-p",
                f"{self.api_port}:9997",
                MEDIAMTX_IMAGE,
            ],
            timeout=120,
        )
        if res.returncode != 0:
            raise RuntimeError(f"docker run mediamtx failed: {res.stderr.strip()}")
        self.mediamtx_started_by_us = True
        time.sleep(1.0)  # give the control API a beat to bind

    def publish_streams(self, count: int, plate_text: str | None = None) -> int:
        """Spawn `count` ffmpeg publishers pushing `lt-src/<i>` paths.

        `testsrc2` produces moving synthetic content — real decode load, real
        bitrate, no sample clip needed. `plate_text` is drawn large across the
        frame so OCR has something to find; font discovery is best-effort and
        skipped silently on hosts without one.
        """
        if not shutil.which("ffmpeg"):
            raise RuntimeError("live mode needs ffmpeg on PATH")
        started = 0
        for i in range(count):
            url = f"{self.publish_base}/lt-src/{i:05d}"
            vf = "testsrc2=size=640x480:rate=10"
            if plate_text:
                vf += (
                    f",drawtext=text='{plate_text}':fontsize=72:fontcolor=white"
                    ":box=1:boxcolor=black:x=(w-text_w)/2:y=(h-text_h)/2"
                )
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                vf,
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-g",
                "30",
                "-f",
                "rtsp",
                "-rtsp_transport",
                "tcp",
                url,
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.processes.append(proc)
            started += 1
        return started

    def stop(self) -> None:
        for proc in self.processes:
            proc.terminate()
        for proc in self.processes:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.processes.clear()
        if self.mediamtx_started_by_us:
            _run(["docker", "rm", "-f", MEDIAMTX_CONTAINER])
            self.mediamtx_started_by_us = False
