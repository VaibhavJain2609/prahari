"""The audited video path: `/api/v1/media/jwks` + `/api/v1/media/preview-ticket`,
and the `_public_camera` projection that keeps stream URLs out of every
browser-facing camera response.

The ticket itself is verified with the issuer's OWN public key — exactly what
the registry's `TicketVerifier` does against `/api/v1/media/jwks` in
production — so a ticket that fails to verify here would fail at the
restreamer too.
"""

from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from fastapi import HTTPException
from fastapi.testclient import TestClient

from prahari_bff.app import app, list_cameras, preview_ticket
from prahari_bff.config import BFFSettings
from prahari_bff.media import MediaTicketIssuer
from prahari_bff.models import PreviewTicketRequest, Principal, Role, User

OPERATOR = Principal(
    id="u1",
    subject="ops.zone4",
    org_id="org-zone4",
    org_path="gj.ahmedabad_city.zone_4",
    role=Role.OPERATOR,
    kind="session",
)


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _decode_ticket(issuer: MediaTicketIssuer, ticket: str) -> dict:
    """Verify + decode a ticket the way the registry's auth callback does:
    resolve `kid` from the public JWKS, check the Ed25519 signature, then
    read the claims."""
    header_b64, payload_b64, sig_b64 = ticket.split(".")
    header = json.loads(_b64url_decode(header_b64))
    assert header["alg"] == "EdDSA"
    jwks = issuer.jwks()
    jwk = next(k for k in jwks["keys"] if k["kid"] == header["kid"])
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    key = Ed25519PublicKey.from_public_bytes(_b64url_decode(jwk["x"]))
    key.verify(_b64url_decode(sig_b64), f"{header_b64}.{payload_b64}".encode())
    return json.loads(_b64url_decode(payload_b64))


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    async def append(self, **kwargs):
        self.entries.append(kwargs)
        return kwargs


class FakeScopeResolver:
    def __init__(self, camera_orgs: dict[str, str | None]) -> None:
        self._camera_orgs = camera_orgs

    async def org_path_for_camera(self, camera_id: str) -> str | None:
        return self._camera_orgs.get(camera_id)


class FakeRegistry:
    """Canned-response registry client — see test_admin_endpoints.py."""

    def __init__(self, status_code: int = 200, body=None) -> None:
        self._response = httpx.Response(status_code, json=body if body is not None else {})
        self.calls: list[tuple] = []

    async def get(self, path: str, params: dict | None = None) -> httpx.Response:
        self.calls.append(("GET", path, params))
        return self._response


def _request(issuer: MediaTicketIssuer | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=BFFSettings(),
                media_issuer=issuer or MediaTicketIssuer(BFFSettings()),
            )
        ),
        query_params={},
    )


# --- the issuer ---------------------------------------------------------------


def test_ticket_verifies_against_the_published_jwks():
    issuer = MediaTicketIssuer(BFFSettings())
    ticket = issuer.mint(subject="ops.zone4", camera_id="cam-1", ttl_s=60)
    payload = _decode_ticket(issuer, ticket)

    assert payload["iss"] == "prahari-bff"
    assert payload["sub"] == "ops.zone4"
    assert payload["mediamtx_permissions"] == [{"action": "read", "path": "cam-cam-1"}]
    assert payload["exp"] - payload["iat"] == 60
    assert payload["exp"] > time.time()


def test_configured_pem_key_round_trips():
    """`PRAHARI_MEDIA_JWT_PRIVATE_KEY` — the stable-deployment path — must
    produce an issuer that verifies what it signs."""
    pem = (
        Ed25519PrivateKey.generate()
        .private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        .decode()
    )
    issuer = MediaTicketIssuer(BFFSettings(media_jwt_private_key=pem))
    payload = _decode_ticket(issuer, issuer.mint(subject="a", camera_id="c", ttl_s=10))
    assert payload["sub"] == "a"


# --- the endpoint ---------------------------------------------------------------


async def test_in_scope_preview_mints_an_audited_ticket():
    audit = FakeAudit()
    issuer = MediaTicketIssuer(BFFSettings())
    request = _request(issuer)
    result = await preview_ticket(
        PreviewTicketRequest(camera_id="cam-7"),
        OPERATOR,
        "incident-441",  # purpose code
        FakeScopeResolver({"cam-7": "gj.ahmedabad_city.zone_4.ward_9"}),
        audit,
        request,
    )

    assert result.camera_id == "cam-7"
    assert result.expires_in == 60
    assert result.whep_url == "http://localhost:8889/cam-cam-7/whep"
    payload = _decode_ticket(issuer, result.ticket)
    assert payload["mediamtx_permissions"] == [{"action": "read", "path": "cam-cam-7"}]
    # The audit entry exists and PRECEDES issuance — the handler appends
    # before calling mint().
    assert audit.entries[-1]["action"] == "video_preview"
    assert audit.entries[-1]["resource"] == "camera:cam-7"
    assert audit.entries[-1]["purpose_code"] == "incident-441"


async def test_out_of_scope_preview_is_403_and_audited_denied():
    audit = FakeAudit()
    request = _request()
    with pytest.raises(HTTPException) as exc:
        await preview_ticket(
            PreviewTicketRequest(camera_id="cam-8"),
            OPERATOR,
            "incident-441",
            FakeScopeResolver({"cam-8": "gj.ahmedabad_city.zone_5"}),
            audit,
            request,
        )
    assert exc.value.status_code == 403
    assert audit.entries[-1]["action"] == "video_preview_denied"


async def test_unknown_camera_is_404_with_no_audit_write():
    """Nothing accessed, nothing audited — a 404 is not a denial."""
    audit = FakeAudit()
    with pytest.raises(HTTPException) as exc:
        await preview_ticket(
            PreviewTicketRequest(camera_id="ghost"),
            OPERATOR,
            "incident-441",
            FakeScopeResolver({}),
            audit,
            _request(),
        )
    assert exc.value.status_code == 404
    assert audit.entries == []


# --- wired through the app ------------------------------------------------------


def _client() -> TestClient:
    user = User(id="u1", username="ops.zone4", org_id="org-zone4", role=Role.OPERATOR)
    app.state.settings = BFFSettings()
    app.state.media_issuer = MediaTicketIssuer(BFFSettings())
    app.state.scope_resolver = FakeScopeResolver({"cam-7": "gj.ahmedabad_city.zone_4"})
    app.state.audit = FakeAudit()

    class _Sessions:
        async def resolve(self, session_id):
            return (user, "gj.ahmedabad_city.zone_4") if session_id == "sess-1" else None

    class _Keys:
        async def resolve(self, token):
            return None

    app.state.session_repo = _Sessions()
    app.state.api_key_repo = _Keys()
    return TestClient(app)


def test_jwks_is_served_without_authentication():
    """A public key is public; the registry fetches it holding no
    credentials, and the JWKS document must never carry private material."""
    resp = _client().get("/api/v1/media/jwks")
    assert resp.status_code == 200
    jwk = resp.json()["keys"][0]
    assert jwk["kty"] == "OKP" and jwk["crv"] == "Ed25519"
    assert "d" not in jwk  # the private half is never serialised


def test_preview_ticket_requires_authentication():
    resp = _client().post("/api/v1/media/preview-ticket", json={"camera_id": "cam-7"})
    assert resp.status_code == 401


def test_preview_ticket_requires_a_purpose_code():
    client = _client()
    client.cookies.set("prahari_session", "sess-1")
    resp = client.post("/api/v1/media/preview-ticket", json={"camera_id": "cam-7"})
    assert resp.status_code == 400


def test_preview_ticket_end_to_end_over_http():
    client = _client()
    client.cookies.set("prahari_session", "sess-1")
    resp = client.post(
        "/api/v1/media/preview-ticket",
        json={"camera_id": "cam-7"},
        headers={"X-Purpose-Code": "incident-441"},
    )
    assert resp.status_code == 200
    body = resp.json()
    payload = _decode_ticket(app.state.media_issuer, body["ticket"])
    assert payload["mediamtx_permissions"] == [{"action": "read", "path": "cam-cam-7"}]


# --- the public camera projection -------------------------------------------------


async def test_camera_list_strips_endpoints_and_keeps_the_preview_flag():
    """The registry's camera payload now carries credential-bearing fan-out
    URLs for workers. Nothing in that shape may reach a browser — the
    projection drops `endpoints` wholesale and keeps `preview`."""
    registry = FakeRegistry(
        200,
        [
            {
                "id": "cam-7",
                "endpoints": {
                    "fanout_rtsp_url": "rtsp://worker:s3cret@mtx:8554/cam-7",
                    "fanout_hls_url": "http://worker:s3cret@mtx:8888/cam-7/index.m3u8",
                    "fanout_whep_url": "http://mtx:8889/cam-7/whep",
                },
                "preview": {"available": True},
            }
        ],
    )
    result = await list_cameras(OPERATOR, registry, _request())

    assert "endpoints" not in result[0]
    assert result[0]["preview"] == {"available": True}
    assert "s3cret" not in json.dumps(result)
