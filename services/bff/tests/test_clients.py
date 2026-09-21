"""The three upstream HTTP clients and the asyncpg pool factory.

Each client is a thin wrapper over `httpx.AsyncClient` carrying one
`X-Internal-Token` header — the tests drive them over `httpx.MockTransport`
(the same fake-IdP pattern test_oidc.py uses) so both the default-constructed
client path and the header/plate-quoting behaviour are exercised for real.
"""

from __future__ import annotations

import httpx
import pytest

import prahari_bff.db as db_module
from prahari_bff.config import BFFSettings
from prahari_bff.correlation_client import CorrelationClient
from prahari_bff.match_engine_client import MatchEngineClient
from prahari_bff.registry_client import RegistryClient

SETTINGS = BFFSettings(
    registry_base_url="http://registry",
    registry_internal_token="reg-secret",
    registry_timeout_s=5.0,
    match_engine_base_url="http://match",
    match_engine_timeout_s=5.0,
    internal_token="shared-secret",
    correlation_base_url="http://correlation",
    correlation_timeout_s=10.0,
)


class FakeUpstream:
    """Records requests; answers whatever the test scripted."""

    def __init__(self, status_code: int = 200, body=None) -> None:
        self.requests: list[httpx.Request] = []
        self._response = (status_code, body if body is not None else {})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status_code, body = self._response
        return httpx.Response(status_code, json=body)


def _client_for(upstream: FakeUpstream, base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler), base_url=base_url)


# --- RegistryClient -----------------------------------------------------------


async def test_registry_client_proxies_all_verbs():
    upstream = FakeUpstream(200, {"ok": True})
    client = RegistryClient(SETTINGS, client=_client_for(upstream, "http://registry"))

    await client.get("/api/v1/cameras", {"scope": "gj"})
    await client.post("/api/v1/cameras", {"external_id": "cam-1"})
    await client.patch("/api/v1/cameras/cam-1", {"site_name": "x"})
    await client.delete("/api/v1/cameras/cam-1")
    await client.aclose()

    methods = [r.method for r in upstream.requests]
    assert methods == ["GET", "POST", "PATCH", "DELETE"]
    assert upstream.requests[0].url.params["scope"] == "gj"
    assert upstream.requests[1].read() == b'{"external_id":"cam-1"}'


async def test_registry_client_default_client_carries_the_internal_token():
    client = RegistryClient(SETTINGS)
    assert isinstance(client._client, httpx.AsyncClient)
    assert str(client._client.base_url) == "http://registry"
    assert client._client.headers["x-internal-token"] == "reg-secret"
    await client.aclose()


async def test_registry_client_falls_back_to_the_service_identity_token():
    # One caller identity per service: with no registry-specific override
    # the BFF's own `internal_token` is what the registry sees — under
    # per-service credentials that is the `bff` identity (bff-token key).
    settings = BFFSettings(registry_internal_token="", internal_token="bff-self")
    client = RegistryClient(settings)
    assert client._client.headers["x-internal-token"] == "bff-self"
    await client.aclose()


# --- MatchEngineClient ----------------------------------------------------------


async def test_match_engine_client_proxies_get_and_post():
    upstream = FakeUpstream(200, {"entries": 3})
    client = MatchEngineClient(SETTINGS, client=_client_for(upstream, "http://match"))

    response = await client.get("/api/v1/watchlist/summary")
    await client.post("/api/v1/watchlist/reload", {"why": "test"})
    await client.aclose()

    assert response.status_code == 200
    assert upstream.requests[1].method == "POST"


async def test_match_engine_client_default_client_carries_the_shared_token():
    client = MatchEngineClient(SETTINGS)
    assert str(client._client.base_url) == "http://match"
    assert client._client.headers["x-internal-token"] == "shared-secret"
    await client.aclose()


# --- CorrelationClient ------------------------------------------------------------


async def test_correlation_get_route_quotes_the_plate():
    upstream = FakeUpstream(200, {"plate": "GJ01AB1234", "hops": []})
    client = CorrelationClient(SETTINGS, client=_client_for(upstream, "http://correlation"))

    route = await client.get_route("../admin?drop=1")

    assert route["plate"] == "GJ01AB1234"
    # Caller-supplied path material is fully quoted — no traversal, no query.
    # `raw_path` keeps the wire form; `.path` percent-decodes it.
    assert upstream.requests[0].url.raw_path == b"/api/v1/routes/..%2Fadmin%3Fdrop%3D1"


async def test_correlation_get_route_raises_on_upstream_error():
    upstream = FakeUpstream(404, {"detail": "no route"})
    client = CorrelationClient(SETTINGS, client=_client_for(upstream, "http://correlation"))
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_route("GJ00XX0000")
    await client.aclose()


async def test_correlation_client_sends_the_shared_token_when_configured():
    upstream = FakeUpstream(200, {})
    client = CorrelationClient(SETTINGS, client=_client_for(upstream, "http://correlation"))
    # The injected client carries no headers — the header-bearing default
    # client is what production builds; assert that separately.
    client_default = CorrelationClient(SETTINGS)
    assert client_default._client.headers["x-internal-token"] == "shared-secret"
    await client.aclose()
    await client_default.aclose()


async def test_correlation_client_omits_the_token_header_when_unset():
    settings = BFFSettings(internal_token="")
    client = CorrelationClient(settings)
    assert "x-internal-token" not in client._client.headers
    await client.aclose()


# --- create_pool -----------------------------------------------------------------


async def test_create_pool_passes_timeouts_and_sizes(monkeypatch):
    captured = {}

    async def fake_create_pool(dsn, **kwargs):
        captured["dsn"] = dsn
        captured.update(kwargs)
        return "pool"

    monkeypatch.setattr(db_module.asyncpg, "create_pool", fake_create_pool)
    settings = BFFSettings(database_url="postgresql://x/y", db_pool_min=1, db_pool_max=4)
    pool = await db_module.create_pool(settings)
    assert pool == "pool"
    assert captured["dsn"] == "postgresql://x/y"
    assert captured["min_size"] == 1
    assert captured["max_size"] == 4
    assert captured["command_timeout"] == 30.0
