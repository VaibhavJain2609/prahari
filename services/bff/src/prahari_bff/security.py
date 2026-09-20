"""Password hashing and API key generation.

Kept as one small module with no database or FastAPI import, so it is exactly
as easy to unit-test as the plate-grammar module `prahari-common` uses for the
same reason: the thing most worth testing in isolation is the thing every
other module trusts blindly.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable

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


def new_session_id() -> str:
    """The session cookie value — a fresh 256-bit token. Never stored; what
    reaches the database is `hash_session_id`'s output."""
    return secrets.token_urlsafe(32)


def hash_session_id(cookie_value: str) -> str:
    """What the `sessions` table stores: sha256(cookie) truncated to 128 bits
    and rendered as a UUID, so it fits the existing `sessions.id uuid` column
    (migrations/006_identity.sql, owned by the registry's migration runner)
    with no schema change.

    Residual risk: 128 of sha256's 256 bits survive — still more than uuid
    v4's 122, so no entropy is lost relative to the scheme this replaces,
    while a leaked sessions row now yields an unusable hash rather than a
    live credential. A full-width hash column would need a registry-owned
    migration; flagged for sequencing rather than done here."""
    digest = hashlib.sha256(cookie_value.encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest[:16]))


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


class SlidingWindowRateLimiter:
    """Per-key sliding-window limiter, in-memory and dependency-free.

    Used for the login endpoint, where "correct" protection is a shared
    store (Redis) — deliberately not taken here: a limiter that dies with
    Redis would take login down with it, and per-process is already enough
    to blunt a credential-stuffing script against a single pod. Callers key
    it on things like `u:{username}` and `ip:{client}`.
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
        self._last_prune = 0.0

    def allow(self, key: str) -> bool:
        """Record one attempt; True while `key` has seen fewer than
        `max_attempts` inside the trailing `window_s`."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            events = self._events.setdefault(key, deque())
            while events and now - events[0] >= self._window_s:
                events.popleft()
            if len(events) >= self._max_attempts:
                return False
            events.append(now)
            return True

    def _prune(self, now: float) -> None:
        """Drop keys whose newest event has aged out, once per window. A
        limiter keyed partly on attacker input (usernames) must not itself
        be an unbounded-memory sink."""
        if now - self._last_prune < self._window_s:
            return
        self._last_prune = now
        cutoff = now - self._window_s
        for key in [k for k, v in self._events.items() if not v or v[-1] < cutoff]:
            del self._events[key]
