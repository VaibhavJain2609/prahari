"""Stage 4c probe: the SSRF guard, SDP parsing, and digest-auth header
computation. `probe_rtsp` itself needs a real RTSP endpoint to exercise end
to end, so these tests hit the pieces around the socket I/O — the guard, the
resolver (against IPs that never touch the network), and the parsers.
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
from base64 import b64encode
from collections import deque

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
    reader = FakeReader(readuntil_error=asyncio.IncompleteReadError(b"RTSP/1.0 200", expected=4))
    with pytest.raises(ProbeError):
        await _request(reader)


async def test_body_shorter_than_content_length_returns_what_arrived():
    reader = FakeReader(
        header_block=b"RTSP/1.0 200 OK\r\nContent-Length: 100\r\n\r\n",
        chunks=[b"short"],  # peer closes early — read() returns b"" after this
    )
    resp = await _request(reader)
    assert resp["body"] == b"short"


async def test_header_read_timeout_is_a_probe_error():
    reader = FakeReader(readuntil_error=TimeoutError())
    with pytest.raises(ProbeError, match="timed out"):
        await _request(reader)


async def test_body_read_timeout_is_a_probe_error():
    reader = FakeReader(header_block=b"RTSP/1.0 200 OK\r\nContent-Length: 100\r\n\r\n")

    async def _stall(n: int) -> bytes:
        raise TimeoutError

    reader.read = _stall
    with pytest.raises(ProbeError, match="timed out"):
        await _request(reader)


def test_parse_sdp_ignores_a_non_numeric_framerate():
    """A malformed `a=framerate` is a source quirk, not a probe failure."""
    _, declared_fps, _ = _parse_sdp(b"a=framerate:soon-ish\r\n")
    assert declared_fps is None


async def test_probe_malformed_url_is_a_probe_error():
    # urlsplit itself raises on a broken IPv6 literal.
    with pytest.raises(ProbeError, match="malformed URL"):
        await probe_rtsp("rtsp://[::1")


async def test_resolve_pinned_ip_dns_failure_is_a_probe_error(monkeypatch):
    loop = asyncio.get_running_loop()

    async def _no_dns(host, *args, **kwargs):
        raise OSError("name or service not known")

    monkeypatch.setattr(loop, "getaddrinfo", _no_dns)
    with pytest.raises(ProbeError, match="could not resolve"):
        await _resolve_pinned_ip("unresolvable.example.invalid")


async def test_resolve_pinned_ip_empty_answer_is_a_probe_error(monkeypatch):
    loop = asyncio.get_running_loop()

    async def _empty(host, *args, **kwargs):
        return []

    monkeypatch.setattr(loop, "getaddrinfo", _empty)
    with pytest.raises(ProbeError, match="could not resolve"):
        await _resolve_pinned_ip("nothing.example.invalid")


# --- the probe end to end -----------------------------------------------------
#
# `probe_rtsp` speaks RTSP over a plain TCP socket. These tests run a scripted
# RTSP server on a real loopback socket — the only stub is `_resolve_pinned_ip`
# pinned to 127.0.0.1, because the SSRF guard's job is precisely to refuse the
# address these tests need to connect to. The wire protocol, the auth retry,
# and the SDP handling are all real code paths.


class ScriptedRTSPServer:
    """Answers each RTSP request with the next canned response, on a real
    socket. Requests are captured so tests can assert on what the probe sent
    (e.g. the Authorization header of an auth retry)."""

    def __init__(self, responses: list[bytes]) -> None:
        self.responses = deque(responses)
        self.requests: list[bytes] = []

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while self.responses:
                self.requests.append(await reader.readuntil(b"\r\n\r\n"))
                writer.write(self.responses.popleft())
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            pass
        finally:
            writer.close()


async def _serve(responses: list[bytes]) -> tuple[ScriptedRTSPServer, asyncio.AbstractServer, int]:
    scripted = ScriptedRTSPServer(responses)
    server = await asyncio.start_server(scripted.handle, "127.0.0.1", 0)
    return scripted, server, server.sockets[0].getsockname()[1]


async def _probe(port: int, monkeypatch, **kwargs):
    """Run probe_rtsp against the scripted server: DNS is stubbed to loopback
    (the only seam — rebinding protection is tested separately), the port is
    allowlisted explicitly, and everything past the socket is real."""

    async def _pinned(host: str) -> str:
        return "127.0.0.1"

    monkeypatch.setattr("prahari_registry.probe._resolve_pinned_ip", _pinned)
    return await probe_rtsp(f"rtsp://dvr.local:{port}/stream1", allowed_ports={port}, **kwargs)


_SDP = (
    b"v=0\r\n"
    b"o=- 0 0 IN IP4 10.0.0.5\r\n"
    b"s=stream1\r\n"
    b"m=video 0 RTP/AVP 96\r\n"
    b"a=rtpmap:96 H264/90000\r\n"
    b"a=framerate:15.0\r\n"
)


def _rtsp(status: str, headers: dict[str, str] | None = None, body: bytes = b"") -> bytes:
    lines = [f"RTSP/1.0 {status}"]
    for k, v in (headers or {}).items():
        lines.append(f"{k}: {v}")
    if body:
        lines.append(f"Content-Length: {len(body)}")
    return "\r\n".join(lines).encode() + b"\r\n\r\n" + body


async def test_probe_reachable_endpoint_reports_codec_and_declared_fps(monkeypatch):
    scripted, server, port = await _serve(
        [_rtsp("200 OK", {"Public": "OPTIONS, DESCRIBE"}), _rtsp("200 OK", body=_SDP)]
    )
    try:
        result = await _probe(port, monkeypatch)
    finally:
        server.close()
        await server.wait_closed()

    assert result.reachable is True
    assert result.status_message == "200 OK"
    assert result.codec == "H264"
    assert result.declared_fps == 15.0
    assert result.sdp_media_lines == ["m=video 0 RTP/AVP 96"]
    assert result.auth_required is False
    # The probe speaks OPTIONS then DESCRIBE, with the Accept header on DESCRIBE.
    assert scripted.requests[0].startswith(b"OPTIONS ")
    assert b"Accept: application/sdp" in scripted.requests[1]


async def test_probe_a_non_rtsp_answer_is_a_probe_error(monkeypatch):
    """status 0 means the status line did not parse — something answered that
    is not an RTSP endpoint."""
    _, server, port = await _serve([b"garbage\r\n\r\n"])
    try:
        with pytest.raises(ProbeError, match="not an RTSP endpoint"):
            await _probe(port, monkeypatch)
    finally:
        server.close()
        await server.wait_closed()


async def test_probe_401_without_credentials_reports_auth_required(monkeypatch):
    _, server, port = await _serve(
        [
            _rtsp("200 OK"),
            _rtsp("401 Unauthorized", {"WWW-Authenticate": 'Digest realm="cam", nonce="n1"'}),
        ]
    )
    try:
        result = await _probe(port, monkeypatch)
    finally:
        server.close()
        await server.wait_closed()

    assert result.reachable is True
    assert result.auth_required is True
    assert result.auth_method == "digest"
    assert "credentials" in result.status_message


async def test_probe_digest_challenge_is_answered_and_retried(monkeypatch):
    """The 401 → credential → retry exchange on a real socket: the second
    DESCRIBE must carry a Digest Authorization header computed from the
    server's nonce."""
    scripted, server, port = await _serve(
        [
            _rtsp("200 OK"),
            _rtsp("401 Unauthorized", {"WWW-Authenticate": 'Digest realm="cam", nonce="n1"'}),
            _rtsp("200 OK", body=_SDP),
        ]
    )
    try:
        result = await _probe(port, monkeypatch, username="admin", password="s3cret")
    finally:
        server.close()
        await server.wait_closed()

    assert result.reachable and result.codec == "H264"
    retry = scripted.requests[2]
    assert b'Digest username="admin"' in retry
    assert b'realm="cam"' in retry and b'nonce="n1"' in retry


async def test_probe_basic_challenge_is_answered_with_basic_auth(monkeypatch):
    scripted, server, port = await _serve(
        [
            _rtsp("200 OK"),
            _rtsp("401 Unauthorized", {"WWW-Authenticate": 'Basic realm="cam"'}),
            _rtsp("200 OK", body=_SDP),
        ]
    )
    try:
        result = await _probe(port, monkeypatch, username="admin", password="s3cret")
    finally:
        server.close()
        await server.wait_closed()

    assert result.auth_method == "basic"
    token = b64encode(b"admin:s3cret")
    assert b"Authorization: Basic " + token in scripted.requests[2]


async def test_probe_describe_non_ok_status_is_reported_not_raised(monkeypatch):
    _, server, port = await _serve([_rtsp("200 OK"), _rtsp("404 Not Found")])
    try:
        result = await _probe(port, monkeypatch)
    finally:
        server.close()
        await server.wait_closed()

    assert result.reachable is True
    assert result.status_message == "DESCRIBE returned 404"


async def test_probe_connection_refused_is_a_probe_error(monkeypatch):
    # A port nothing is listening on: bind, learn the port, close.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    async def _pinned(host: str) -> str:
        return "127.0.0.1"

    monkeypatch.setattr("prahari_registry.probe._resolve_pinned_ip", _pinned)
    with pytest.raises(ProbeError, match="could not connect"):
        await probe_rtsp(f"rtsp://dvr.local:{port}/x", allowed_ports={port})


async def test_probe_writer_close_race_does_not_mask_the_result(monkeypatch):
    """`writer.wait_closed` can raise OSError when the peer resets mid-close —
    that must not turn an already-answered probe into an exception."""

    class ResettingWriter(FakeWriter):
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            raise OSError("connection reset by peer")

    reader = FakeReader(readuntil_error=asyncio.IncompleteReadError(b"", expected=4))

    async def _open(ip, port):
        return reader, ResettingWriter()

    async def _pinned(host: str) -> str:
        return "10.0.0.5"

    monkeypatch.setattr("prahari_registry.probe._resolve_pinned_ip", _pinned)
    monkeypatch.setattr(asyncio, "open_connection", _open)
    with pytest.raises(ProbeError, match="connection closed"):
        await probe_rtsp("rtsp://dvr.local:554/x")
