"""`CameraScopeResolver`: camera_id -> org_path, root-scoped so the caller's
own scope never hides a camera from this internal lookup, cached on a TTL so
the SSE relay is not one registry round trip per alert.
"""

from __future__ import annotations

import httpx

from prahari_bff.scope_resolver import CameraScopeResolver


class FakePool:
    def __init__(self, paths: dict[str, str]) -> None:
        self._paths = paths

    async def fetchval(self, query: str, org_id: str) -> str | None:
        return self._paths.get(org_id)


class FakeRegistry:
    def __init__(self, cameras: dict[str, dict]) -> None:
        self._cameras = cameras
        self.calls: list[tuple[str, dict | None]] = []

    async def get(self, path: str, params: dict | None = None) -> httpx.Response:
        self.calls.append((path, params))
        camera_id = path.rsplit("/", 1)[-1]
        camera = self._cameras.get(camera_id)
        if camera is None:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json=camera)


def _resolver(registry: FakeRegistry, pool: FakePool) -> CameraScopeResolver:
    return CameraScopeResolver(registry, pool, root_scope="gj", ttl_s=300.0)


async def test_resolves_camera_to_its_org_path():
    registry = FakeRegistry({"cam-1": {"id": "cam-1", "org_id": "org-zone4"}})
    pool = FakePool({"org-zone4": "gj.ahmedabad_city.zone_4"})
    resolver = _resolver(registry, pool)

    assert await resolver.org_path_for_camera("cam-1") == "gj.ahmedabad_city.zone_4"


async def test_unknown_camera_resolves_to_none():
    registry = FakeRegistry({})
    resolver = _resolver(registry, FakePool({}))

    assert await resolver.org_path_for_camera("does-not-exist") is None


async def test_root_scope_is_used_for_the_internal_lookup_not_a_callers_scope():
    registry = FakeRegistry({"cam-1": {"id": "cam-1", "org_id": "org-zone4"}})
    pool = FakePool({"org-zone4": "gj.ahmedabad_city.zone_4"})
    resolver = _resolver(registry, pool)

    await resolver.org_path_for_camera("cam-1")

    assert registry.calls == [("/api/v1/cameras/cam-1", {"org_scope": "gj"})]


async def test_result_is_cached_within_ttl():
    registry = FakeRegistry({"cam-1": {"id": "cam-1", "org_id": "org-zone4"}})
    pool = FakePool({"org-zone4": "gj.ahmedabad_city.zone_4"})
    resolver = _resolver(registry, pool)

    await resolver.org_path_for_camera("cam-1")
    await resolver.org_path_for_camera("cam-1")

    assert len(registry.calls) == 1


async def test_a_miss_is_cached_too():
    registry = FakeRegistry({})
    resolver = _resolver(registry, FakePool({}))

    await resolver.org_path_for_camera("ghost")
    await resolver.org_path_for_camera("ghost")

    assert len(registry.calls) == 1


async def test_camera_row_without_an_org_id_resolves_to_none():
    """A 200 that carries no org_id is a miss, not a crash — the registry
    answered but there is no org to scope against."""
    registry = FakeRegistry({"cam-1": {"id": "cam-1"}})
    resolver = _resolver(registry, FakePool({}))
    assert await resolver.org_path_for_camera("cam-1") is None


async def test_registry_error_resolves_to_none_rather_than_raising():
    class FailingRegistry:
        async def get(self, path, params=None):
            raise httpx.ConnectError("connection refused")

    resolver = CameraScopeResolver(FailingRegistry(), FakePool({}), root_scope="gj")
    assert await resolver.org_path_for_camera("cam-1") is None
