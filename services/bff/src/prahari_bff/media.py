"""Media preview tickets — the audited way a browser reaches a stream.

MediaMTX 1.9.x runs `authMethod: http` and defers every credential check to
the registry's `/api/v1/mediamtx/auth`. What a browser sends is not a shared
password but a ticket minted here: a short-lived Ed25519 JWT carrying

    {"mediamtx_permissions": [{"action": "read", "path": "cam-<camera_id>"}]}

— the claim name and `{action, path}` shape are the ones MediaMTX's own JWT
mode defines (verified against v1.9.3's mediamtx.yml), which the registry's
auth callback honours identically under `authMethod: http`.

The chain of custody is the invariant: `POST /api/v1/media/preview-ticket`
audits `video_preview` *before* minting (fail closed), and every ticket names
one camera path — a ticket is a scoped grant, not a media-plane passport.

The signing key is Ed25519 because tickets are minted on the request path and
verified per connection: it signs and verifies fast and the public half
serialises into a one-key JWKS. `PRAHARI_MEDIA_JWT_PRIVATE_KEY` (PEM) makes
the key stable across restarts; unset, an ephemeral keypair is generated at
boot and tickets simply stop verifying when the pod restarts — acceptable
because they are seconds-lived and re-minted on demand.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_private_key,
)

from .config import BFFSettings

log = logging.getLogger(__name__)

__all__ = ["MediaTicketIssuer"]

_ISSUER = "prahari-bff"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class MediaTicketIssuer:
    """Owns the Ed25519 keypair and mints per-camera read tickets."""

    def __init__(self, settings: BFFSettings) -> None:
        if settings.media_jwt_private_key:
            loaded = load_pem_private_key(settings.media_jwt_private_key.encode(), password=None)
            if not isinstance(loaded, Ed25519PrivateKey):
                raise TypeError("PRAHARI_MEDIA_JWT_PRIVATE_KEY must be an Ed25519 key")
            self._private = loaded
        else:
            self._private = Ed25519PrivateKey.generate()
            log.warning(
                "PRAHARI_MEDIA_JWT_PRIVATE_KEY unset — generated an ephemeral "
                "media-ticket keypair. Preview tickets stop verifying on pod "
                "restart; set the key (prahari-internal/media-jwt-private-key) "
                "where that matters."
            )
        self._public: Ed25519PublicKey = self._private.public_key()
        # kid = a stable fingerprint of the public key, so a verifier fetching
        # the JWKS can tell at a glance whether it holds the right key.
        raw_public = self._public.public_bytes(Encoding.Raw, PublicFormat.Raw)
        self._kid = _b64url(hashlib.sha256(raw_public).digest()[:12])

    @property
    def kid(self) -> str:
        return self._kid

    def jwks(self) -> dict:
        """The public half, RFC 8038 OKP form. Served unauthenticated — it is
        a public key; secrecy would add nothing and the registry fetches it
        without credentials."""
        return {
            "keys": [
                {
                    "kty": "OKP",
                    "crv": "Ed25519",
                    "x": _b64url(self._public.public_bytes(Encoding.Raw, PublicFormat.Raw)),
                    "kid": self._kid,
                    "alg": "EdDSA",
                    "use": "sig",
                }
            ]
        }

    def mint(self, *, subject: str, camera_id: str, ttl_s: int) -> str:
        """One ticket = one principal, one camera path, one short TTL.

        `path` is the MediaMTX path name — `cam-<internal camera id>`, the
        same naming `prahari_registry.mediamtx.path_name` produces, so a
        ticket authorises exactly the stream the audit entry names.
        """
        now = int(time.time())
        header = {"alg": "EdDSA", "typ": "JWT", "kid": self._kid}
        payload = {
            "iss": _ISSUER,
            "sub": subject,
            "jti": uuid.uuid4().hex,
            "iat": now,
            "exp": now + ttl_s,
            "camera_id": camera_id,
            "mediamtx_permissions": [{"action": "read", "path": f"cam-{camera_id}"}],
        }
        signing_input = (
            f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}."
            f"{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
        )
        signature = self._private.sign(signing_input.encode())
        return f"{signing_input}.{_b64url(signature)}"
