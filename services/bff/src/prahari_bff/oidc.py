"""OIDC (Keycloak) login for the console — Authorization Code + PKCE.

This module owns everything the `/api/v1/auth/oidc/*` routes in `app.py` need
and nothing they don't: the authorize redirect, the signed state cookie that
carries the PKCE verifier across the IdP round-trip, the token exchange, JWKS
validation of the returned `id_token`, and claim→role mapping.

The design decisions are in docs/KEYCLOAK.md and docs/NEXT-PHASE-PLAN.md §3.
The load-bearing ones:

- **One confidential client, `prahari-bff`.** The BFF does the code exchange
  server-side, so the client secret never reaches a browser. PKCE is still
  sent (defence in depth — a stolen code is useless without the verifier).
- **The browser gets the same opaque `prahari_session` cookie builtin login
  mints.** Access/refresh tokens are never exposed to the browser — SSE's
  `EventSource` cannot set headers, so the session row is what carries auth.
- **`state` is a CSRF nonce stored in a signed, short-lived httponly cookie**
  (`prahari_oidc_state`, 5 min). The cookie also carries the PKCE verifier and
  the post-login `next` path, so nothing server-side is persisted and any
  replica can complete the flow.
- **Claims are validated, never trusted.** `realm_access.roles` is intersected
  with the real role set; `org_path` must match the ltree-label alphabet and
  resolve to an org that actually exists — else the login is denied, because
  an IdP claim that names no real org is a misconfiguration, not a scope.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import HTTPException, status

from .config import BFFSettings
from .models import Role

STATE_COOKIE_NAME = "prahari_oidc_state"
"""Holds the PKCE verifier + CSRF nonce + post-login path for the duration of
the IdP round-trip. Signed, httponly, 5-minute TTL."""

OIDC_MARKER_COOKIE_NAME = "prahari_oidc"
"""Set alongside `prahari_session` at callback so logout can tell an
OIDC-born session from a builtin one — `sessions` has no `sub`/`auth_via`
column (006_identity.sql is registry-owned), so the marker is a cookie. It is
not a security boundary: the worst a forged marker does is trigger an
RP-initiated logout redirect the console may ignore."""

STATE_TTL_S = 300
JWKS_CACHE_TTL_S = 300

_ALLOWED_ALGS = ("RS256", "ES256")
"""Pinned asymmetric algs — no `none`, no HS256 (an HS256 token verified
against a published JWKS would be a confused-deputy downgrade anyway, but the
allowlist is the correct place to say so)."""

_CLOCK_SKEW_S = 30

_ROLE_RANK = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}

_LTREE_PATH = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
"""The ltree label alphabet — `in_scope` is string-prefix logic, so a claim
outside this alphabet is a forgery attempt, not an org name."""


def _b64url(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def new_pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge). The verifier is the 128-char maximum —
    entropy is free and a shorter verifier buys nothing."""
    verifier = secrets.token_urlsafe(96)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def safe_next(next_path: str | None) -> str:
    """`next` becomes a `Location` header after callback — anything that isn't
    a same-origin absolute path is an open redirect. `/` on anything even
    slightly odd."""
    if not next_path:
        return "/"
    if not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    if any(ord(c) < 0x20 for c in next_path):
        return "/"
    return next_path


class OidcClient:
    """The BFF's server-side half of the code flow.

    Constructed once per process (app.state.oidc, lazily on first use) so the
    JWKS cache actually caches. `http_client` is injectable so tests can serve
    a fake IdP over `httpx.MockTransport` — the house pattern for upstreams.
    """

    def __init__(
        self,
        settings: BFFSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.oidc_issuer_url:
            raise RuntimeError("oidc_enabled requires oidc_issuer_url")
        if not settings.oidc_redirect_base:
            raise RuntimeError("oidc_enabled requires oidc_redirect_base")
        self._issuer = settings.oidc_issuer_url.rstrip("/")
        self._internal = (settings.oidc_internal_url or self._issuer).rstrip("/")
        self._client_id = settings.oidc_client_id
        self._client_secret = settings.oidc_client_secret
        self._redirect_uri = settings.oidc_redirect_base.rstrip("/") + "/api/bff/auth/oidc/callback"
        self._scopes = settings.oidc_scopes
        self._http = http_client or httpx.AsyncClient(timeout=httpx.Timeout(5.0))
        # kid -> (PyJWK, fetched_at). kid-tolerant: an unknown kid triggers one
        # forced refetch (key rotation), then fails closed.
        self._jwks: dict[str, jwt.PyJWK] = {}
        self._jwks_fetched_at = 0.0
        # HMAC key for the state cookie. The client secret is the natural key
        # when one exists (stable across replicas and restarts); a boot-generated
        # key is the fallback for secret-less local dev, where a restart just
        # abandons in-flight logins — acceptable, and documented.
        self._state_key = settings.oidc_client_secret.encode() or secrets.token_bytes(32)

    # --- browser-facing endpoints (public issuer) ---------------------------

    @property
    def redirect_uri(self) -> str:
        return self._redirect_uri

    def authorize_url(self, *, state: str, challenge: str, nonce: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "scope": self._scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._issuer}/protocol/openid-connect/auth?{query}"

    def end_session_url(self, *, post_logout_redirect_uri: str) -> str:
        """RP-initiated logout — without this hop, "Sign out" drops the local
        session and the next login silently re-authenticates off the still-
        live Keycloak SSO session."""
        query = urlencode(
            {
                "client_id": self._client_id,
                "post_logout_redirect_uri": post_logout_redirect_uri,
            }
        )
        return f"{self._issuer}/protocol/openid-connect/logout?{query}"

    # --- server-to-server endpoints (internal base) ---------------------------

    async def exchange_code(self, *, code: str, verifier: str) -> dict:
        try:
            response = await self._http.post(
                f"{self._internal}/protocol/openid-connect/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "redirect_uri": self._redirect_uri,
                    "code_verifier": verifier,
                },
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "identity provider unreachable"
            ) from exc
        if response.status_code != 200:
            # Detail deliberately generic — Keycloak's error body can echo
            # request parameters back into the response.
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authorization code exchange failed")
        body = response.json()
        if "id_token" not in body:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "identity provider returned no id_token"
            )
        return body

    async def validate_id_token(self, id_token: str, *, expected_nonce: str) -> dict:
        """Signature + claims in one pass: JWKS by `kid` (refetching once on a
        miss, for rotation), alg pinned to the allowlist, iss/aud/exp enforced
        by pyjwt, `azp` and `nonce` checked here. Raises HTTPException 401 on
        any failure — an invalid token is an authentication failure, and the
        reason stays in the log, not the response body."""
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.InvalidTokenError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed id_token") from exc
        alg = header.get("alg", "")
        if alg not in _ALLOWED_ALGS:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unexpected token algorithm")
        key = await self._key_for_kid(header.get("kid"))
        try:
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=[alg],
                audience=self._client_id,
                issuer=self._issuer,
                leeway=_CLOCK_SKEW_S,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.InvalidTokenError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "id_token validation failed") from exc
        # pyjwt's iat validation rejects a future iat at the same leeway, so
        # this can never fire — kept as a second gate in case the decode
        # call's options ever stop enforcing it.
        if claims["iat"] > time.time() + _CLOCK_SKEW_S:  # pragma: no cover — decode rejects first
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "id_token issued in the future")
        # When Keycloak emits `azp` it names the client the token was minted
        # for — a token cut for a different client must not slide in on aud.
        if "azp" in claims and claims["azp"] != self._client_id:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "azp mismatch")
        if "nonce" in claims and claims["nonce"] != expected_nonce:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "nonce mismatch")
        return claims

    async def _key_for_kid(self, kid: str | None):
        if kid is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "id_token has no kid")
        cached = self._jwks.get(kid)
        if cached is not None:
            return cached.key
        # Unknown kid: either rotation or a forged header. Refetch once, then
        # fail closed.
        await self._refresh_jwks(force=True)
        key = self._jwks.get(kid)
        if key is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown signing key")
        return key.key

    async def _refresh_jwks(self, *, force: bool = False) -> None:
        if not force and time.monotonic() - self._jwks_fetched_at < JWKS_CACHE_TTL_S:
            return
        try:
            response = await self._http.get(f"{self._internal}/protocol/openid-connect/certs")
            response.raise_for_status()
            keys = response.json().get("keys", [])
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "could not fetch identity provider keys"
            ) from exc
        jwks: dict[str, jwt.PyJWK] = {}
        for jwk in keys:
            try:
                parsed = jwt.PyJWK(jwk)
            except jwt.exceptions.PyJWKError:
                continue  # a non-signing key in the set is not an error
            if parsed.key_id:
                jwks[parsed.key_id] = parsed
        self._jwks = jwks
        self._jwks_fetched_at = time.monotonic()

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- state cookie ----------------------------------------------------------

    def seal_state(self, *, nonce: str, verifier: str, next_path: str) -> str:
        """`b64(json).b64(hmac)` — HMAC over the payload, no server state."""
        payload = json.dumps(
            {"n": nonce, "v": verifier, "next": next_path, "iat": int(time.time())},
            separators=(",", ":"),
        ).encode()
        return f"{_b64url(payload)}.{_b64url(self._sign(payload))}"

    def open_state(self, cookie_value: str) -> dict:
        """Verify signature and freshness. A bad cookie is a 400 (the client
        sent us back malformed state), never a silent pass."""
        try:
            payload_b64, sig_b64 = cookie_value.split(".", 1)
            payload = _b64decode(payload_b64)
            expected = _b64decode(sig_b64)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "malformed oidc state") from exc
        if not hmac.compare_digest(self._sign(payload), expected):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "oidc state signature mismatch")
        try:
            data = json.loads(payload)
            iat = int(data["iat"])
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "malformed oidc state") from exc
        if abs(time.time() - iat) > STATE_TTL_S:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "oidc state expired")
        return data

    def _sign(self, payload: bytes) -> bytes:
        return hmac.new(self._state_key, payload, hashlib.sha256).digest()


def _b64decode(data: str) -> bytes:
    return urlsafe_b64decode(data + "=" * (-len(data) % 4))


_ROLE_NAMES = {r.value for r in _ROLE_RANK}


def map_realm_role(claims: dict) -> Role:
    """`realm_access.roles` ∩ {viewer, operator, admin}, highest wins. A user
    with no mapped realm role lands at viewer — least privilege by default,
    and an admin grants more by assigning the realm role in Keycloak."""
    roles = (claims.get("realm_access") or {}).get("roles") or []
    mapped = [Role(r) for r in roles if r in _ROLE_NAMES]
    if not mapped:
        return Role.VIEWER
    return max(mapped, key=lambda r: _ROLE_RANK[r])


def validate_org_path_claim(claims: dict) -> str | None:
    """The `org_path` claim, alphabet-checked. None means "not asserted" (the
    claim is optional for users already in the users table — Postgres is
    authoritative there). A present-but-malformed claim is a 400: fail closed
    on forgery-shaped input rather than ignoring it."""
    org_path = claims.get("org_path")
    if org_path is None:
        return None
    if not isinstance(org_path, str) or not _LTREE_PATH.match(org_path):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "org_path claim is not a valid ltree path")
    return org_path
