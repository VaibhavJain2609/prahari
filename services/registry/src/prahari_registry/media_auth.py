"""MediaMTX's credential check, answered by this service.

MediaMTX 1.9.x `authMethod` is exclusive — `internal`, `http` and `jwt` are
alternatives, not layers, so "internal users AND JWT" is not a configuration
that exists. We run `authMethod: http` (`templates/mediamtx-config.yaml`),
which POSTs every authentication decision here as
`{ip, user, password, action, path, protocol, id, query}` and treats any
non-2xx as a refusal. One endpoint then recognises the two credential shapes
the estate actually has:

* **Machines — HTTP basic.** `worker:<internal-token>` grants `read` on
  `cam-*` paths (embedded in the fan-out URLs `fanout_endpoints` hands to
  inference workers — RTSP consumers have no header channel). `internal:
  <internal-token>` grants `api` (this service's own reconcile client).
* **Browsers — a BFF preview ticket.** An Ed25519 JWT minted by the BFF's
  `/api/v1/media/preview-ticket` after an audited scope check. MediaMTX
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
password-equality oracle by construction — the comparison is
`hmac.compare_digest`, same as the token gate it mirrors.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import time
from urllib.parse import parse_qs

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel

from .config import RegistrySettings
from .mediamtx import MTX_API_USER, MTX_READER_USER

log = logging.getLogger(__name__)

__all__ = ["MediaMTXAuthRequest", "TicketVerifier", "authorize"]

_PATH_PREFIX = "cam-"
"""Every path this registry reconciles into MediaMTX is `cam-<uuid>`
(`mediamtx.path_name`). Internal credentials never read outside that prefix."""


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
    """MediaMTX's own permission semantics, minimally: an entry matches when
    the action is equal and the entry's path is empty (any path) or exactly
    the requested one. The BFF mints exact `cam-<id>` paths only; `~`-regex
    entries are treated as literals and simply never match."""
    permissions = payload.get("mediamtx_permissions")
    if not isinstance(permissions, list):
        return False
    for entry in permissions:
        if not isinstance(entry, dict):
            continue
        if entry.get("action") != action:
            continue
        granted_path = entry.get("path")
        if granted_path in (None, "") or granted_path == path:
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
    """

    def __init__(self, settings: RegistrySettings, client: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._client = client
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._fetched_at = 0.0

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
        # since the cache was filled. Still unknown after that means forged
        # or expired-issuer tickets; deny.
        try:
            await self._refresh()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("media JWKS fetch failed: %s", exc)
        return self._keys.get(kid)

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

    # Publishing and playback are refused outright: we consume streams, never
    # accept them, and no recording exists to play back.
    if req.action in ("publish", "playback"):
        return False

    if req.password and hmac.compare_digest(
        req.password.encode(), settings.internal_token.encode()
    ):
        if req.user == MTX_API_USER:
            # The reconcile client — path config on :9997. Metrics too: the
            # chart excludes `metrics` from the auth callback anyway, so an
            # arriving request means someone asked with the credential.
            return req.action in ("api", "metrics", "pprof")
        if req.user == MTX_READER_USER:
            return req.action == "read" and req.path.startswith(_PATH_PREFIX)
        return False

    if req.action == "read" and req.path.startswith(_PATH_PREFIX):
        token = parse_qs(req.query).get("jwt", [None])[0]
        if token:
            return await verifier.allows(token, action="read", path=req.path)

    return False
