"""Stage 4d: CSV row → camera create payload. The org-scope check itself is
`_check_target_org`, already covered by `test_camera_writes.py` — this file
covers what is new here: turning one CSV row into the same shape
`create_camera` sends the registry, without a database or an HTTP call.
"""

from __future__ import annotations

from prahari_bff.app import _row_to_camera_payload


def test_minimal_row_yields_only_external_id():
    payload = _row_to_camera_payload({"external_id": "cam-001"})
    assert payload == {"external_id": "cam-001"}


def test_blank_optional_fields_are_omitted_not_sent_as_empty_strings():
    row = {"external_id": "cam-001", "site_name": "", "district": "   "}
    payload = _row_to_camera_payload(row)
    assert "site_name" not in payload
    assert "district" not in payload


def test_string_fields_are_stripped_and_included():
    row = {"external_id": "cam-001", "site_name": "  Zone 4 Junction  ", "org_id": "org-ward-9"}
    payload = _row_to_camera_payload(row)
    assert payload["site_name"] == "Zone 4 Junction"
    assert payload["org_id"] == "org-ward-9"


def test_numeric_fields_are_converted():
    row = {
        "external_id": "cam-001",
        "native_width": "1920",
        "native_height": "1080",
        "declared_fps": "12.5",
    }
    payload = _row_to_camera_payload(row)
    assert payload["native_width"] == 1920
    assert payload["native_height"] == 1080
    assert payload["declared_fps"] == 12.5


def test_lat_lon_columns_become_a_nested_location():
    row = {"external_id": "cam-001", "latitude": "23.03", "longitude": "72.58"}
    payload = _row_to_camera_payload(row)
    assert payload["location"] == {"latitude": 23.03, "longitude": 72.58}


def test_only_one_of_lat_lon_is_dropped_not_partially_applied():
    row = {"external_id": "cam-001", "latitude": "23.03"}
    payload = _row_to_camera_payload(row)
    assert "location" not in payload


def test_credentials_pass_through_as_plaintext_for_the_registry_to_encrypt():
    """The BFF never encrypts — it forwards `stream_password` plaintext to
    the registry's own create path, same as a single manual `create_camera`
    call, so encryption happens exactly once, in one place."""
    row = {"external_id": "cam-001", "stream_username": "admin", "stream_password": "s3cr3t"}
    payload = _row_to_camera_payload(row)
    assert payload["stream_username"] == "admin"
    assert payload["stream_password"] == "s3cr3t"


def test_malformed_numeric_field_raises_value_error():
    """Caught by the caller (`import_cameras`) and turned into a per-row
    failure rather than aborting the whole batch — asserted here only as
    the raw behavior this function must have for that to work."""
    row = {"external_id": "cam-001", "native_width": "not-a-number"}
    try:
        _row_to_camera_payload(row)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a malformed numeric field")
