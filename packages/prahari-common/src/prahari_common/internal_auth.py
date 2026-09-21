"""The shared half of the cluster-internal token gate.

The registry, the correlation service and the match engine all gate on the
`X-Internal-Token` credential, and the BFF, the ingest worker and the
correlation service all *send* it. The things every one of them must agree
on live here once rather than drifting: the header/metadata name, the
empty-expected semantics (an unset expected token means the gate is OFF —
never "expected is empty, so an empty header matches"), and the
credential→identity resolution that turns one shared secret into
per-service caller identities.

Two postures exist on purpose:

* **shared** — only `internal_token` is configured. The legacy single-token
  check: every internal caller holds the same secret. This is what keeps
  every deployment predating per-service credentials working unchanged.
* **isolated** — `internal_tokens` (caller-name → token) is configured. A
  presented token resolves to a caller identity by constant-time comparison
  over the map, and the caller must be in the service's allowlist. A token
  matching nothing is anonymous and denied. A token matching
  `internal_token` resolves to the `"internal"` caller, which is accepted
  everywhere — the compatibility identity for callers that still hold the
  shared credential (the loadtest driver, ad-hoc curl, a not-yet-migrated
  service).

Canonical caller names are the services' chart names: `"bff"`,
`"correlation"`, `"inference"`, plus the synthetic `"internal"` above.

Stdlib only — prahari-common stays dependency-light so a service that only
reads the catalogue inherits no transport stack. Each service wraps these
functions in its own transport check (FastAPI middleware,
grpc.ServerInterceptor).
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Container, Mapping

__all__ = [
    "HEADER_NAME",
    "INTERNAL_CALLER",
    "caller_accepted",
    "expected_token_ok",
    "gate_posture",
    "parse_caller_tokens",
    "provided_token",
    "resolve_caller",
]

HEADER_NAME = "x-internal-token"
"""The credential's name on the wire. Lowercase covers both transports: HTTP
headers are case-insensitive and gRPC lowercases all metadata keys anyway."""

INTERNAL_CALLER = "internal"
"""The caller name `internal_token` resolves to when `internal_tokens` is
configured. Accepted on every gated service — it is the backward-compat
identity for anything still holding the pre-isolation shared token."""


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


def resolve_caller(provided: str | None, tokens: Mapping[str, str]) -> str | None:
    """The caller name `provided` authenticates as, or None.

    Compares against every entry — no early exit on a hit — so the resolution
    time depends on the map's size, not on which key matched. A token that
    matches nothing is anonymous: the caller decides what anonymity means
    (`caller_accepted` denies it).
    """
    if not provided:
        return None
    candidate = provided.encode("utf-8", "replace")
    matched: str | None = None
    for name, token in tokens.items():
        if token and hmac.compare_digest(candidate, token.encode()):
            matched = name
    return matched


def caller_accepted(
    provided: str | None,
    *,
    internal_token: str,
    caller_tokens: Mapping[str, str],
    accepted_callers: Container[str],
) -> bool:
    """Whether a call carrying `provided` may pass the internal gate.

    Three postures:

    * **open** — neither `internal_token` nor `caller_tokens` configured:
      everything passes (the local/dev default, loudly logged at startup).
    * **shared** — `caller_tokens` empty: the pre-isolation check, a
      byte-identical match on `internal_token`.
    * **isolated** — `caller_tokens` set: `provided` must resolve to a caller
      in `accepted_callers`. `internal_token` is added to the map as the
      `"internal"` caller, which every service accepts — the compat path for
      callers still holding the shared credential.
    """
    if not caller_tokens:
        return expected_token_ok(provided, internal_token)
    tokens: dict[str, str] = dict(caller_tokens)
    if internal_token:
        tokens.setdefault(INTERNAL_CALLER, internal_token)
    caller = resolve_caller(provided, tokens)
    if caller is None:
        return False
    return caller == INTERNAL_CALLER or caller in accepted_callers


def gate_posture(internal_token: str, caller_tokens: Mapping[str, str]) -> str:
    """`"isolated" | "shared" | "open"` — what `/readyz` reports so an operator
    can see which credential model a service is enforcing without reading its
    env."""
    if caller_tokens:
        return "isolated"
    if internal_token:
        return "shared"
    return "open"


def parse_caller_tokens(value: object) -> dict[str, str]:
    """Parse a caller→token map from a settings-provided value.

    Three accepted shapes, because the chart and hand-rolled deployments
    deliver the map differently:

    * a mapping already (test code constructing settings directly);
    * a JSON object string — `PRAHARI_*_INTERNAL_TOKENS='{"bff": "..."}'`
      (the field is `NoDecode`-annotated so the raw env string reaches the
      field validator instead of pydantic-settings' own JSON decode, which
      is what lets the next shape exist at all);
    * the comma form `bff:tok,correlation:tok2` — for env contexts where
      embedding a quoted JSON object is awkward.

    Empty/None yields the empty map, which `caller_accepted` reads as
    "not configured → shared mode". Entries with an empty name or token are
    dropped rather than kept: a tokenless caller can never resolve anyway,
    and keeping it would make a typo silently narrow the map.
    """
    if value is None or isinstance(value, Mapping):
        return {str(k): str(v) for k, v in (value or {}).items() if str(k) and str(v)}
    text = str(value).strip()
    if not text:
        return {}
    if text[0] in "{[":
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("internal_tokens JSON must be an object of name: token")
        return {str(k): str(v) for k, v in parsed.items() if str(k) and str(v)}
    parsed = {}
    for part in text.split(","):
        name, sep, token = part.partition(":")
        name, token = name.strip(), token.strip()
        if sep and name and token:
            parsed[name] = token
    return parsed
