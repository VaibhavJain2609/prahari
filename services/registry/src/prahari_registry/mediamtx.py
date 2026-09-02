"""MediaMTX fan-out paths, driven by the registry.

Every client that connects to a source receives its own copy of the stream, so
N inference workers plus M browser previews would open N×M connections to a
shared government feed. MediaMTX pulls each camera **once** and fans it out;
everything downstream connects to MediaMTX, never to the gateway.

Paths are written here at runtime, which is why the chart's ConfigMap ships with
`paths:` deliberately empty. A hardcoded path passes locally and fails on demo
day when catalogue ids rotate — and MediaMTX reloads its file config on restart,
so reconciling on a timer is not belt-and-braces, it is the only thing that
brings the paths back after the pod is rescheduled.

Path names use our internal camera id, not the catalogue's. The external id is
an attribute and can change under us; a path that renames itself mid-demo breaks
every consumer holding its URL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .config import RegistrySettings
from .models import StreamEndpoints

log = logging.getLogger(__name__)


def path_name(camera_id: str) -> str:
    return f"cam-{camera_id}"


def fanout_endpoints(
    settings: RegistrySettings, camera_id: str, upstream: StreamEndpoints
) -> StreamEndpoints:
    """Upstream URLs plus the fan-out URLs consumers should actually use."""
    name = path_name(camera_id)
    host = settings.mediamtx_public_host
    return StreamEndpoints(
        rtsp_url=upstream.rtsp_url,
        hls_url=upstream.hls_url,
        whep_url=upstream.whep_url,
        fanout_rtsp_url=f"rtsp://{host}:{settings.mediamtx_rtsp_port}/{name}",
        fanout_hls_url=f"http://{host}:{settings.mediamtx_hls_port}/{name}/index.m3u8",
        fanout_whep_url=f"http://{host}:{settings.mediamtx_whep_port}/{name}/whep",
    )


@dataclass(frozen=True)
class ReconcileResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    failed: int = 0
    skipped_reason: str | None = None


class MediaMTXClient:
    """Thin client for the MediaMTX control API.

    Scope note: this configures OUR restreamer. It is not the government
    gateway's control API, which we never call — we consume that feed and
    nothing else.
    """

    def __init__(self, settings: RegistrySettings, client: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._client = client

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._s.mediamtx_api_url, timeout=10.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def list_paths(self) -> dict[str, dict]:
        """Configured paths, keyed by name. Configured, not active: an on-demand
        path with no consumer is correctly configured and correctly idle."""
        http = await self._http()
        resp = await http.get("/v3/config/paths/list", params={"itemsPerPage": 10_000})
        resp.raise_for_status()
        return {item["name"]: item for item in resp.json().get("items", [])}

    def _path_config(self, source_url: str) -> dict:
        return {
            "source": source_url,
            # On demand, so a connection to the government feed exists only
            # while something is actually watching. This is what keeps us inside
            # the "open only the cameras you are processing" rule even though
            # every camera has a configured path.
            "sourceOnDemand": True,
            "sourceOnDemandStartTimeout": "15s",
            "sourceOnDemandCloseAfter": "30s",
            # TCP. UDP drops silently under load and yields corrupt frames that
            # look like detections rather than like errors.
            "rtspTransport": "tcp",
        }

    async def reconcile(self, desired: dict[str, str]) -> ReconcileResult:
        """Make MediaMTX's paths match `desired` ({path_name: source_url}).

        Only paths we own (the `cam-` prefix) are removed, so a path added by
        hand for debugging survives a reconcile.
        """
        if not self._s.mediamtx_reconcile:
            return ReconcileResult(skipped_reason="mediamtx_reconcile disabled")

        http = await self._http()
        try:
            existing = await self.list_paths()
        except (httpx.HTTPError, KeyError) as exc:
            # MediaMTX being unreachable must not fail a catalogue sync. The
            # registry is still correct; the fan-out is simply not configured
            # yet, and the next reconcile will fix it.
            log.warning("mediamtx unreachable, skipping reconcile: %s", exc)
            return ReconcileResult(skipped_reason=str(exc))

        result = ReconcileResult()
        added = updated = removed = failed = 0

        for name, source_url in desired.items():
            config = self._path_config(source_url)
            current = existing.get(name)
            try:
                if current is None:
                    resp = await http.post(f"/v3/config/paths/add/{name}", json=config)
                    resp.raise_for_status()
                    added += 1
                elif current.get("source") != source_url:
                    resp = await http.patch(f"/v3/config/paths/patch/{name}", json=config)
                    resp.raise_for_status()
                    updated += 1
            except httpx.HTTPError as exc:
                log.warning("mediamtx path %s failed: %s", name, exc)
                failed += 1

        for name in existing:
            if name.startswith("cam-") and name not in desired:
                try:
                    resp = await http.delete(f"/v3/config/paths/delete/{name}")
                    resp.raise_for_status()
                    removed += 1
                except httpx.HTTPError as exc:
                    log.warning("mediamtx path %s not removed: %s", name, exc)
                    failed += 1

        result = ReconcileResult(added=added, updated=updated, removed=removed, failed=failed)
        log.info(
            "mediamtx reconcile: +%d ~%d -%d (%d failed, %d desired)",
            added,
            updated,
            removed,
            failed,
            len(desired),
        )
        return result
