"""The admin/dashboard endpoints added on top of the security batch: user
list/disable/enable, API-key list/revoke, the catalogue-sync and watchlist
proxies, camera health-history, and the recent-alerts list.

Same style as test_hardening.py — handlers called directly against fakes,
no database — plus one small TestClient section at the bottom for the parts
that are only real once wired through the dependencies (401 with no
credentials, 403 with the wrong role).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from prahari_bff.app import (
    app,
    camera_health_history,
    disable_user,
    enable_user,
    list_alerts,
    list_api_keys,
    list_sync_runs,
    list_users,
    revoke_api_key,
    trigger_sync,
    watchlist_reload,
    watchlist_summary,
)
from prahari_bff.models import ApiKey, ApiKeyPurpose, Principal, Role, User

ADMIN_ZONE4 = Principal(
    id="u2",
    subject="admin.zone4",
    org_id="org-zone4",
    org_path="gj.ahmedabad_city.zone_4",
    role=Role.ADMIN,
    kind="session",
)
ADMIN_ROOT = Principal(
    id="u0",
    subject="root.admin",
    org_id="org-root",
    org_path="gj",
    role=Role.ADMIN,
    kind="session",
)
VIEWER_ZONE4 = Principal(
    id="u3",
    subject="viewer.zone4",
    org_id="org-zone4",
    org_path="gj.ahmedabad_city.zone_4",
    role=Role.VIEWER,
    kind="session",
)

TARGET_USER = User(
    id="u9",
    username="viewer.ward9",
    org_id="org-ward-9",
    role=Role.VIEWER,
    created_at=datetime(2026, 9, 1, tzinfo=UTC),
)
TARGET_KEY = ApiKey(
    id="k9",
    org_id="org-ward-9",
    role=Role.VIEWER,
    purpose=ApiKeyPurpose.VENDOR_ADAPTER,
    label="vendor-y",
)


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    async def append(self, **kwargs):
        self.entries.append(kwargs)
        return kwargs


class FakePool:
    """org_id -> org_path, the one read the handlers need."""

    def __init__(self, paths: dict[str, str] | None = None) -> None:
        self._paths = paths or {}

    async def fetchval(self, query: str, *args):
        return self._paths.get(args[0]) if args else None


class FakeUserRepo:
    def __init__(self, users: dict[str, User] | None = None) -> None:
        self._users = users or {}
        self.list_scope: str | None = None
        self.disabled_calls: list[tuple[str, bool]] = []

    async def list_users(self, scope: str) -> list[User]:
        self.list_scope = scope
        return list(self._users.values())

    async def get(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    async def set_disabled(self, user_id: str, *, disabled: bool) -> User | None:
        self.disabled_calls.append((user_id, disabled))
        user = self._users.get(user_id)
        if user is None:
            return None
        return user.model_copy(update={"disabled_at": datetime.now(UTC) if disabled else None})


class FakeApiKeyRepo:
    def __init__(self, keys: dict[str, ApiKey] | None = None) -> None:
        self._keys = keys or {}
        self.list_scope: str | None = None
        self.revoked: list[str] = []

    async def list_keys(self, scope: str) -> list[ApiKey]:
        self.list_scope = scope
        return list(self._keys.values())

    async def get(self, key_id: str) -> ApiKey | None:
        return self._keys.get(key_id)

    async def revoke(self, key_id: str) -> ApiKey | None:
        self.revoked.append(key_id)
        key = self._keys.get(key_id)
        if key is None:
            return None
        return key.model_copy(update={"revoked_at": datetime.now(UTC)})


class FakeRegistry:
    def __init__(self, status_code: int = 200, body=None) -> None:
        self._response = httpx.Response(status_code, json=body if body is not None else {})
        self.calls: list[tuple] = []

    async def get(self, path: str, params: dict | None = None) -> httpx.Response:
        self.calls.append(("GET", path, params))
        return self._response

    async def post(self, path: str, json: dict | None = None) -> httpx.Response:
        self.calls.append(("POST", path, json))
        return self._response


# Same canned-response fake as FakeRegistry: the client boundary is
# `get`/`post` returning an `httpx.Response`, which is what a real
# `httpx.AsyncClient`-backed client (or an ASGI-transport fake) also looks
# like — app.state injection is the seam either way.
class FakeMatchEngine(FakeRegistry):
    pass


class FakeScopeResolver:
    def __init__(self, camera_orgs: dict[str, str | None] | None = None) -> None:
        self._camera_orgs = camera_orgs or {}

    async def org_path_for_camera(self, camera_id: str) -> str | None:
        return self._camera_orgs.get(camera_id)


def _request(*, pool=None, user_repo=None, api_key_repo=None):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                pool=pool or FakePool(),
                user_repo=user_repo or FakeUserRepo(),
                api_key_repo=api_key_repo or FakeApiKeyRepo(),
            )
        ),
    )


# --- users: list / disable / enable ---------------------------------------


async def test_list_users_passes_the_callers_own_org_path_as_scope():
    repo = FakeUserRepo({"u9": TARGET_USER})
    result = await list_users(ADMIN_ZONE4, _request(user_repo=repo))
    assert repo.list_scope == "gj.ahmedabad_city.zone_4"
    assert result == [TARGET_USER]


async def test_disable_user_sets_disabled_at_and_audits():
    audit = FakeAudit()
    repo = FakeUserRepo({"u9": TARGET_USER})
    request = _request(
        pool=FakePool({"org-ward-9": "gj.ahmedabad_city.zone_4.ward_9"}),
        user_repo=repo,
    )
    user = await disable_user("u9", ADMIN_ZONE4, audit, request)
    assert user.disabled_at is not None
    assert repo.disabled_calls == [("u9", True)]
    assert audit.entries[-1]["action"] == "user_disable"
    assert audit.entries[-1]["purpose_code"] == "admin"
    assert audit.entries[-1]["resource"] == "user:viewer.ward9"


async def test_enable_user_clears_disabled_at_and_audits():
    audit = FakeAudit()
    disabled = TARGET_USER.model_copy(update={"disabled_at": datetime.now(UTC)})
    repo = FakeUserRepo({"u9": disabled})
    request = _request(
        pool=FakePool({"org-ward-9": "gj.ahmedabad_city.zone_4.ward_9"}),
        user_repo=repo,
    )
    user = await enable_user("u9", ADMIN_ZONE4, audit, request)
    assert user.disabled_at is None
    assert repo.disabled_calls == [("u9", False)]
    assert audit.entries[-1]["action"] == "user_enable"


async def test_disable_user_outside_caller_subtree_is_403_and_audited():
    audit = FakeAudit()
    repo = FakeUserRepo({"u9": TARGET_USER})
    request = _request(
        pool=FakePool({"org-ward-9": "gj.ahmedabad_city.zone_5.ward_9"}),
        user_repo=repo,
    )
    with pytest.raises(HTTPException) as exc:
        await disable_user("u9", ADMIN_ZONE4, audit, request)
    assert exc.value.status_code == 403
    assert repo.disabled_calls == []  # the write never ran
    assert [e["action"] for e in audit.entries] == ["user_disable_denied"]


async def test_enable_user_outside_caller_subtree_is_403_and_audited():
    audit = FakeAudit()
    request = _request(
        pool=FakePool({"org-ward-9": "gj.surat_city"}),
        user_repo=FakeUserRepo({"u9": TARGET_USER}),
    )
    with pytest.raises(HTTPException) as exc:
        await enable_user("u9", ADMIN_ZONE4, audit, request)
    assert exc.value.status_code == 403
    assert [e["action"] for e in audit.entries] == ["user_enable_denied"]


async def test_disable_unknown_user_is_404():
    audit = FakeAudit()
    with pytest.raises(HTTPException) as exc:
        await disable_user("ghost", ADMIN_ZONE4, audit, _request())
    assert exc.value.status_code == 404
    assert audit.entries == []


async def test_disable_is_idempotent_when_already_disabled():
    """set_disabled COALESCEs, but the endpoint must also just succeed a
    second time — no 409, no error, one more audit entry."""
    audit = FakeAudit()
    disabled = TARGET_USER.model_copy(update={"disabled_at": datetime.now(UTC)})
    repo = FakeUserRepo({"u9": disabled})
    request = _request(
        pool=FakePool({"org-ward-9": "gj.ahmedabad_city.zone_4.ward_9"}),
        user_repo=repo,
    )
    user = await disable_user("u9", ADMIN_ZONE4, audit, request)
    assert user.disabled_at is not None
    assert audit.entries[-1]["action"] == "user_disable"


# --- api keys: list / revoke ------------------------------------------------


async def test_list_api_keys_passes_the_callers_own_org_path_as_scope():
    repo = FakeApiKeyRepo({"k9": TARGET_KEY})
    result = await list_api_keys(ADMIN_ZONE4, _request(api_key_repo=repo))
    assert repo.list_scope == "gj.ahmedabad_city.zone_4"
    assert result == [TARGET_KEY]
    # The response model carries no hash field at all — this can't leak.
    assert "key_hash" not in result[0].model_dump()


async def test_revoke_api_key_audits_and_returns_revoked_key():
    audit = FakeAudit()
    repo = FakeApiKeyRepo({"k9": TARGET_KEY})
    request = _request(
        pool=FakePool({"org-ward-9": "gj.ahmedabad_city.zone_4.ward_9"}),
        api_key_repo=repo,
    )
    key = await revoke_api_key("k9", ADMIN_ZONE4, audit, request)
    assert key.revoked_at is not None
    assert repo.revoked == ["k9"]
    assert audit.entries[-1]["action"] == "api_key_revoke"
    assert audit.entries[-1]["resource"] == "api_key:vendor-y"


async def test_revoke_api_key_outside_caller_subtree_is_403_and_audited():
    audit = FakeAudit()
    repo = FakeApiKeyRepo({"k9": TARGET_KEY})
    request = _request(
        pool=FakePool({"org-ward-9": "gj.surat_city"}),
        api_key_repo=repo,
    )
    with pytest.raises(HTTPException) as exc:
        await revoke_api_key("k9", ADMIN_ZONE4, audit, request)
    assert exc.value.status_code == 403
    assert repo.revoked == []
    assert [e["action"] for e in audit.entries] == ["api_key_revoke_denied"]


async def test_revoke_unknown_api_key_is_404():
    with pytest.raises(HTTPException) as exc:
        await revoke_api_key("ghost", ADMIN_ZONE4, FakeAudit(), _request())
    assert exc.value.status_code == 404


# --- catalogue sync proxy ----------------------------------------------------


async def test_trigger_sync_forwards_and_audits():
    audit = FakeAudit()
    registry = FakeRegistry(200, {"run_id": "r1", "cameras_seen": 42})
    result = await trigger_sync(ADMIN_ROOT, registry, audit)
    assert registry.calls == [("POST", "/api/v1/sync", None)]
    assert result["run_id"] == "r1"
    assert audit.entries[-1]["action"] == "sync_trigger"


async def test_trigger_sync_passes_through_upstream_409():
    """A sync already running upstream is the registry's 409, verbatim — not
    rewritten into a 500 or a retry."""
    audit = FakeAudit()
    registry = FakeRegistry(409, {"detail": "a sync is already running"})
    with pytest.raises(HTTPException) as exc:
        await trigger_sync(ADMIN_ROOT, registry, audit)
    assert exc.value.status_code == 409
    assert exc.value.detail == "a sync is already running"
    assert audit.entries[-1]["action"] == "sync_trigger_failed"


async def test_trigger_sync_passes_through_upstream_503():
    audit = FakeAudit()
    registry = FakeRegistry(503, {"detail": "gateway credentials not configured"})
    with pytest.raises(HTTPException) as exc:
        await trigger_sync(ADMIN_ROOT, registry, audit)
    assert exc.value.status_code == 503


async def test_list_sync_runs_forwards_limit():
    registry = FakeRegistry(200, [{"run_id": "r1"}, {"run_id": "r2"}])
    result = await list_sync_runs(ADMIN_ROOT, registry, 25)
    assert registry.calls == [("GET", "/api/v1/sync/runs", {"limit": 25})]
    assert len(result) == 2


# --- watchlist proxy ----------------------------------------------------------


async def test_watchlist_summary_proxies_to_match_engine():
    match = FakeMatchEngine(200, {"entries": 1200, "bloom_size_bits": 4096})
    result = await watchlist_summary(ADMIN_ROOT, match)
    assert match.calls == [("GET", "/api/v1/watchlist/summary", None)]
    assert result["entries"] == 1200


async def test_watchlist_reload_is_audited():
    audit = FakeAudit()
    match = FakeMatchEngine(200, {"status": "reloaded", "entries": 1300})
    result = await watchlist_reload(ADMIN_ROOT, match, audit)
    assert match.calls == [("POST", "/api/v1/watchlist/reload", None)]
    assert result["status"] == "reloaded"
    assert audit.entries[-1]["action"] == "watchlist_reload"
    assert audit.entries[-1]["purpose_code"] == "admin"


async def test_watchlist_reload_upstream_error_is_audited_as_failed():
    audit = FakeAudit()
    match = FakeMatchEngine(500, {"detail": "watchlist dir unreadable"})
    with pytest.raises(HTTPException) as exc:
        await watchlist_reload(ADMIN_ROOT, match, audit)
    assert exc.value.status_code == 500
    assert audit.entries[-1]["action"] == "watchlist_reload_failed"


# --- alerts: recent buffer, scope-filtered ------------------------------------


def _alert(alert_id: str, camera_id: str | None) -> dict:
    detection = {"camera_id": camera_id} if camera_id is not None else {}
    return {"alert_id": alert_id, "detection": detection}


async def test_list_alerts_drops_cameras_outside_the_callers_subtree():
    match = FakeMatchEngine(
        200,
        [
            _alert("a1", "cam-in"),
            _alert("a2", "cam-out"),
            _alert("a3", "cam-gone"),
            _alert("a4", None),
        ],
    )
    resolver = FakeScopeResolver(
        {
            "cam-in": "gj.ahmedabad_city.zone_4",
            "cam-out": "gj.surat_city",
            "cam-gone": None,
        }
    )
    result = await list_alerts(VIEWER_ZONE4, match, resolver, 50)
    assert match.calls == [("GET", "/api/v1/alerts", {"limit": 50})]
    assert [a["alert_id"] for a in result] == ["a1"]


async def test_list_alerts_root_scope_sees_everything_with_a_known_camera():
    match = FakeMatchEngine(200, [_alert("a1", "cam-in"), _alert("a2", "cam-out")])
    resolver = FakeScopeResolver({"cam-in": "gj.ahmedabad_city.zone_4", "cam-out": "gj.surat_city"})
    result = await list_alerts(ADMIN_ROOT, match, resolver, 50)
    assert [a["alert_id"] for a in result] == ["a1", "a2"]


async def test_list_alerts_passes_through_upstream_errors():
    match = FakeMatchEngine(503, {"detail": "match engine down"})
    with pytest.raises(HTTPException) as exc:
        await list_alerts(ADMIN_ROOT, match, FakeScopeResolver(), 50)
    assert exc.value.status_code == 503


# --- camera health-history proxy -----------------------------------------------


async def test_health_history_forces_caller_scope_and_passes_filters():
    registry = FakeRegistry(200, [{"state": "healthy"}])
    request = _request()
    request.query_params = {"limit": "5", "org_scope": "gj.surat_city"}
    result = await camera_health_history("cam-1", VIEWER_ZONE4, registry, request)
    assert result == [{"state": "healthy"}]
    _, path, params = registry.calls[0]
    assert path == "/api/v1/cameras/cam-1/health-history"
    # Whatever scope the caller asked for is replaced by their own.
    assert params["org_scope"] == "gj.ahmedabad_city.zone_4"
    assert params["limit"] == "5"


# --- wired through the app: 401 / 403 -------------------------------------------
#
# Everything above exercises handler logic; this section exercises the
# dependency wiring itself — no credentials is 401, a non-admin role is 403 —
# via a TestClient over the real app with app.state fakes. No lifespan: the
# state is populated directly, the same seam the lifespan uses.


class FakeSessionRepo:
    def __init__(self, sessions: dict[str, tuple[User, str]] | None = None) -> None:
        self._sessions = sessions or {}

    async def resolve(self, session_id: str):
        return self._sessions.get(session_id)


def _client(*, session_repo=None) -> TestClient:
    app.state.settings = SimpleNamespace(session_cookie_name="prahari_session")
    app.state.session_repo = session_repo or FakeSessionRepo()
    app.state.api_key_repo = FakeApiKeyRepo()
    app.state.user_repo = FakeUserRepo({"u9": TARGET_USER})
    app.state.pool = FakePool({"org-ward-9": "gj.ahmedabad_city.zone_4.ward_9"})
    # health-history returns a list upstream — the `-> list` annotation on the
    # handler means FastAPI validates the forwarded body.
    app.state.registry = FakeRegistry(200, [])
    app.state.match_engine = FakeMatchEngine(200, [])
    app.state.scope_resolver = FakeScopeResolver()
    app.state.audit = FakeAudit()
    return TestClient(app, raise_server_exceptions=True)


def test_admin_endpoints_require_authentication():
    client = _client()
    for method, path in [
        ("GET", "/api/v1/auth/users"),
        ("POST", "/api/v1/auth/users/u9/disable"),
        ("POST", "/api/v1/auth/users/u9/enable"),
        ("GET", "/api/v1/auth/api-keys"),
        ("POST", "/api/v1/auth/api-keys/k9/revoke"),
        ("POST", "/api/v1/sync"),
        ("GET", "/api/v1/sync/runs"),
        ("GET", "/api/v1/watchlist/summary"),
        ("POST", "/api/v1/watchlist/reload"),
        ("GET", "/api/v1/alerts"),
        ("GET", "/api/v1/cameras/cam-1/health-history"),
    ]:
        response = client.request(method, path)
        assert response.status_code == 401, f"{method} {path} -> {response.status_code}"


def test_admin_endpoints_reject_non_admin_roles():
    viewer = User(id="u3", username="v", org_id="org-zone4", role=Role.VIEWER)
    client = _client(session_repo=FakeSessionRepo({"sess-v": (viewer, "gj.ahmedabad_city.zone_4")}))
    client.cookies.set("prahari_session", "sess-v")
    for method, path in [
        ("GET", "/api/v1/auth/users"),
        ("POST", "/api/v1/auth/users/u9/disable"),
        ("GET", "/api/v1/auth/api-keys"),
        ("POST", "/api/v1/auth/api-keys/k9/revoke"),
        ("POST", "/api/v1/sync"),
        ("GET", "/api/v1/sync/runs"),
        ("GET", "/api/v1/watchlist/summary"),
        ("POST", "/api/v1/watchlist/reload"),
    ]:
        response = client.request(method, path)
        assert response.status_code == 403, f"{method} {path} -> {response.status_code}"


def test_alerts_and_health_history_allow_non_admin_authenticated_roles():
    viewer = User(id="u3", username="v", org_id="org-zone4", role=Role.VIEWER)
    client = _client(session_repo=FakeSessionRepo({"sess-v": (viewer, "gj.ahmedabad_city.zone_4")}))
    client.cookies.set("prahari_session", "sess-v")
    assert client.get("/api/v1/alerts").status_code == 200
    assert client.get("/api/v1/cameras/cam-1/health-history").status_code == 200


def test_admin_session_can_list_users_and_disable_one():
    admin = User(id="u2", username="a", org_id="org-zone4", role=Role.ADMIN)
    client = _client(session_repo=FakeSessionRepo({"sess-a": (admin, "gj.ahmedabad_city.zone_4")}))
    client.cookies.set("prahari_session", "sess-a")
    response = client.get("/api/v1/auth/users")
    assert response.status_code == 200
    assert response.json()[0]["username"] == "viewer.ward9"
    assert "password_hash" not in response.json()[0]

    response = client.post("/api/v1/auth/users/u9/disable")
    assert response.status_code == 200
    assert response.json()["disabled_at"] is not None
    assert app.state.audit.entries[-1]["action"] == "user_disable"
