"""Password hashing and API key generation, tested in isolation from any
database — the one module in this service every other module trusts blindly,
so it is the one most worth pinning down on its own.
"""

from __future__ import annotations

from prahari_bff.security import (
    API_KEY_PREFIX,
    hash_api_key,
    hash_password,
    looks_like_api_key,
    new_api_key,
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
