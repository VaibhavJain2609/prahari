"""AES-GCM encryption for camera stream credentials.

Credentials for locally-registered analog/DVR cameras (Stage 4,
docs/ORG-TIERS-DESIGN.md §2.4 / the plan's Stage 4 credential note) are the
one piece of camera data this service must never hand back in a response —
not masked, absent. `camera_from_row` (repository.py) never reads
`stream_secret`, and `Camera`/`CameraCreate`'s response shape has no field
for it; encrypting at rest is defense in depth for the database itself, on
top of that absence, not a substitute for it.

`PRAHARI_CREDENTIAL_KEY` is a 32-byte AES-256 key, urlsafe-base64-encoded in
the env — generate one with:
    python -c "import secrets,base64;print(
        base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
AES-GCM needs a fresh 12-byte nonce per encryption; it is not secret, so it
is stored prepended to the ciphertext: `stream_secret = nonce || ciphertext`
(the GCM tag is part of what `AESGCM.encrypt` appends to the ciphertext).
"""

from __future__ import annotations

import base64
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["CredentialKeyError", "decrypt_credential", "encrypt_credential"]

_NONCE_LEN = 12
_KEY_LEN = 32


class CredentialKeyError(ValueError):
    """Raised only when a credential actually needs encrypting or decrypting
    and `PRAHARI_CREDENTIAL_KEY` is missing or malformed — never at import
    time, so a registry with no locally-registered cameras carrying
    credentials never needs the setting configured at all."""


def _load_key(key_b64: str) -> bytes:
    if not key_b64:
        raise CredentialKeyError(
            "PRAHARI_CREDENTIAL_KEY is not set — required to store or read a camera "
            "stream credential"
        )
    try:
        key = base64.urlsafe_b64decode(key_b64)
    except Exception as exc:
        raise CredentialKeyError("PRAHARI_CREDENTIAL_KEY is not valid base64") from exc
    if len(key) != _KEY_LEN:
        raise CredentialKeyError(
            f"PRAHARI_CREDENTIAL_KEY must decode to {_KEY_LEN} bytes (AES-256), "
            f"got {len(key)}"
        )
    return key


def encrypt_credential(plaintext: str, key_b64: str) -> bytes:
    key = _load_key(key_b64)
    nonce = secrets.token_bytes(_NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ciphertext


def decrypt_credential(blob: bytes, key_b64: str) -> str:
    key = _load_key(key_b64)
    nonce, ciphertext = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise CredentialKeyError(
            "stream credential failed to decrypt — wrong PRAHARI_CREDENTIAL_KEY, or "
            "the stored value is corrupt"
        ) from exc
    return plaintext.decode("utf-8")
