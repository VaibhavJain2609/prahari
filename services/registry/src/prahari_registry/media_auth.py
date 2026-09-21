"""MediaMTX's credential check, answered by this service.

MediaMTX 1.9.x `authMethod` is exclusive — `internal`, `http` and `jwt` are
alternatives, not layers, so "internal users AND JWT" is not a configuration
that exists. We run `authMethod: http` (`templates/mediamtx-config.yaml`),
which POSTs every authentication decision here as
`{ip, user, password, action, path, protocol, id, query}` and treats any
non-2xx as a refusal. One endpoint then recognises the two credential shapes
the estate actually has:

* **Machines — HTTP basic.** `worker:<worker-media-token>` grants `read` on
  `cam-*` paths (embedded in the fan-out URLs `fanout_endpoints` hands to
  inference workers — RTSP consumers have no header channel). `internal:
  <internal-token>` grants `api` (this service's own reconcile client). The
  two passwords are INDEPENDENT secrets (`worker_media_token` vs
  `internal_token`): the media credential lives inside URLs on every worker
  pod, so it must not also unlock the internal API, and each rotates on its
  own schedule. A `worker:` request is refused outright when no
  `worker_media_token` is configured — a credential that cannot be checked
  cannot be granted.
* **Browsers — a BFF ticket.** An Ed25519 JWT minted by the BFF's
  `/api/v1/media/preview-ticket` (grant: `read`) or
  `/api/v1/evidence/requests/{id}/ticket` (grants: `read` + `playback`,
  docs/EVIDENCE.md) after an audited scope check. MediaMTX
  rewrites a request's `Authorization: Bearer <jwt>` header into `jwt=` in
  the `query` it forwards (auth/manager.go in 1.9.3), so tickets arrive the
  same way whether the client sent a header or a `?jwt=` URL. Verified
  against the BFF's public JWKS, fetched on demand and cached.

Semantics mirror `require_internal_token`: with `internal_token` unset the
enforcement is OFF and everything is allowed — the documented, loudly-logged
local default. With a token set this endpoint fails closed.

The endpoint itself is exempt from the `X-Internal-Token` middleware:
MediaMTX cannot send that header, and gating the credential check behind the
credential it exists to check would deadlock. That makes the route a
password-equality oracle by construction — two mitigations, since the gate
itself cannot exist:

* `hmac.compare_digest` on every secret comparison, same as the token gate
  it mirrors — no byte-at-a-time timing leak.
* A per-source-IP sliding-window rate limit (`SlidingWindowRateLimiter`
  below, enforced in `app.py:mediamtx_auth`), bounding guess attempts to
  `media_auth_rate_limit_attempts` per window.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel

from .config import RegistrySettings
from .mediamtx import MTX_API_USER, MTX_READER_USER

log = logging.getLogger(__name__)

__all__ = [
    "MediaMTXAuthRequest",
    "SlidingWindowRateLimiter",
    "TicketVerifier",
    "authorize",
    "log_safe",
]

_PATH_PREFIX = "cam-"
"""Every path this registry reconciles into MediaMTX is `cam-<uuid>`
(`mediamtx.path_name`). Internal credentials never read outside that prefix."""

_LOG_FIELD_MAX_LEN = 128
"""Ceiling on an attacker-controlled field echoed into a log line."""


def log_safe(value: str, *, max_len: int = _LOG_FIELD_MAX_LEN) -> str:
    """An attacker-controlled string made safe to put in a log line.

    `user`, `path` and friends arrive in the auth POST body — a caller can
    stuff newlines (forged log lines), terminal escapes, or a megabyte of
    padding into them. Kept characters are printable ASCII only; everything
    else (control chars, newlines, non-ASCII) becomes `?`, and the result is
    truncated at `max_len` with a `…` marker so truncation is visible rather
    than silent.
    """
    cleaned = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in value)
    if len(cleaned) > max_len:
        return cleaned[:max_len] + "…"
    return cleaned


class SlidingWindowRateLimiter:
    """Per-key sliding-window limiter, in-memory and dependency-free.

    Exists because `/api/v1/mediamtx/auth` cannot be credential-gated (it IS
    the credential check) and so must not be an unbounded guess-an-per-request
    oracle. Per-process is deliberate, same reasoning as the BFF's login
    limiter: a limiter that dies with Redis would take the video plane down
    with it, and per-pod is already enough to bound a stuffing script.
    """

    def __init__(
        self,
        max_attempts: int,
        window_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_attempts = max_attempts
        self._window_s = window_s
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Record one attempt; True while `key` has seen fewer than
        `max_attempts` inside the trailing `window_s`."""
        now = self._clock()
        with self._lock:
            events = self._events.setdefault(key, deque())
            while events and now - events[0] >= self._window_s:
                events.popleft()
            if len(events) >= self._max_attempts:
                return False
            events.append(now)
            if len(self._events) > 4096:
                # Bound the key map: a scanner sweeping source addresses would
                # otherwise grow `_events` one entry per distinct spoofed key.
                cutoff = now - self._window_s
                self._events = {k: v for k, v in self._events.items() if v and v[-1] >= cutoff}
            return True


class MediaMTXAuthRequest(BaseModel):
    """The body MediaMTX POSTs to `authHTTPAddress` (1.9.3 shape)."""

    ip: str = ""
    user: str = ""
    password: str = ""
    action: str = ""
    path: str = ""
    protocol: str = ""
    id: str | None = None
    query: str = ""


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _ticket_grants(payload: dict, *, action: str, path: str) -> bool:
    """MediaMTX's own permission semantics, minimally, minus its wildcard: an
    entry matches when the action is equal and the entry's path is exactly the
    requested one. The BFF mints exact `cam-<id>` paths only; `~`-regex
    entries are treated as literals and simply never match.

    An empty or absent `path` in a grant is NOT "any path" here — a ticket
    naming no path grants nothing. The BFF never mints one, so a grant that
    claims everything is indistinguishable from a malformed (or attacker-
    edited) payload and is refused the same way."""
    permissions = payload.get("mediamtx_permissions")
    if not isinstance(permissions, list):
        return False
    for entry in permissions:
        if not isinstance(entry, dict):
            continue
        if entry.get("action") != action:
            continue
        granted_path = entry.get("path")
        if granted_path and granted_path == path:
            return True
    return False


class TicketVerifier:
    """Verifies BFF preview tickets against the BFF's public JWKS.

    The JWKS is pulled lazily and cached for `media_auth_jwks_ttl_s`; a ticket
    naming a `kid` the cache does not hold forces one refresh and is refused
    if still unknown — a ticket we cannot verify is denied, never skipped.
    A JWKS fetch failure keeps serving the last good set: if the BFF is down
    no new tickets can be minted anyway, and the outstanding ones are
    seconds-lived.

    The forced refresh is negative-cached (`_UNKNOWN_KID_TTL_S`): without it,
    a flood of tickets all naming a bogus `kid` would turn this endpoint into
    a request amplifier against the BFF — every ticket costing a JWKS fetch.
    Within the window a still-unknown kid is denied locally; a real rotation
    becomes visible after the window, bounded and harmless.
    """

    _UNKNOWN_KID_TTL_S = 30.0
    """How long a `kid` confirmed absent from the JWKS stays denied without a
    re-fetch. Short because the BFF's keypair can be ephemeral per boot — a
    restart is the only legitimate new-kid event and 30 s bounds the cost."""

    def __init__(self, settings: RegistrySettings, client: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._client = client
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._fetched_at = 0.0
        self._unknown_kids: dict[str, float] = {}

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._s.media_auth_jwks_timeout_s)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _refresh(self) -> dict[str, Ed25519PublicKey]:
        http = await self._http()
        response = await http.get(self._s.media_auth_jwks_url)
        response.raise_for_status()
        keys: dict[str, Ed25519PublicKey] = {}
        for jwk in response.json().get("keys", []):
            if jwk.get("kty") == "OKP" and jwk.get("crv") == "Ed25519" and jwk.get("x"):
                try:
                    keys[jwk.get("kid") or ""] = Ed25519PublicKey.from_public_bytes(
                        _b64url_decode(jwk["x"])
                    )
                except (ValueError, binascii.Error):
                    log.warning("skipping undecodable JWKS key kid=%r", jwk.get("kid"))
        self._keys = keys
        self._fetched_at = time.monotonic()
        return keys

    async def _key_for(self, kid: str) -> Ed25519PublicKey | None:
        if not self._keys or time.monotonic() - self._fetched_at > self._s.media_auth_jwks_ttl_s:
            try:
                await self._refresh()
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                log.warning("media JWKS fetch failed: %s", exc)
        if kid in self._keys:
            return self._keys[kid]
        # Unknown kid — one forced refresh in case the BFF rotated its key
        # since the cache was filled, negative-cached so a flood of forged
        # tickets cannot each cost a fetch. Still unknown after that means
        # forged or expired-issuer tickets; deny.
        denied_at = self._unknown_kids.get(kid)
        if denied_at is not None and time.monotonic() - denied_at < self._UNKNOWN_KID_TTL_S:
            return None
        try:
            await self._refresh()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("media JWKS fetch failed: %s", exc)
        key = self._keys.get(kid)
        if key is None:
            self._unknown_kids[kid] = time.monotonic()
            if len(self._unknown_kids) > 1024:
                # Bound the negative cache the same way the JWKS itself is
                # bounded — forged kids are unbounded input.
                cutoff = time.monotonic() - self._UNKNOWN_KID_TTL_S
                self._unknown_kids = {k: t for k, t in self._unknown_kids.items() if t >= cutoff}
        else:
            self._unknown_kids.pop(kid, None)
        return key

    async def allows(self, token: str, *, action: str, path: str) -> bool:
        """True iff `token` is a valid BFF ticket granting `action` on `path`."""
        try:
            header_b64, payload_b64, sig_b64 = token.split(".")
            header = json.loads(_b64url_decode(header_b64))
            payload = json.loads(_b64url_decode(payload_b64))
            signature = _b64url_decode(sig_b64)
        except (ValueError, binascii.Error, json.JSONDecodeError):
            return False
        if header.get("alg") != "EdDSA" or not header.get("kid"):
            return False
        key = await self._key_for(str(header["kid"]))
        if key is None:
            return False
        try:
            key.verify(signature, f"{header_b64}.{payload_b64}".encode())
        except InvalidSignature:
            return False
        exp = payload.get("exp")
        if not isinstance(exp, int | float) or exp <= time.time():
            return False
        return _ticket_grants(payload, action=action, path=path)


async def authorize(
    settings: RegistrySettings, verifier: TicketVerifier, req: MediaMTXAuthRequest
) -> bool:
    """The credential check itself. Order matters: the internal-token path is
    tried first so machine traffic never depends on the BFF's key being
    reachable — a BFF outage must deny browser previews, not worker ingest."""
    if not settings.internal_token:
        # Enforcement off — the same documented fail-open default as
        # `require_internal_token` (no internal Secret = local dev).
        return True

    # Publishing is refused outright: we consume streams, never accept them.
    # Playback is NOT refused here — recordings do not exist yet (reconciled
    # paths set no `record`), but evidence tickets already carry a `playback`
    # grant so that enabling recording later changes nothing on this path
    # (docs/EVIDENCE.md). It remains ticket-only below: no internal credential
    # can claim it.
    if req.action == "publish":
        return False

    # Machine credentials are keyed on the user, and each user compares
    # against its OWN secret — `internal` against `internal_token` (API
    # callers), `worker` against `worker_media_token` (stream readers). The
    # two are independent: a leaked fan-out URL is a media credential, not an
    # internal-API one, and each rotates without dragging the other.
    if req.user == MTX_API_USER:
        if req.password and hmac.compare_digest(
            req.password.encode(), settings.internal_token.encode()
        ):
            # The reconcile client — path config on :9997. Metrics too: the
            # chart excludes `metrics` from the auth callback anyway, so an
            # arriving request means someone asked with the credential.
            return req.action in ("api", "metrics", "pprof")
        return False

    if req.user == MTX_READER_USER:
        if not settings.worker_media_token:
            # Fail closed: a credential that cannot be checked cannot be
            # granted. Enforcement is armed (internal_token is set), so the
            # empty reader token is a misconfiguration, not local dev.
            return False
        if req.password and hmac.compare_digest(
            req.password.encode(), settings.worker_media_token.encode()
        ):
            return req.action == "read" and req.path.startswith(_PATH_PREFIX)
        return False

    if req.action in ("read", "playback") and req.path.startswith(_PATH_PREFIX):
        token = parse_qs(req.query).get("jwt", [None])[0]
        if token:
            # `_ticket_grants` matches the action exactly: a preview ticket
            # (read only) cannot play back, and a playback evidence ticket
            # reaches only the `cam-<id>` path it names.
            return await verifier.allows(token, action=req.action, path=req.path)

    return False
