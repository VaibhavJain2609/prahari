"""Stage 4c probe: the SSRF guard, SDP parsing, and digest-auth header
computation. `probe_rtsp` itself needs a real RTSP endpoint to exercise end
to end, so these tests hit the pieces around the socket I/O — the guard, the
resolver (against IPs that never touch the network), and the parsers.
"""

from __future__ import annotations

import hashlib

import pytest

from prahari_registry.probe import (
    ProbeError,
    SSRFBlockedError,
    _digest_header,
    _guard_ip,
    _parse_challenge,
    _parse_sdp,
    _resolve_pinned_ip,
    probe_rtsp,
)


def test_guard_ip_rejects_loopback():
    with pytest.raises(SSRFBlockedError):
        _guard_ip("127.0.0.1")


def test_guard_ip_rejects_link_local():
    with pytest.raises(SSRFBlockedError):
        _guard_ip("169.254.169.254")


def test_guard_ip_rejects_multicast():
    with pytest.raises(SSRFBlockedError):
        _guard_ip("224.0.0.1")


def test_guard_ip_rejects_unspecified():
    with pytest.raises(SSRFBlockedError):
        _guard_ip("0.0.0.0")


def test_guard_ip_rejects_ipv6_link_local():
    with pytest.raises(SSRFBlockedError):
        _guard_ip("fe80::1")


def test_guard_ip_allows_rfc1918_private_ranges():
    """DVRs legitimately live here — the whole point of not blanket-blocking
    private IPs."""
    for ip in ("10.0.0.5", "172.16.0.5", "192.168.1.50"):
        _guard_ip(ip)  # must not raise


def test_guard_ip_allows_public_addresses():
    _guard_ip("8.8.8.8")  # must not raise


async def test_resolve_pinned_ip_rejects_loopback_numeric_host():
    with pytest.raises(SSRFBlockedError):
        await _resolve_pinned_ip("127.0.0.1")


async def test_resolve_pinned_ip_allows_private_numeric_host():
    assert await _resolve_pinned_ip("10.0.0.5") == "10.0.0.5"


async def test_probe_rtsp_rejects_non_rtsp_scheme_before_any_network_io():
    with pytest.raises(ProbeError):
        await probe_rtsp("http://192.168.1.50/stream1")


async def test_probe_rtsp_rejects_url_with_no_host():
    with pytest.raises(ProbeError):
        await probe_rtsp("rtsp:///stream1")


def test_parse_sdp_extracts_codec_and_declared_fps_and_media_lines():
    body = (
        b"v=0\r\n"
        b"o=- 0 0 IN IP4 127.0.0.1\r\n"
        b"m=video 0 RTP/AVP 96\r\n"
        b"a=rtpmap:96 H264/90000\r\n"
        b"a=framerate:25.0\r\n"
        b"m=audio 0 RTP/AVP 97\r\n"
        b"a=rtpmap:97 PCMA/8000\r\n"
    )
    codec, declared_fps, media_lines = _parse_sdp(body)
    assert codec == "H264"
    assert declared_fps == 25.0
    assert media_lines == ["m=video 0 RTP/AVP 96", "m=audio 0 RTP/AVP 97"]


def test_parse_sdp_uses_first_rtpmap_codec_only():
    body = b"a=rtpmap:96 H264/90000\r\na=rtpmap:97 H265/90000\r\n"
    codec, _, _ = _parse_sdp(body)
    assert codec == "H264"


def test_parse_sdp_handles_x_framerate_attribute():
    body = b"a=x-framerate:12.5\r\n"
    _, declared_fps, _ = _parse_sdp(body)
    assert declared_fps == 12.5


def test_parse_sdp_missing_attributes_returns_none():
    codec, declared_fps, media_lines = _parse_sdp(b"v=0\r\n")
    assert codec is None
    assert declared_fps is None
    assert media_lines == []


def test_parse_challenge_extracts_scheme_and_params():
    header = 'Digest realm="camera", nonce="abc123", algorithm="MD5"'
    scheme, params = _parse_challenge(header)
    assert scheme == "Digest"
    assert params["realm"] == "camera"
    assert params["nonce"] == "abc123"


def test_digest_header_matches_hand_computed_rfc2069_response():
    challenge = {"realm": "camera", "nonce": "abc123"}
    header = _digest_header(
        method="DESCRIBE",
        uri="rtsp://192.168.1.50/stream1",
        username="admin",
        password="secret",
        challenge=challenge,
    )
    ha1 = hashlib.md5(b"admin:camera:secret").hexdigest()  # noqa: S324
    ha2 = hashlib.md5(b"DESCRIBE:rtsp://192.168.1.50/stream1").hexdigest()  # noqa: S324
    expected_response = hashlib.md5(f"{ha1}:abc123:{ha2}".encode()).hexdigest()  # noqa: S324
    assert f'response="{expected_response}"' in header
    assert 'username="admin"' in header
    assert 'realm="camera"' in header
