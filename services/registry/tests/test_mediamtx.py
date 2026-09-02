"""MediaMTX fan-out reconciliation.

Exercised against a fake control API. What matters here is not the HTTP calls
but the two rules that keep the demo alive: we only ever delete paths we own,
and the restreamer being unreachable is never fatal.
"""

from __future__ import annotations

import httpx
import pytest

from prahari_registry.config import RegistrySettings
from prahari_registry.mediamtx import MediaMTXClient, fanout_endpoints, path_name
from prahari_registry.models import StreamEndpoints

SETTINGS = RegistrySettings(
    mediamtx_public_host="prahari-mediamtx",
    mediamtx_api_url="http://mediamtx:9997",
)


class FakeAPI:
    """Minimal MediaMTX control API."""

    def __init__(self, paths: dict[str, dict] | None = None, fail: bool = False) -> None:
        self.paths = paths or {}
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if self.fail:
            return httpx.Response(500, json={"error": "boom"})
        path = request.url.path
        if path == "/v3/config/paths/list":
            items = [{"name": n, **cfg} for n, cfg in self.paths.items()]
            return httpx.Response(200, json={"items": items, "pageCount": 1})
        if path.startswith("/v3/config/paths/add/"):
            self.paths[path.rsplit("/", 1)[1]] = {"source": "added"}
            return httpx.Response(200, json={})
        if path.startswith("/v3/config/paths/patch/"):
            return httpx.Response(200, json={})
        if path.startswith("/v3/config/paths/delete/"):
            self.paths.pop(path.rsplit("/", 1)[1], None)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})


def client_for(api: FakeAPI) -> MediaMTXClient:
    transport = httpx.MockTransport(api.handler)
    http = httpx.AsyncClient(transport=transport, base_url=SETTINGS.mediamtx_api_url)
    return MediaMTXClient(SETTINGS, client=http)


# --- naming and URLs ---------------------------------------------------------


def test_path_name_uses_the_internal_id():
    """Not the catalogue id. External ids rotate, and a path that renames itself
    mid-demo breaks every consumer holding its URL."""
    assert path_name("3f1c-uuid") == "cam-3f1c-uuid"


def test_fanout_urls_point_at_mediamtx_not_the_gateway():
    """Every client that connects to a source gets its own copy of the stream.
    Consumers must reach the restreamer, or N workers become N connections to a
    shared government feed."""
    upstream = StreamEndpoints(rtsp_url="rtsp://gateway.example:8554/stream/101")
    endpoints = fanout_endpoints(SETTINGS, "abc", upstream)

    assert endpoints.rtsp_url == "rtsp://gateway.example:8554/stream/101"
    assert endpoints.fanout_rtsp_url == "rtsp://prahari-mediamtx:8554/cam-abc"
    assert endpoints.fanout_hls_url == "http://prahari-mediamtx:8888/cam-abc/index.m3u8"
    assert endpoints.fanout_whep_url == "http://prahari-mediamtx:8889/cam-abc/whep"


# --- reconcile ---------------------------------------------------------------


async def test_reconcile_adds_missing_paths():
    api = FakeAPI()
    result = await client_for(api).reconcile({"cam-1": "rtsp://gw/stream/1"})
    assert result.added == 1
    assert "cam-1" in api.paths


async def test_reconcile_patches_a_changed_upstream():
    """Catalogue ids change. When the upstream URL moves, the path must follow
    it rather than keep pulling a stream that no longer exists."""
    api = FakeAPI({"cam-1": {"source": "rtsp://gw/stream/OLD"}})
    result = await client_for(api).reconcile({"cam-1": "rtsp://gw/stream/NEW"})
    assert (result.added, result.updated) == (0, 1)


async def test_reconcile_leaves_an_unchanged_path_alone():
    api = FakeAPI({"cam-1": {"source": "rtsp://gw/stream/1"}})
    result = await client_for(api).reconcile({"cam-1": "rtsp://gw/stream/1"})
    assert (result.added, result.updated, result.removed) == (0, 0, 0)


async def test_reconcile_removes_only_paths_we_own():
    """A path added by hand for debugging survives. Deleting everything we did
    not put there would make the restreamer unusable for anything else."""
    api = FakeAPI(
        {
            "cam-gone": {"source": "rtsp://gw/stream/gone"},
            "debug-clip": {"source": "file:///tmp/clip.mp4"},
        }
    )
    result = await client_for(api).reconcile({})

    assert result.removed == 1
    assert "debug-clip" in api.paths
    assert "cam-gone" not in api.paths


async def test_unreachable_mediamtx_is_not_fatal():
    """The registry is still correct when the restreamer is down; the fan-out is
    simply not configured yet, and the next pass fixes it. Failing the sync here
    would mean a restreamer restart also stops camera onboarding."""
    result = await client_for(FakeAPI(fail=True)).reconcile({"cam-1": "rtsp://gw/1"})
    assert result.skipped_reason is not None
    assert (result.added, result.removed) == (0, 0)


async def test_a_single_failing_path_does_not_abort_the_rest():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v3/config/paths/list":
            return httpx.Response(200, json={"items": []})
        if request.url.path.endswith("/cam-bad"):
            return httpx.Response(400, json={"error": "invalid source"})
        return httpx.Response(200, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mtx")
    result = await MediaMTXClient(SETTINGS, client=http).reconcile(
        {"cam-bad": "rtsp://gw/bad", "cam-ok": "rtsp://gw/ok"}
    )

    assert (result.added, result.failed) == (1, 1)


@pytest.mark.parametrize("disabled", [True])
async def test_reconcile_can_be_switched_off(disabled: bool):
    """`profile=local` may run without a restreamer at all; the switch must not
    require a code change."""
    settings = RegistrySettings(mediamtx_reconcile=not disabled)
    result = await MediaMTXClient(settings).reconcile({"cam-1": "rtsp://gw/1"})
    assert result.skipped_reason == "mediamtx_reconcile disabled"
