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
    SlidingWindowRateLimiter,
    TicketVerifier,
    _ticket_grants,
    authorize,
    log_safe,
)

TOKEN = "test-internal-token"
WORKER_TOKEN = "test-worker-media-token"


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


def _settings(**kwargs) -> RegistrySettings:
    kwargs.setdefault("internal_token", TOKEN)
    kwargs.setdefault("worker_media_token", WORKER_TOKEN)
    return RegistrySettings(**kwargs)


async def test_api_user_gets_api_and_nothing_else():
    settings = _settings()
    verifier = _verifier(settings, {"keys": []})
    assert await authorize(settings, verifier, _req(user="internal", password=TOKEN, action="api"))
    # The api credential is not a stream credential.
    assert not await authorize(
        settings, verifier, _req(user="internal", password=TOKEN, action="read", path="cam-x")
    )


async def test_worker_reads_cam_paths_only():
    settings = _settings()
    verifier = _verifier(settings, {"keys": []})
    assert await authorize(
        settings,
        verifier,
        _req(user="worker", password=WORKER_TOKEN, action="read", path="cam-42"),
    )
    assert not await authorize(
        # The worker credential must not reach the control API — a leaked
        # assignment URL is a reader, not an operator.
        settings,
        verifier,
        _req(user="worker", password=WORKER_TOKEN, action="api"),
    )
    assert not await authorize(
        settings,
        verifier,
        _req(user="worker", password=WORKER_TOKEN, action="read", path="debug-clip"),
    )


async def test_the_media_credential_is_not_the_internal_token():
    """H1: the two machine secrets are independent — the internal API token
    must NOT read streams, and the worker media token must NOT reach the API.
    A leaked fan-out URL is a media credential, nothing more."""
    settings = _settings()
    verifier = _verifier(settings, {"keys": []})
    assert not await authorize(
        settings, verifier, _req(user="worker", password=TOKEN, action="read", path="cam-1")
    )
    assert not await authorize(
        settings,
        verifier,
        _req(user="internal", password=WORKER_TOKEN, action="api"),
    )


async def test_worker_auth_fails_closed_without_a_worker_token():
    """Enforcement armed (internal_token set) but no worker_media_token:
    `worker:` requests are denied outright — a credential that cannot be
    checked cannot be granted. This is the missing-Secret case, and it must
    be loud denial, not an allow."""
    settings = _settings(worker_media_token="")
    verifier = _verifier(settings, {"keys": []})
    assert not await authorize(
        settings,
        verifier,
        _req(user="worker", password=TOKEN, action="read", path="cam-1"),
    )
    assert not await authorize(
        settings,
        verifier,
        _req(user="worker", password="anything", action="read", path="cam-1"),
    )
    # The internal API user is unaffected — its own secret is configured.
    assert await authorize(settings, verifier, _req(user="internal", password=TOKEN, action="api"))


async def test_wrong_password_is_denied():
    settings = _settings()
    verifier = _verifier(settings, {"keys": []})
    assert not await authorize(
        settings,
        verifier,
        _req(user="worker", password="nope", action="read", path="cam-1"),
    )


async def test_publish_and_playback_are_denied_for_internal_credentials():
    """Consume-only is not a credential question for MACHINES: `internal`
    and `worker` may neither write into the restreamer nor replay out of it.
    Playback exists only as a BFF-ticket grant — see the playback block
    below."""
    settings = _settings()
    verifier = _verifier(settings, {"keys": []})
    for action in ("publish", "playback"):
        for user, secret in (("internal", TOKEN), ("worker", WORKER_TOKEN)):
            assert not await authorize(
                settings,
                verifier,
                _req(user=user, password=secret, action=action, path="cam-1"),
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
    app.state.settings = RegistrySettings(
        internal_token=TOKEN, worker_media_token=WORKER_TOKEN, sync_enabled=False
    )
    app.state.ticket_verifier = _verifier(app.state.settings, {"keys": []})
    app.state.media_auth_limiter = SlidingWindowRateLimiter(20, 60.0)
    return TestClient(app)


def test_auth_endpoint_is_exempt_from_the_internal_token(armed_client):
    """MediaMTX cannot send X-Internal-Token — gating the credential check
    behind the credential would deadlock the video plane."""
    resp = armed_client.post(
        "/api/v1/mediamtx/auth",
        json={"user": "worker", "password": WORKER_TOKEN, "action": "read", "path": "cam-1"},
    )
    assert resp.status_code == 200


def test_auth_endpoint_rate_limits_per_source_ip(armed_client):
    """The endpoint is an unauthenticated credential oracle by necessity —
    the sliding window is what bounds guess attempts. Past the cap the
    answer is 429, which MediaMTX reads as a refusal like any non-2xx."""
    app.state.media_auth_limiter = SlidingWindowRateLimiter(3, 60.0)
    body = {"user": "worker", "password": "guess", "action": "read", "path": "cam-1"}
    codes = [armed_client.post("/api/v1/mediamtx/auth", json=body).status_code for _ in range(6)]
    assert codes[:3] == [401, 401, 401]
    assert codes[3:] == [429, 429, 429]


def test_auth_denial_log_is_sanitized(armed_client, caplog):
    """`user`/`path` are attacker-controlled body fields — a newline or
    control char in them must not forge extra log lines."""
    import logging

    caplog.set_level(logging.INFO, logger="prahari_registry.app")
    armed_client.post(
        "/api/v1/mediamtx/auth",
        json={
            "user": "worker\nFAKE-LOG-LINE",
            "password": "wrong",
            "action": "read",
            "path": "cam-1\x00\x1b[31m",
        },
    )
    denied = [r for r in caplog.records if "auth denied" in r.getMessage()]
    assert denied, "expected a denial log line"
    message = denied[-1].getMessage()
    # No raw control characters survive — the injected newline cannot split
    # the line into a forged second entry, the escape cannot colour it.
    assert "\n" not in message and "\x1b" not in message and "\x00" not in message
    # The text itself stays (replaced chars mark the tampering rather than
    # hiding it): one log line, visibly sanitized.
    assert "worker?FAKE-LOG-LINE" in message


def test_auth_denial_log_bounds_field_length(armed_client, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="prahari_registry.app")
    armed_client.post(
        "/api/v1/mediamtx/auth",
        json={
            "user": "u" * 4096,
            "password": "wrong",
            "action": "read",
            "path": "cam-1",
        },
    )
    denied = [r for r in caplog.records if "auth denied" in r.getMessage()]
    assert denied and len(denied[-1].getMessage()) < 1024


def test_auth_endpoint_denies_bad_credentials(armed_client):
    resp = armed_client.post(
        "/api/v1/mediamtx/auth",
        json={"user": "worker", "password": "wrong", "action": "read", "path": "cam-1"},
    )
    assert resp.status_code == 401


# --- _ticket_grants: MediaMTX's permission semantics, minimally ---------------------


def test_ticket_grants_requires_a_permissions_list():
    assert not _ticket_grants({"mediamtx_permissions": "read"}, action="read", path="cam-1")
    assert not _ticket_grants({}, action="read", path="cam-1")


def test_ticket_grants_skips_entries_that_are_not_grants():
    """A malformed entry must not open anything — it is skipped, not trusted."""
    payload = {"mediamtx_permissions": [42, "read", {"action": "read", "path": "cam-1"}]}
    assert _ticket_grants(payload, action="read", path="cam-1")


def test_ticket_grants_action_must_match_exactly():
    payload = {"mediamtx_permissions": [{"action": "publish", "path": "cam-1"}]}
    assert not _ticket_grants(payload, action="read", path="cam-1")


def test_ticket_grants_empty_path_grants_nothing():
    """L2: a grant naming no path is not "any path" — the BFF never mints
    one, so an empty/absent path is indistinguishable from a malformed (or
    attacker-edited) payload and is refused."""
    payload = {"mediamtx_permissions": [{"action": "read", "path": ""}]}
    assert not _ticket_grants(payload, action="read", path="cam-anything")
    payload = {"mediamtx_permissions": [{"action": "read"}]}
    assert not _ticket_grants(payload, action="read", path="cam-anything")
    payload = {"mediamtx_permissions": [{"action": "read", "path": None}]}
    assert not _ticket_grants(payload, action="read", path="cam-anything")


def test_log_safe_strips_control_chars_and_bounds_length():
    assert log_safe("worker\nforged-line") == "worker?forged-line"
    assert log_safe("cam-1\x00\x1b[31m") == "cam-1??[31m"
    assert len(log_safe("x" * 1000)) <= 129  # 128 chars + the truncation marker
    assert log_safe("x" * 1000).endswith("…")


async def test_authorize_correct_token_under_an_unknown_user_is_denied():
    """The internal token buys exactly two identities (`internal` for the API,
    `worker` for reads). Anyone else holding it gets nothing."""
    settings = RegistrySettings(internal_token=TOKEN)
    verifier = _verifier(settings, {"keys": []})
    assert not await authorize(settings, verifier, _req(user="admin", password=TOKEN, action="api"))


# --- the verifier's JWKS machinery ---------------------------------------------------


def _counting_jwks_client(jwks_by_call: list) -> tuple[httpx.AsyncClient, list]:
    """A MockTransport that pops a response (or raises) per call and counts."""

    calls: list = []
    responses = list(jwks_by_call)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=item)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


async def test_verifier_builds_and_closes_its_own_http_client():
    """Production path: no client injected → one is created lazily and
    released by aclose() at shutdown."""
    verifier = TicketVerifier(RegistrySettings(internal_token=TOKEN))
    assert verifier._client is None
    assert await verifier._http() is not None
    await verifier.aclose()
    assert verifier._client is None


async def test_jwks_undecodable_keys_are_skipped_not_trusted():
    jwks = {
        "keys": [
            {"kty": "OKP", "crv": "Ed25519", "x": "!!!", "kid": "short"},
            {"kty": "RSA", "crv": "nope", "x": "aaaa", "kid": "wrong-kty"},
        ]
    }
    settings = RegistrySettings(internal_token=TOKEN)
    verifier = _verifier(settings, jwks)
    assert await verifier._refresh() == {}


async def test_unknown_kid_forces_one_refresh_then_denies():
    """A ticket naming a `kid` the cache does not hold forces exactly one
    re-pull — the BFF may have rotated. Still unknown after that is a forged
    or expired-issuer ticket: deny."""
    settings = RegistrySettings(internal_token=TOKEN)
    key = Ed25519PrivateKey.generate()
    http, calls = _counting_jwks_client([_jwks_for(key), _jwks_for(key)])
    verifier = TicketVerifier(settings, client=http)
    ticket = _make_ticket(key, kid="k9", path="cam-abc")

    assert not await verifier.allows(ticket, action="read", path="cam-abc")
    assert len(calls) == 2  # one cache-fill refresh plus the forced re-check


async def test_a_still_unknown_kid_is_negative_cached_and_costs_no_fetch():
    """L2: a flood of tickets all naming a bogus `kid` must not each cost a
    JWKS fetch — that turns this endpoint into a request amplifier against
    the BFF. Within the negative-cache window the same unknown kid is denied
    locally, no refresh."""
    settings = RegistrySettings(internal_token=TOKEN)
    key = Ed25519PrivateKey.generate()
    http, calls = _counting_jwks_client([_jwks_for(key), _jwks_for(key)])
    verifier = TicketVerifier(settings, client=http)
    ticket = _make_ticket(key, kid="k9", path="cam-abc")

    assert not await verifier.allows(ticket, action="read", path="cam-abc")
    assert len(calls) == 2
    # Second and third requests with the same forged kid: denied locally.
    assert not await verifier.allows(ticket, action="read", path="cam-abc")
    assert not await verifier.allows(ticket, action="read", path="cam-abc")
    assert len(calls) == 2  # no further fetches


async def test_an_unknown_kid_retries_after_the_negative_window():
    """The negative cache is short: a real BFF key rotation must become
    visible again after the window, not be remembered as forged forever."""
    settings = RegistrySettings(internal_token=TOKEN)
    key = Ed25519PrivateKey.generate()
    rotated = _jwks_for(key)
    rotated["keys"][0]["kid"] = "k9"  # the post-rotation JWKS knows k9
    http, calls = _counting_jwks_client([_jwks_for(key), _jwks_for(key), rotated])
    verifier = TicketVerifier(settings, client=http)
    ticket = _make_ticket(key, kid="k9", path="cam-abc")

    assert not await verifier.allows(ticket, action="read", path="cam-abc")
    assert len(calls) == 2
    # Age the negative-cache entry past its TTL: the next check re-fetches
    # and the rotated key now verifies the ticket.
    verifier._unknown_kids["k9"] -= verifier._UNKNOWN_KID_TTL_S + 1
    assert await verifier.allows(ticket, action="read", path="cam-abc")
    assert len(calls) == 3


async def test_unknown_kid_with_an_unreachable_bff_denies():
    """The forced re-pull failing means the ticket simply cannot be verified —
    deny, never skip the check."""
    settings = RegistrySettings(internal_token=TOKEN)
    key = Ed25519PrivateKey.generate()
    http, _ = _counting_jwks_client([_jwks_for(key), httpx.ConnectError("bff down")])
    verifier = TicketVerifier(settings, client=http)
    ticket = _make_ticket(key, kid="k9", path="cam-abc")

    assert not await verifier.allows(ticket, action="read", path="cam-abc")


async def test_a_jwks_fetch_failure_keeps_serving_the_last_good_set():
    """If the BFF is down no new tickets can be minted anyway; the cached keys
    keep verifying the seconds-lived outstanding ones."""
    settings = RegistrySettings(internal_token=TOKEN, media_auth_jwks_ttl_s=0)
    key = Ed25519PrivateKey.generate()
    http, _ = _counting_jwks_client([_jwks_for(key), httpx.ConnectError("bff down")])
    verifier = TicketVerifier(settings, client=http)
    ticket = _make_ticket(key, path="cam-abc")

    assert await verifier.allows(ticket, action="read", path="cam-abc")
    # TTL=0 forces a refresh on the next lookup; it fails, and the cached key
    # still answers — the ticket is still accepted.
    assert await verifier.allows(ticket, action="read", path="cam-abc")


async def test_malformed_and_foreign_alg_tokens_are_denied_without_a_fetch():
    settings = RegistrySettings(internal_token=TOKEN)
    http, calls = _counting_jwks_client([])
    verifier = TicketVerifier(settings, client=http)

    assert not await verifier.allows("not-a-jwt", action="read", path="cam-1")
    assert not await verifier.allows("a.b", action="read", path="cam-1")

    # A correctly shaped JWT with the wrong alg must not reach the verifier.
    now = int(time.time())
    header = {"alg": "RS256", "kid": "k1"}
    payload = {"exp": now + 60, "mediamtx_permissions": [{"action": "read", "path": "cam-1"}]}
    token = (
        f"{_b64url(json.dumps(header).encode())}."
        f"{_b64url(json.dumps(payload).encode())}.{_b64url(b'fakesig')}"
    )
    assert not await verifier.allows(token, action="read", path="cam-1")
    assert calls == []  # nothing was fetched for tokens that fail locally


async def test_a_ticket_without_exp_is_denied():
    """`exp` absent or non-numeric is not 'never expires' — it is malformed."""
    settings = RegistrySettings(internal_token=TOKEN)
    key = Ed25519PrivateKey.generate()
    verifier = _verifier(settings, _jwks_for(key))

    now = int(time.time())
    header = {"alg": "EdDSA", "kid": "k1"}
    payload = {
        "iss": "prahari-bff",
        "iat": now,
        "mediamtx_permissions": [{"action": "read", "path": "cam-1"}],
    }
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    token = f"{signing_input}.{_b64url(key.sign(signing_input.encode()))}"
    assert not await verifier.allows(token, action="read", path="cam-1")
