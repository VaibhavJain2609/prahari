from __future__ import annotations

import httpx

from prahari_correlation.registry_client import RegistryClient


def _client_for(handler) -> RegistryClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="http://registry")
    return RegistryClient(base_url="http://registry", timeout_s=5.0, cache_ttl_s=300.0, client=http)


async def test_camera_location_parses_a_found_camera() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/cameras/CAM-1"
        return httpx.Response(
            200, json={"id": "CAM-1", "location": {"latitude": 23.0, "longitude": 72.5}}
        )

    location = await _client_for(handler).camera_location("CAM-1")
    assert location is not None
    assert (location.latitude, location.longitude) == (23.0, 72.5)


async def test_camera_location_is_none_when_camera_has_no_location() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "CAM-1", "location": None})

    location = await _client_for(handler).camera_location("CAM-1")
    assert location is None


async def test_camera_location_is_none_on_404_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    location = await _client_for(handler).camera_location("CAM-MISSING")
    assert location is None


async def test_camera_location_is_cached_within_the_ttl() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"id": "CAM-1", "location": {"latitude": 1.0, "longitude": 2.0}}
        )

    client = _client_for(handler)
    await client.camera_location("CAM-1")
    await client.camera_location("CAM-1")

    assert calls == 1


async def test_a_cached_miss_does_not_re_fetch_either() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"id": "CAM-1", "location": None})

    client = _client_for(handler)
    await client.camera_location("CAM-1")
    await client.camera_location("CAM-1")

    assert calls == 1


async def test_dark_zones_parses_the_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/gaps/dark-zones"
        return httpx.Response(
            200,
            json=[
                {"camera_id": "CAM-1", "location": {"latitude": 1.0, "longitude": 2.0}},
                {"camera_id": "CAM-2", "location": None},
            ],
        )

    zones = await _client_for(handler).dark_zones()
    assert [z.camera_id for z in zones] == ["CAM-1", "CAM-2"]
    assert zones[0].location is not None
    assert zones[1].location is None


async def test_dark_zones_returns_empty_list_on_failure_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    zones = await _client_for(handler).dark_zones()
    assert zones == []
