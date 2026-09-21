"""Coverage pass over app.py's remaining branches — everything the earlier
suites never drove: bootstrap seeding, the lifespan, probes, the login/logout
happy paths, the admin 404/denial branches, the plain proxy getters, the CSV
import's per-row error shapes, upstream-failure forwarding on the route and
export endpoints, alert ack, the SSE relay's failure and filter paths, the
audit reads, and the unauthenticated `/auth/mode` probe.

Same house style as test_hardening.py / test_admin_endpoints.py: handlers
called directly against fakes and `SimpleNamespace` requests, no database.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import asyncpg
import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response
from prahari.v1 import events_pb2

import prahari_bff.app as app_module
from prahari_bff.app import (
    _IMPORT_MAX_BODY_BYTES,
    _audit_event,
    _audit_mutation,
    _data_error_handler,
    _forward_json,
    _read_bounded_body,
    _redact_url_credentials,
    _seed_bootstrap_admin,
    _upstream_detail,
    acknowledge_alert,
    alerts_stream,
    app,
    audit_head,
    auth_mode,
    cameras_geojson,
    cameras_summary,
    create_api_key,
    create_org,
    create_user,
    decommission_camera,
    disable_user,
    export_route,
    gaps_dark_zones,
    gaps_districts,
    gaps_nearest,
    get_audit_log,
    get_camera,
    get_correlation_client,
    get_match_engine_client,
    get_registry_client,
    get_route,
    get_scope_resolver,
    healthz,
    import_cameras,
    lifespan,
    list_alerts,
    list_audit_entries,
    list_orgs,
    login,
    logout,
    me,
    probe_camera,
    readyz,
    revoke_api_key,
    update_camera,
)
from prahari_bff.audit import AuditEntry
from prahari_bff.config import BFFSettings
from prahari_bff.models import (
    ApiKey,
    ApiKeyCreate,
    ApiKeyPurpose,
    LoginRequest,
    Principal,
    Role,
    User,
    UserCreate,
)
from prahari_bff.security import SlidingWindowRateLimiter, hash_password

ADMIN = Principal(
    id="u0",
    subject="root.admin",
    org_id="org-root",
    org_path="gj",
    role=Role.ADMIN,
    kind="session",
)
ADMIN_ZONE4 = Principal(
    id="u2",
    subject="admin.zone4",
    org_id="org-zone4",
    org_path="gj.ahmedabad_city.zone_4",
    role=Role.ADMIN,
    kind="session",
)
OPERATOR = Principal(
    id="u1",
    subject="ops.zone4",
    org_id="org-zone4",
    org_path="gj.ahmedabad_city.zone_4",
    role=Role.OPERATOR,
    kind="session",
)


class FakeAudit:
    """`fail_on` names the actions whose append raises — lets a test break
    the outcome append while the intent append still lands."""

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.entries: list[dict] = []
        self._fail_on = fail_on or set()

    async def append(self, **kwargs):
        if kwargs.get("action") in self._fail_on or "*" in self._fail_on:
            raise RuntimeError("audit db is gone")
        self.entries.append(kwargs)
        return kwargs

    async def verify(self):
        return True, None

    async def head(self):
        return "deadbeef" * 8, len(self.entries)

    async def recent(self, limit=100, **filters):
        return [
            AuditEntry(
                id=1,
                actor="ops.zone4",
                org_path="gj.ahmedabad_city.zone_4",
                purpose_code="case-1",
                resource="camera:cam-1",
                action="read",
                occurred_at="2026-09-01T00:00:00+00:00",
                prev_hash="0" * 64,
                hash="a" * 64,
            )
        ][:limit]


class FakeRegistry:
    def __init__(self, status_code: int = 200, body=None, *, text: str | None = None) -> None:
        self._status = status_code
        self._body = body
        self._text = text
        self.calls: list[tuple] = []

    def _response(self) -> httpx.Response:
        if self._text is not None:
            return httpx.Response(self._status, text=self._text)
        return httpx.Response(self._status, json=self._body if self._body is not None else {})

    async def get(self, path: str, params: dict | None = None) -> httpx.Response:
        self.calls.append(("GET", path, params))
        return self._response()

    async def post(self, path: str, json: dict | None = None) -> httpx.Response:
        self.calls.append(("POST", path, json))
        return self._response()

    async def patch(self, path: str, json: dict | None = None) -> httpx.Response:
        self.calls.append(("PATCH", path, json))
        return self._response()

    async def delete(self, path: str) -> httpx.Response:
        self.calls.append(("DELETE", path, None))
        return self._response()


class ExplodingRegistry(FakeRegistry):
    """POST dies mid-call — the mid-batch-abort shape for the CSV import."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def post(self, path: str, json: dict | None = None) -> httpx.Response:
        raise self._error


class FakeMatchEngine(FakeRegistry):
    pass


class FakeCorrelation:
    def __init__(self, route: dict | None = None, error: Exception | None = None) -> None:
        self._route = route or {"plate": "GJ01AB1234", "hops": []}
        self._error = error

    async def get_route(self, plate: str) -> dict:
        if self._error is not None:
            raise self._error
        return self._route


def _status_error(status_code: int, body=None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://correlation/api/v1/routes/X")
    response = httpx.Response(
        status_code, json=body if body is not None else {"detail": "upstream"}, request=request
    )
    return httpx.HTTPStatusError(str(status_code), request=request, response=response)


class FakeScopeResolver:
    def __init__(
        self, camera_orgs: dict[str, str | None] | None = None, org_path: str | None = None
    ) -> None:
        self._camera_orgs = camera_orgs or {}
        self._org_path = org_path

    async def org_path_for_camera(self, camera_id: str) -> str | None:
        if self._camera_orgs:
            return self._camera_orgs.get(camera_id)
        return self._org_path


class FakePool:
    """org_id -> org_path for fetchval; `error` simulates a driver failure."""

    def __init__(self, paths: dict[str, str] | None = None, error: Exception | None = None) -> None:
        self._paths = paths or {}
        self._error = error
        self.closed = False

    async def fetchval(self, query: str, *args):
        if self._error is not None:
            raise self._error
        return self._paths.get(args[0]) if args else None

    async def close(self):
        self.closed = True


class FakeUserRepo:
    def __init__(self, users: dict[str, User] | None = None, resolved=None) -> None:
        self._users = users or {}
        self._resolved = resolved
        self.created: list = []
        self.disabled_calls: list = []
        self._count = len(self._users)

    async def count(self) -> int:
        return self._count

    async def create(self, payload):
        self.created.append(payload)
        user = User(id="u-new", username=payload.username, org_id=payload.org_id, role=payload.role)
        self._users[user.id] = user
        self._count += 1
        return user

    async def get_by_username_with_hash(self, username: str):
        return self._resolved

    async def get(self, user_id: str):
        return self._users.get(user_id)

    async def set_disabled(self, user_id: str, *, disabled: bool):
        self.disabled_calls.append((user_id, disabled))
        user = self._users.get(user_id)
        if user is None:
            return None
        return user.model_copy(update={"disabled_at": datetime.now(UTC) if disabled else None})


class FakeSessionRepo:
    def __init__(self, sessions: dict[str, tuple[User, str]] | None = None) -> None:
        self._sessions = sessions or {}
        self.revoked: list[str] = []
        self.created_for: str | None = None

    async def create(self, user_id: str, *, ttl_hours: int):
        from datetime import timedelta

        self.created_for = user_id
        return "cookie-value", datetime.now(UTC) + timedelta(hours=ttl_hours)

    async def resolve(self, session_id: str):
        return self._sessions.get(session_id)

    async def revoke(self, session_id: str) -> None:
        self.revoked.append(session_id)


class FakeApiKeyRepo:
    def __init__(self, keys: dict[str, ApiKey] | None = None, *, revoke_result=None) -> None:
        self._keys = keys or {}
        self._revoke_result = revoke_result
        self.revoked: list[str] = []

    async def get(self, key_id: str):
        return self._keys.get(key_id)

    async def revoke(self, key_id: str):
        self.revoked.append(key_id)
        if self._revoke_result is not None:
            return self._revoke_result
        key = self._keys.get(key_id)
        return None if key is None else key.model_copy(update={"revoked_at": datetime.now(UTC)})


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        session_cookie_name="prahari_session",
        session_ttl_hours=12,
        session_cookie_secure=False,
        sse_max_connections=2,
        redis_url=None,
        alert_stream_key="prahari:alerts",
        login_ip_rate_limit_attempts=120,
        media_ticket_ttl_s=60,
        media_whep_base_url="http://localhost:8889",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _request(
    *,
    pool=None,
    user_repo=None,
    session_repo=None,
    api_key_repo=None,
    audit=None,
    login_limiter=None,
    scope_resolver=None,
    client=None,
    cookies=None,
    settings=None,
) -> SimpleNamespace:
    state = SimpleNamespace(
        pool=pool or FakePool(),
        user_repo=user_repo or FakeUserRepo(),
        session_repo=session_repo or FakeSessionRepo(),
        api_key_repo=api_key_repo or FakeApiKeyRepo(),
        login_limiter=login_limiter or SlidingWindowRateLimiter(10, 60.0),
        settings=settings or _settings(),
        scope_resolver=scope_resolver or FakeScopeResolver(),
        sse_active=0,
    )
    if audit is not None:
        state.audit = audit
    return SimpleNamespace(
        cookies=cookies or {},
        client=client,
        app=SimpleNamespace(state=state),
    )


def _json_request(body: dict, **kwargs) -> SimpleNamespace:
    request = _request(**kwargs)

    async def _json():
        return body

    request.json = _json
    return request


# --- bootstrap seeding ---------------------------------------------------------


async def test_bootstrap_admin_is_a_noop_unless_both_credentials_are_set():
    repo = FakeUserRepo()
    await _seed_bootstrap_admin(FakePool(), SimpleNamespace(bootstrap_admin_username=None), repo)
    await _seed_bootstrap_admin(
        FakePool(),
        SimpleNamespace(bootstrap_admin_username="root", bootstrap_admin_password=None),
        repo,
    )
    assert repo.created == []


async def test_bootstrap_admin_does_not_run_once_users_exist():
    repo = FakeUserRepo({"u1": User(id="u1", username="a", org_id="o", role=Role.ADMIN)})
    settings = SimpleNamespace(
        bootstrap_admin_username="root",
        bootstrap_admin_password="secret-pw",
        bootstrap_admin_org_path="gj",
    )
    await _seed_bootstrap_admin(FakePool(), settings, repo)
    assert repo.created == []


async def test_bootstrap_admin_warns_and_returns_when_the_seed_org_is_missing():
    repo = FakeUserRepo()
    settings = SimpleNamespace(
        bootstrap_admin_username="root",
        bootstrap_admin_password="secret-pw",
        bootstrap_admin_org_path="gj",
    )
    await _seed_bootstrap_admin(FakePool(paths={}), settings, repo)
    assert repo.created == []


async def test_bootstrap_admin_creates_the_first_admin_at_the_seed_org():
    repo = FakeUserRepo()
    settings = SimpleNamespace(
        bootstrap_admin_username="root",
        bootstrap_admin_password="secret-pw",
        bootstrap_admin_org_path="gj",
    )
    await _seed_bootstrap_admin(FakePool(paths={"gj": "org-root"}), settings, repo)
    assert len(repo.created) == 1
    created = repo.created[0]
    assert created.username == "root"
    assert created.org_id == "org-root"
    assert created.role == Role.ADMIN


async def test_bootstrap_admin_seed_writes_auth_admin_seeded():
    """The lifespan now builds the audit log BEFORE the seed runs, so the
    first account a deployment ever creates lands in the hash chain."""
    repo = FakeUserRepo()
    settings = SimpleNamespace(
        bootstrap_admin_username="root",
        bootstrap_admin_password="secret-pw",
        bootstrap_admin_org_path="gj",
    )
    audit = FakeAudit()
    await _seed_bootstrap_admin(FakePool(paths={"gj": "org-root"}), settings, repo, audit=audit)
    assert [e["action"] for e in audit.entries] == ["auth_admin_seeded"]
    assert audit.entries[0]["actor"] == "root"
    assert audit.entries[0]["purpose_code"] == "auth"


async def test_bootstrap_admin_seed_survives_a_failed_audit_append():
    """Best-effort by design: the users row is itself the durable record of
    the seed, so a dead audit store is logged, never fatal to boot."""
    repo = FakeUserRepo()
    settings = SimpleNamespace(
        bootstrap_admin_username="root",
        bootstrap_admin_password="secret-pw",
        bootstrap_admin_org_path="gj",
    )
    audit = FakeAudit(fail_on={"*"})
    await _seed_bootstrap_admin(FakePool(paths={"gj": "org-root"}), settings, repo, audit=audit)
    assert len(repo.created) == 1


# --- lifespan -----------------------------------------------------------------


async def test_lifespan_wires_state_and_closes_it(monkeypatch, tmp_path):
    settings = BFFSettings(audit_db_path=str(tmp_path / "audit.db"), session_cookie_secure=False)
    fake_pool = FakePool()
    closed = []

    async def fake_create_pool(s):
        return fake_pool

    monkeypatch.setattr(app_module, "bff_settings", lambda: settings)
    monkeypatch.setattr(app_module, "create_pool", fake_create_pool)
    # The real registry/match/correlation clients are real httpx clients —
    # nothing connects during construction, and the finally block closes them.
    marker = SimpleNamespace(state=SimpleNamespace())
    async with lifespan(marker):
        state = marker.state
        assert state.pool is fake_pool
        assert state.settings is settings
        assert state.sse_active == 0
        # Prove the teardown half ran by closing the scope over it.
        closed.append(state.registry)
    assert fake_pool.closed is True


# --- probes & plumbing ----------------------------------------------------------


async def test_healthz():
    assert await healthz() == {"status": "ok", "service": "bff"}


async def test_readyz_ok_and_db_error():
    ok_request = _request()
    ok_response = Response()
    assert await readyz(ok_request, ok_response) == {"status": "ready", "database": "ok"}

    bad_request = _request(pool=FakePool(error=RuntimeError("db gone")))
    bad_response = Response()
    result = await readyz(bad_request, bad_response)
    assert bad_response.status_code == 503
    assert result == {"status": "unavailable", "database": "error"}


async def test_data_error_handler_maps_driver_errors_to_422():
    response = await _data_error_handler(SimpleNamespace(), asyncpg.DataError("bad"))
    assert response.status_code == 422


def test_dependency_getters_read_app_state():
    state = SimpleNamespace(
        registry="reg", correlation="corr", match_engine="me", scope_resolver="sr", audit="audit"
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    assert get_registry_client(request) == "reg"
    assert get_correlation_client(request) == "corr"
    assert get_match_engine_client(request) == "me"
    assert get_scope_resolver(request) == "sr"
    assert get_audit_log(request) == "audit"


# --- login / logout / me ---------------------------------------------------------


async def test_login_success_sets_the_session_cookie_and_audits(monkeypatch):
    monkeypatch.setattr(app_module, "verify_password", lambda pw, h: True)
    user = User(id="u1", username="ops", org_id="org-zone4", role=Role.OPERATOR)
    audit = FakeAudit()
    request = _request(
        user_repo=FakeUserRepo(resolved=(user, hash_password("x"))),
        session_repo=FakeSessionRepo(),
        pool=FakePool({"org-zone4": "gj.ahmedabad_city.zone_4"}),
        audit=audit,
    )
    response = Response()
    result = await login(LoginRequest(username="ops", password="pw"), request, response)
    assert result.id == "u1"
    cookie = response.headers["set-cookie"]
    assert "prahari_session=cookie-value" in cookie
    assert [e["action"] for e in audit.entries] == ["auth_login"]
    assert audit.entries[0]["org_path"] == "gj.ahmedabad_city.zone_4"


async def test_login_denied_writes_an_auth_event_when_audit_exists():
    """A failed login is itself an audit entry; an audit append that fails
    is logged and swallowed, never masking the 401."""
    audit = FakeAudit()
    request = _request(user_repo=FakeUserRepo(resolved=None), audit=audit)
    with pytest.raises(HTTPException) as exc:
        await login(LoginRequest(username="ghost", password="pw"), request, Response())
    assert exc.value.status_code == 401
    assert audit.entries[0]["action"] == "auth_login_denied"

    broken = FakeAudit(fail_on={"*"})
    request = _request(user_repo=FakeUserRepo(resolved=None), audit=broken)
    with pytest.raises(HTTPException) as exc:
        await login(LoginRequest(username="ghost", password="pw"), request, Response())
    assert exc.value.status_code == 401


async def test_audit_event_is_a_noop_without_an_audit_log():
    request = _request()
    await _audit_event(request, actor="a", org_path="-", resource="r", action="x")  # no raise


async def test_login_per_ip_limit_returns_429():
    request = _request(
        user_repo=FakeUserRepo(resolved=None),
        client=SimpleNamespace(host="10.0.0.1"),
        settings=_settings(login_ip_rate_limit_attempts=1),
    )
    payload = LoginRequest(username="ops", password="pw")
    with pytest.raises(HTTPException) as exc:
        await login(payload, request, Response())
    assert exc.value.status_code == 401  # first attempt consumes the ip bucket
    with pytest.raises(HTTPException) as exc:
        await login(LoginRequest(username="someone-else", password="pw"), request, Response())
    assert exc.value.status_code == 429


async def test_logout_revokes_and_audits_a_live_session():
    user = User(id="u1", username="ops", org_id="org-zone4", role=Role.OPERATOR)
    session_repo = FakeSessionRepo({"sess-1": (user, "gj.ahmedabad_city.zone_4")})
    audit = FakeAudit()
    request = _request(
        session_repo=session_repo, cookies={"prahari_session": "sess-1"}, audit=audit
    )
    response = Response()
    assert await logout(request, response) == {"status": "ok"}
    assert session_repo.revoked == ["sess-1"]
    assert audit.entries[0]["action"] == "auth_logout"
    assert "prahari_session" in response.headers["set-cookie"]


async def test_logout_with_an_unresolvable_or_absent_session_still_clears_the_cookie():
    session_repo = FakeSessionRepo()
    request = _request(session_repo=session_repo, cookies={"prahari_session": "sess-x"})
    assert await logout(request, Response()) == {"status": "ok"}
    assert session_repo.revoked == ["sess-x"]

    request = _request(session_repo=session_repo, cookies={})
    assert await logout(request, Response()) == {"status": "ok"}
    assert session_repo.revoked == ["sess-x"]


# --- fail-closed auth events (M3) --------------------------------------------------
#
# Successful auth mutations — a session minted or revoked — must never
# commit unaudited. A failed append is a 500 BEFORE the mutation, not a
# logged warning beside a committed one. Denied attempts keep the lenient
# `_audit_event` (covered by test_login_denied_writes_an_auth_event...).


async def test_login_with_a_failed_audit_append_500s_and_mints_no_session(monkeypatch):
    monkeypatch.setattr(app_module, "verify_password", lambda pw, h: True)
    user = User(id="u1", username="ops", org_id="org-zone4", role=Role.OPERATOR)
    session_repo = FakeSessionRepo()
    request = _request(
        user_repo=FakeUserRepo(resolved=(user, hash_password("x"))),
        session_repo=session_repo,
        pool=FakePool({"org-zone4": "gj.ahmedabad_city.zone_4"}),
        audit=FakeAudit(fail_on={"*"}),
    )
    with pytest.raises(HTTPException) as exc:
        await login(LoginRequest(username="ops", password="pw"), request, Response())
    assert exc.value.status_code == 500
    assert session_repo.created_for is None  # no session without its audit row


async def test_login_with_no_audit_log_at_all_is_fail_closed(monkeypatch):
    """`app.state.audit` absent (broken lifespan) is not a silent skip on the
    success path — it is the same 500 as a failed append."""
    monkeypatch.setattr(app_module, "verify_password", lambda pw, h: True)
    user = User(id="u1", username="ops", org_id="org-zone4", role=Role.OPERATOR)
    session_repo = FakeSessionRepo()
    request = _request(  # deliberately no audit on state
        user_repo=FakeUserRepo(resolved=(user, hash_password("x"))),
        session_repo=session_repo,
        pool=FakePool({"org-zone4": "gj.ahmedabad_city.zone_4"}),
    )
    with pytest.raises(HTTPException) as exc:
        await login(LoginRequest(username="ops", password="pw"), request, Response())
    assert exc.value.status_code == 500
    assert session_repo.created_for is None


async def test_logout_with_a_failed_audit_append_500s_and_keeps_the_session():
    """Revoking unaudited is the same hole as minting unaudited: the session
    stays alive rather than dying unrecorded."""
    user = User(id="u1", username="ops", org_id="org-zone4", role=Role.OPERATOR)
    session_repo = FakeSessionRepo({"sess-1": (user, "gj.ahmedabad_city.zone_4")})
    request = _request(
        session_repo=session_repo,
        cookies={"prahari_session": "sess-1"},
        audit=FakeAudit(fail_on={"*"}),
    )
    with pytest.raises(HTTPException) as exc:
        await logout(request, Response())
    assert exc.value.status_code == 500
    assert session_repo.revoked == []


async def test_logout_auth_row_lands_before_the_revoke_commits(monkeypatch):
    """Ordering, not just presence: at the instant the session is revoked
    the `auth_logout` row must already exist."""
    user = User(id="u1", username="ops", org_id="org-zone4", role=Role.OPERATOR)
    audit = FakeAudit()
    seen_at_revoke: list[list[str]] = []

    class RecordingSessions(FakeSessionRepo):
        async def revoke(self, session_id: str):
            seen_at_revoke.append([e["action"] for e in audit.entries])
            await super().revoke(session_id)

    session_repo = RecordingSessions({"sess-1": (user, "gj.ahmedabad_city.zone_4")})
    request = _request(
        session_repo=session_repo, cookies={"prahari_session": "sess-1"}, audit=audit
    )
    assert await logout(request, Response()) == {"status": "ok"}
    assert seen_at_revoke == [["auth_logout"]]


async def test_me_returns_the_principal():
    assert await me(OPERATOR) is OPERATOR


# --- admin 404 / denial branches ------------------------------------------------


async def test_create_user_unknown_target_org_is_404():
    payload = UserCreate(
        username="x", password="long-enough-password", org_id="ghost", role="viewer"
    )
    with pytest.raises(HTTPException) as exc:
        await create_user(payload, ADMIN, FakeAudit(), _request(pool=FakePool({})))
    assert exc.value.status_code == 404


async def test_create_api_key_unknown_org_and_scope_denial():
    payload = ApiKeyCreate(
        org_id="ghost", role=Role.VIEWER, purpose=ApiKeyPurpose.VENDOR_ADAPTER, label="k"
    )
    with pytest.raises(HTTPException) as exc:
        await create_api_key(payload, ADMIN, FakeAudit(), _request(pool=FakePool({})))
    assert exc.value.status_code == 404

    audit = FakeAudit()
    payload = ApiKeyCreate(
        org_id="org-zone5",
        role=Role.VIEWER,
        purpose=ApiKeyPurpose.VENDOR_ADAPTER,
        label="k",
    )
    request = _request(pool=FakePool({"org-zone5": "gj.ahmedabad_city.zone_5"}))
    with pytest.raises(HTTPException) as exc:
        await create_api_key(payload, ADMIN_ZONE4, audit, request)
    assert exc.value.status_code == 403
    assert audit.entries[-1]["action"] == "api_key_create_denied"


async def test_disable_user_unknown_org_and_vanished_user_are_404():
    target = User(id="u9", username="t", org_id="org-gone", role=Role.VIEWER)
    request = _request(
        pool=FakePool({}),  # org lookup misses
        user_repo=FakeUserRepo({"u9": target}),
    )
    with pytest.raises(HTTPException) as exc:
        await disable_user("u9", ADMIN, FakeAudit(), request)
    assert exc.value.status_code == 404

    # The row was there for `get` but the write raced a delete.
    repo = FakeUserRepo({"u9": target})
    repo.set_disabled = lambda *a, **k: _none()  # noqa: E731

    async def _none():
        return None

    request = _request(pool=FakePool({"org-gone": "gj"}), user_repo=repo)
    with pytest.raises(HTTPException) as exc:
        await disable_user("u9", ADMIN, FakeAudit(), request)
    assert exc.value.status_code == 404


async def test_revoke_api_key_unknown_org_and_vanished_key_are_404():
    key = ApiKey(
        id="k9",
        org_id="org-gone",
        role=Role.VIEWER,
        purpose=ApiKeyPurpose.VENDOR_ADAPTER,
        label="k",
    )
    request = _request(pool=FakePool({}), api_key_repo=FakeApiKeyRepo({"k9": key}))
    with pytest.raises(HTTPException) as exc:
        await revoke_api_key("k9", ADMIN, FakeAudit(), request)
    assert exc.value.status_code == 404

    repo = FakeApiKeyRepo({"k9": key}, revoke_result=None)
    repo.revoke = lambda *a, **k: _none()  # noqa: E731

    async def _none():
        return None

    request = _request(pool=FakePool({"org-gone": "gj"}), api_key_repo=repo)
    with pytest.raises(HTTPException) as exc:
        await revoke_api_key("k9", ADMIN, FakeAudit(), request)
    assert exc.value.status_code == 404


async def test_audit_mutation_logs_loudly_when_the_failed_append_also_fails():
    """Mutation raised AND the `*_failed` append can't be written — the
    original exception still propagates, the audit loss is only logged."""
    audit = FakeAudit(fail_on={"x_failed"})

    async def _boom():
        raise RuntimeError("mutation died")

    with pytest.raises(RuntimeError, match="mutation died"):
        await _audit_mutation(
            audit,
            OPERATOR,
            purpose_code="admin",
            resource="r",
            action="x",
            mutation=_boom,
        )
    assert [e["action"] for e in audit.entries] == ["x_requested"]


# --- orgs ------------------------------------------------------------------


async def test_list_orgs_forwards_the_caller_scope():
    registry = FakeRegistry(200, [{"id": "org-1"}])
    result = await list_orgs(OPERATOR, registry)
    assert result == [{"id": "org-1"}]
    assert registry.calls == [("GET", "/api/v1/orgs", {"scope": "gj.ahmedabad_city.zone_4"})]


async def test_create_org_defaults_parent_to_the_callers_own_org():
    audit = FakeAudit()
    registry = FakeRegistry(201, {"id": "org-new"})
    request = _json_request({"label": "Ward 9"}, pool=FakePool())
    result = await create_org(ADMIN_ZONE4, registry, audit, request)
    assert result["id"] == "org-new"
    _, path, body = registry.calls[0]
    assert path == "/api/v1/orgs"
    assert body["parent_id"] == "org-zone4"  # the caller's org, not nothing
    assert [e["action"] for e in audit.entries] == ["org_create_requested", "org_create"]


async def test_create_org_with_out_of_scope_parent_is_denied_and_audited():
    audit = FakeAudit()
    registry = FakeRegistry(201, {"id": "org-new"})
    request = _json_request(
        {"label": "Ward 9", "parent_id": "org-zone5"},
        pool=FakePool({"org-zone5": "gj.surat_city"}),
    )
    with pytest.raises(HTTPException) as exc:
        await create_org(ADMIN_ZONE4, registry, audit, request)
    assert exc.value.status_code == 403
    assert registry.calls == []  # the upstream create never ran
    assert [e["action"] for e in audit.entries] == ["org_create_denied"]


# --- upstream detail extraction ---------------------------------------------------


def test_upstream_detail_handles_non_json_and_shapeless_bodies():
    assert _upstream_detail(httpx.Response(500, text="proxy exploded")) == "proxy exploded"
    assert _upstream_detail(httpx.Response(500, json=[1, 2])) == "[1,2]"
    assert _upstream_detail(httpx.Response(404, json={"detail": "nope"})) == "nope"
    with pytest.raises(HTTPException) as exc:
        _forward_json(httpx.Response(502, text="bad gateway"))
    assert exc.value.status_code == 502
    assert exc.value.detail == "bad gateway"


# --- plain proxy getters --------------------------------------------------------


async def test_scoped_read_proxies_forward_the_callers_scope():
    registry = FakeRegistry(200, {"k": "v"})
    request = _request()
    request.query_params = {}
    assert await cameras_summary(OPERATOR, registry, request) == {"k": "v"}
    assert await cameras_geojson(OPERATOR, registry, request) == {"k": "v"}

    registry_list = FakeRegistry(200, [])
    assert await gaps_districts(OPERATOR, registry_list, request) == []
    assert await gaps_dark_zones(OPERATOR, registry_list, request) == []
    assert await gaps_nearest(OPERATOR, registry_list, request) == []
    paths = [c[1] for c in registry_list.calls]
    assert paths == [
        "/api/v1/gaps/districts",
        "/api/v1/gaps/dark-zones",
        "/api/v1/gaps/nearest",
    ]
    assert registry_list.calls[0][2]["org_scope"] == "gj.ahmedabad_city.zone_4"


async def test_get_camera_unknown_camera_is_404():
    resolver = FakeScopeResolver(org_path=None)
    with pytest.raises(HTTPException) as exc:
        await get_camera("cam-x", OPERATOR, "case-1", FakeRegistry(), resolver, FakeAudit())
    assert exc.value.status_code == 404


# --- camera update / decommission / probe -----------------------------------------


async def test_update_camera_happy_path_and_org_reassign_check():
    audit = FakeAudit()
    registry = FakeRegistry(200, {"id": "cam-1", "endpoints": {"x": "y"}})
    resolver = FakeScopeResolver(org_path="gj.ahmedabad_city.zone_4")
    request = _json_request(
        {"site_name": "New", "org_id": "org-ward9"},
        pool=FakePool({"org-ward9": "gj.ahmedabad_city.zone_4.ward_9"}),
    )
    result = await update_camera("cam-1", OPERATOR, registry, resolver, audit, request)
    assert result == {"id": "cam-1"}  # endpoints stripped
    _, path, body = registry.calls[0]
    assert path == "/api/v1/cameras/cam-1"
    assert body["org_id"] == "org-ward9"
    assert [e["action"] for e in audit.entries] == ["camera_update_requested", "camera_update"]


async def test_update_camera_404_and_scope_denial():
    audit = FakeAudit()
    resolver = FakeScopeResolver(org_path=None)
    with pytest.raises(HTTPException) as exc:
        await update_camera("cam-x", OPERATOR, FakeRegistry(), resolver, audit, _json_request({}))
    assert exc.value.status_code == 404

    resolver = FakeScopeResolver(org_path="gj.surat_city")
    with pytest.raises(HTTPException) as exc:
        await update_camera("cam-9", OPERATOR, FakeRegistry(), resolver, audit, _json_request({}))
    assert exc.value.status_code == 403
    assert audit.entries[-1]["action"] == "camera_update_denied"


async def test_update_camera_reassign_to_out_of_scope_org_is_denied():
    audit = FakeAudit()
    resolver = FakeScopeResolver(org_path="gj.ahmedabad_city.zone_4")
    request = _json_request(
        {"org_id": "org-zone5"},
        pool=FakePool({"org-zone5": "gj.surat_city"}),
    )
    with pytest.raises(HTTPException) as exc:
        await update_camera("cam-1", OPERATOR, FakeRegistry(), resolver, audit, request)
    assert exc.value.status_code == 403
    assert [e["action"] for e in audit.entries] == ["camera_update_denied"]


async def test_decommission_camera_404_and_scope_denial():
    audit = FakeAudit()
    with pytest.raises(HTTPException) as exc:
        await decommission_camera("cam-x", OPERATOR, FakeRegistry(), FakeScopeResolver(), audit)
    assert exc.value.status_code == 404

    resolver = FakeScopeResolver(org_path="gj.surat_city")
    with pytest.raises(HTTPException) as exc:
        await decommission_camera("cam-9", OPERATOR, FakeRegistry(), resolver, audit)
    assert exc.value.status_code == 403
    assert audit.entries[-1]["action"] == "camera_delete_denied"


async def test_probe_camera_audits_and_forwards_upstream_errors():
    audit = FakeAudit()
    registry = FakeRegistry(200, {"reachable": True})
    request = _json_request({"rtsp_url": "rtsp://10.0.0.1/cam1"})
    result = await probe_camera(OPERATOR, "case-1", registry, audit, request)
    assert result == {"reachable": True}
    assert [e["action"] for e in audit.entries] == ["probe_requested", "probe"]
    assert audit.entries[0]["purpose_code"] == "case-1"
    assert audit.entries[0]["resource"] == "camera-probe:rtsp://10.0.0.1/cam1"

    audit = FakeAudit()
    registry = FakeRegistry(422, {"detail": "ssrf blocked"})
    with pytest.raises(HTTPException) as exc:
        await probe_camera(OPERATOR, "case-1", registry, audit, request)
    assert exc.value.status_code == 422
    assert audit.entries[-1]["action"] == "probe_failed"


async def test_probe_audit_resource_redacts_credentials_and_query():
    """The L1 finding: the audit `resource` used to carry the raw
    `rtsp://user:pass@host/...` verbatim, pinning operator credentials into
    the immutable hash chain. The upstream probe still gets the real URL —
    only the log's copy is redacted."""
    audit = FakeAudit()
    registry = FakeRegistry(200, {"reachable": True})
    url = "rtsp://operator:s3cret@10.0.0.1:554/cam1?token=abc123"
    request = _json_request({"rtsp_url": url})
    result = await probe_camera(OPERATOR, "case-1", registry, audit, request)
    assert result == {"reachable": True}

    resources = [e["resource"] for e in audit.entries]
    assert resources == ["camera-probe:rtsp://10.0.0.1:554/cam1"] * 2
    for entry in audit.entries:
        assert "s3cret" not in entry["resource"]
        assert "operator@" not in entry["resource"]
        assert "token" not in entry["resource"]
    # The credential-bearing form is still what the registry probes.
    assert registry.calls[0][2]["rtsp_url"] == url


async def test_probe_audit_resource_handles_a_nonstring_rtsp_url():
    audit = FakeAudit()
    registry = FakeRegistry(200, {"reachable": False})
    request = _json_request({"rtsp_url": {"unexpected": "shape"}})
    await probe_camera(OPERATOR, "case-1", registry, audit, request)
    assert audit.entries[0]["resource"] == "camera-probe:<unparsable>"


def test_redact_url_credentials_unit_cases():
    assert _redact_url_credentials("rtsp://u:p@10.0.0.5:554/ch1") == "rtsp://10.0.0.5:554/ch1"
    # Query strings drop entirely — DVRs accept ?username=&password= auth.
    assert _redact_url_credentials("rtsp://u:p@dvr/live?x=1") == "rtsp://dvr/live"
    # Credential-free URLs pass through untouched.
    assert _redact_url_credentials("rtsp://10.0.0.5/ch") == "rtsp://10.0.0.5/ch"
    # A malformed port degrades to host-only, never a raise from the audit path.
    assert _redact_url_credentials("rtsp://u:p@dvr:abc/ch") == "rtsp://dvr/ch"
    # IPv6 literals get their brackets back (hostname strips them).
    assert _redact_url_credentials("rtsp://u:p@[fd00::1]:8554/ch") == "rtsp://[fd00::1]:8554/ch"


# --- CSV import edge cases ---------------------------------------------------------


def _csv_request(raw: bytes, headers: dict | None = None, **kwargs) -> SimpleNamespace:
    request = _request(**kwargs)
    request.headers = headers or {}

    async def _body():
        return raw

    async def _stream():
        yield raw

    request.body = _body
    request.stream = _stream
    return request


async def test_import_without_an_external_id_column_is_400():
    request = _csv_request(b"site_name,district\nAlpha,AMD\n")
    with pytest.raises(HTTPException) as exc:
        await import_cameras(OPERATOR, FakeRegistry(), FakeAudit(), request)
    assert exc.value.status_code == 400


async def test_import_row_with_a_blank_external_id_is_a_row_error_not_a_crash():
    result = await import_cameras(
        OPERATOR,
        FakeRegistry(201, {"id": "x"}),
        FakeAudit(),
        _csv_request(b"external_id,site_name\n,NoID\ncam-1,Alpha\n"),
    )
    assert result["total"] == 2
    assert result["succeeded"] == 1
    assert result["rows"][0] == {"row": 2, "ok": False, "error": "external_id is required"}
    assert result["rows"][1]["ok"] is True


async def test_import_row_scoped_outside_the_callers_subtree_is_denied_per_row():
    """An out-of-scope org_id in one row is a per-row failure plus a
    `camera_import_denied` audit entry — the batch itself still completes."""
    audit = FakeAudit()
    registry = FakeRegistry(201, {"id": "cam-1"})
    request = _csv_request(
        b"external_id,org_id\ncam-1,org-zone5\n",
        pool=FakePool({"org-zone5": "gj.surat_city"}),
    )
    result = await import_cameras(OPERATOR, registry, audit, request)
    assert result["failed"] == 1
    assert registry.calls == []  # the upstream create never ran
    actions = [e["action"] for e in audit.entries]
    assert actions[0] == "camera_import_requested"
    assert "camera_import_denied" in actions
    assert actions[-1] == "camera_import"


async def test_import_row_with_a_bad_numeric_field_is_a_row_error():
    result = await import_cameras(
        OPERATOR,
        FakeRegistry(201, {"id": "cam-1"}),
        FakeAudit(),
        _csv_request(b"external_id,native_width\ncam-1,not-a-number\n"),
    )
    assert result["failed"] == 1
    assert result["rows"][0]["ok"] is False


async def test_import_row_upstream_rejection_is_a_row_error_with_the_detail():
    registry = FakeRegistry(422, {"detail": "external_id already exists"})
    result = await import_cameras(
        OPERATOR,
        registry,
        FakeAudit(),
        _csv_request(b"external_id\ncam-dup\n"),
    )
    assert result["failed"] == 1
    assert result["rows"][0]["error"] == "external_id already exists"


async def test_import_abort_mid_batch_is_audited_failed_even_if_that_append_fails():
    """The registry connection dying mid-batch raises out of the per-row
    handlers; the batch-failed audit append is best-effort and must not mask
    the original error."""
    audit = FakeAudit(fail_on={"camera_import_failed"})
    registry = ExplodingRegistry(RuntimeError("connection dropped"))
    request = _csv_request(b"external_id\ncam-1\n")
    with pytest.raises(RuntimeError, match="connection dropped"):
        await import_cameras(OPERATOR, registry, audit, request)
    assert [e["action"] for e in audit.entries] == ["camera_import_requested"]


async def test_import_abort_mid_batch_writes_camera_import_failed():
    audit = FakeAudit()
    registry = ExplodingRegistry(RuntimeError("connection dropped"))
    request = _csv_request(b"external_id\ncam-1\n")
    with pytest.raises(RuntimeError):
        await import_cameras(OPERATOR, registry, audit, request)
    actions = [e["action"] for e in audit.entries]
    assert actions == ["camera_import_requested", "camera_import_failed"]


async def test_import_body_over_the_size_cap_is_413():
    """The L5 bound: the body is refused before any CSV parsing or upstream
    call — a multi-megabyte POST is not an import, it is a memory attack."""
    audit = FakeAudit()
    registry = FakeRegistry(201, {"id": "x"})
    big = b"external_id,site_name\n" + b"cam-x," + b"y" * _IMPORT_MAX_BODY_BYTES
    request = _csv_request(big)
    with pytest.raises(HTTPException) as exc:
        await import_cameras(OPERATOR, registry, audit, request)
    assert exc.value.status_code == 413
    assert registry.calls == []
    assert audit.entries == []  # refused before the intent row too


async def test_import_declared_content_length_over_the_cap_is_413():
    """A truthfully-declared oversized body is refused on the header alone —
    the streaming bound stays as the backstop for chunked/lying lengths."""
    request = _csv_request(
        b"external_id\ncam-1\n",
        headers={"content-length": str(_IMPORT_MAX_BODY_BYTES + 1)},
    )
    with pytest.raises(HTTPException) as exc:
        await import_cameras(OPERATOR, FakeRegistry(201, {"id": "x"}), FakeAudit(), request)
    assert exc.value.status_code == 413


async def test_bounded_body_allows_exactly_the_cap_and_refuses_one_byte_more():
    """The boundary itself, exercised on the helper: exactly `max_bytes` is
    a body, one byte more is a 413."""
    at_cap = _csv_request(b"x" * _IMPORT_MAX_BODY_BYTES)
    assert len(await _read_bounded_body(at_cap, max_bytes=_IMPORT_MAX_BODY_BYTES)) == (
        _IMPORT_MAX_BODY_BYTES
    )
    over = _csv_request(b"x" * (_IMPORT_MAX_BODY_BYTES + 1))
    with pytest.raises(HTTPException) as exc:
        await _read_bounded_body(over, max_bytes=_IMPORT_MAX_BODY_BYTES)
    assert exc.value.status_code == 413


# --- routes & export error paths ---------------------------------------------------


async def test_route_correlation_unreachable_is_502_and_audited_read_failed():
    audit = FakeAudit()
    correlation = FakeCorrelation(error=httpx.ConnectError("refused"))
    with pytest.raises(HTTPException) as exc:
        await get_route("GJ01AB1234", OPERATOR, "case-1", correlation, audit)
    assert exc.value.status_code == 502
    assert [e["action"] for e in audit.entries] == ["read_failed"]


async def test_export_upstream_status_error_forwards_and_audits_read_failed():
    audit = FakeAudit()
    correlation = FakeCorrelation(error=_status_error(404, {"detail": "no route"}))
    with pytest.raises(HTTPException) as exc:
        await export_route("GJ01XX9999", OPERATOR, "case-1", correlation, audit, "csv")
    assert exc.value.status_code == 404
    assert [e["action"] for e in audit.entries] == ["read_failed"]


async def test_export_correlation_unreachable_is_502():
    audit = FakeAudit()
    correlation = FakeCorrelation(error=httpx.ConnectError("refused"))
    with pytest.raises(HTTPException) as exc:
        await export_route("GJ01AB1234", OPERATOR, "case-1", correlation, audit, "pdf")
    assert exc.value.status_code == 502
    assert [e["action"] for e in audit.entries] == ["read_failed"]


async def test_export_pdf_renders_and_audits():
    audit = FakeAudit()
    response = await export_route("GJ01AB1234", OPERATOR, "case-1", FakeCorrelation(), audit, "pdf")
    assert response.media_type == "application/pdf"
    assert bytes(response.body).startswith(b"%PDF")
    assert response.headers["content-disposition"].endswith('route-GJ01AB1234.pdf"')
    assert [e["action"] for e in audit.entries] == ["export:pdf"]


# --- alerts: list params + ack ------------------------------------------------------


async def test_list_alerts_forwards_every_filter_param():
    match = FakeMatchEngine(200, [])
    resolver = FakeScopeResolver()
    since = datetime(2026, 9, 1, tzinfo=UTC)
    await list_alerts(
        OPERATOR,
        match,
        resolver,
        25,
        since,
        camera_id="cam-1",
        plate="GJ01",
        acknowledged=False,
    )
    _, path, params = match.calls[0]
    assert path == "/api/v1/alerts"
    assert params == {
        "limit": 25,
        "since": since.isoformat(),
        "camera_id": "cam-1",
        "plate": "GJ01",
        "acknowledged": "false",
    }


async def test_ack_alert_unknown_alert_is_404():
    match = FakeMatchEngine(404, {"detail": "no such alert"})
    with pytest.raises(HTTPException) as exc:
        await acknowledge_alert(
            "a-x", OPERATOR, match, FakeScopeResolver(), FakeAudit(), _request()
        )
    assert exc.value.status_code == 404


async def test_ack_alert_other_upstream_errors_pass_through():
    match = FakeMatchEngine(503, {"detail": "match engine down"})
    with pytest.raises(HTTPException) as exc:
        await acknowledge_alert(
            "a-1", OPERATOR, match, FakeScopeResolver(), FakeAudit(), _request()
        )
    assert exc.value.status_code == 503


async def test_ack_alert_out_of_scope_or_unresolvable_camera_is_denied():
    audit = FakeAudit()
    match = FakeMatchEngine(200, {"alert_id": "a1", "detection": {"camera_id": "cam-out"}})
    resolver = FakeScopeResolver({"cam-out": "gj.surat_city"})
    with pytest.raises(HTTPException) as exc:
        await acknowledge_alert("a1", OPERATOR, match, resolver, audit, _request())
    assert exc.value.status_code == 403
    assert [e["action"] for e in audit.entries] == ["alert_ack_denied"]

    # An alert whose camera cannot be resolved to an org is denied, not
    # leaked — same rule as the list filter.
    audit = FakeAudit()
    match = FakeMatchEngine(200, {"alert_id": "a2", "detection": {}})
    with pytest.raises(HTTPException) as exc:
        await acknowledge_alert("a2", OPERATOR, match, FakeScopeResolver(), audit, _request())
    assert exc.value.status_code == 403
    assert [e["action"] for e in audit.entries] == ["alert_ack_denied"]


async def test_ack_alert_audits_and_proxies_the_ack():
    audit = FakeAudit()

    class AckMatch(FakeMatchEngine):
        def __init__(self):
            super().__init__(200, {"alert_id": "a1", "detection": {"camera_id": "cam-1"}})

        async def post(self, path, json=None):
            self.calls.append(("POST", path, json))
            return httpx.Response(200, json={"alert_id": "a1", "acknowledged": True})

    match = AckMatch()
    resolver = FakeScopeResolver({"cam-1": "gj.ahmedabad_city.zone_4"})
    result = await acknowledge_alert("a1", OPERATOR, match, resolver, audit, _request())
    assert result == {"alert_id": "a1", "acknowledged": True}
    assert match.calls[-1] == ("POST", "/api/v1/alerts/a1/ack", {"by": "ops.zone4"})
    # `_audit_mutation` ordering, same as every other mutation: intent row
    # before the upstream POST, outcome row after it.
    assert [e["action"] for e in audit.entries] == ["alert_ack_requested", "alert_ack"]


async def test_ack_alert_forwards_an_ack_post_failure():
    audit = FakeAudit()

    class AckMatch(FakeMatchEngine):
        def __init__(self):
            super().__init__(200, {"alert_id": "a1", "detection": {"camera_id": "cam-1"}})

        async def post(self, path, json=None):
            return httpx.Response(409, json={"detail": "already acknowledged"})

    resolver = FakeScopeResolver({"cam-1": "gj.ahmedabad_city.zone_4"})
    with pytest.raises(HTTPException) as exc:
        await acknowledge_alert("a1", OPERATOR, AckMatch(), resolver, audit, _request())
    assert exc.value.status_code == 409


async def test_ack_alert_upstream_failure_writes_requested_then_failed():
    """The M3 finding: an `alert_ack` row written BEFORE the upstream POST
    means an upstream 500 leaves a success-looking row for a mutation that
    never happened. `_audit_mutation` gives the honest pair instead."""
    audit = FakeAudit()

    class AckMatch(FakeMatchEngine):
        def __init__(self):
            super().__init__(200, {"alert_id": "a1", "detection": {"camera_id": "cam-1"}})

        async def post(self, path, json=None):
            return httpx.Response(500, json={"detail": "match engine exploded"})

    resolver = FakeScopeResolver({"cam-1": "gj.ahmedabad_city.zone_4"})
    with pytest.raises(HTTPException) as exc:
        await acknowledge_alert("a1", OPERATOR, AckMatch(), resolver, audit, _request())
    assert exc.value.status_code == 500
    assert [e["action"] for e in audit.entries] == ["alert_ack_requested", "alert_ack_failed"]


# --- SSE relay ---------------------------------------------------------------------


async def test_alerts_stream_is_503_without_a_redis_url():
    request = _request()
    with pytest.raises(HTTPException) as exc:
        await alerts_stream(OPERATOR, request)
    assert exc.value.status_code == 503


async def test_alerts_stream_releases_the_slot_when_redis_connect_fails(monkeypatch):
    def _boom(url):
        raise RuntimeError("bad redis url")

    monkeypatch.setattr(app_module.redis_lib.Redis, "from_url", staticmethod(_boom))
    request = _request(settings=_settings(redis_url="redis://broken"))
    with pytest.raises(RuntimeError, match="bad redis url"):
        await alerts_stream(OPERATOR, request)
    assert request.app.state.sse_active == 0


class FakeRedis:
    """xread returns the scripted stream entries once, then nothing; `close`
    records that the finally block ran."""

    def __init__(self, entries=None):
        self._entries = entries or []
        self._served = False
        self.closed = False

    def xread(self, streams, count=None, block=None):
        if self._served:
            return []
        self._served = True
        return [(b"prahari:alerts", self._entries)]

    def close(self):
        self.closed = True


async def test_alerts_stream_relays_in_scope_alerts_and_filters_the_rest(monkeypatch):
    in_scope = events_pb2.Alert(alert_id="a-in")
    in_scope.detection.camera_id = "cam-in"
    out_of_scope = events_pb2.Alert(alert_id="a-out")
    out_of_scope.detection.camera_id = "cam-out"
    fake_redis = FakeRedis(
        [
            (b"1-0", {b"alert": in_scope.SerializeToString()}),
            (b"2-0", {b"alert": out_of_scope.SerializeToString()}),
        ]
    )
    monkeypatch.setattr(
        app_module.redis_lib.Redis, "from_url", staticmethod(lambda url: fake_redis)
    )
    request = _request(
        settings=_settings(redis_url="redis://localhost:6379/0"),
        scope_resolver=FakeScopeResolver(
            {"cam-in": "gj.ahmedabad_city.zone_4", "cam-out": "gj.surat_city"}
        ),
    )
    # Disconnect after the first poll so the loop terminates.
    calls = {"n": 0}

    async def _is_disconnected():
        calls["n"] += 1
        return calls["n"] > 1

    request.is_disconnected = _is_disconnected
    response = await alerts_stream(OPERATOR, request)
    assert response.media_type == "text/event-stream"
    assert request.app.state.sse_active == 1

    chunks = [chunk async for chunk in response.body_iterator]
    assert len(chunks) == 1
    payload = json.loads(chunks[0].split("data: ", 1)[1])
    assert payload["alert_id"] == "a-in" or payload["alertId"] == "a-in"
    # The slot was released and the redis client closed by the finally block.
    assert request.app.state.sse_active == 0
    assert fake_redis.closed is True


async def test_alerts_stream_cancellation_mid_stream_cleans_up(monkeypatch):
    """The browser hung up between polls: CancelledError delivered at the
    yield point is swallowed by the generator (the disconnect is the normal
    end of an SSE stream, not an error) and the finally block still frees
    the connection slot and the redis client."""
    in_scope = events_pb2.Alert(alert_id="a-in")
    in_scope.detection.camera_id = "cam-in"
    fake_redis = FakeRedis([(b"1-0", {b"alert": in_scope.SerializeToString()})])
    monkeypatch.setattr(
        app_module.redis_lib.Redis, "from_url", staticmethod(lambda url: fake_redis)
    )
    request = _request(
        settings=_settings(redis_url="redis://localhost:6379/0"),
        scope_resolver=FakeScopeResolver({"cam-in": "gj.ahmedabad_city.zone_4"}),
    )

    async def _connected():
        return False

    request.is_disconnected = _connected
    response = await alerts_stream(OPERATOR, request)

    agen = response.body_iterator
    chunk = await agen.__anext__()
    assert "a-in" in chunk
    with pytest.raises(StopAsyncIteration):
        await agen.athrow(asyncio.CancelledError())
    assert request.app.state.sse_active == 0
    assert fake_redis.closed is True


# --- audit reads --------------------------------------------------------------------


async def test_list_audit_entries_and_head():
    audit = FakeAudit()
    entries = await list_audit_entries(ADMIN, audit, 10, 0, "ops.zone4", "read", None)
    assert entries[0]["actor"] == "ops.zone4"
    assert entries[0]["prev_hash"] == "0" * 64

    head = await audit_head(ADMIN, audit)
    assert head == {"head_hash": "deadbeef" * 8, "row_count": 0}


# --- auth mode ----------------------------------------------------------------------


async def test_auth_mode_reports_keycloak_when_oidc_is_enabled():
    request = _request(settings=BFFSettings(oidc_enabled=True))
    result = await auth_mode(request)
    assert result == {
        "kind": "keycloak",
        "sso_login_url": "/api/bff/auth/oidc/login",
    }


async def test_auth_mode_reports_builtin_and_survives_missing_settings():
    request = _request(settings=BFFSettings(oidc_enabled=False))
    assert await auth_mode(request) == {"kind": "builtin", "sso_login_url": None}

    bare = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    assert await auth_mode(bare) == {"kind": "builtin", "sso_login_url": None}


def test_bff_settings_factory_returns_the_settings_class():
    from prahari_bff.config import bff_settings

    assert isinstance(bff_settings(), BFFSettings)


def test_app_is_wired():
    assert isinstance(app, FastAPI)
