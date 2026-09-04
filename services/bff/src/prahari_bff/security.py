"""Password hashing and API key generation.

Kept as one small module with no database or FastAPI import, so it is exactly
as easy to unit-test as the plate-grammar module `prahari-common` uses for the
same reason: the thing most worth testing in isolation is the thing every
other module trusts blindly.
"""

from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()

API_KEY_PREFIX = "pk_"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Never raises. A malformed or foreign hash (e.g. a hand-edited row) must
    fail closed as "wrong password", not surface as a 500 that leaks whether
    the row exists."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


def new_session_id_cookie_value(session_id: str) -> str:
    """The session id IS the cookie value — see migrations/006_identity.sql's
    comment on `sessions.id`. This wrapper exists so call sites read as
    "the cookie value" rather than reaching for a uuid directly, in case that
    ever needs to change to a separately-hashed token."""
    return session_id


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def new_api_key() -> tuple[str, str]:
    """Returns (plaintext, hash). Only the hash is ever persisted; the
    plaintext is returned to the caller once and never again — losing it
    means issuing a new key, the same as losing a password means resetting
    it."""
    plaintext = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return plaintext, hash_api_key(plaintext)


def looks_like_api_key(bearer_token: str) -> bool:
    return bearer_token.startswith(API_KEY_PREFIX)
