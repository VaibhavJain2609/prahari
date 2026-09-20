"""The worker-sharding endpoints: register, lease-driven membership, and the
modulo-slice assignment.

`FakeWorkerRepo` mirrors `WorkerRepository`'s semantics in memory — the same
`shard_membership` math and the same `(rn - 1) % count == index` slicing the
SQL implements — so the endpoint contract and the membership/res hard
behaviour are tested without a database. The SQL itself is thin and follows
the repo's existing convention: no test in this service opens Postgres.

A wall-clock `now` the fake controls stands in for `last_seen`, which is what
lets "worker dies → others absorb" be tested without sleeping.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prahari_registry.app import app
from prahari_registry.config import RegistrySettings
from prahari_registry.models import Camera, WorkerAssignment, WorkerRegistration
from prahari_registry.repository import shard_membership

TOKEN = "test-internal-token"
HEADERS = {"x-internal-token": TOKEN}

LEASE_S = 60


class FakeWorkerRepo:
    """In-memory `workers` table with a controllable clock.

    `seen` is `worker_id -> last_seen`; `clock` is now. A worker counts toward
    `shard_count` while `last_seen >= now - 2*lease` — the same predicate the
    SQL applies — and `die()`/`elapse()` simulate a pod that stops refreshing.
    """

    def __init__(self, lease_s: int = LEASE_S) -> None:
        self.lease_s = lease_s
        self.clock = 1_000.0
        self.seen: dict[str, float] = {}
        self.cameras: list[Camera] = []
        self.register_calls: list[str] = []
        self.scopes: list[str] = []

    def _alive(self) -> list[str]:
        horizon = self.clock - self._ALIVE_LEASES * self.lease_s
        return sorted(w for w, t in self.seen.items() if t >= horizon)

    _ALIVE_LEASES = 2

    async def register(self, worker_id: str) -> WorkerRegistration:
        self.register_calls.append(worker_id)
        self.seen[worker_id] = self.clock
        index, count = shard_membership(worker_id, self._alive())
        return WorkerRegistration(
            worker_id=worker_id,
            shard_index=index,
            shard_count=count,
            lease_s=self.lease_s,
        )

    async def assignment(self, worker_id: str, *, scope: str) -> WorkerAssignment:
        self.scopes.append(scope)
        registration = await self.register(worker_id)
        ordered = sorted(self.cameras, key=lambda c: c.id)
        # Mirrors `(rn - 1) % shard_count = shard_index` over ORDER BY id.
        shard = [
            c
            for i, c in enumerate(ordered)
            if i % registration.shard_count == registration.shard_index
        ]
        return WorkerAssignment(**registration.model_dump(), cameras=shard)

    def elapse(self, seconds: float) -> None:
        self.clock += seconds


@pytest.fixture
def workers() -> FakeWorkerRepo:
    repo = FakeWorkerRepo()
    app.state.worker_repo = repo
    app.state.settings = RegistrySettings(internal_token=TOKEN, sync_enabled=False)
    return repo


@pytest.fixture
def client(workers) -> TestClient:
    return TestClient(app)


def _cameras(n: int) -> list[Camera]:
    return [
        Camera(id=f"00000000-0000-0000-0000-{i:012d}", source="gateway", external_id=f"cam-{i}")
        for i in range(n)
    ]


# --- the internal token gate applies here like everywhere else -----------------


def test_worker_endpoints_require_the_internal_token(client):
    assert client.post("/api/v1/workers/register", json={"worker_id": "w1"}).status_code == 401
    assert client.get("/api/v1/assignments", params={"worker_id": "w1"}).status_code == 401


# --- register -----------------------------------------------------------------


def test_first_worker_registers_as_shard_zero_of_one(workers, client):
    resp = client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "worker_id": "w1",
        "shard_index": 0,
        "shard_count": 1,
        "lease_s": LEASE_S,
    }


def test_register_is_idempotent_and_is_the_keepalive(workers, client):
    """Re-registering refreshes last_seen rather than erroring — the lease
    renewal IS the endpoint, there is no separate heartbeat."""
    for _ in range(3):
        resp = client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w1"})
        assert resp.status_code == 200
    assert workers.register_calls == ["w1", "w1", "w1"]
    assert resp.json()["shard_count"] == 1


def test_register_rejects_a_missing_worker_id(client):
    assert client.post("/api/v1/workers/register", headers=HEADERS, json={}).status_code == 422
    assert (
        client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": ""}).status_code
        == 422
    )


# --- membership math -----------------------------------------------------------


def test_two_workers_split_the_pool(workers, client):
    r1 = client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w1"})
    assert r1.json()["shard_count"] == 1  # alone when it registered

    client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w2"})

    # Coordinates order on worker_id, not registration order.
    r1 = client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w1"})
    r2 = client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w2"})
    assert (r1.json()["shard_index"], r1.json()["shard_count"]) == (0, 2)
    assert (r2.json()["shard_index"], r2.json()["shard_count"]) == (1, 2)


def test_a_third_worker_joining_reshards_everyone(workers, client):
    """KEDA scale-out: shard_count is recomputed from the alive set on every
    register, so a new pod reshards the pool on its first heartbeat."""
    for wid in ("w1", "w2"):
        client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": wid})
    client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w3"})

    coords = {}
    for wid in ("w1", "w2", "w3"):
        resp = client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": wid})
        coords[wid] = (resp.json()["shard_index"], resp.json()["shard_count"])
    assert coords == {"w1": (0, 3), "w2": (1, 3), "w3": (2, 3)}


def test_a_dead_worker_drops_out_of_shard_count(workers, client):
    """KEDA scale-in / a crashed pod: once `last_seen` ages past 2x the lease
    the dead worker stops counting, and the survivors absorb its slice."""
    for wid in ("w1", "w2", "w3"):
        client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": wid})

    # w3 dies at t=1000. w1 and w2 keep refreshing — at t=1150 their last_seen
    # (1050) is inside the 2x-lease window (horizon 1030) while w3's (1000)
    # is not: shard_count shrinks to the survivors.
    workers.elapse(50)
    for wid in ("w1", "w2"):
        client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": wid})
    workers.elapse(100)

    resp = client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w1"})
    assert (resp.json()["shard_index"], resp.json()["shard_count"]) == (0, 2)
    resp = client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w2"})
    assert (resp.json()["shard_index"], resp.json()["shard_count"]) == (1, 2)


def test_shard_membership_rejects_an_unregistered_worker():
    """index -1 must never be silently assigned: a caller asking for the
    coordinates of a worker that is not in the alive set gets a loud error."""
    with pytest.raises(ValueError):
        shard_membership("ghost", ["w1", "w2"])


# --- assignments ---------------------------------------------------------------


def test_assignments_returns_only_the_workers_slice(workers, client):
    """Shard 0 of 2 gets every other camera in the stable ordering — the
    complement of shard 1, with no overlap."""
    workers.cameras = _cameras(5)
    for wid in ("w1", "w2"):
        client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": wid})

    resp = client.get("/api/v1/assignments", headers=HEADERS, params={"worker_id": "w1"})
    assert resp.status_code == 200
    body = resp.json()
    assert (body["shard_index"], body["shard_count"]) == (0, 2)
    ids = [c["id"] for c in body["cameras"]]
    ordered = [c.id for c in sorted(workers.cameras, key=lambda c: c.id)]
    assert ids == ordered[0::2]


def test_assignments_partitions_cover_the_estate_without_overlap(workers, client):
    """The property the whole feature exists for: every active camera lands on
    exactly one worker's slice, whatever the pool size."""
    workers.cameras = _cameras(7)
    # The whole pool registers first — fetching mid-join is legitimate (each
    # worker gets the count at call time) but would compare slices computed
    # against different shard_counts.
    for wid in ("w1", "w2", "w3"):
        client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": wid})
    slices = []
    for wid in ("w1", "w2", "w3"):
        resp = client.get("/api/v1/assignments", headers=HEADERS, params={"worker_id": wid})
        slices.append([c["id"] for c in resp.json()["cameras"]])

    flat = [i for s in slices for i in s]
    assert sorted(flat) == sorted(c.id for c in workers.cameras)
    assert len(flat) == len(set(flat))  # disjoint


def test_assignments_registers_an_unknown_worker_rather_than_404(workers, client):
    """A pod's first call after a cold start IS its registration — a worker
    that only ever polls /assignments still joins the pool."""
    resp = client.get("/api/v1/assignments", headers=HEADERS, params={"worker_id": "w9"})
    assert resp.status_code == 200
    assert resp.json()["worker_id"] == "w9"
    assert workers.seen["w9"] == workers.clock


def test_assignments_refresh_counts_toward_the_lease(workers, client):
    """Polling /assignments alone keeps last_seen warm — the lease renews on
    the fetch, not only on the register call."""
    client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w1"})
    workers.elapse(LEASE_S)  # one lease passes with only assignment polls
    for _ in range(2):
        client.get("/api/v1/assignments", headers=HEADERS, params={"worker_id": "w2"})
        workers.elapse(LEASE_S)
    # w2 stayed alive through the gap; w1 (never refreshed) has fallen out.
    resp = client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w2"})
    assert resp.json()["shard_count"] == 1


def test_assignments_requires_a_worker_id(client):
    assert client.get("/api/v1/assignments", headers=HEADERS).status_code == 422
    assert (
        client.get("/api/v1/assignments", headers=HEADERS, params={"worker_id": ""}).status_code
        == 422
    )


def test_assignments_uses_the_estate_root_scope_not_a_client_claim(workers, client):
    """Workers pull for the whole registry — the scope is
    `sync_default_org_path`, never an `org_scope` the caller passed."""
    client.get("/api/v1/assignments", headers=HEADERS, params={"worker_id": "w1"})
    assert workers.scopes == ["gj"]
