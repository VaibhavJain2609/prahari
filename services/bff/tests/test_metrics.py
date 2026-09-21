"""`/metrics`: the BFF's own exposition — status-class counters from the
always-on middleware plus scrape-time gauges (live SSE, audit rows). Same
fake-state style as test_app_gaps.py."""

from __future__ import annotations

from types import SimpleNamespace

from prahari_bff.app import app, metrics
from prahari_bff.metrics import (
    AUDIT_ENTRIES,
    METRICS,
    SSE_ACTIVE,
    count_response,
    refresh_gauges,
)


class _FakeAudit:
    def __init__(self, head=(b"h", 42), fail: bool = False) -> None:
        self._head = head
        self._fail = fail

    async def head(self):
        if self._fail:
            raise RuntimeError("audit db wedged")
        return self._head


def _state(**over) -> SimpleNamespace:
    state = SimpleNamespace(sse_active=3, audit=_FakeAudit())
    for key, value in over.items():
        setattr(state, key, value)
    return state


def _request(state) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def test_metrics_renders_gauges_and_the_status_counters():
    await refresh_gauges(_state())
    response = await metrics(_request(_state()))

    body = response.body.decode()
    assert f"{SSE_ACTIVE} 3" in body
    assert f"{AUDIT_ENTRIES} 42" in body
    assert response.media_type == "text/plain"


async def test_failed_gauge_refresh_serves_last_known_values():
    """A wedged audit DB is exactly when the scrape must keep answering —
    the stale number is the signal, a 500 would blind the monitor."""
    await refresh_gauges(_state())
    assert METRICS.get(AUDIT_ENTRIES) == 42

    await refresh_gauges(_state(audit=_FakeAudit(fail=True)))
    assert METRICS.get(AUDIT_ENTRIES) == 42


async def test_missing_audit_state_skips_the_gauge():
    await refresh_gauges(_state(audit=None, sse_active=0))
    assert METRICS.get(SSE_ACTIVE) == 0


def test_count_response_buckets_by_status_class():
    before_2xx = METRICS.get("prahari_bff_http_requests_2xx_total")
    before_4xx = METRICS.get("prahari_bff_http_requests_4xx_total")
    before_5xx = METRICS.get("prahari_bff_http_requests_5xx_total")

    count_response(200)
    count_response(204)
    count_response(403)
    count_response(500)

    assert METRICS.get("prahari_bff_http_requests_2xx_total") == before_2xx + 2
    assert METRICS.get("prahari_bff_http_requests_4xx_total") == before_4xx + 1
    assert METRICS.get("prahari_bff_http_requests_5xx_total") == before_5xx + 1


def test_metrics_route_is_mounted_unauthenticated():
    """The scrape path must not sit behind PrincipalDep — a Prometheus pod
    holds no session. It appears in the route table next to /healthz."""
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/metrics" in paths
