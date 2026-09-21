"""The OIDC flow, exercised at the handler level in the house style —
fake repos and `SimpleNamespace` requests, no TestClient, no database.

The "IdP" is `httpx.MockTransport` serving a token endpoint and a JWKS
document for a real throwaway RSA keypair (pyjwt[crypto] is a real dep, so
tests sign id_tokens with the same library the BFF verifies with). Issuer and
internal URL are deliberately different so the public-iss / cluster-internal
split is exercised, not just asserted.
"""

from __future__ import annotations

import hashlib
import hmac
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
    STATE_TTL_S,
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
    sub="kc-sub-1",
    iat_delta=0,
    exp_delta=300,
    azp="prahari-bff",
) -> str:
    now = int(time.time())
    claims = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "iat": now + iat_delta,
        "exp": now + exp_delta,
        "preferred_username": username,
        "realm_access": {"roles": list(roles)},
        "azp": azp,
    }
    if username is None:
        claims.pop("preferred_username")
    if nonce:
        claims["nonce"] = nonce
    if org_path is not None:
        claims["org_path"] = org_path
    headers = {} if kid is None else {"kid": kid}
    return pyjwt.encode(claims, key, algorithm="RS256", headers=headers)


class FakeIdP:
    """The token + JWKS endpoints, nothing else. `id_token` is set per test so
    one transport serves both happy-path and adversarial tokens. The status/
    body overrides script the failure modes (IdP 500s, unreachable transport,
    a JWKS document with a junk key in it)."""

    def __init__(
        self,
        id_token: str = "",
        *,
        token_status: int = 200,
        token_body: dict | None = None,
        jwks_status: int = 200,
        jwks_body: dict | None = None,
        fail: bool = False,
    ) -> None:
        self.id_token = id_token
        self.token_requests: list[dict] = []
        self._token_status = token_status
        self._token_body = token_body
        self._jwks_status = jwks_status
        self._jwks_body = jwks_body
        self._fail = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self._fail:
            raise httpx.ConnectError("idp unreachable")
        if request.url.path == "/realms/prahari/protocol/openid-connect/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            if self._token_status != 200:
                return httpx.Response(self._token_status, json={"error": "invalid_grant"})
            body = self._token_body or {
                "id_token": self.id_token,
                "access_token": "at",
                "token_type": "Bearer",
            }
            return httpx.Response(200, json=body)
        if request.url.path == "/realms/prahari/protocol/openid-connect/certs":
            if self._jwks_status != 200:
                return httpx.Response(self._jwks_status, json={"error": "boom"})
            return httpx.Response(200, json=self._jwks_body or _jwks())
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
    def __init__(self, sessions: dict[str, tuple[User, str]] | None = None) -> None:
        self.created_for: str | None = None
        self.revoked: list[str] = []
        self._sessions = sessions or {}

    async def create(self, user_id: str, *, ttl_hours: int):
        self.created_for = user_id
        return "session-cookie", datetime.now(UTC) + timedelta(hours=ttl_hours)

    async def resolve(self, session_id: str) -> tuple[User, str] | None:
        return self._sessions.get(session_id)

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


def test_safe_next_rejects_control_characters():
    """A `\n` or `\r` in `next` would land in the callback's Location header —
    CR/LF injection territory, so it collapses to `/` like any other oddity."""
    assert safe_next("/ok\r\nLocation: https://evil.example") == "/"
    assert safe_next("/ok\tevil") == "/"


# --- OidcClient construction --------------------------------------------------


def test_oidc_client_requires_issuer_url():
    with pytest.raises(RuntimeError, match="oidc_issuer_url"):
        OidcClient(BFFSettings(oidc_enabled=True, oidc_redirect_base=REDIRECT_BASE))


def test_oidc_client_requires_redirect_base():
    with pytest.raises(RuntimeError, match="oidc_redirect_base"):
        OidcClient(BFFSettings(oidc_enabled=True, oidc_issuer_url=ISSUER))


def test_oidc_client_redirect_uri_and_internal_fallback():
    client = OidcClient(
        BFFSettings(
            oidc_enabled=True,
            oidc_issuer_url=ISSUER + "/",  # trailing slash is stripped
            oidc_redirect_base=REDIRECT_BASE,
        )
    )
    # No internal URL configured: server-to-server calls fall back to the
    # public issuer.
    assert client.redirect_uri == f"{REDIRECT_BASE}/api/bff/auth/oidc/callback"
    assert client._internal == ISSUER


# --- token exchange failures ----------------------------------------------------


async def _callback_after_login(oidc: OidcClient, **request_kwargs):
    """Run the login leg, then the callback — the shared preamble for every
    exchange/validation failure test."""
    state, state_cookie = await _start_login(oidc)
    request = _request(oidc=oidc, cookies={STATE_COOKIE_NAME: state_cookie}, **request_kwargs)
    return request, state


async def test_callback_idp_unreachable_is_502():
    oidc = _oidc(FakeIdP(fail=True))
    request, state = await _callback_after_login(oidc)
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code", state)
    assert exc.value.status_code == 502


async def test_callback_token_exchange_rejection_is_401():
    """Keycloak refusing the code (replayed, wrong verifier) is a 401 — the
    IdP's own error body is never echoed into the response."""
    oidc = _oidc(FakeIdP(token_status=400))
    request, state = await _callback_after_login(oidc)
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code", state)
    assert exc.value.status_code == 401


async def test_callback_token_response_without_id_token_is_502():
    oidc = _oidc(FakeIdP(token_body={"access_token": "at", "token_type": "Bearer"}))
    request, state = await _callback_after_login(oidc)
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code", state)
    assert exc.value.status_code == 502


# --- id_token validation failures -----------------------------------------------


async def test_callback_malformed_id_token_is_401():
    idp = FakeIdP()
    oidc = _oidc(idp)
    request, state = await _callback_after_login(oidc)
    # Not a JWT at all — the unverified-header parse fails before anything else.
    idp.id_token = "not-a-jwt"
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code", state)
    assert exc.value.status_code == 401


async def test_callback_disallowed_algorithm_is_401():
    """An HS256 token is rejected at the alg allowlist — before any JWKS
    fetch, so a confused-deputy downgrade never gets that far."""
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    hs_token = pyjwt.encode(
        {
            "iss": ISSUER,
            "aud": "prahari-bff",
            "sub": "x",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        },
        "shared-secret",
        algorithm="HS256",
        headers={"kid": "test-key"},
    )
    idp.id_token = hs_token
    request = _request(oidc=oidc, cookies={STATE_COOKIE_NAME: state_cookie})
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code", state)
    assert exc.value.status_code == 401
    assert "unexpected token algorithm" in str(exc.value.detail)


async def test_callback_token_without_kid_is_401():
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce=state, kid=None)
    request = _request(oidc=oidc, cookies={STATE_COOKIE_NAME: state_cookie})
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code", state)
    assert exc.value.status_code == 401


async def test_callback_unknown_kid_fails_closed_after_one_refetch():
    """A kid the JWKS doesn't carry triggers exactly one forced refetch (key
    rotation), then the login is denied — it is not retried forever."""
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce=state, kid="kid-that-does-not-exist")
    request = _request(oidc=oidc, cookies={STATE_COOKIE_NAME: state_cookie})
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code", state)
    assert exc.value.status_code == 401
    assert "unknown signing key" in str(exc.value.detail)


async def test_callback_jwks_fetch_failure_is_502():
    idp = FakeIdP(jwks_status=500)
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce=state)
    request = _request(oidc=oidc, cookies={STATE_COOKIE_NAME: state_cookie})
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code", state)
    assert exc.value.status_code == 502


async def test_jwks_document_skips_unusable_keys():
    """A non-signing or malformed key in the JWKS set is skipped, not fatal —
    the valid key alongside it still resolves."""
    good = _jwks()
    junk = {"kty": "RSA", "alg": "BOGUS", "kid": "junk", "use": "sig", "n": "x", "e": "x"}
    idp = FakeIdP(jwks_body={"keys": [junk, *good["keys"]]})
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce=state)
    request = _request(
        oidc=oidc,
        cookies={STATE_COOKIE_NAME: state_cookie},
        pool=FakePool(org_ids={"gj.ahmedabad_city.zone_4": "org-z4"}),
    )
    response = await oidc_callback(request, "auth-code", state)
    assert response.status_code == 302


async def test_cached_jwks_key_is_reused_for_a_second_token():
    idp = FakeIdP()
    oidc = _oidc(idp)
    first = _id_token()
    assert (await oidc.validate_id_token(first, expected_nonce=""))["sub"] == "kc-sub-1"
    # Second validation hits the kid cache — no refetch.
    second = _id_token(username="other.user")
    claims = await oidc.validate_id_token(second, expected_nonce="")
    assert claims["preferred_username"] == "other.user"


async def test_callback_future_iat_is_401():
    """A token 'issued' 10 minutes from now is an authentication failure —
    pyjwt's iat validation rejects it inside decode."""
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce=state, iat_delta=600)
    request = _request(oidc=oidc, cookies={STATE_COOKIE_NAME: state_cookie})
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code", state)
    assert exc.value.status_code == 401


async def test_callback_azp_mismatch_is_401():
    """`azp` names the client the token was minted for — a token cut for a
    different client must not slide in on a matching `aud`."""
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce=state, azp="some-other-client")
    request = _request(oidc=oidc, cookies={STATE_COOKIE_NAME: state_cookie})
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code", state)
    assert exc.value.status_code == 401


async def test_jwks_refresh_within_the_cache_ttl_is_a_noop():
    idp = FakeIdP()
    oidc = _oidc(idp)
    await oidc._refresh_jwks(force=True)
    fetched_at = oidc._jwks_fetched_at
    await oidc._refresh_jwks()  # non-force, still inside TTL — no second fetch
    assert oidc._jwks_fetched_at == fetched_at


async def test_oidc_client_close():
    oidc = _oidc(FakeIdP())
    await oidc.aclose()


# --- state cookie ----------------------------------------------------------------


def _b64(data: bytes) -> str:
    from base64 import urlsafe_b64encode

    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _forged_state_cookie(payload: bytes, *, key: bytes = b"test-secret") -> str:
    """A correctly-shaped state cookie signed by hand — lets a test carry an
    arbitrary payload (expired iat, missing keys) past `seal_state`."""
    sig = hmac.new(key, payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def test_open_state_rejects_a_structurally_broken_cookie():
    oidc = _oidc(FakeIdP())
    for bad in ("", "no-dot-at-all", ".", "..", "not!b64.sig"):
        with pytest.raises(HTTPException) as exc:
            oidc.open_state(bad)
        assert exc.value.status_code == 400


def test_open_state_rejects_a_tampered_payload():
    oidc = _oidc(FakeIdP())
    sealed = oidc.seal_state(nonce="n", verifier="v", next_path="/")
    payload_b64, sig_b64 = sealed.split(".", 1)
    tampered = f"{payload_b64[:-1]}{'a' if payload_b64[-1] != 'a' else 'b'}.{sig_b64}"
    with pytest.raises(HTTPException) as exc:
        oidc.open_state(tampered)
    assert exc.value.status_code == 400
    assert "signature" in str(exc.value.detail)


def test_open_state_rejects_a_well_signed_but_shapeless_payload():
    """The signature is valid but the JSON isn't what a login leg wrote —
    `iat` missing entirely."""
    oidc = _oidc(FakeIdP())
    cookie = _forged_state_cookie(b'{"n": "x"}')
    with pytest.raises(HTTPException) as exc:
        oidc.open_state(cookie)
    assert exc.value.status_code == 400


def test_open_state_rejects_an_expired_state():
    oidc = _oidc(FakeIdP())
    payload = json.dumps(
        {"n": "x", "v": "y", "next": "/", "iat": int(time.time()) - STATE_TTL_S - 60}
    ).encode()
    with pytest.raises(HTTPException) as exc:
        oidc.open_state(_forged_state_cookie(payload))
    assert exc.value.status_code == 400
    assert "expired" in str(exc.value.detail)


def test_open_state_rejects_a_cookie_signed_under_another_key():
    oidc = _oidc(FakeIdP())
    payload = b'{"n": "x", "v": "y", "next": "/", "iat": 1}'
    cookie = _forged_state_cookie(payload, key=b"the-wrong-secret")
    with pytest.raises(HTTPException) as exc:
        oidc.open_state(cookie)
    assert exc.value.status_code == 400


# --- app.py branches that belong to the OIDC surface ----------------------------


async def test_oidc_login_constructs_the_client_lazily(monkeypatch):
    """`app.state.oidc` unset → `_get_oidc` builds a real OidcClient on first
    use and stashes it — the seam tests exploit by injecting their own."""
    idp = FakeIdP()
    request = _request(oidc=None)  # attribute absent-ish: _get_oidc builds one
    # Build it over the fake transport so nothing tries a real network call.
    real_client = OidcClient(
        SETTINGS, http_client=httpx.AsyncClient(transport=httpx.MockTransport(idp.handler))
    )
    constructed = []

    def _ctor(settings, **kwargs):
        constructed.append(settings)
        return real_client

    monkeypatch.setattr("prahari_bff.app.OidcClient", _ctor)
    response = await oidc_login(request, "/")
    assert response.status_code == 302
    assert constructed == [SETTINGS]
    assert request.app.state.oidc is real_client


async def test_callback_missing_code_or_state_is_400():
    oidc = _oidc(FakeIdP())
    request = _request(oidc=oidc)
    for code, state in [("", "s"), ("c", ""), ("", "")]:
        with pytest.raises(HTTPException) as exc:
            await oidc_callback(request, code, state)
        assert exc.value.status_code == 400


async def test_callback_token_with_no_usable_subject_is_401():
    """`preferred_username` absent and `sub` empty — there is no account to
    attach the session to, so the login is denied outright."""
    idp = FakeIdP()
    oidc = _oidc(idp)
    state, state_cookie = await _start_login(oidc)
    idp.id_token = _id_token(nonce=state, username=None, sub="")
    request = _request(oidc=oidc, cookies={STATE_COOKIE_NAME: state_cookie})
    with pytest.raises(HTTPException) as exc:
        await oidc_callback(request, "auth-code", state)
    assert exc.value.status_code == 401


async def test_oidc_logout_audits_a_resolved_session():
    """A session that still resolves at logout writes `auth_logout` before
    the revoke commits — same ordering as builtin logout."""
    idp = FakeIdP()
    oidc = _oidc(idp)
    user = User(id="u1", username="ops.zone4", org_id="org-z4", role=Role.OPERATOR)
    session_repo = FakeSessionRepo({"sess-1": (user, "gj.ahmedabad_city.zone_4")})

    class FakeAudit:
        def __init__(self):
            self.entries = []

        async def append(self, **kwargs):
            self.entries.append(kwargs)

    audit = FakeAudit()
    request = _request(
        oidc=oidc,
        session_repo=session_repo,
        cookies={"prahari_session": "sess-1", OIDC_MARKER_COOKIE_NAME: "1"},
    )
    request.app.state.audit = audit
    result = await oidc_logout(request, Response())
    assert result["status"] == "ok"
    assert session_repo.revoked == ["sess-1"]
    assert audit.entries[0]["action"] == "auth_logout"
    assert audit.entries[0]["actor"] == "ops.zone4"
