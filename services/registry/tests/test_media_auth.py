"""`/api/v1/mediamtx/auth` — the credential check MediaMTX defers to.

Covers the three credential shapes `media_auth.authorize` recognises
(internal API user, worker reader, BFF preview ticket) plus the two
invariants the endpoint exists for: it is exempt from `X-Internal-Token`
(the restreamer cannot hold the token it asks us to check) and it fails
closed whenever a token IS configured.
"""

from __future__ import annotations

import base64
import json
import time
import uuid

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from prahari_registry.app import app
from prahari_registry.config import RegistrySettings
from prahari_registry.media_auth import (
    MediaMTXAuthRequest,
    TicketVerifier,
    authorize,
)

TOKEN = "test-internal-token"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_ticket(
    key: Ed25519PrivateKey,
    *,
    kid: str = "k1",
    path: str = "cam-abc",
    action: str = "read",
    exp_in: float = 60.0,
) -> str:
    now = int(time.time())
    header = {"alg": "EdDSA", "typ": "JWT", "kid": kid}
    payload = {
        "iss": "prahari-bff",
        "sub": "ops.zone4",
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": int(now + exp_in),
        "mediamtx_permissions": [{"action": action, "path": path}],
    }
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    return f"{signing_input}.{_b64url(key.sign(signing_input.encode()))}"


def _jwks_for(key: Ed25519PrivateKey, kid: str = "k1") -> dict:
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {
        "keys": [{"kty": "OKP", "crv": "Ed25519", "x": _b64url(raw), "kid": kid, "alg": "EdDSA"}]
    }


def _verifier(settings: RegistrySettings, jwks: dict) -> TicketVerifier:
    """A TicketVerifier whose JWKS fetch is answered by a mock transport."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jwks)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TicketVerifier(settings, client=http)


def _req(**kwargs) -> MediaMTXAuthRequest:
    return MediaMTXAuthRequest(**kwargs)


# --- policy: internal credentials --------------------------------------------


async def test_api_user_gets_api_and_nothing_else():
    settings = RegistrySettings(internal_token=TOKEN)
    verifier = _verifier(settings, {"keys": []})
    assert await authorize(settings, verifier, _req(user="internal", password=TOKEN, action="api"))
    # The api credential is not a stream credential.
    assert not await authorize(
        settings, verifier, _req(user="internal", password=TOKEN, action="read", path="cam-x")
    )


async def test_worker_reads_cam_paths_only():
    settings = RegistrySettings(internal_token=TOKEN)
    verifier = _verifier(settings, {"keys": []})
    assert await authorize(
        settings, verifier, _req(user="worker", password=TOKEN, action="read", path="cam-42")
    )
    assert not await authorize(
        # The worker credential must not reach the control API — a leaked
        # assignment URL is a reader, not an operator.
        settings,
        verifier,
        _req(user="worker", password=TOKEN, action="api"),
    )
    assert not await authorize(
        settings,
        verifier,
        _req(user="worker", password=TOKEN, action="read", path="debug-clip"),
    )


async def test_wrong_password_is_denied():
    settings = RegistrySettings(internal_token=TOKEN)
    verifier = _verifier(settings, {"keys": []})
    assert not await authorize(
        settings, verifier, _req(user="worker", password="nope", action="read", path="cam-1")
    )


async def test_publish_and_playback_are_denied_for_internal_credentials():
    """Consume-only is not a credential question for MACHINES: `internal`
    and `worker` may neither write into the restreamer nor replay out of it.
    Playback exists only as a BFF-ticket grant — see the playback block
    below."""
    settings = RegistrySettings(internal_token=TOKEN)
    verifier = _verifier(settings, {"keys": []})
    for action in ("publish", "playback"):
        for user in ("internal", "worker"):
            assert not await authorize(
                settings,
                verifier,
                _req(user=user, password=TOKEN, action=action, path="cam-1"),
            )


async def test_no_token_means_enforcement_off():
    """The documented local default: an unset internal_token disables the
    gate here exactly as it disables `require_internal_token`."""
    settings = RegistrySettings(internal_token="")
    verifier = _verifier(settings, {"keys": []})
    assert await authorize(settings, verifier, _req(action="read", path="cam-1"))


# --- policy: BFF preview tickets ---------------------------------------------


async def test_valid_ticket_grants_read_on_its_path():
    settings = RegistrySettings(internal_token=TOKEN)
    key = Ed25519PrivateKey.generate()
    verifier = _verifier(settings, _jwks_for(key))
    ticket = _make_ticket(key, path="cam-abc")

    assert await authorize(
        settings, verifier, _req(action="read", path="cam-abc", query=f"jwt={ticket}")
    )


async def test_ticket_is_scoped_to_the_one_camera_it_names():
    """A ticket for cam-abc must not open cam-other — that is the entire
    point of per-camera grants over a shared credential."""
    settings = RegistrySettings(internal_token=TOKEN)
    key = Ed25519PrivateKey.generate()
    verifier = _verifier(settings, _jwks_for(key))
    ticket = _make_ticket(key, path="cam-abc")

    assert not await authorize(
        settings, verifier, _req(action="read", path="cam-other", query=f"jwt={ticket}")
    )


async def test_expired_ticket_is_denied():
    settings = RegistrySettings(internal_token=TOKEN)
    key = Ed25519PrivateKey.generate()
    verifier = _verifier(settings, _jwks_for(key))
    ticket = _make_ticket(key, path="cam-abc", exp_in=-10)

    assert not await authorize(
        settings, verifier, _req(action="read", path="cam-abc", query=f"jwt={ticket}")
    )


async def test_ticket_signed_by_an_unknown_key_is_denied():
    settings = RegistrySettings(internal_token=TOKEN)
    other_key = Ed25519PrivateKey.generate()
    verifier = _verifier(settings, _jwks_for(other_key))  # serves the WRONG key
    ticket = _make_ticket(Ed25519PrivateKey.generate(), path="cam-abc")

    assert not await authorize(
        settings, verifier, _req(action="read", path="cam-abc", query=f"jwt={ticket}")
    )


async def test_ticket_cannot_do_anything_but_read():
    settings = RegistrySettings(internal_token=TOKEN)
    key = Ed25519PrivateKey.generate()
    verifier = _verifier(settings, _jwks_for(key))
    # A ticket self-describing an api grant must still fail: the ticket path
    # is only ever consulted for `action: read` or `playback` on `cam-*`.
    ticket = _make_ticket(key, path="", action="api")
    assert not await authorize(settings, verifier, _req(action="api", query=f"jwt={ticket}"))


# --- policy: BFF playback (evidence) tickets -----------------------------------
#
# Evidence tickets grant `read` + `playback` on `cam-<id>` so that enabling
# `record` on the reconciled paths later changes nothing here
# (docs/EVIDENCE.md). The grant is exact-match per action: a preview ticket
# (read only) can never play back.


async def test_playback_ticket_grants_playback_on_its_path():
    settings = RegistrySettings(internal_token=TOKEN)
    key = Ed25519PrivateKey.generate()
    verifier = _verifier(settings, _jwks_for(key))
    ticket = _make_ticket(key, path="cam-abc", action="playback")

    assert await authorize(
        settings, verifier, _req(action="playback", path="cam-abc", query=f"jwt={ticket}")
    )


async def test_read_only_ticket_cannot_play_back():
    """A preview ticket must not become a playback credential — the grant
    is per-action, so `read` on the path does not imply `playback`."""
    settings = RegistrySettings(internal_token=TOKEN)
    key = Ed25519PrivateKey.generate()
    verifier = _verifier(settings, _jwks_for(key))
    ticket = _make_ticket(key, path="cam-abc", action="read")

    assert not await authorize(
        settings, verifier, _req(action="playback", path="cam-abc", query=f"jwt={ticket}")
    )


async def test_playback_ticket_is_scoped_to_the_one_camera_it_names():
    settings = RegistrySettings(internal_token=TOKEN)
    key = Ed25519PrivateKey.generate()
    verifier = _verifier(settings, _jwks_for(key))
    ticket = _make_ticket(key, path="cam-abc", action="playback")

    assert not await authorize(
        settings, verifier, _req(action="playback", path="cam-other", query=f"jwt={ticket}")
    )


async def test_expired_playback_ticket_is_denied():
    settings = RegistrySettings(internal_token=TOKEN)
    key = Ed25519PrivateKey.generate()
    verifier = _verifier(settings, _jwks_for(key))
    ticket = _make_ticket(key, path="cam-abc", action="playback", exp_in=-10)

    assert not await authorize(
        settings, verifier, _req(action="playback", path="cam-abc", query=f"jwt={ticket}")
    )


async def test_playback_without_a_ticket_is_denied():
    """No jwt in the query, no internal credential — playback has no other
    credential shape."""
    settings = RegistrySettings(internal_token=TOKEN)
    verifier = _verifier(settings, {"keys": []})
    assert not await authorize(settings, verifier, _req(action="playback", path="cam-abc"))


# --- the HTTP surface ---------------------------------------------------------


@pytest.fixture
def armed_client():
    app.state.settings = RegistrySettings(internal_token=TOKEN, sync_enabled=False)
    app.state.ticket_verifier = _verifier(app.state.settings, {"keys": []})
    return TestClient(app)


def test_auth_endpoint_is_exempt_from_the_internal_token(armed_client):
    """MediaMTX cannot send X-Internal-Token — gating the credential check
    behind the credential would deadlock the video plane."""
    resp = armed_client.post(
        "/api/v1/mediamtx/auth",
        json={"user": "worker", "password": TOKEN, "action": "read", "path": "cam-1"},
    )
    assert resp.status_code == 200


def test_auth_endpoint_denies_bad_credentials(armed_client):
    resp = armed_client.post(
        "/api/v1/mediamtx/auth",
        json={"user": "worker", "password": "wrong", "action": "read", "path": "cam-1"},
    )
    assert resp.status_code == 401
