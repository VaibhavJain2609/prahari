"""Password hashing and API key generation, tested in isolation from any
database — the one module in this service every other module trusts blindly,
so it is the one most worth pinning down on its own.
"""

from __future__ import annotations

import uuid

from prahari_bff.security import (
    API_KEY_PREFIX,
    SlidingWindowRateLimiter,
    hash_api_key,
    hash_password,
    hash_session_id,
    looks_like_api_key,
    new_api_key,
    new_session_id,
    verify_password,
)


def test_hash_and_verify_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)


def test_verify_rejects_wrong_password():
    h = hash_password("correct horse battery staple")
    assert not verify_password("wrong password", h)


def test_verify_rejects_a_malformed_hash_without_raising():
    """A hand-edited row or a foreign hash format must fail closed as "wrong
    password", not surface as a 500 that leaks whether the account exists."""
    assert not verify_password("anything", "not-an-argon2-hash")


def test_hash_never_contains_the_plaintext():
    h = hash_password("hunter2")
    assert "hunter2" not in h


def test_new_api_key_has_the_prefix_and_a_matching_hash():
    plaintext, key_hash = new_api_key()
    assert plaintext.startswith(API_KEY_PREFIX)
    assert key_hash == hash_api_key(plaintext)
    assert key_hash != plaintext


def test_new_api_key_is_unique_per_call():
    first, _ = new_api_key()
    second, _ = new_api_key()
    assert first != second


def test_looks_like_api_key():
    assert looks_like_api_key("pk_abc123")
    assert not looks_like_api_key("some-session-cookie-value")
    assert not looks_like_api_key("")


def test_session_cookie_is_a_fresh_token_per_call():
    assert new_session_id() != new_session_id()


def test_session_hash_fits_the_uuid_column_and_is_not_the_cookie():
    """`sessions.id` stores sha256(cookie) truncated to 128 bits rendered as
    a uuid — a leaked row must not be a usable credential, and the existing
    `uuid` column must still accept it."""
    cookie = new_session_id()
    stored = hash_session_id(cookie)
    assert uuid.UUID(stored)  # parses — the column accepts it
    assert stored != cookie
    assert hash_session_id(cookie) == stored  # deterministic lookup key
    assert hash_session_id(new_session_id()) != stored


def test_rate_limiter_allows_up_to_the_cap_then_denies():
    limiter = SlidingWindowRateLimiter(max_attempts=3, window_s=60.0)
    assert [limiter.allow("u:alice") for _ in range(3)] == [True, True, True]
    assert limiter.allow("u:alice") is False
    # A different key is unaffected.
    assert limiter.allow("u:bob") is True


def test_rate_limiter_frees_slots_as_the_window_slides():
    now = [1000.0]
    limiter = SlidingWindowRateLimiter(max_attempts=2, window_s=60.0, clock=lambda: now[0])
    assert limiter.allow("u:alice") is True
    assert limiter.allow("u:alice") is True
    assert limiter.allow("u:alice") is False
    now[0] += 61.0  # both events have aged out
    assert limiter.allow("u:alice") is True


def test_rate_limiter_expires_only_the_aged_head_of_a_keys_window():
    """A key whose oldest attempt has slid out of the window frees exactly
    that slot — the newer attempts inside the window still count."""
    now = [1000.0]
    limiter = SlidingWindowRateLimiter(max_attempts=2, window_s=60.0, clock=lambda: now[0])
    assert limiter.allow("u:alice") is True
    now[0] += 30
    assert limiter.allow("u:alice") is True  # deque is full: [t0, t0+30]
    now[0] += 31  # t0+61 — the head has aged out, the tail has not
    assert limiter.allow("u:alice") is True
    # Still two live events now — the next call is back at the cap.
    assert limiter.allow("u:alice") is False


def test_rate_limiter_denied_attempts_do_not_extend_the_ban_forever():
    """Denied attempts are not recorded — otherwise a sustained spray would
    starve a legitimate user of that username indefinitely."""
    now = [1000.0]
    limiter = SlidingWindowRateLimiter(max_attempts=1, window_s=60.0, clock=lambda: now[0])
    assert limiter.allow("u:alice") is True
    now[0] += 30  # inside the window — denied, not recorded
    assert limiter.allow("u:alice") is False
    now[0] += 31  # the one recorded attempt has now aged out
    assert limiter.allow("u:alice") is True
