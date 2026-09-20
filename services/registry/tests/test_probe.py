"""Stage 4c probe: the SSRF guard, SDP parsing, and digest-auth header
computation. `probe_rtsp` itself needs a real RTSP endpoint to exercise end
to end, so these tests hit the pieces around the socket I/O — the guard, the
resolver (against IPs that never touch the network), and the parsers.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from prahari_registry.probe import (
    _MAX_RESPONSE_BYTES,
    ProbeError,
    SSRFBlockedError,
    _digest_header,
    _guard_ip,
    _guard_port,
    _parse_challenge,
    _parse_sdp,
    _resolve_pinned_ip,
    _RTSPSession,
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


def test_guard_ip_rejects_ipv4_mapped_addresses():
    """`::ffff:a.b.c.d` is a v4 address in v6 clothes. Newer Pythons classify it
    by the mapped address; older ones do not — the explicit re-guard makes the
    answer the same everywhere, so this pins it."""
    with pytest.raises(SSRFBlockedError):
        _guard_ip("::ffff:127.0.0.1")
    with pytest.raises(SSRFBlockedError):
        _guard_ip("::ffff:169.254.169.254")


def test_guard_ip_allows_ipv4_mapped_private():
    """A mapped RFC1918 address is the same DVR case as a literal one."""
    _guard_ip("::ffff:10.0.0.5")  # must not raise


def test_guard_port_rejects_ports_outside_the_allowlist():
    with pytest.raises(SSRFBlockedError):
        _guard_port(22, {554})
    with pytest.raises(SSRFBlockedError):
        _guard_port(6379, {554})
    _guard_port(554, {554})  # the default
    _guard_port(8554, {554, 8554})  # configured extras pass


async def test_probe_rejects_disallowed_port_before_dns(monkeypatch):
    """No reason to resolve a host we would refuse to connect to anyway."""
    from prahari_registry import probe

    async def _resolve_should_not_run(host):
        raise AssertionError("DNS was consulted for a refused port")

    monkeypatch.setattr(probe, "_resolve_pinned_ip", _resolve_should_not_run)
    with pytest.raises(SSRFBlockedError):
        await probe_rtsp("rtsp://10.0.0.5:22/stream")


async def test_probe_invalid_port_is_a_probe_error_not_a_500():
    # urlsplit defers port validation to `.port` access.
    with pytest.raises(ProbeError):
        await probe_rtsp("rtsp://10.0.0.5:99999/stream")
    with pytest.raises(ProbeError):
        await probe_rtsp("rtsp://10.0.0.5:abc/stream")


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


# --- response framing -----------------------------------------------------------
#
# `_RTSPSession.request` reads a live socket. These fakes stand in for the
# StreamReader/StreamWriter pair so the framing edge cases — oversized bodies,
# hostile Content-Length headers, closed connections — are exercised without a
# socket at all.


class FakeWriter:
    def __init__(self) -> None:
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass


class FakeReader:
    """`readuntil` returns the canned header block; `read(n)` serves `chunks`
    respecting n, the way a real StreamReader does."""

    def __init__(
        self,
        header_block: bytes = b"RTSP/1.0 200 OK\r\n\r\n",
        chunks: list[bytes] | None = None,
        readuntil_error: Exception | None = None,
    ) -> None:
        self._header_block = header_block
        self._chunks = list(chunks or [])
        self._readuntil_error = readuntil_error
        self.bytes_read = 0

    async def readuntil(self, sep: bytes) -> bytes:
        if self._readuntil_error is not None:
            raise self._readuntil_error
        return self._header_block

    async def read(self, n: int) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks[0][:n]
        self._chunks[0] = self._chunks[0][n:]
        if not self._chunks[0]:
            self._chunks.pop(0)
        self.bytes_read += len(chunk)
        return chunk


async def _request(reader: FakeReader) -> dict:
    session = _RTSPSession(reader, FakeWriter(), "rtsp://10.0.0.5/x")
    return await session.request("OPTIONS")


async def test_response_body_is_capped_in_total():
    """The cap is on the TOTAL body, not each socket read: a huge
    Content-Length must not be fetched in full 64 KiB at a time."""
    big = 3 * _MAX_RESPONSE_BYTES
    reader = FakeReader(
        header_block=f"RTSP/1.0 200 OK\r\nContent-Length: {big}\r\n\r\n".encode(),
        chunks=[b"x" * _MAX_RESPONSE_BYTES, b"x" * _MAX_RESPONSE_BYTES],
    )
    resp = await _request(reader)
    assert len(resp["body"]) == _MAX_RESPONSE_BYTES
    assert reader.bytes_read == _MAX_RESPONSE_BYTES  # nothing past the cap was pulled


async def test_malformed_content_length_is_a_probe_error():
    reader = FakeReader(
        header_block=b"RTSP/1.0 200 OK\r\nContent-Length: banana\r\n\r\n",
    )
    with pytest.raises(ProbeError):
        await _request(reader)


async def test_oversized_header_block_is_a_probe_error():
    """readuntil raises LimitOverrunError when the peer stuffs more than the
    stream limit into the headers — a policy failure of the endpoint, not ours."""
    reader = FakeReader(
        readuntil_error=asyncio.LimitOverrunError("header too long", _MAX_RESPONSE_BYTES)
    )
    with pytest.raises(ProbeError):
        await _request(reader)


async def test_connection_closed_mid_response_is_a_probe_error():
    reader = FakeReader(
        readuntil_error=asyncio.IncompleteReadError(b"RTSP/1.0 200", expected=4)
    )
    with pytest.raises(ProbeError):
        await _request(reader)


async def test_body_shorter_than_content_length_returns_what_arrived():
    reader = FakeReader(
        header_block=b"RTSP/1.0 200 OK\r\nContent-Length: 100\r\n\r\n",
        chunks=[b"short"],  # peer closes early — read() returns b"" after this
    )
    resp = await _request(reader)
    assert resp["body"] == b"short"
