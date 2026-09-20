"""The pure helpers in `repository.py` — URL redaction and the `observed_at`
clamp — tested without a database.

Both exist because of specific failure modes: decrypted DVR credentials must
never reach an HTTP response, and a far-future heartbeat timestamp must never
be allowed to pin `last_heartbeat_at` forward via GREATEST.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from prahari_registry.repository import (
    _MAX_OBSERVED_AT_SKEW_S,
    _clamp_observed_at,
    redact_url_credentials,
)


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
