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

import hmac

import pytest
from fastapi.testclient import TestClient

from prahari_registry.app import WORKER_SECRET_HEADER, app
from prahari_registry.config import RegistrySettings
from prahari_registry.models import Camera, WorkerAssignment, WorkerRegistration
from prahari_registry.repository import (
    WorkerSecretError,
    hash_worker_secret,
    shard_membership,
)

TOKEN = "test-internal-token"
HEADERS = {"x-internal-token": TOKEN}

LEASE_S = 60


class FakeWorkerRepo:
    """In-memory `workers` table with a controllable clock.

    `seen` is `worker_id -> last_seen`; `clock` is now. A worker counts toward
    `shard_count` while `last_seen >= now - 2*lease` — the same predicate the
    SQL applies — and `die()`/`elapse()` simulate a pod that stops refreshing.

    `hashes` is `worker_id -> stored secret_hash`, mirroring the migration-010
    binding semantics the real `WorkerRepository.register` implements: NULL
    (absent key) is unbound, a mint happens only on the `rotate` flag or —
    on a bound row — after the CURRENT secret is presented.
    """

    def __init__(self, lease_s: int = LEASE_S) -> None:
        self.lease_s = lease_s
        self.clock = 1_000.0
        self.seen: dict[str, float] = {}
        self.hashes: dict[str, str] = {}
        self.required = False  # mirrors RegistrySettings.worker_secret_required
        self.minted: list[str] = []
        self.cameras: list[Camera] = []
        self.register_calls: list[str] = []
        self.scopes: list[str] = []

    def _alive(self) -> list[str]:
        horizon = self.clock - self._ALIVE_LEASES * self.lease_s
        return sorted(w for w, t in self.seen.items() if t >= horizon)

    _ALIVE_LEASES = 2

    def _mint(self) -> str:
        secret = f"minted-secret-{len(self.minted)}"
        self.minted.append(secret)
        return secret

    async def bound_secret_hash(self, worker_id: str) -> str | None:
        return self.hashes.get(worker_id)

    async def register(
        self,
        worker_id: str,
        *,
        presented_secret: str | None = None,
        rotate: bool = False,
    ) -> WorkerRegistration:
        self.register_calls.append(worker_id)
        stored = self.hashes.get(worker_id)
        minted: str | None = None
        if stored is not None:
            if presented_secret is None or not hmac.compare_digest(
                hash_worker_secret(presented_secret), stored
            ):
                raise WorkerSecretError(f"worker {worker_id} is bound")
            if rotate:
                minted = self._mint()
        elif rotate:
            minted = self._mint()
        elif self.required:
            raise WorkerSecretError("worker_secret_required is set")
        if minted is not None:
            self.hashes[worker_id] = hash_worker_secret(minted)
        self.seen[worker_id] = self.clock
        index, count = shard_membership(worker_id, self._alive())
        return WorkerRegistration(
            worker_id=worker_id,
            shard_index=index,
            shard_count=count,
            lease_s=self.lease_s,
            worker_secret=minted,
        )

    async def assignment(self, worker_id: str, *, scope: str) -> WorkerAssignment | None:
        """Mirrors the real repo's post-M2 contract: serves only a worker_id
        already registered AND alive — no implicit register on the fetch."""
        self.scopes.append(scope)
        alive = self._alive()
        if worker_id not in alive:
            return None
        index, count = shard_membership(worker_id, alive)
        ordered = sorted(self.cameras, key=lambda c: c.id)
        # Mirrors `(rn - 1) % shard_count = shard_index` over ORDER BY id.
        shard = [c for i, c in enumerate(ordered) if i % count == index]
        return WorkerAssignment(
            worker_id=worker_id,
            shard_index=index,
            shard_count=count,
            lease_s=self.lease_s,
            cameras=shard,
        )

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
        "worker_secret": None,
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


def test_assignments_404s_for_an_unregistered_worker(workers, client):
    """M2: worker identity is not caller-asserted — a token holder cannot
    mint a phantom worker (and read its would-be slice, which carries the
    credential-bearing fan-out URLs) just by querying with a made-up id."""
    resp = client.get("/api/v1/assignments", headers=HEADERS, params={"worker_id": "w9"})
    assert resp.status_code == 404
    assert "w9" not in workers.seen  # nothing was registered as a side effect


def test_assignments_404s_for_a_worker_whose_lease_expired(workers, client):
    """Registered is not enough — a worker whose last_seen has aged past the
    alive horizon must re-register, not keep pulling assignments forever."""
    client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w1"})
    workers.elapse(2 * LEASE_S + 1)  # past the 2x-lease alive horizon
    resp = client.get("/api/v1/assignments", headers=HEADERS, params={"worker_id": "w1"})
    assert resp.status_code == 404
    # Registering again re-joins the pool and the poll works.
    client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w1"})
    resp = client.get("/api/v1/assignments", headers=HEADERS, params={"worker_id": "w1"})
    assert resp.status_code == 200


def test_register_then_poll_is_the_full_assignment_cycle(workers, client):
    """The worker client's actual flow: register refreshes the lease, the
    assignment fetch then serves the shard."""
    client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w1"})
    resp = client.get("/api/v1/assignments", headers=HEADERS, params={"worker_id": "w1"})
    assert resp.status_code == 200
    assert resp.json()["worker_id"] == "w1"


def test_assignments_requires_a_worker_id(client):
    assert client.get("/api/v1/assignments", headers=HEADERS).status_code == 422
    assert (
        client.get("/api/v1/assignments", headers=HEADERS, params={"worker_id": ""}).status_code
        == 422
    )


def test_assignments_uses_the_estate_root_scope_not_a_client_claim(workers, client):
    """Workers pull for the whole registry — the scope is
    `sync_default_org_path`, never an `org_scope` the caller passed."""
    client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w1"})
    client.get("/api/v1/assignments", headers=HEADERS, params={"worker_id": "w1"})
    assert workers.scopes == ["gj"]


# --- per-worker secrets (migration 010) ---------------------------------------
#
# X-Worker-Secret is the second credential, layered on X-Internal-Token: the
# token says "an inference worker may call", the secret says WHICH worker —
# it is what stops a stolen inference-token from claiming a bound worker_id.


def _bind(workers, client, worker_id: str = "w1") -> str:
    """Register with the mint flag and return the issued secret."""
    resp = client.post(
        "/api/v1/workers/register",
        headers=HEADERS,
        json={"worker_id": worker_id, "rotate_secret": True},
    )
    assert resp.status_code == 200
    secret = resp.json()["worker_secret"]
    assert secret
    return secret


def test_opt_in_register_mints_the_secret_once(workers, client):
    """The `rotate_secret` flag on an unbound worker_id is the bind request —
    the response carries the plaintext exactly once and only its digest is
    stored."""
    secret = _bind(workers, client)
    assert workers.hashes["w1"] == hash_worker_secret(secret)
    # The keep-alive with the secret refreshes WITHOUT re-minting — a new
    # secret on every refresh would make the credential useless as identity.
    resp = client.post(
        "/api/v1/workers/register",
        headers={**HEADERS, WORKER_SECRET_HEADER: secret},
        json={"worker_id": "w1"},
    )
    assert resp.status_code == 200
    assert resp.json()["worker_secret"] is None


def test_unbound_register_stays_unbound(workers, client):
    """Backward compat: a worker that never sends the flag is never handed a
    secret it could not present — the pre-binding behaviour, verbatim."""
    for _ in range(2):
        resp = client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w1"})
        assert resp.status_code == 200
        assert resp.json()["worker_secret"] is None
    assert "w1" not in workers.hashes


def test_bound_worker_reregister_without_the_secret_is_403(workers, client):
    """The whole point of the feature: the shared inference-token alone can
    no longer refresh a bound worker's lease."""
    _bind(workers, client)
    resp = client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w1"})
    assert resp.status_code == 403


def test_bound_worker_reregister_with_the_wrong_secret_is_403(workers, client):
    _bind(workers, client)
    resp = client.post(
        "/api/v1/workers/register",
        headers={**HEADERS, WORKER_SECRET_HEADER: "not-the-secret"},
        json={"worker_id": "w1"},
    )
    assert resp.status_code == 403


def test_bound_worker_reregister_with_the_right_secret_refreshes(workers, client):
    secret = _bind(workers, client)
    resp = client.post(
        "/api/v1/workers/register",
        headers={**HEADERS, WORKER_SECRET_HEADER: secret},
        json={"worker_id": "w1"},
    )
    assert resp.status_code == 200
    assert workers.register_calls == ["w1", "w1"]


def test_rotation_requires_the_current_secret_and_replaces_it(workers, client):
    """A stolen secret cannot re-key the identity: rotate is gated on the
    CURRENT secret, and the old one stops working the moment a new one is
    issued."""
    secret = _bind(workers, client)

    # Rotate with a wrong secret → refused, the binding is untouched.
    resp = client.post(
        "/api/v1/workers/register",
        headers={**HEADERS, WORKER_SECRET_HEADER: "stolen-or-wrong"},
        json={"worker_id": "w1", "rotate_secret": True},
    )
    assert resp.status_code == 403
    assert workers.hashes["w1"] == hash_worker_secret(secret)

    # Rotate with the current secret → a fresh one, returned once.
    resp = client.post(
        "/api/v1/workers/register",
        headers={**HEADERS, WORKER_SECRET_HEADER: secret},
        json={"worker_id": "w1", "rotate_secret": True},
    )
    assert resp.status_code == 200
    new_secret = resp.json()["worker_secret"]
    assert new_secret and new_secret != secret

    # The old secret is dead on every worker-facing call.
    resp = client.post(
        "/api/v1/workers/register",
        headers={**HEADERS, WORKER_SECRET_HEADER: secret},
        json={"worker_id": "w1"},
    )
    assert resp.status_code == 403
    resp = client.post(
        "/api/v1/workers/register",
        headers={**HEADERS, WORKER_SECRET_HEADER: new_secret},
        json={"worker_id": "w1"},
    )
    assert resp.status_code == 200


def test_assignments_require_the_secret_once_bound(workers, client):
    """Bound worker_id + no/wrong secret → 403 before the shard is served —
    otherwise a token holder could still pull the credential-bearing fan-out
    URLs under the victim's name."""
    secret = _bind(workers, client)

    assert (
        client.get("/api/v1/assignments", headers=HEADERS, params={"worker_id": "w1"}).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/assignments",
            headers={**HEADERS, WORKER_SECRET_HEADER: "wrong"},
            params={"worker_id": "w1"},
        ).status_code
        == 403
    )
    resp = client.get(
        "/api/v1/assignments",
        headers={**HEADERS, WORKER_SECRET_HEADER: secret},
        params={"worker_id": "w1"},
    )
    assert resp.status_code == 200
    assert resp.json()["worker_id"] == "w1"


def test_assignments_for_an_unbound_worker_are_unchanged(workers, client):
    """Compat: nothing presented, nothing required — the pre-binding path."""
    client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w1"})
    resp = client.get("/api/v1/assignments", headers=HEADERS, params={"worker_id": "w1"})
    assert resp.status_code == 200


def test_worker_secret_required_refuses_an_unbound_register(workers, client):
    """The arm-after-upgrade switch: with `worker_secret_required` a register
    that does not ask for a secret is refused rather than quietly left
    unbound — the last path a shared-token holder could still claim."""
    workers.required = True
    resp = client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w1"})
    assert resp.status_code == 403
    # Asking for a secret still works — refusal is about staying unbound,
    # not about refusing new workers.
    assert _bind(workers, client)


def test_secret_binding_survives_lease_expiry(workers, client):
    """A lapsed worker re-registers under its secret, not around it: the
    binding lives on the row, independent of the lease clock."""
    secret = _bind(workers, client)
    workers.elapse(2 * LEASE_S + 1)
    resp = client.post("/api/v1/workers/register", headers=HEADERS, json={"worker_id": "w1"})
    assert resp.status_code == 403
    resp = client.post(
        "/api/v1/workers/register",
        headers={**HEADERS, WORKER_SECRET_HEADER: secret},
        json={"worker_id": "w1"},
    )
    assert resp.status_code == 200
