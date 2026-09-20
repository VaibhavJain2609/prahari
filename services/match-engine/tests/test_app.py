"""app.py: the FastAPI surface. `/healthz` must survive a broken watchlist
without touching it; `/readyz` must actually report whether one loaded.

Each test starts a real (in-process) app via `TestClient` as a context
manager, so the lifespan runs -- including binding the gRPC server on an
ephemeral port (`grpc_port=0`), which is what keeps parallel test runs from
fighting over a fixed port.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from prahari_match.app import app


@pytest.fixture
def watchlist_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "watchlist"
    directory.mkdir()
    (directory / "wl.json").write_text(
        json.dumps(
            [
                {"entry_id": "E1", "plate": "GJ01AB1234", "reason": "stolen"},
                {"entry_id": "E2", "plate": "GJ05CD5678", "reason": "wanted"},
            ]
        ),
        encoding="utf-8",
    )
    return directory


def _client(
    monkeypatch: pytest.MonkeyPatch, watchlist_dir: Path, internal_token: str | None = None
) -> TestClient:
    monkeypatch.setenv("PRAHARI_MATCH_WATCHLIST_DIR", str(watchlist_dir))
    monkeypatch.setenv("PRAHARI_MATCH_GRPC_PORT", "0")  # ephemeral -- no fixed-port collisions
    if internal_token is not None:
        monkeypatch.setenv("PRAHARI_MATCH_INTERNAL_TOKEN", internal_token)
    return TestClient(app)


class TestProbes:
    def test_healthz_never_touches_the_watchlist(
        self, monkeypatch: pytest.MonkeyPatch, watchlist_dir: Path
    ) -> None:
        with _client(monkeypatch, watchlist_dir) as client:
            response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_readyz_reports_watchlist_loaded(
        self, monkeypatch: pytest.MonkeyPatch, watchlist_dir: Path
    ) -> None:
        with _client(monkeypatch, watchlist_dir) as client:
            response = client.get("/readyz")
        body = response.json()
        assert response.status_code == 200
        assert body["status"] == "ready"
        assert body["entries"] == 2

    def test_readyz_reports_unavailable_when_watchlist_is_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        empty_dir = tmp_path / "empty-watchlist"
        empty_dir.mkdir()
        with _client(monkeypatch, empty_dir) as client:
            response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "unavailable"


class TestWatchlistAdmin:
    def test_summary_reflects_loaded_entries(
        self, monkeypatch: pytest.MonkeyPatch, watchlist_dir: Path
    ) -> None:
        with _client(monkeypatch, watchlist_dir) as client:
            response = client.get("/api/v1/watchlist/summary")
        body = response.json()
        assert body["entries"] == 2
        assert body["bloom_size_bits"] > 0

    def test_reload_picks_up_a_newly_added_entry(
        self, monkeypatch: pytest.MonkeyPatch, watchlist_dir: Path
    ) -> None:
        with _client(monkeypatch, watchlist_dir) as client:
            assert client.get("/api/v1/watchlist/summary").json()["entries"] == 2

            (watchlist_dir / "extra.json").write_text(
                json.dumps([{"entry_id": "E3", "plate": "GJ18EF9012", "reason": "blacklisted"}]),
                encoding="utf-8",
            )
            reload_response = client.post("/api/v1/watchlist/reload")
            assert reload_response.status_code == 200
            assert reload_response.json()["entries"] == 3
            assert client.get("/api/v1/watchlist/summary").json()["entries"] == 3


class TestInternalToken:
    """`require_internal_token`: once PRAHARI_MATCH_INTERNAL_TOKEN is set, the
    HTTP admin surface (watchlist, /alerts) answers 401 without the credential.
    Unset means the gate is off entirely -- the local/dev default. The gRPC
    surface's gate is `InternalTokenInterceptor`, covered in
    test_grpc_server.py."""

    def test_unset_token_leaves_the_api_open(
        self, monkeypatch: pytest.MonkeyPatch, watchlist_dir: Path
    ) -> None:
        with _client(monkeypatch, watchlist_dir) as client:
            assert client.get("/api/v1/alerts").status_code == 200

    def test_armed_gate_rejects_a_missing_token(
        self, monkeypatch: pytest.MonkeyPatch, watchlist_dir: Path
    ) -> None:
        with _client(monkeypatch, watchlist_dir, internal_token="tok-1") as client:
            assert client.get("/api/v1/alerts").status_code == 401
            assert client.post("/api/v1/watchlist/reload").status_code == 401

    def test_armed_gate_rejects_a_wrong_token(
        self, monkeypatch: pytest.MonkeyPatch, watchlist_dir: Path
    ) -> None:
        with _client(monkeypatch, watchlist_dir, internal_token="tok-1") as client:
            response = client.get("/api/v1/alerts", headers={"x-internal-token": "wrong"})
        assert response.status_code == 401

    def test_armed_gate_accepts_the_right_token(
        self, monkeypatch: pytest.MonkeyPatch, watchlist_dir: Path
    ) -> None:
        with _client(monkeypatch, watchlist_dir, internal_token="tok-1") as client:
            response = client.get("/api/v1/alerts", headers={"x-internal-token": "tok-1"})
        assert response.status_code == 200

    def test_healthz_stays_open_when_armed(
        self, monkeypatch: pytest.MonkeyPatch, watchlist_dir: Path
    ) -> None:
        # A liveness probe carries no data and must not depend on a secret
        # being wired correctly to answer.
        with _client(monkeypatch, watchlist_dir, internal_token="tok-1") as client:
            assert client.get("/healthz").status_code == 200


class TestAlerts:
    def test_list_alerts_starts_empty(
        self, monkeypatch: pytest.MonkeyPatch, watchlist_dir: Path
    ) -> None:
        with _client(monkeypatch, watchlist_dir) as client:
            response = client.get("/api/v1/alerts")
        assert response.status_code == 200
        assert response.json() == []

    def test_unknown_alert_id_is_404(
        self, monkeypatch: pytest.MonkeyPatch, watchlist_dir: Path
    ) -> None:
        with _client(monkeypatch, watchlist_dir) as client:
            response = client.get("/api/v1/alerts/does-not-exist")
        assert response.status_code == 404
