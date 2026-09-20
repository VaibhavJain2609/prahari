"""The shared half of the cluster-internal token gate.

The registry, the correlation service and the match engine all gate on the
same `X-Internal-Token` credential, and the BFF, the ingest worker and the
correlation service all *send* it. The two things every one of them must agree
on live here once rather than drifting: the header/metadata name, and the
empty-expected semantics (an unset expected token means the gate is OFF — never
"expected is empty, so an empty header matches").

Stdlib only — prahari-common stays dependency-light so a service that only
reads the catalogue inherits no transport stack. Each service wraps these two
functions in its own transport check (FastAPI middleware, grpc.ServerInterceptor).
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping

__all__ = ["HEADER_NAME", "expected_token_ok", "provided_token"]

HEADER_NAME = "x-internal-token"
"""The credential's name on the wire. Lowercase covers both transports: HTTP
headers are case-insensitive and gRPC lowercases all metadata keys anyway."""


def provided_token(headers: Mapping[str, str]) -> str | None:
    """The token a caller sent, or None when the header is absent.

    Any mapping works: Starlette/httpx `Headers`, or a dict built from gRPC
    invocation metadata (`dict(handler_call_details.invocation_metadata)`).
    """
    return headers.get(HEADER_NAME)


def expected_token_ok(provided: str | None, expected: str) -> bool:
    """Whether a call carrying `provided` may pass a gate armed with `expected`.

    Empty `expected` disables the gate — everything passes. That is the
    local/dev default; the chart arms it per profile, and failing closed on an
    unset secret would turn a missing env var into an outage instead of a
    warning. When armed, `hmac.compare_digest` keeps a wrong token costing the
    same time as a right one.
    """
    if not expected:
        return True
    if provided is None:
        return False
    # compare_digest on str raises TypeError for non-ASCII input — a header is
    # attacker-controlled bytes, so encode explicitly rather than 500 on it.
    return hmac.compare_digest(provided.encode("utf-8", "replace"), expected.encode())
