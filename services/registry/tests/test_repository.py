"""The pure helpers in `repository.py` — URL redaction and the `observed_at`
clamp — tested without a database.

Both exist because of specific failure modes: decrypted DVR credentials must
never reach an HTTP response, and a far-future heartbeat timestamp must never
be allowed to pin `last_heartbeat_at` forward via GREATEST.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from prahari_registry.config import RegistrySettings
from prahari_registry.repository import (
    _MAX_OBSERVED_AT_SKEW_S,
    _clamp_observed_at,
    camera_from_row,
    redact_url_credentials,
)


def _row(**overrides) -> dict:
    """A `camera_current`-shaped record (a dict stands in for asyncpg.Record —
    `camera_from_row` only ever does `row["name"]` lookups)."""
    row = {
        "id": "00000000-0000-0000-0000-0000000000ab",
        "source": "gujarat-sentinel",
        "external_id": "cam-42",
        "latitude": 23.0,
        "longitude": 72.5,
        "site_name": "Zone 4 junction",
        "district": "Ahmedabad",
        "department": None,
        "owner": None,
        "org_id": "00000000-0000-0000-0000-000000000001",
        "adapter": "gateway",
        "camera_type": "ip",
        "vendor": None,
        "vms_platform": None,
        "codec": "h264",
        "native_width": 1920,
        "native_height": 1080,
        "rtsp_url": "rtsp://gateway.gov.example:8554/stream/42",
        "hls_url": "https://gateway.gov.example/hls/42.m3u8",
        "whep_url": None,
        "storage_location": None,
        "retention_days": None,
        "commissioned_at": None,
        "amc_expires_at": None,
        "lifecycle": "active",
        "catalogue_live": True,
        "present_in_catalogue": True,
        "last_seen_in_catalogue": None,
        "effective_health_state": "healthy",
        "effective_health_reason": None,
        "last_heartbeat_at": None,
        "last_frame_at": None,
        "observed_fps": 8.0,
        "declared_fps": 25.0,
        "black_frame_ratio": None,
        "tamper_suspected": False,
        "consecutive_failures": 0,
        "loop_epoch": 0,
        "last_error": None,
        "created_at": None,
        "updated_at": None,
    }
    row.update(overrides)
    return row


def test_response_never_carries_upstream_urls():
    """The catalogued `rtsp_url` is the government's pull URL — the invariant
    this whole change exists for. A Camera must contain no upstream URL and
    no credential, whatever the row holds."""
    settings = RegistrySettings(internal_token="tok", mediamtx_public_host="mtx")
    camera = camera_from_row(_row(), settings)
    dumped = camera.model_dump()

    assert "rtsp_url" not in dumped["endpoints"]
    assert "hls_url" not in dumped["endpoints"]
    assert "whep_url" not in dumped["endpoints"]
    text = str(dumped)
    assert "gateway.gov.example" not in text

    # What remains: fan-out URLs for workers (credentialed) and the public
    # capability flag.
    assert dumped["endpoints"]["fanout_rtsp_url"].startswith("rtsp://worker:tok@mtx:8554/")
    assert dumped["preview"]["available"] is True


def test_camera_without_upstream_gets_no_endpoints():
    """A camera MediaMTX cannot pull advertises no fan-out and no preview —
    `desired_mediamtx_paths` would never create the path, so the response
    must not pretend it exists."""
    camera = camera_from_row(_row(rtsp_url=None), RegistrySettings())
    assert camera.endpoints.fanout_rtsp_url is None
    assert camera.preview.available is False


def test_redact_strips_userinfo():
    assert (
        redact_url_credentials("rtsp://admin:s3cret@10.0.0.5:554/ch1") == "rtsp://10.0.0.5:554/ch1"
    )


def test_redact_handles_escaped_password_and_missing_port():
    # DVR factory passwords routinely contain `@` and `:`; they arrive
    # percent-escaped (see `_with_credentials`) and the netloc still has to
    # come apart cleanly.
    assert (
        redact_url_credentials("rtsp://u:p%40ss%3Aword@dvr.local/live") == "rtsp://dvr.local/live"
    )


def test_redact_leaves_credential_free_urls_untouched():
    url = "rtsp://10.0.0.5:554/ch1?x=1"
    assert redact_url_credentials(url) == url


def test_clamp_observed_at_defaults_to_now():
    before = datetime.now(UTC)
    got = _clamp_observed_at(None)
    assert before <= got <= datetime.now(UTC)


def test_clamp_observed_at_keeps_past_timestamps():
    ts = datetime.now(UTC) - timedelta(seconds=30)
    assert _clamp_observed_at(ts) == ts


def test_clamp_observed_at_tolerates_small_clock_skew():
    """Workers' clocks are not perfect; inside the skew window the report is
    trusted as stamped — clamping it would just shift real observations."""
    ts = datetime.now(UTC) + timedelta(seconds=_MAX_OBSERVED_AT_SKEW_S - 1)
    assert _clamp_observed_at(ts) == ts


def test_clamp_observed_at_clamps_a_far_future_timestamp():
    """The bug this exists for: `last_heartbeat_at` updates with GREATEST, so
    one report stamped next year would suppress the staleness overlay and make
    a dead camera read healthy forever."""
    future = datetime.now(UTC) + timedelta(days=30)
    got = _clamp_observed_at(future)
    assert got <= datetime.now(UTC)
    assert got > datetime.now(UTC) - timedelta(seconds=5)


def test_clamp_observed_at_treats_naive_as_utc():
    naive_future = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)
    assert _clamp_observed_at(naive_future) <= datetime.now(UTC)
