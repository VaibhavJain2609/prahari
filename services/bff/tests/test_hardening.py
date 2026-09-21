"""The hardening pass, exercised at the handler/middleware level with the
same fake-repos-and-SimpleNamespace style as test_auth.py — no database, no
TestClient: the invariant under test is what the handler does (audit before
serve, fail closed, deny api-key minting), not the framework wiring around
it.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException, Response

import prahari_bff.app as app_module
from prahari_bff.app import (
    _DUMMY_PASSWORD_HASH,
    alerts_stream,
    create_api_key,
    create_camera,
    create_user,
    export_route,
    get_camera,
    get_route,
    import_cameras,
    login,
    origin_and_security_headers,
    verify_audit,
)
from prahari_bff.models import (
    ApiKeyCreate,
    ApiKeyPurpose,
    LoginRequest,
    Principal,
    Role,
    User,
    UserCreate,
)
from prahari_bff.security import SlidingWindowRateLimiter, hash_password

OPERATOR = Principal(
    id="u1",
    subject="ops.zone4",
    org_id="org-zone4",
    org_path="gj.ahmedabad_city.zone_4",
    role=Role.OPERATOR,
    kind="session",
)
ADMIN = Principal(
    id="u0",
    subject="root.admin",
    org_id="org-root",
    org_path="gj",
    role=Role.ADMIN,
    kind="session",
)
KEY_ADMIN = Principal(
    id="k1",
    subject="vendor-admin-key",
    org_id="org-root",
    org_path="gj",
    role=Role.ADMIN,
    kind="api_key",
)


class FakeAudit:
    def __init__(self, *, fail: bool = False) -> None:
        self.entries: list[dict] = []
        self._fail = fail

    async def append(self, **kwargs):
        if self._fail:
            raise RuntimeError("audit db is gone")
        self.entries.append(kwargs)
        return kwargs

    async def verify(self):
        return True, None

    async def head(self):
        return "deadbeef" * 8, len(self.entries)

    async def recent(self, limit=100, **filters):
        return []


class FakeRegistry:
    def __init__(self, status_code: int = 200, body: dict | None = None) -> None:
        self._response = httpx.Response(status_code, json=body or {})

    async def get(self, path: str, params: dict | None = None) -> httpx.Response:
        return self._response

    async def post(self, path: str, json: dict | None = None) -> httpx.Response:
        return self._response

    async def patch(self, path: str, json: dict | None = None) -> httpx.Response:
        return self._response

    async def delete(self, path: str) -> httpx.Response:
        return self._response


class FakeCorrelation:
    def __init__(self, route: dict | None = None, error: Exception | None = None) -> None:
        self._route = route or {"plate": "GJ01AB1234", "hops": []}
        self._error = error

    async def get_route(self, plate: str) -> dict:
        if self._error is not None:
            raise self._error
        return self._route


def _upstream_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://correlation/api/v1/routes/X")
    response = httpx.Response(status_code, json={"detail": "no route"}, request=request)
    return httpx.HTTPStatusError(str(status_code), request=request, response=response)


class FakeScopeResolver:
    def __init__(self, org_path: str | None = "gj.ahmedabad_city.zone_4") -> None:
        self._org_path = org_path

    async def org_path_for_camera(self, camera_id: str) -> str | None:
        return self._org_path


class FakePool:
    def __init__(self, paths: dict[str, str] | None = None) -> None:
        self._paths = paths or {}

    async def fetchval(self, query: str, *args):
        return self._paths.get(args[0]) if args else None


def _request(*, pool=None, user_repo=None, session_repo=None, login_limiter=None, client=None):
    return SimpleNamespace(
        client=client,
        app=SimpleNamespace(
            state=SimpleNamespace(
                pool=pool or FakePool(),
                user_repo=user_repo,
                session_repo=session_repo,
                login_limiter=login_limiter or SlidingWindowRateLimiter(10, 60.0),
                settings=SimpleNamespace(
                    session_cookie_name="prahari_session",
                    session_ttl_hours=12,
                    session_cookie_secure=False,
                    sse_max_connections=2,
                    redis_url=None,
                    alert_stream_key="prahari:alerts",
                    login_ip_rate_limit_attempts=120,
                ),
            )
        ),
    )


def _json_request(body: dict, **kwargs):
    request = _request(**kwargs)

    async def _json():
        return body

    request.json = _json
    return request


# --- audit-before-serve ---------------------------------------------------


async def test_route_read_is_audited_before_the_response_is_served():
    audit = FakeAudit()
    result = await get_route("GJ01AB1234", OPERATOR, "case-1", FakeCorrelation(), audit)
    assert result["plate"] == "GJ01AB1234"
    assert audit.entries[-1]["action"] == "read"
    assert audit.entries[-1]["resource"] == "route:GJ01AB1234"


async def test_route_upstream_404_is_audited_as_read_failed_not_read():
    audit = FakeAudit()
    with pytest.raises(HTTPException) as exc:
        await get_route(
            "GJ01XX9999",
            OPERATOR,
            "case-1",
            FakeCorrelation(error=_upstream_error(404)),
            audit,
        )
    assert exc.value.status_code == 404
    assert [e["action"] for e in audit.entries] == ["read_failed"]


async def test_failing_audit_append_fails_the_read_closed():
    """The invariant: no response is served without a successful append. A
    broken audit backend is a 500, never a silently unlogged 200."""
    audit = FakeAudit(fail=True)
    with pytest.raises(HTTPException) as exc:
        await get_route("GJ01AB1234", OPERATOR, "case-1", FakeCorrelation(), audit)
    assert exc.value.status_code == 500


async def test_camera_detail_denied_scope_is_audited_before_403():
    audit = FakeAudit()
    resolver = FakeScopeResolver(org_path="gj.surat_city")  # outside caller's subtree
    with pytest.raises(HTTPException) as exc:
        await get_camera("cam-9", OPERATOR, "case-1", FakeRegistry(), resolver, audit)
    assert exc.value.status_code == 403
    assert [e["action"] for e in audit.entries] == ["denied"]


async def test_camera_detail_upstream_error_is_read_failed():
    audit = FakeAudit()
    with pytest.raises(HTTPException) as exc:
        await get_camera(
            "cam-9", OPERATOR, "case-1", FakeRegistry(status_code=500), FakeScopeResolver(), audit
        )
    assert exc.value.status_code == 500
    assert [e["action"] for e in audit.entries] == ["read_failed"]


async def test_export_filename_quotes_the_plate():
    audit = FakeAudit()
    response = await export_route(
        'GJ01"INJECT\r\nX- Bad', OPERATOR, "case-1", FakeCorrelation(), audit, "csv"
    )
    disposition = response.headers["content-disposition"]
    assert '"' not in disposition.split('filename="', 1)[1].rstrip('"')
    assert "%22" in disposition
    assert "\r" not in disposition and "\n" not in disposition
    assert [e["action"] for e in audit.entries] == ["export:csv"]


# --- admin/config auditing ------------------------------------------------


async def test_create_user_writes_an_admin_audit_entry():
    audit = FakeAudit()

    class FakeUserRepo:
        async def create(self, payload):
            return User(
                id="u9", username=payload.username, org_id=payload.org_id, role=payload.role
            )

    request = _request(
        pool=FakePool({"org-ward-9": "gj.ahmedabad_city.zone_4.ward_9"}), user_repo=FakeUserRepo()
    )
    payload = UserCreate(
        username="viewer.ward9", password="long-enough-password", org_id="org-ward-9", role="viewer"
    )
    user = await create_user(payload, OPERATOR_ADMIN(), audit, request)
    assert user.username == "viewer.ward9"
    assert audit.entries[-1]["action"] == "user_create"
    assert audit.entries[-1]["purpose_code"] == "admin"


def OPERATOR_ADMIN() -> Principal:
    return Principal(
        id="u2",
        subject="admin.zone4",
        org_id="org-zone4",
        org_path="gj.ahmedabad_city.zone_4",
        role=Role.ADMIN,
        kind="session",
    )


async def test_create_user_denied_scope_is_audited():
    audit = FakeAudit()
    request = _request(pool=FakePool({"org-zone5": "gj.ahmedabad_city.zone_5"}))
    payload = UserCreate(
        username="x", password="long-enough-password", org_id="org-zone5", role="viewer"
    )
    with pytest.raises(HTTPException) as exc:
        await create_user(payload, OPERATOR_ADMIN(), audit, request)
    assert exc.value.status_code == 403
    assert audit.entries[-1]["action"] == "user_create_denied"


async def test_api_key_principal_cannot_create_users():
    audit = FakeAudit()
    request = _request()
    payload = UserCreate(
        username="x", password="long-enough-password", org_id="org-root", role="viewer"
    )
    with pytest.raises(HTTPException) as exc:
        await create_user(payload, KEY_ADMIN, audit, request)
    assert exc.value.status_code == 403
    assert audit.entries[-1]["action"] == "user_create_denied"


async def test_api_key_principal_cannot_mint_more_api_keys():
    """An admin-purpose API key minting more admin keys would be a
    self-renewing root of trust — sessions only."""
    audit = FakeAudit()
    request = _request()
    payload = ApiKeyCreate(
        org_id="org-root",
        role=Role.ADMIN,
        purpose=ApiKeyPurpose.INTERNAL_SERVICE,
        label="second-key",
    )
    with pytest.raises(HTTPException) as exc:
        await create_api_key(payload, KEY_ADMIN, audit, request)
    assert exc.value.status_code == 403
    assert audit.entries[-1]["action"] == "api_key_create_denied"


async def test_session_admin_can_mint_api_keys_with_created_by_set():
    audit = FakeAudit()

    class FakeApiKeyRepo:
        def __init__(self):
            self.created_by = "unset"

        async def create(self, payload, *, created_by):
            self.created_by = created_by
            from prahari_bff.models import ApiKey

            return (
                ApiKey(
                    id="k9",
                    org_id=payload.org_id,
                    role=payload.role,
                    purpose=payload.purpose,
                    label=payload.label,
                ),
                "pk_plaintext",
            )

    repo = FakeApiKeyRepo()
    request = _request(pool=FakePool({"org-root": "gj"}))
    request.app.state.api_key_repo = repo
    payload = ApiKeyCreate(
        org_id="org-root",
        role=Role.VIEWER,
        purpose=ApiKeyPurpose.VENDOR_ADAPTER,
        label="vendor-y",
    )
    result = await create_api_key(payload, ADMIN, audit, request)
    assert result.plaintext == "pk_plaintext"
    assert repo.created_by == "u0"  # session principal id, no more NULL
    assert audit.entries[-1]["action"] == "api_key_create"


async def test_create_camera_writes_an_admin_audit_entry():
    audit = FakeAudit()
    registry = FakeRegistry(status_code=201, body={"id": "cam-9"})
    request = _json_request({"external_id": "cam-9"})
    result = await create_camera(OPERATOR, registry, audit, request)
    assert result["id"] == "cam-9"
    assert audit.entries[-1]["action"] == "camera_create"
    assert audit.entries[-1]["resource"] == "camera:cam-9"
    assert audit.entries[-1]["purpose_code"] == "admin"


async def test_csv_import_writes_an_intent_then_outcome_audit_entry():
    """Two entries per import, not one per row: `camera_import_requested`
    before any row is created upstream, `camera_import` with the row counts
    once the batch settles."""
    audit = FakeAudit()
    registry = FakeRegistry(status_code=201, body={"id": "cam-1"})

    async def _body():
        return b"external_id,site_name\ncam-1,Alpha\ncam-2,Beta\n"

    async def _stream():
        yield b"external_id,site_name\ncam-1,Alpha\ncam-2,Beta\n"

    request = _request()
    request.headers = {}
    request.body = _body
    request.stream = _stream
    result = await import_cameras(OPERATOR, registry, audit, request)
    assert result["succeeded"] == 2
    assert [e["action"] for e in audit.entries] == ["camera_import_requested", "camera_import"]
    assert audit.entries[0]["resource"] == "cameras-import:2"
    assert audit.entries[-1]["resource"] == "cameras-import:2/2"


async def test_verify_response_carries_head_and_row_count():
    result = await verify_audit(ADMIN, FakeAudit())
    assert result["ok"] is True
    assert result["head_hash"] == "deadbeef" * 8
    assert result["row_count"] == 0


# --- login hardening --------------------------------------------------------


class FakeUserRepo:
    def __init__(self, resolved=None):
        self._resolved = resolved

    async def get_by_username_with_hash(self, username: str):
        return self._resolved


class FakeSessionRepo:
    async def create(self, user_id: str, *, ttl_hours: int):
        from datetime import UTC, datetime, timedelta

        return "cookie-value", datetime.now(UTC) + timedelta(hours=ttl_hours)


async def test_login_unknown_user_still_runs_password_verify(monkeypatch):
    """The timing-oracle fix: "no such user" must cost the same argon2
    verification as "wrong password" — verified by spying on the call."""
    calls = []

    def spy(password: str, password_hash: str) -> bool:
        calls.append((password, password_hash))
        return False

    monkeypatch.setattr(app_module, "verify_password", spy)
    request = _request(user_repo=FakeUserRepo(resolved=None))
    with pytest.raises(HTTPException) as exc:
        await login(LoginRequest(username="ghost", password="pw"), request, Response())
    assert exc.value.status_code == 401
    assert calls == [("pw", _DUMMY_PASSWORD_HASH)]


async def test_login_known_user_wrong_password_is_401(monkeypatch):
    monkeypatch.setattr(app_module, "verify_password", lambda pw, h: False)
    user = User(id="u1", username="ops", org_id="org-zone4", role=Role.OPERATOR)
    request = _request(user_repo=FakeUserRepo(resolved=(user, hash_password("x"))))
    with pytest.raises(HTTPException) as exc:
        await login(LoginRequest(username="ops", password="pw"), request, Response())
    assert exc.value.status_code == 401


async def test_login_rate_limit_returns_429():
    limiter = SlidingWindowRateLimiter(max_attempts=2, window_s=60.0)
    request = _request(
        user_repo=FakeUserRepo(resolved=None),
        login_limiter=limiter,
        client=SimpleNamespace(host="10.0.0.1"),
    )
    payload = LoginRequest(username="ops", password="pw")
    for _ in range(2):
        with pytest.raises(HTTPException) as exc:
            await login(payload, request, Response())
        assert exc.value.status_code == 401
    with pytest.raises(HTTPException) as exc:
        await login(payload, request, Response())
    assert exc.value.status_code == 429


# --- middleware -------------------------------------------------------------


def _mw_request(method: str, headers: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(method=method, headers=headers)


async def _next(request):
    return Response(content=b"ok")


async def test_mutating_request_with_foreign_origin_is_403():
    request = _mw_request("POST", {"origin": "https://evil.example", "host": "prahari.example"})
    response = await origin_and_security_headers(request, _next)
    assert response.status_code == 403


async def test_mutating_request_with_matching_origin_passes():
    request = _mw_request("POST", {"origin": "https://prahari.example", "host": "prahari.example"})
    response = await origin_and_security_headers(request, _next)
    assert response.status_code == 200


async def test_mutating_request_without_origin_passes():
    """curl/services don't send Origin — the check is presence-triggered."""
    request = _mw_request("POST", {"host": "prahari.example"})
    response = await origin_and_security_headers(request, _next)
    assert response.status_code == 200


async def test_get_requests_are_not_origin_checked():
    request = _mw_request("GET", {"origin": "https://evil.example", "host": "prahari.example"})
    response = await origin_and_security_headers(request, _next)
    assert response.status_code == 200


async def test_security_headers_are_stamped_on_every_response():
    for method in ("GET", "POST"):
        request = _mw_request(method, {"host": "prahari.example"})
        response = await origin_and_security_headers(request, _next)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["content-security-policy"] == "default-src 'self'"


# --- SSE bound ---------------------------------------------------------------


async def test_alerts_stream_rejects_beyond_the_connection_cap():
    request = _request()
    request.app.state.settings.redis_url = "redis://localhost:6379/0"
    request.app.state.scope_resolver = FakeScopeResolver()
    request.app.state.sse_active = 2  # == sse_max_connections
    with pytest.raises(HTTPException) as exc:
        await alerts_stream(OPERATOR, request)
    assert exc.value.status_code == 429


async def test_alerts_stream_slot_is_released_when_the_generator_closes():
    request = _request()
    request.app.state.settings.redis_url = "redis://localhost:6379/0"
    request.app.state.scope_resolver = FakeScopeResolver()
    request.app.state.sse_active = 0

    async def _disconnected():
        return True

    request.is_disconnected = _disconnected
    response = await alerts_stream(OPERATOR, request)
    assert request.app.state.sse_active == 1
    # Drive the body iterator once: the loop sees the disconnect and exits,
    # the finally releases the slot and closes the (never-connected) client.
    async for _ in response.body_iterator:
        pass
    assert request.app.state.sse_active == 0
