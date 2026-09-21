"""Credential redaction for URLs before they reach a log line or an error
string.

The MediaMTX fan-out URLs the registry hands this service carry
`worker:<worker-media-token>` userinfo — a real credential, embedded because
RTSP consumers have no header channel. Anything that logs or reports the URL
must render it through `redact_url_credentials` first; the registry has the
same helper (`prahari_registry.repository.redact_url_credentials`), mirrored
here rather than imported because the inference image must not grow a
registry dependency for five lines of urllib.

The query string is dropped entirely as well: some DVR lines accept
`?username=&password=` auth, and scheme/host/port/path is all a log line
ever needs.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

__all__ = ["redact_url_credentials", "redact_url_in_text"]


def redact_url_credentials(url: str) -> str:
    """The same URL minus its userinfo — `rtsp://user:pass@host:554/x` becomes
    `rtsp://host:554/x`. `parts.port` is guarded so a malformed port degrades
    to a host-only rendering rather than raising inside a logging call."""
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"  # IPv6 literal — hostname strips the brackets
    try:
        if parts.port is not None:
            netloc += f":{parts.port}"
    except ValueError:
        pass  # malformed port — emit host-only rather than fail the log call
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def redact_url_in_text(text: str, url: str) -> str:
    """`text` with any literal occurrence of `url` replaced by its redacted
    form — for exception messages (ffmpeg can echo the connect URL back) that
    are not themselves URLs and so cannot be parsed."""
    return text.replace(url, redact_url_credentials(url)) if url else text
