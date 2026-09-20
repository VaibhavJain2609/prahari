"""The OIDC flow, exercised at the handler level in the house style —
fake repos and `SimpleNamespace` requests, no TestClient, no database.

The "IdP" is `httpx.MockTransport` serving a token endpoint and a JWKS
document for a real throwaway RSA keypair (pyjwt[crypto] is a real dep, so
tests sign id_tokens with the same library the BFF verifies with). Issuer and
internal URL are deliberately different so the public-iss / cluster-internal
split is exercised, not just asserted.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Response
from jwt.algorithms import RSAAlgorithm

from prahari_bff.app import oidc_callback, oidc_login, oidc_logout
from prahari_bff.config import BFFSettings
from prahari_bff.models import Role, User
from prahari_bff.oidc import (
    OIDC_MARKER_COOKIE_NAME,
    STATE_COOKIE_NAME,
    OidcClient,
    map_realm_role,
    safe_next,
    validate_org_path_claim,
)

ISSUER = "https://sso.example.gov.in/realms/prahari"
INTERNAL = "http://prahari-keycloak:8080/realms/prahari"
REDIRECT_BASE = "http://localhost:3000"

SETTINGS = BFFSettings(
    oidc_enabled=True,
    oidc_issuer_url=ISSUER,
    oidc_internal_url=INTERNAL,
    oidc_client_id="prahari-bff",
    oidc_client_secret="test-secret",
    oidc_redirect_base=REDIRECT_BASE,
    session_cookie_secure=False,
)

DISABLED_SETTINGS = BFFSettings(oidc_enabled=False)

_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks() -> dict:
    jwk = json.loads(RSAAlgorithm.to_jwk(_PRIVATE.public_key()))
    jwk.update({"kid": "test-key", "alg": "RS256", "use": "sig"})
    return {"keys": [jwk]}


def _id_token(
    *,
    key=_PRIVATE,
    kid="test-key",
    iss=ISSUER,
    aud="prahari-bff",
    nonce="",
    roles=("operator",),
    org_path="gj.ahmedabad_city.zone_4",
    username="ops.zone4",
    exp_delta=300,
    azp="prahari-bff",
) -> str:
    now = int(time.time())
    claims = {
        "iss": iss,
        "aud": aud,
        "sub": "kc-sub-1",
        "iat": now,
        "exp": now + exp_delta,
        "preferred_username": username,
        "realm_access": {"roles": list(roles)},
        "azp": azp,
    }
    if nonce:
        claims["nonce"] = nonce
    if org_path is not None:
        claims["org_path"] = org_path
    return pyjwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


class FakeIdP:
    """The token + JWKS endpoints, nothing else. `id_token` is set per test so
    one transport serves both happy-path and adversarial tokens."""

    def __init__(self, id_token: str = "") -> None:
        self.id_token = id_token
        self.token_requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/realms/prahari/protocol/openid-connect/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            return httpx.Response(
                200,
                json={
                    "id_token": self.id_token,
                    "access_token": "at",
                    "token_type": "Bearer",
                },
            )
        if request.url.path == "/realms/prahari/protocol/openid-connect/certs":
            return httpx.Response(200, json=_jwks())
        return httpx.Response(404)


def _oidc(idp: FakeIdP) -> OidcClient:
    return OidcClient(
        SETTINGS, http_client=httpx.AsyncClient(transport=httpx.MockTransport(idp.handler))
    )


class FakeUserRepo:
    def __init__(self, resolved=None) -> None:
        self._resolved = resolved
        self.created = []

    async def get_by_username_with_hash(self, username: str):
        return self._resolved

    async def create(self, payload):
        self.created.append(payload)
        return User(id="u-new", username=payload.username, org_id=payload.org_id, role=payload.role)


class FakeSessionRepo:
    def __init__(self) -> None:
        self.created_for: str | None = None
        self.revoked: list[str] = []

    async def create(self, user_id: str, *, ttl_hours: int):
        self.created_for = user_id
        return "session-cookie", datetime.now(UTC) + timedelta(hours=ttl_hours)

    async def revoke(self, session_id: str) -> None:
        self.revoked.append(session_id)


class FakePool:
    """Two lookups by query shape: org id by ltree path (JIT), org path by id
    (the existing-user consistency check)."""

    def __init__(self, org_ids: dict | None = None, org_paths: dict | None = None) -> None:
        self._org_ids = org_ids or {}
        self._org_paths = org_paths or {}

    async def fetchval(self, query: str, *args):
        if "WHERE path" in query:
            return self._org_ids.get(args[0])
        return self._org_paths.get(args[0])


def _request(
    *,
    oidc: OidcClient | None,
    cookies: dict | None = None,
    user_repo=None,
    session_repo=None,
    pool=None,
    settings=SETTINGS,
):
    return SimpleNamespace(
        cookies=cookies or {},
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=settings,
                oidc=oidc,
                user_repo=user_repo or FakeUserRepo(),
                session_repo=session_repo or FakeSessionRepo(),
                pool=pool or FakePool(),
            )
        ),
    )


def _set_cookie_values(response) -> dict[str, str]:
    values = {}
    for raw in response.raw_headers:
        if raw[0].decode().lower() == "set-cookie":
            pair = raw[1].decode().split(";", 1)[0]
            name, _, value = pair.partition("=")
            values[name] = value
    return values


async def _start_login(oidc: OidcClient, next_path: str = "/"):
    """Drive /oidc/login and return (state, state_cookie_value) — the two
    halves of the CSRF handshake the callback needs."""
    request = _request(oidc=oidc)
    response = await oidc_login(request, next_path)
    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    state = parse_qs(location.query)["state"][0]
    state_cookie = _set_cookie_values(response)[STATE_COOKIE_NAME]
    return state, state_cookie


# --- login ------------------------------------------------------------------


async def test_login_redirects_to_the_authorize_endpoint_with_pkce():
    idp = FakeIdP()
    request = _request(oidc=_oidc(idp))
    response = await oidc_login(request, "/boards/alerts")

    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        f"{ISSUER}/protocol/openid-connect/auth"
    )
    query = parse_qs(location.query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["prahari-bff"]
    assert query["redirect_uri"] == [f"{REDIRECT_BASE}/api/bff/auth/oidc/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0] and query["code_challenge"][0] != "verifier"
    assert query["state"] and query["nonce"] == query["state"]
    assert "openid" in query["scope"][0]

    cookie = response.headers["set-cookie"]
    assert STATE_COOKIE_NAME in cookie
    assert "httponly" in cookie.lower()


async def test_login_sanitizes_open_redirect_next():
    idp = FakeIdP()
    request = _request(oidc=_oidc(idp))
    response = await oidc_login(request, "https://evil.example/phish")
    # The bad `next` is neutralised inside the signed cookie; nothing here
    # may ever become a Location to an off-origin URL.
    state_cookie = _set_cookie_values(response)[STATE_COOKIE_NAME]
    assert "evil.example" not in state_cookie


# --- callback ---------------------------------------------------------------


async def test_callback_happy_path_jit_provisions_and_sets_session():
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc, "/boards")
    idp.id_token = _id_token(nonce=state)

    user_repo = FakeUserRepo(resolved=None)
    session_repo = FakeSessionRepo()
    pool = FakePool(org_ids={"gj.ahmedabad_city.zone_4": "org-zone4-uuid"})
    request = _request(
        oidc=oidc,
        cookies={STATE_COOKIE_NAME: state_cookie},
        user_repo=user_repo,
        session_repo=session_repo,
        pool=pool,
    )
    response = await oidc_callback(request, "auth-code-1", state)

    assert response.status_code == 302
    assert response.headers["location"] == "/boards"
    cookies = _set_cookie_values(response)
    assert cookies["prahari_session"] == "session-cookie"
    assert cookies[OIDC_MARKER_COOKIE_NAME] == "1"
    # consumed — delete_cookie emits an empty (quoted) value with Max-Age=0
    assert cookies[STATE_COOKIE_NAME] in ('""', "")
    assert any(
        b"max-age=0" in raw[1].lower() and STATE_COOKIE_NAME.encode() in raw[1]
        for raw in response.raw_headers
        if raw[0].decode().lower() == "set-cookie"
    )

    # JIT: one user row created, org from the validated claim, role mapped
    # from realm roles, and a password nobody knows.
    assert len(user_repo.created) == 1
    created = user_repo.created[0]
    assert created.username == "ops.zone4"
    assert created.org_id == "org-zone4-uuid"
    assert created.role == Role.OPERATOR
    assert session_repo.created_for == "u-new"

    # The token exchange went server-side with the verifier from the cookie.
    assert len(idp.token_requests) == 1
    form = idp.token_requests[0]
    assert form["grant_type"] == "authorization_code"
    assert form["client_id"] == "prahari-bff"
    assert form["client_secret"] == "test-secret"
    assert form["code_verifier"]
    assert form["redirect_uri"] == f"{REDIRECT_BASE}/api/bff/auth/oidc/callback"


async def test_callback_existing_user_uses_db_role_and_org_not_claims():
    """Postgres is authoritative for established accounts: an IdP claiming
    admin for a user who is a viewer here must not elevate them."""
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce=state, roles=("admin",), org_path=None)

    existing = User(id="u1", username="ops.zone4", org_id="org-zone4-uuid", role=Role.VIEWER)
    user_repo = FakeUserRepo(resolved=(existing, "argon2-hash"))
    session_repo = FakeSessionRepo()
    pool = FakePool(org_paths={"org-zone4-uuid": "gj.ahmedabad_city.zone_4"})
    request = _request(
        oidc=oidc,
        cookies={STATE_COOKIE_NAME: state_cookie},
        user_repo=user_repo,
        session_repo=session_repo,
        pool=pool,
    )
    response = await oidc_callback(request, "auth-code-1", state)
    assert response.status_code == 302
    assert user_repo.created == []  # no provisioning for a known user
    assert session_repo.created_for == "u1"


async def test_callback_existing_user_with_mismatched_org_path_claim_is_denied():
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce=state, org_path="gj.surat_city")

    existing = User(id="u1", username="ops.zone4", org_id="org-zone4-uuid", role=Role.VIEWER)
    request = _request(
        oidc=oidc,
        cookies={STATE_COOKIE_NAME: state_cookie},
        user_repo=FakeUserRepo(resolved=(existing, "h")),
        pool=FakePool(org_paths={"org-zone4-uuid": "gj.ahmedabad_city.zone_4"}),
    )
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code-1", state)
    assert exc.value.status_code == 403


async def test_callback_disabled_existing_user_is_denied():
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce=state, org_path=None)

    existing = User(
        id="u1",
        username="ops.zone4",
        org_id="org-zone4-uuid",
        role=Role.VIEWER,
        disabled_at=datetime.now(UTC),
    )
    request = _request(
        oidc=oidc,
        cookies={STATE_COOKIE_NAME: state_cookie},
        user_repo=FakeUserRepo(resolved=(existing, "h")),
    )
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code-1", state)
    assert exc.value.status_code == 403


async def test_callback_wrong_state_is_400():
    idp = FakeIdP()
    oidc = _oidc(idp)
    _, state_cookie = await _start_login(oidc)
    request = _request(oidc=oidc, cookies={STATE_COOKIE_NAME: state_cookie})
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code-1", "forged-state")
    assert exc.value.status_code == 400
    assert idp.token_requests == []  # rejected before the code exchange


async def test_callback_missing_state_cookie_is_400():
    idp = FakeIdP()
    oidc = _oidc(idp)
    request = _request(oidc=oidc, cookies={})
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code-1", "some-state")
    assert exc.value.status_code == 400


async def test_callback_wrong_audience_is_401():
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce=state, aud="some-other-client")
    request = _request(oidc=oidc, cookies={STATE_COOKIE_NAME: state_cookie})
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code-1", state)
    assert exc.value.status_code == 401


async def test_callback_expired_token_is_401():
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce=state, exp_delta=-600)
    request = _request(oidc=oidc, cookies={STATE_COOKIE_NAME: state_cookie})
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code-1", state)
    assert exc.value.status_code == 401


async def test_callback_bad_signature_is_401():
    """Signed under a different RSA key but presenting our JWKS kid — the
    signature check must fail, not the kid lookup."""
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(key=_OTHER_PRIVATE, nonce=state)
    request = _request(oidc=oidc, cookies={STATE_COOKIE_NAME: state_cookie})
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code-1", state)
    assert exc.value.status_code == 401


async def test_callback_wrong_issuer_is_401():
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce=state, iss="https://attacker.example/realms/prahari")
    request = _request(oidc=oidc, cookies={STATE_COOKIE_NAME: state_cookie})
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code-1", state)
    assert exc.value.status_code == 401


async def test_callback_unknown_org_path_claim_is_denied():
    """JIT with an org_path that names no org — fail closed: an IdP claim
    that matches no real org is a misconfiguration, not a scope."""
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce=state, org_path="gj.no_such_place")

    user_repo = FakeUserRepo(resolved=None)
    request = _request(
        oidc=oidc,
        cookies={STATE_COOKIE_NAME: state_cookie},
        user_repo=user_repo,
        pool=FakePool(org_ids={}),
    )
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code-1", state)
    assert exc.value.status_code == 403
    assert user_repo.created == []


async def test_callback_jit_without_org_path_claim_is_denied():
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce=state, org_path=None)
    request = _request(
        oidc=oidc,
        cookies={STATE_COOKIE_NAME: state_cookie},
        user_repo=FakeUserRepo(resolved=None),
    )
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code-1", state)
    assert exc.value.status_code == 403


async def test_callback_malformed_org_path_claim_is_400():
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce=state, org_path="gj..../../etc")
    request = _request(
        oidc=oidc,
        cookies={STATE_COOKIE_NAME: state_cookie},
        user_repo=FakeUserRepo(resolved=None),
    )
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code-1", state)
    assert exc.value.status_code == 400


async def test_callback_nonce_mismatch_is_401():
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce="not-the-state")
    request = _request(oidc=oidc, cookies={STATE_COOKIE_NAME: state_cookie})
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code-1", state)
    assert exc.value.status_code == 401


# --- logout -------------------------------------------------------------------


async def test_oidc_logout_revokes_session_and_returns_end_session_url():
    idp = FakeIdP()
    oidc = _oidc(idp)
    session_repo = FakeSessionRepo()
    request = _request(
        oidc=oidc,
        session_repo=session_repo,
        cookies={"prahari_session": "sess-1", OIDC_MARKER_COOKIE_NAME: "1"},
    )
    result = await oidc_logout(request, Response())
    assert result["status"] == "ok"
    assert session_repo.revoked == ["sess-1"]
    url = urlparse(result["end_session_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == (f"{ISSUER}/protocol/openid-connect/logout")
    assert parse_qs(url.query)["post_logout_redirect_uri"] == [REDIRECT_BASE]


async def test_oidc_logout_of_a_builtin_session_returns_no_end_session_url():
    idp = FakeIdP()
    oidc = _oidc(idp)
    session_repo = FakeSessionRepo()
    request = _request(
        oidc=oidc,
        session_repo=session_repo,
        cookies={"prahari_session": "sess-1"},
    )
    result = await oidc_logout(request, Response())
    assert result == {"status": "ok"}
    assert session_repo.revoked == ["sess-1"]


# --- disabled -----------------------------------------------------------------


async def test_oidc_routes_are_404_when_disabled():
    request = _request(oidc=None, settings=DISABLED_SETTINGS)
    with pytest.raises(HTTPException) as exc:
        await oidc_login(request, "/")
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "code", "state")
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        await oidc_logout(request, Response())
    assert exc.value.status_code == 404


# --- claim helpers ------------------------------------------------------------


def test_map_realm_role_picks_the_highest_mapped_role():
    assert map_realm_role({"realm_access": {"roles": ["viewer", "admin"]}}) == Role.ADMIN
    assert map_realm_role({"realm_access": {"roles": ["viewer", "operator"]}}) == Role.OPERATOR


def test_map_realm_role_defaults_to_viewer_on_no_match():
    assert map_realm_role({"realm_access": {"roles": ["uma_authorization"]}}) == Role.VIEWER
    assert map_realm_role({}) == Role.VIEWER


def test_validate_org_path_claim_accepts_ltree_and_rejects_garbage():
    assert validate_org_path_claim({"org_path": "gj.ahmedabad_city.zone_4"}) == (
        "gj.ahmedabad_city.zone_4"
    )
    assert validate_org_path_claim({}) is None
    with pytest.raises(HTTPException) as exc:
        validate_org_path_claim({"org_path": "GJ.Bad Path"})
    assert exc.value.status_code == 400


def test_safe_next_rejects_off_origin_values():
    assert safe_next("/boards/alerts") == "/boards/alerts"
    assert safe_next("https://evil.example") == "/"
    assert safe_next("//evil.example") == "/"
    assert safe_next(None) == "/"
    assert safe_next("") == "/"
