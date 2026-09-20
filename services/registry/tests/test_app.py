"""The registry HTTP surface, exercised with FastAPI's TestClient against a
faked repository — no database, no network.

`TestClient(app)` is deliberately used WITHOUT its context manager: entering
it would run the lifespan, which wants a real Postgres. Whatever the lifespan
would have installed on `app.state` is set directly per fixture instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import asyncpg
import pytest
from fastapi.testclient import TestClient

from prahari_registry.app import app
from prahari_registry.config import RegistrySettings
from prahari_registry.models import Camera, HeartbeatSample
from prahari_registry.repository import _clamp_observed_at

TOKEN = "test-internal-token"
HEADERS = {"x-internal-token": TOKEN}
CAM_ID = "00000000-0000-0000-0000-0000000000ab"


class FakeRepo:
    """Records what the endpoints asked for. Same idea as the FakeRepo in
    test_sync.py: the database is not what is being tested here."""

    def __init__(self) -> None:
        self.cameras: dict[str, Camera] = {}
        self.recorded: list[tuple[str, object, object]] = []
        self.history: list[HeartbeatSample] = []
        self.history_calls: list[dict] = []
        self.paths: dict[str, str] = {}
        self.raise_on_get: Exception | None = None

    async def get(self, camera_id: str, *, scope: str) -> Camera | None:
        if self.raise_on_get is not None:
            raise self.raise_on_get
        return self.cameras.get(camera_id)

    async def recent_health_history(self, camera_id: str, *, window_s: int, limit: int):
        return [], []

    async def record_heartbeat(self, camera_id, heartbeat, verdict) -> None:
        # Store what the real repository would store — the clamped timestamp —
        # so tests see the value that would have landed in the table.
        self.recorded.append((camera_id, heartbeat, _clamp_observed_at(heartbeat.observed_at)))

    async def desired_mediamtx_paths(self) -> dict[str, str]:
        return dict(self.paths)

    async def health_history(
        self, camera_id: str, *, scope: str, since=None, limit: int = 100
    ) -> list[HeartbeatSample]:
        self.history_calls.append(
            {"camera_id": camera_id, "scope": scope, "since": since, "limit": limit}
        )
        return self.history[:limit]


@pytest.fixture
def repo():
    repo = FakeRepo()
    app.state.repo = repo
    app.state.settings = RegistrySettings(internal_token=TOKEN, sync_enabled=False)
    app.state.gateway_configured = False
    return repo


@pytest.fixture
def client(repo):
    return TestClient(app)


def _camera() -> Camera:
    return Camera(id=CAM_ID, source="manual", external_id="cam-x")


# --- internal token ------------------------------------------------------------


def test_api_requires_the_token_when_one_is_configured(client):
    assert client.get("/api/v1/streams/paths").status_code == 401
    assert (
        client.get("/api/v1/streams/paths", headers={"x-internal-token": "wrong"}).status_code
        == 401
    )
    assert client.get("/api/v1/streams/paths", headers=HEADERS).status_code == 200


def test_health_probes_are_exempt_from_the_token(client):
    # A kubelet liveness probe carries no credential; it must not 401.
    assert client.get("/healthz").status_code == 200


def test_empty_token_disables_enforcement(repo):
    """The dev default is fail-OPEN — documented, and loudly logged at startup.
    This test pins the semantics so nobody "fixes" it silently one way or the
    other."""
    app.state.settings = RegistrySettings(internal_token="", sync_enabled=False)
    assert TestClient(app).get("/api/v1/streams/paths").status_code == 200


# --- streams/paths credential leak --------------------------------------------


def test_streams_paths_never_returns_credentials(repo, client):
    """`desired_mediamtx_paths` embeds decrypted DVR credentials for the
    reconcile path. The HTTP surface must strip userinfo from every URL —
    regression cover for the leak where this endpoint returned it verbatim."""
    repo.paths = {
        "cam-1": "rtsp://admin:s3cret%40pw@10.0.0.5:554/ch1",
        "cam-2": "rtsp://10.0.0.6:554/ch2",
    }
    resp = client.get("/api/v1/streams/paths", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["cam-1"] == "rtsp://10.0.0.5:554/ch1"
    assert body["cam-2"] == "rtsp://10.0.0.6:554/ch2"
    for url in body.values():
        assert "@" not in urlsplit(url).netloc
    assert "s3cret" not in resp.text


# --- heartbeats ----------------------------------------------------------------


def test_heartbeat_is_accepted_and_recorded(repo, client):
    repo.cameras[CAM_ID] = _camera()
    resp = client.post(
        f"/api/v1/cameras/{CAM_ID}/heartbeat",
        headers=HEADERS,
        json={"worker_id": "w1", "connected": True, "measured_fps": 8.0},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "healthy"
    assert repo.recorded[0][0] == CAM_ID


def test_heartbeat_on_unknown_camera_is_404(client):
    resp = client.post(
        f"/api/v1/cameras/{CAM_ID}/heartbeat",
        headers=HEADERS,
        json={"worker_id": "w1"},
    )
    assert resp.status_code == 404


def test_far_future_observed_at_is_clamped_not_stored(repo, client):
    """A worker clock stamped a day ahead must not pin `last_heartbeat_at`
    forward via GREATEST — that would suppress staleness for a dead camera."""
    repo.cameras[CAM_ID] = _camera()
    resp = client.post(
        f"/api/v1/cameras/{CAM_ID}/heartbeat",
        headers=HEADERS,
        json={
            "worker_id": "w1",
            "observed_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    )
    assert resp.status_code == 200
    stored_at = repo.recorded[0][2]
    assert stored_at <= datetime.now(UTC)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("measured_fps", -1.0),
        ("black_frame_ratio", 1.5),
        ("black_frame_ratio", -0.1),
        ("consecutive_failures", -1),
        ("frames_decoded", -5),
    ],
)
def test_heartbeat_rejects_out_of_range_fields(client, field, value):
    """Negative rates and ratios outside [0,1] are reporter bugs; letting them
    through would corrupt the drift baseline they get folded into."""
    resp = client.post(
        f"/api/v1/cameras/{CAM_ID}/heartbeat",
        headers=HEADERS,
        json={"worker_id": "w1", field: value},
    )
    assert resp.status_code == 422


# --- health history ------------------------------------------------------------


def test_health_history_returns_recent_samples(repo, client):
    repo.cameras[CAM_ID] = _camera()
    repo.history = [
        HeartbeatSample(
            observed_at=datetime.now(UTC), worker_id="w1", connected=True, measured_fps=8.0
        )
    ]
    resp = client.get(f"/api/v1/cameras/{CAM_ID}/health-history", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["worker_id"] == "w1"
    assert data[0]["measured_fps"] == 8.0


def test_health_history_unknown_camera_is_404(client):
    resp = client.get(f"/api/v1/cameras/{CAM_ID}/health-history", headers=HEADERS)
    assert resp.status_code == 404


def test_health_history_limit_is_bounded(repo, client):
    """The drawer paginates; nothing may pull the whole retention window."""
    repo.cameras[CAM_ID] = _camera()
    assert (
        client.get(
            f"/api/v1/cameras/{CAM_ID}/health-history", headers=HEADERS, params={"limit": 501}
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/v1/cameras/{CAM_ID}/health-history", headers=HEADERS, params={"limit": 500}
        ).status_code
        == 200
    )


def test_health_history_passes_since_and_limit_to_the_repo(repo, client):
    repo.cameras[CAM_ID] = _camera()
    since = "2026-09-01T10:00:00+00:00"
    resp = client.get(
        f"/api/v1/cameras/{CAM_ID}/health-history",
        headers=HEADERS,
        params={"limit": 25, "since": since},
    )
    assert resp.status_code == 200
    call = repo.history_calls[0]
    assert call["limit"] == 25
    assert call["since"] == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


# --- malformed identifiers ------------------------------------------------------


def test_non_uuid_camera_id_maps_to_404_not_500(repo, client):
    """`WHERE id = $1::uuid` on a non-UUID raises asyncpg.DataError; without the
    handler that was a 500. No camera can have this id — 404 is the truth."""
    repo.raise_on_get = asyncpg.exceptions.InvalidTextRepresentationError(
        'invalid input syntax for type uuid: "zzz"'
    )
    resp = client.get("/api/v1/cameras/zzz", headers=HEADERS)
    assert resp.status_code == 404


def test_malformed_ltree_scope_maps_to_422(repo, client):
    repo.raise_on_get = asyncpg.exceptions.InvalidTextRepresentationError(
        'invalid input syntax for type ltree: "!!bad"'
    )
    resp = client.get(f"/api/v1/cameras/{CAM_ID}", headers=HEADERS, params={"org_scope": "!!bad"})
    assert resp.status_code == 422


# --- probe ---------------------------------------------------------------------


def test_probe_rejects_ports_outside_the_allowlist(client):
    """Default allowlist is {554}; a probe against 8554 (or 22, or 6379) is a
    400, refused before DNS is even consulted."""
    resp = client.post(
        "/api/v1/cameras/probe",
        headers=HEADERS,
        json={"rtsp_url": "rtsp://10.0.0.5:8554/stream"},
    )
    assert resp.status_code == 400
    assert "allowlist" in resp.json()["detail"]
