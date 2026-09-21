"""Stage 4c — probe an RTSP endpoint before registering it as a camera.

Speaks RTSP directly over a plain `asyncio` TCP socket (OPTIONS, then
DESCRIBE) rather than decoding any video. That is deliberate, not a
shortcut: the registry has no OpenCV/ffmpeg dependency by design (see
`packages/prahari-common`'s docstring — a service that only reads the
catalogue must not inherit a decoder), and a probe that opened a real
decoder would need one. Never using UDP also means "force rtsp_transport=tcp"
holds by construction — there is no UDP code path to force away from.

What this reports is deliberately narrow: reachability, the auth scheme the
source demands, and whatever a `DESCRIBE`'s SDP answer states about codec and
frame rate. That rate is the source's own *declared* claim in its SDP (an
`a=framerate` or `a=x-framerate` attribute, when present) — never something
this probe measured by watching frames, and it is reported to the caller
labelled `declared_fps` for exactly that reason. `CLAUDE.md`'s "never trust
CAP_PROP_FPS" is about not deriving health/cadence decisions from a claimed
rate; labelling it honestly here is what keeps this probe on the right side
of that line. Resolution is not in an SDP answer without decoding a frame, so
this probe does not claim to know it.

SSRF is the actual hazard: this is a server-side fetch of a URL a caller
supplies. DVRs legitimately sit on RFC1918 ranges, so a blanket private-IP
block would reject the normal case. What is blocked: link-local
(169.254.0.0/16, fe80::/10 — this is how cloud metadata endpoints are
reached), loopback, multicast, and unspecified/reserved ranges. The resolved
IP is checked once and then connected to *by IP*, never re-resolved, so a
DNS answer that changes between the check and the connect (rebinding) cannot
smuggle a blocked address past the guard.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
from base64 import b64encode
from collections.abc import Iterable
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

__all__ = ["ProbeError", "ProbeResult", "SSRFBlockedError", "probe_rtsp", "validate_rtsp_url"]

_DEFAULT_RTSP_PORT = 554
_CONNECT_TIMEOUT_S = 3.0
_READ_TIMEOUT_S = 5.0
_MAX_RESPONSE_BYTES = 64 * 1024
"""Hard ceiling on the TOTAL bytes read for one RTSP response — header block
plus body together. A hostile or broken endpoint advertising a huge
`Content-Length` must not turn the probe into an unbounded read; an SDP answer
is a few hundred bytes and 64 KiB is already generous."""

DEFAULT_ALLOWED_PORTS: frozenset[int] = frozenset({_DEFAULT_RTSP_PORT})
"""Ports the probe is willing to connect to. RTSP is 554; anything else is
configurable (`RegistrySettings.probe_allowed_ports`) but defaults closed —
this is a server-side fetch of a caller-supplied URL, and an open port list
would make it a connect-anywhere primitive against the cluster's neighbours."""


class ProbeError(Exception):
    """A probe that failed for an ordinary reason: refused, timed out, not
    RTSP, wrong credentials. Distinct from `SSRFBlockedError`, which is a
    policy refusal to even attempt the connection."""


class SSRFBlockedError(ProbeError):
    """The resolved address is link-local, loopback, multicast, or otherwise
    a range no camera legitimately lives on. Reported to the caller as a 400,
    never silently downgraded to "unreachable"."""


class ProbeResult(BaseModel):
    reachable: bool
    transport: str = "tcp"
    auth_required: bool = False
    auth_method: str | None = None
    codec: str | None = None
    declared_fps: float | None = None
    """The source's own SDP claim (`a=framerate`/`a=x-framerate`), when
    present — never a rate this probe measured. No decode happens here, so
    there is nothing to measure it from."""
    status_message: str | None = None
    sdp_media_lines: list[str] = Field(default_factory=list)


def _guard_ip(ip_str: str) -> None:
    ip = ipaddress.ip_address(ip_str)
    # `::ffff:a.b.c.d` is an IPv4 address wearing IPv6 clothes. Newer Pythons
    # evaluate the mapped address's properties for is_private/is_reserved, but
    # older ones do not — guard the mapped v4 address explicitly so the answer
    # is the same on every interpreter we might run on.
    mapped = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) else None
    if mapped is not None:
        _guard_ip(str(mapped))
    if ip.is_link_local or ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        raise SSRFBlockedError(f"resolved address {ip_str} is not a permitted probe target")


def _guard_port(port: int, allowed_ports: Iterable[int]) -> None:
    """Second SSRF leg: the IP may be a legitimate DVR address and the port
    still be a Redis or a metadata sidecar living on it."""
    if port not in allowed_ports:
        raise SSRFBlockedError(f"port {port} is not in the probe allowlist {sorted(allowed_ports)}")


async def _resolve_pinned_ip(host: str) -> str:
    """DNS lookup once, guarded once, then used as the literal connect
    target — the TOCTOU gap a second lookup at connect time would open."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ProbeError(f"could not resolve {host}: {exc}") from exc
    if not infos:
        raise ProbeError(f"could not resolve {host}")
    ip_str = infos[0][4][0]
    _guard_ip(ip_str)
    return ip_str


async def validate_rtsp_url(
    url: str, *, allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS
) -> str:
    """The SSRF guard, without the connect — everything `probe_rtsp` checks
    before it opens a socket: scheme is rtsp, a host exists, the port is in
    the allowlist, and the resolved IP is not loopback/link-local/multicast/
    reserved. Returns the pinned IP (the checked address the caller may then
    connect to).

    Shared with camera registration (`POST /api/v1/cameras`): the stored
    `rtsp_url` is what MediaMTX later connects to on our behalf, so an
    unvalidated stored URL is the same server-side-fetch primitive the probe
    is guarded against, reached by a slower path. Policy here is exactly the
    probe's — RFC1918 stays allowed because DVRs legitimately live on it —
    applied at write time rather than only at probe time.

    Raises `ProbeError` for malformed/unresolvable input and
    `SSRFBlockedError` for a policy refusal; callers turn both into a 400 —
    a URL that cannot be resolved cannot be checked, so unresolvable fails
    closed here (the probe reports it as simply "unreachable" instead).
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ProbeError(f"malformed URL: {exc}") from exc
    if parts.scheme != "rtsp":
        raise ProbeError(f"unsupported scheme {parts.scheme!r} — only rtsp:// is allowed")
    host = parts.hostname
    if not host:
        raise ProbeError("URL has no host")
    try:
        port = parts.port or _DEFAULT_RTSP_PORT
    except ValueError as exc:
        # urlsplit defers port validation to `.port` access — a garbage port
        # in the URL surfaces here, not at parse time.
        raise ProbeError(f"invalid port in URL: {exc}") from exc
    # Port check happens before DNS: there is no reason to resolve a host we
    # would refuse to connect to anyway.
    _guard_port(port, allowed_ports)
    return await _resolve_pinned_ip(host)


def _digest_header(
    *, method: str, uri: str, username: str, password: str, challenge: dict[str, str]
) -> str:
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()  # noqa: S324
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()  # noqa: S324
    response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()  # noqa: S324
    return (
        f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
        f'uri="{uri}", response="{response}"'
    )


def _parse_challenge(header: str) -> tuple[str, dict[str, str]]:
    scheme, _, rest = header.partition(" ")
    params = dict(re.findall(r'(\w+)="?([^",]*)"?', rest))
    return scheme, params


class _RTSPSession:
    """One TCP connection, a handful of request/response round trips. Not a
    general RTSP client — only what a probe needs: OPTIONS then DESCRIBE."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, url: str):
        self._reader = reader
        self._writer = writer
        self._url = url
        self._cseq = 0

    async def request(self, method: str, *, extra_headers: dict[str, str] | None = None) -> dict:
        self._cseq += 1
        lines = [f"{method} {self._url} RTSP/1.0", f"CSeq: {self._cseq}"]
        for key, value in (extra_headers or {}).items():
            lines.append(f"{key}: {value}")
        lines.append("\r\n")
        self._writer.write("\r\n".join(lines).encode())
        await self._writer.drain()
        try:
            raw = await asyncio.wait_for(
                self._reader.readuntil(b"\r\n\r\n"), timeout=_READ_TIMEOUT_S
            )
        except TimeoutError as exc:
            raise ProbeError("timed out waiting for the RTSP response headers") from exc
        except asyncio.LimitOverrunError as exc:
            # The header block alone exceeded the StreamReader limit — that is
            # not a camera answering, it is something stuffing bytes at us.
            raise ProbeError(
                "response headers exceed the stream buffer limit; not an RTSP endpoint"
            ) from exc
        except asyncio.IncompleteReadError as exc:
            raise ProbeError("connection closed before the response headers") from exc
        head, _, _ = raw.partition(b"\r\n\r\n")
        status_line, *header_lines = head.decode(errors="replace").split("\r\n")
        parts = status_line.split(" ", 2)
        status_code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        headers = {}
        for line in header_lines:
            key, sep, value = line.partition(":")
            if sep:
                headers[key.strip().lower()] = value.strip()
        body = b""
        try:
            content_length = int(headers.get("content-length", 0) or 0)
        except ValueError as exc:
            raise ProbeError(
                f"malformed Content-Length {headers['content-length']!r} — "
                "not a well-formed RTSP endpoint"
            ) from exc
        # `want` is the TOTAL read for this body — the cap applies to the
        # accumulation, not to each socket read, or a 10 MB Content-Length
        # would be fetched in full 64 KiB at a time. A truncated SDP still
        # parses; an unbounded buffer does not stay bounded.
        want = min(max(content_length, 0), _MAX_RESPONSE_BYTES)
        if want:
            remaining = raw[len(head) + 4 :]
            while len(remaining) < want:
                try:
                    chunk = await asyncio.wait_for(
                        self._reader.read(want - len(remaining)), timeout=_READ_TIMEOUT_S
                    )
                except TimeoutError as exc:
                    raise ProbeError("timed out reading the response body") from exc
                if not chunk:
                    break  # peer closed early; parse what arrived
                remaining += chunk
            body = remaining[:want]
        return {"status": status_code, "headers": headers, "body": body}


def _parse_sdp(body: bytes) -> tuple[str | None, float | None, list[str]]:
    codec: str | None = None
    declared_fps: float | None = None
    media_lines: list[str] = []
    for raw_line in body.decode(errors="replace").splitlines():
        line = raw_line.strip()
        if line.startswith("m=video") or line.startswith("m=audio"):
            media_lines.append(line)
        elif line.startswith("a=rtpmap:") and codec is None:
            # a=rtpmap:96 H264/90000
            match = re.match(r"a=rtpmap:\d+\s+([\w-]+)/", line)
            if match:
                codec = match.group(1)
        elif line.lower().startswith(("a=framerate:", "a=x-framerate:")):
            try:
                declared_fps = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return codec, declared_fps, media_lines


async def probe_rtsp(
    url: str,
    *,
    username: str | None = None,
    password: str | None = None,
    allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
) -> ProbeResult:
    ip = await validate_rtsp_url(url, allowed_ports=allowed_ports)
    parts = urlsplit(url)
    port = parts.port or _DEFAULT_RTSP_PORT

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=_CONNECT_TIMEOUT_S
        )
    except (OSError, TimeoutError) as exc:
        raise ProbeError(f"could not connect to {parts.hostname}:{port} ({ip}): {exc}") from exc

    try:
        session = _RTSPSession(reader, writer, url)
        options = await session.request("OPTIONS")
        if options["status"] == 0:
            raise ProbeError("did not receive a valid RTSP response — not an RTSP endpoint?")

        describe = await session.request("DESCRIBE", extra_headers={"Accept": "application/sdp"})
        auth_required = False
        auth_method: str | None = None

        if describe["status"] == 401:
            auth_required = True
            challenge_header = describe["headers"].get("www-authenticate", "")
            scheme, challenge = _parse_challenge(challenge_header)
            auth_method = scheme.lower()
            if username is None or password is None:
                return ProbeResult(
                    reachable=True,
                    auth_required=True,
                    auth_method=auth_method,
                    status_message="401 Unauthorized — credentials required to go further",
                )
            if scheme.lower() == "digest":
                auth_header = _digest_header(
                    method="DESCRIBE",
                    uri=url,
                    username=username,
                    password=password,
                    challenge=challenge,
                )
            else:
                token = b64encode(f"{username}:{password}".encode()).decode()
                auth_header = f"Basic {token}"
            describe = await session.request(
                "DESCRIBE",
                extra_headers={"Accept": "application/sdp", "Authorization": auth_header},
            )

        if describe["status"] != 200:
            return ProbeResult(
                reachable=True,
                auth_required=auth_required,
                auth_method=auth_method,
                status_message=f"DESCRIBE returned {describe['status']}",
            )

        codec, declared_fps, media_lines = _parse_sdp(describe["body"])
        return ProbeResult(
            reachable=True,
            auth_required=auth_required,
            auth_method=auth_method,
            codec=codec,
            declared_fps=declared_fps,
            status_message="200 OK",
            sdp_media_lines=media_lines,
        )
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
