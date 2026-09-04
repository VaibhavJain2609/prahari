"""`get_principal` and `require_admin`, exercised against fake session/API-key
repositories in the same house style as `services/registry/tests/test_sync.
py`'s `FakeRepo` — no database, no FastAPI TestClient, just the resolver
logic every downstream handler depends on.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from prahari_bff.auth import get_principal, require_admin, require_operator, require_purpose_code
from prahari_bff.models import ApiKey, ApiKeyPurpose, Principal, Role, User

SOME_USER = User(id="u1", username="ops.zone4", org_id="org-zone4", role=Role.OPERATOR)
SOME_KEY = ApiKey(
    id="k1",
    org_id="org-zone4",
    role=Role.VIEWER,
    purpose=ApiKeyPurpose.VENDOR_ADAPTER,
    label="vendor-x",
)


class FakeSettings:
    session_cookie_name = "prahari_session"


class FakeSessionRepo:
    def __init__(self, sessions: dict[str, tuple[User, str]] | None = None) -> None:
        self._sessions = sessions or {}

    async def resolve(self, session_id: str) -> tuple[User, str] | None:
        return self._sessions.get(session_id)


class FakeApiKeyRepo:
    def __init__(self, keys: dict[str, tuple[ApiKey, str]] | None = None) -> None:
        self._keys = keys or {}

    async def resolve(self, plaintext: str) -> tuple[ApiKey, str] | None:
        return self._keys.get(plaintext)


def make_request(
    *,
    cookies: dict | None = None,
    headers: dict | None = None,
    session_repo: FakeSessionRepo | None = None,
    api_key_repo: FakeApiKeyRepo | None = None,
):
    return SimpleNamespace(
        cookies=cookies or {},
        headers=headers or {},
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=FakeSettings(),
                session_repo=session_repo or FakeSessionRepo(),
                api_key_repo=api_key_repo or FakeApiKeyRepo(),
            )
        ),
    )


async def test_valid_session_cookie_resolves_a_session_principal():
    repo = FakeSessionRepo({"sess-1": (SOME_USER, "gj.ahmedabad_city.zone_4")})
    request = make_request(cookies={"prahari_session": "sess-1"}, session_repo=repo)

    principal = await get_principal(request)

    assert principal.kind == "session"
    assert principal.id == "u1"
    assert principal.subject == "ops.zone4"
    assert principal.org_path == "gj.ahmedabad_city.zone_4"
    assert principal.role == Role.OPERATOR


async def test_valid_bearer_api_key_resolves_an_api_key_principal():
    repo = FakeApiKeyRepo({"pk_abc123": (SOME_KEY, "gj.ahmedabad_city.zone_4")})
    request = make_request(headers={"authorization": "Bearer pk_abc123"}, api_key_repo=repo)

    principal = await get_principal(request)

    assert principal.kind == "api_key"
    assert principal.id == "k1"
    assert principal.subject == "vendor-x"
    assert principal.org_path == "gj.ahmedabad_city.zone_4"
    assert principal.role == Role.VIEWER


async def test_stale_or_revoked_session_cookie_is_401():
    request = make_request(cookies={"prahari_session": "does-not-exist"})
    with pytest.raises(HTTPException) as exc:
        await get_principal(request)
    assert exc.value.status_code == 401


async def test_no_credentials_at_all_is_401():
    request = make_request()
    with pytest.raises(HTTPException) as exc:
        await get_principal(request)
    assert exc.value.status_code == 401


async def test_bearer_token_that_is_not_an_api_key_is_rejected_before_any_lookup():
    """A bearer token in the wrong format (no `pk_` prefix) must fail the
    format check rather than reach `api_key_repo.resolve` — a JWT-looking
    string is not a key to hash and compare."""
    request = make_request(headers={"authorization": "Bearer some.jwt.looking.thing"})
    with pytest.raises(HTTPException) as exc:
        await get_principal(request)
    assert exc.value.status_code == 401


async def test_unknown_api_key_is_401():
    request = make_request(headers={"authorization": "Bearer pk_doesnotexist"})
    with pytest.raises(HTTPException) as exc:
        await get_principal(request)
    assert exc.value.status_code == 401


def test_require_admin_allows_admin_role():
    principal = Principal(
        id="u1", subject="root", org_id="org-root", org_path="gj", role=Role.ADMIN, kind="session"
    )
    assert require_admin(principal) is principal


def test_require_admin_rejects_non_admin_role():
    principal = Principal(
        id="u2",
        subject="viewer",
        org_id="org-root",
        org_path="gj",
        role=Role.VIEWER,
        kind="session",
    )
    with pytest.raises(HTTPException) as exc:
        require_admin(principal)
    assert exc.value.status_code == 403


def test_require_operator_allows_operator_role():
    principal = Principal(
        id="u1",
        subject="ops",
        org_id="org-zone4",
        org_path="gj.ahmedabad_city.zone_4",
        role=Role.OPERATOR,
        kind="session",
    )
    assert require_operator(principal) is principal


def test_require_operator_allows_admin_role_too():
    principal = Principal(
        id="u1", subject="root", org_id="org-root", org_path="gj", role=Role.ADMIN, kind="session"
    )
    assert require_operator(principal) is principal


def test_require_operator_rejects_viewer_role():
    principal = Principal(
        id="u2",
        subject="viewer",
        org_id="org-root",
        org_path="gj",
        role=Role.VIEWER,
        kind="session",
    )
    with pytest.raises(HTTPException) as exc:
        require_operator(principal)
    assert exc.value.status_code == 403


def test_require_purpose_code_accepts_a_present_header():
    assert require_purpose_code("case-2026-0417") == "case-2026-0417"


def test_require_purpose_code_rejects_absent_header():
    with pytest.raises(HTTPException) as exc:
        require_purpose_code(None)
    assert exc.value.status_code == 400


def test_require_purpose_code_rejects_empty_header():
    with pytest.raises(HTTPException) as exc:
        require_purpose_code("")
    assert exc.value.status_code == 400
