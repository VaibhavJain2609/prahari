"""Camera stream credentials: AES-GCM roundtrip, wrong-key failure, URL
injection, and the invariant the whole feature exists for — the secret is
absent from every response model, not masked.
"""

from __future__ import annotations

import base64
import secrets

import pytest

from prahari_registry.crypto import CredentialKeyError, decrypt_credential, encrypt_credential
from prahari_registry.models import Camera, CameraCreate, CameraUpdate
from prahari_registry.repository import _with_credentials


def _key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def test_encrypt_then_decrypt_roundtrips():
    key = _key()
    blob = encrypt_credential("s3cret!", key)
    assert decrypt_credential(blob, key) == "s3cret!"


def test_encrypt_is_nondeterministic_because_the_nonce_is_random():
    key = _key()
    assert encrypt_credential("same-password", key) != encrypt_credential("same-password", key)


def test_decrypt_with_the_wrong_key_raises_credential_key_error():
    blob = encrypt_credential("s3cret!", _key())
    with pytest.raises(CredentialKeyError):
        decrypt_credential(blob, _key())


def test_missing_key_raises_credential_key_error_not_a_generic_error():
    with pytest.raises(CredentialKeyError):
        encrypt_credential("s3cret!", "")


def test_malformed_key_raises_credential_key_error():
    with pytest.raises(CredentialKeyError):
        encrypt_credential("s3cret!", "not-valid-base64-and-wrong-length")


def test_with_credentials_percent_encodes_special_characters():
    url = _with_credentials("rtsp://10.0.0.5:554/ch1", "admin", "p@ss:w0rd/x")
    assert url == "rtsp://admin:p%40ss%3Aw0rd%2Fx@10.0.0.5:554/ch1"


def test_with_credentials_handles_a_blank_username():
    url = _with_credentials("rtsp://10.0.0.5/ch1", None, "secret")
    assert url == "rtsp://:secret@10.0.0.5/ch1"


# --- the gate: the secret never appears in a response shape ------------------


def test_camera_response_model_has_no_credential_field():
    assert "stream_secret" not in Camera.model_fields
    assert "stream_password" not in Camera.model_fields
    assert "stream_username" not in Camera.model_fields


def test_camera_create_and_update_accept_credentials_as_input_only():
    # The write-side models legitimately carry the plaintext password in --
    # that is the only way an operator can supply it -- but the encrypted
    # column name never appears here either, so there is no path by which a
    # ciphertext blob could round-trip back out through one of these models.
    assert "stream_password" in CameraCreate.model_fields
    assert "stream_password" in CameraUpdate.model_fields
    assert "stream_secret" not in CameraCreate.model_fields
    assert "stream_secret" not in CameraUpdate.model_fields


def test_camera_serialized_to_json_never_contains_the_word_password_or_secret():
    camera = Camera(id="cam-1", source="manual", external_id="ext-1")
    dumped = camera.model_dump_json()
    assert "password" not in dumped
    assert "secret" not in dumped
