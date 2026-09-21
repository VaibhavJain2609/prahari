"""The BFF's browser-facing surface.

Auth (login/logout/whoami, admin user/API-key management) plus, as of Stage 3
(docs/ORG-TIERS-DESIGN.md §3–4): the scoped camera/gap proxy, the plate→route
mandatory path and its CSV/PDF export, the SSE alert relay, and the
hash-chained audit log. This is the only service a browser reaches directly
once the registry's `PRAHARI_INTERNAL_TOKEN` gate is on — every scoped read
below goes registry-and-correlation-ward as a trusted internal caller,
forcing the caller's own org scope rather than trusting whatever a client
asked for.
"""

from __future__ import annotations

import asyncio
import csv
import dataclasses
import hmac
import io
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import quote, urlparse, urlsplit, urlunsplit

import asyncpg
import httpx
import redis as redis_lib
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from google.protobuf.json_format import MessageToDict
from prahari.v1 import events_pb2
from prahari_common.bus import RedisStreamConsumer

from .audit import AuditLog
from .auth import AdminDep, OperatorDep, PrincipalDep, PurposeCodeDep
from .config import BFFSettings, bff_settings
from .correlation_client import CorrelationClient
from .db import create_pool
from .export import route_to_csv, route_to_pdf
from .match_engine_client import MatchEngineClient
from .media import MediaTicketIssuer
from .metrics import METRICS, count_response, refresh_gauges
from .models import (
    ApiKey,
    ApiKeyCreate,
    ApiKeyCreated,
    LoginRequest,
    PreviewTicket,
    PreviewTicketRequest,
    Principal,
    User,
    UserCreate,
)
from .oidc import (
    OIDC_MARKER_COOKIE_NAME,
    STATE_COOKIE_NAME,
    STATE_TTL_S,
    OidcClient,
    map_realm_role,
    new_pkce_pair,
    safe_next,
    validate_org_path_claim,
)
from .registry_client import RegistryClient
from .repository import (
    ApiKeyRepository,
    SessionRepository,
    UserRepository,
    in_scope,
    org_path_for_id,
)
from .scope_resolver import CameraScopeResolver
from .security import SlidingWindowRateLimiter, verify_password

log = logging.getLogger(__name__)


async def _seed_bootstrap_admin(
    pool,
    settings: BFFSettings,
    user_repo: UserRepository,
    audit: AuditLog | None = None,
) -> None:
    """Create exactly one admin, once, only when the operator has explicitly
    asked for it and no user exists yet. See `BFFSettings.
    bootstrap_admin_username` for why this exists instead of a CLI."""
    if not (settings.bootstrap_admin_username and settings.bootstrap_admin_password):
        return
    if await user_repo.count() > 0:
        return
    org_id = await pool.fetchval(
        "SELECT id FROM orgs WHERE path = $1::ltree", settings.bootstrap_admin_org_path
    )
    if org_id is None:
        log.warning(
            "bootstrap admin not created: org %r does not exist "
            "(is migration 005's seed row present?)",
            settings.bootstrap_admin_org_path,
        )
        return
    await user_repo.create(
        UserCreate(
            username=settings.bootstrap_admin_username,
            password=settings.bootstrap_admin_password,
            org_id=str(org_id),
            role="admin",
        )
    )
    # Recorded after the create, best-effort: the seed runs inside lifespan,
    # before any request exists, and the users row it wrote is itself the
    # durable record — a failed append here is logged, not fatal. The
    # strict `_audit_auth_event` ordering (audit before mutation) is for
    # request paths where a failed append can still abort the mutation;
    # there is nothing left to abort once the row exists.
    if audit is not None:
        try:
            await audit.append(
                actor=settings.bootstrap_admin_username,
                org_path=settings.bootstrap_admin_org_path,
                purpose_code="auth",
                resource=f"user:{settings.bootstrap_admin_username}",
                action="auth_admin_seeded",
            )
        except Exception as exc:  # noqa: BLE001 - log, never mask a successful seed
            log.error("audit append failed for auth_admin_seeded: %s", exc)
    log.warning(
        "bootstrap admin '%s' created at org %r — this only ever runs once, while the "
        "users table is empty. Create a second admin and stop setting "
        "PRAHARI_BOOTSTRAP_ADMIN_PASSWORD once you have logged in.",
        settings.bootstrap_admin_username,
        settings.bootstrap_admin_org_path,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = bff_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    pool = await create_pool(settings)
    user_repo = UserRepository(pool)
    session_repo = SessionRepository(pool)
    api_key_repo = ApiKeyRepository(pool)

    # Built before the bootstrap seed so `auth_admin_seeded` lands in the
    # same hash chain every other auth event does — the first account ever
    # created on a deployment is exactly the row an auditor most wants to
    # see recorded.
    audit = AuditLog(settings.audit_db_path)
    await _seed_bootstrap_admin(pool, settings, user_repo, audit=audit)

    registry = RegistryClient(settings)
    correlation = CorrelationClient(settings)
    match_engine = MatchEngineClient(settings)
    scope_resolver = CameraScopeResolver(
        registry,
        pool,
        root_scope=settings.state_root_org_path,
        ttl_s=settings.camera_org_cache_ttl_s,
    )
    media_issuer = MediaTicketIssuer(settings)

    app.state.pool = pool
    app.state.settings = settings
    app.state.user_repo = user_repo
    app.state.session_repo = session_repo
    app.state.api_key_repo = api_key_repo
    app.state.registry = registry
    app.state.correlation = correlation
    app.state.match_engine = match_engine
    app.state.scope_resolver = scope_resolver
    app.state.audit = audit
    app.state.media_issuer = media_issuer
    app.state.login_limiter = SlidingWindowRateLimiter(
        settings.login_rate_limit_attempts, settings.login_rate_limit_window_s
    )
    app.state.sse_active = 0
    try:
        yield
    finally:
        await registry.aclose()
        await correlation.aclose()
        await match_engine.aclose()
        audit.close()
        await pool.close()


app = FastAPI(
    title="PRAHARI BFF",
    version="0.1.0",
    summary="Identity, sessions, API keys — the browser's only way into the estate",
    lifespan=lifespan,
)


@app.exception_handler(asyncpg.DataError)
async def _data_error_handler(request: Request, exc: asyncpg.DataError) -> JSONResponse:
    # Malformed uuid/literal inputs (e.g. an org_id that isn't a uuid) reach
    # asyncpg as DataError — a caller error, not a server failure. The registry
    # already maps this to 422; the BFF must not 500 on it.
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "invalid identifier"},
    )


# --- dependency accessors ---------------------------------------------------
#
# These must be defined *before* every route that uses them: `from __future__
# import annotations` turns parameter annotations into strings that FastAPI
# resolves via `get_type_hints()` at route-decoration time, not lazily at
# request time. A route decorated before its `XDep` alias exists gets
# silently reinterpreted as a required query parameter named after the
# argument instead of a dependency — no import error, no crash, just a 422
# on every call. `/api/v1/orgs` shipped exactly that bug once already.


def get_registry_client(request: Request) -> RegistryClient:
    return request.app.state.registry


def get_correlation_client(request: Request) -> CorrelationClient:
    return request.app.state.correlation


def get_match_engine_client(request: Request) -> MatchEngineClient:
    return request.app.state.match_engine


def get_scope_resolver(request: Request) -> CameraScopeResolver:
    return request.app.state.scope_resolver


def get_audit_log(request: Request) -> AuditLog:
    return request.app.state.audit


RegistryDep = Annotated[RegistryClient, Depends(get_registry_client)]
CorrelationDep = Annotated[CorrelationClient, Depends(get_correlation_client)]
MatchEngineDep = Annotated[MatchEngineClient, Depends(get_match_engine_client)]
ScopeResolverDep = Annotated[CameraScopeResolver, Depends(get_scope_resolver)]
AuditDep = Annotated[AuditLog, Depends(get_audit_log)]


# --- browser-facing middleware ----------------------------------------------


_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # The API serves JSON/SSE/files to exactly one origin (the console),
    # so the most conservative policy is also the correct one.
    "Content-Security-Policy": "default-src 'self'",
}


def _with_security_headers(response: Response) -> Response:
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


@app.middleware("http")
async def origin_and_security_headers(request: Request, call_next):
    """Two small always-on guards in one pass:

    1. Origin check on mutations: a POST/PATCH/PUT/DELETE carrying an
       `Origin` header that does not match the request's `Host` is a
       cross-site form/fetch and is refused — the session cookie's
       `SameSite=Lax` already covers top-level navigations, this covers
       what Lax does not. Requests with no `Origin` (curl, services,
       non-browser clients) are unaffected; `/auth/login` is deliberately
       included, since a cross-site login is exactly what CSRF means here.
    2. A fixed set of defensive response headers on everything that leaves,
       errors included.
    """
    origin = request.headers.get("origin")
    if request.method in _MUTATING_METHODS and origin:
        # Browser traffic arrives via the Next.js proxy: the BFF's own Host is
        # the in-cluster service name, so the proxy forwards the browser's host
        # as X-Forwarded-Host and the real Origin through verbatim. The proxy is
        # the only path that can legitimately set X-Forwarded-Host — direct
        # callers that spoof it still fail unless their Origin also lies to
        # match, which a browser's fetch cannot do.
        expected_host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        if urlparse(origin).netloc != expected_host:
            denied = _with_security_headers(
                JSONResponse(
                    {"detail": "origin does not match request host"},
                    status_code=status.HTTP_403_FORBIDDEN,
                )
            )
            count_response(denied.status_code)
            return denied
    response = _with_security_headers(await call_next(request))
    count_response(response.status_code)
    return response


# --- audit helper -------------------------------------------------------------
#
# The privacy invariant: every evidence access is written to the hash-chained
# audit log — no exceptions, no "internal" bypass. In code that means the
# append happens *before* the response is served, and a failed append fails
# the request closed (500) rather than serving an unaudited access. Mutations
# get the same guarantee from `_audit_mutation`: the intent row lands before
# the mutation commits, so a committed change can never exist unrecorded.

_ADMIN_PURPOSE = "admin"
"""Audit `purpose_code` for admin/config actions (user/API-key/org/camera
writes, CSV import). These are not evidence reads, so they do not require
`X-Purpose-Code` — but they still get recorded."""


async def _audit_access(
    audit: AuditLog,
    principal: Principal,
    *,
    purpose_code: str,
    resource: str,
    action: str,
) -> None:
    """Append one entry, failing closed if the append itself fails.

    A response served after a failed append is an unaudited access — the one
    thing the audit log exists to prevent. Callers invoke this before
    returning; a raise here surfaces as 500, not a silently unlogged 200."""
    try:
        await audit.append(
            actor=principal.subject,
            org_path=principal.org_path,
            purpose_code=purpose_code,
            resource=resource,
            action=action,
        )
    except Exception as exc:
        log.error("audit append failed for %s on %s: %s", action, resource, exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "audit log unavailable") from exc


async def _audit_event(
    request: Request,
    *,
    actor: str,
    org_path: str,
    resource: str,
    action: str,
) -> None:
    """`_audit_access` for events with no Principal — authentication itself.
    A police platform's audit log that records every camera read but no
    login is missing its most security-relevant entries.

    Best-effort, and ONLY for denied/failed attempts (`auth_login_denied`,
    `oidc_link_denied`): the attempt failed anyway, so a lost row records a
    loss that changed nothing — and a failed append must never mask the 401
    the caller is owed, or turn a missing audit log into a second failure.
    Successful authentication *mutations* take `_audit_auth_event` instead —
    fail-closed, because a session that exists unrecorded is the exact hole
    this log exists to close."""
    audit: AuditLog | None = getattr(request.app.state, "audit", None)
    if audit is None:
        return
    try:
        await audit.append(
            actor=actor,
            org_path=org_path,
            purpose_code="auth",
            resource=resource,
            action=action,
        )
    except Exception as exc:
        log.error("audit append failed for %s on %s: %s", action, resource, exc)


async def _audit_auth_event(
    request: Request,
    *,
    actor: str,
    org_path: str,
    resource: str,
    action: str,
) -> None:
    """The fail-closed half of the auth-event pair — `_audit_access` for the
    mutations authentication itself commits: a session minted (`auth_login`),
    a session revoked (`auth_logout`), an SSO user provisioned
    (`auth_user_provisioned`).

    Same contract as `_audit_access` and `_audit_mutation`'s intent row: the
    append lands BEFORE the mutation runs, and a failed append — or an audit
    log that is absent entirely — aborts the request with 500 so no session
    or account can ever commit unrecorded. Callers place this between
    "credentials verified" and "mutation", never after."""
    audit: AuditLog | None = getattr(request.app.state, "audit", None)
    if audit is None:
        log.error(
            "audit log unavailable for %s on %s — refusing to commit unaudited",
            action,
            resource,
        )
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "audit log unavailable")
    try:
        await audit.append(
            actor=actor,
            org_path=org_path,
            purpose_code="auth",
            resource=resource,
            action=action,
        )
    except Exception as exc:
        log.error("audit append failed for %s on %s: %s", action, resource, exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "audit log unavailable") from exc


async def _require_session_admin(
    principal: Principal, *, audit: AuditLog, audit_action: str
) -> None:
    """Minting credentials is session-only: an admin-purpose API key must not
    be able to create users or mint more keys — a leaked key would otherwise
    be a self-renewing root of trust, and `created_by` would have no real
    user to name. The denial is itself audited."""
    if principal.kind == "api_key":
        await _audit_access(
            audit,
            principal,
            purpose_code=_ADMIN_PURPOSE,
            resource=f"api_key:{principal.subject}",
            action=audit_action,
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "API keys cannot create users or API keys — a session principal is required",
        )


async def _audit_mutation[T](
    audit: AuditLog,
    principal: Principal,
    *,
    purpose_code: str,
    resource: str,
    action: str,
    mutation: Callable[[], Awaitable[T]],
) -> T:
    """Run one state-changing operation between two audit rows.

    The ordering is the whole point. The pre-fix shape appended only *after*
    the upstream call returned, so a failed append left a committed mutation
    with no record at all — exactly backwards for an accountability log.
    Here `<action>_requested` lands before the mutation runs and fails
    closed: if the intent row cannot be written the mutation never happens
    (HTTPException 500, nothing to record because nothing occurred).

    Once the callable settles, the outcome row is `<action>` on success or
    `<action>_failed` on any exception it raises — an upstream 4xx included,
    since callers put `_forward_json` inside the callable. A refused scope
    check is *not* this helper's job: denials stay where they are, audited
    as `<action>_denied` before the mutation is ever attempted.

    Failure semantics, decided and documented:

    - Intent append fails → abort, 500. Fail-closed, same as reads.
    - Mutation fails → best-effort `<action>_failed` row, then the original
      exception propagates (it is the honest response to the caller). If
      that append also fails the loss is logged loudly, not masked.
    - Mutation succeeds but the outcome append fails → 500. The 500 cannot
      un-commit the mutation, but it does not need to: the intent row
      already records who changed what, and a degraded audit log must
      surface rather than let the request return 200 with its outcome row
      silently missing.
    """
    await _audit_access(
        audit,
        principal,
        purpose_code=purpose_code,
        resource=resource,
        action=f"{action}_requested",
    )
    try:
        result = await mutation()
    except Exception:
        try:
            await audit.append(
                actor=principal.subject,
                org_path=principal.org_path,
                purpose_code=purpose_code,
                resource=resource,
                action=f"{action}_failed",
            )
        except Exception as audit_exc:  # noqa: BLE001 - log, never mask the real error
            log.error(
                "audit append failed for %s_failed on %s (mutation error is propagating): %s",
                action,
                resource,
                audit_exc,
            )
        raise
    await _audit_access(
        audit,
        principal,
        purpose_code=purpose_code,
        resource=resource,
        action=action,
    )
    return result


# --- probes --------------------------------------------------------------


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict:
    return {"status": "ok", "service": "bff"}


@app.get("/readyz", tags=["ops"])
async def readyz(request: Request, response: Response) -> dict:
    try:
        await request.app.state.pool.fetchval("SELECT 1")
    except Exception:  # noqa: BLE001 - readiness reports any failure, not a chosen subset
        # Log the real error; return a generic one — exception text can carry
        # connection strings or schema details that an unauthenticated probe
        # has no business seeing.
        log.exception("readiness check failed")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "error"}
    return {"status": "ready", "database": "ok"}


@app.get("/metrics", tags=["ops"])
async def metrics(request: Request) -> PlainTextResponse:
    """Prometheus exposition on the BFF's own port. Unauthenticated like
    match-engine's and the registry's — a scraper cannot hold a session, and
    the payload is operational counters only (request status classes, live
    SSE connections, audit row count): no routes, actors or query strings.
    """
    await refresh_gauges(request.app.state)
    return PlainTextResponse(METRICS.render())


# --- auth ------------------------------------------------------------------


_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$jCxO1K7cdhHDRBrnpjRl+g$"
    "LtRw0h8MiJnZw+QrvdSLl9oDKOb201kVoXoezI9ii34"
)
"""A real argon2id hash (of a throwaway constant), verified for unknown
usernames so the timing profile of "no such user" is indistinguishable from
"wrong password" — without it, the username-enumeration oracle is one
`verify_password` early-exit wide."""


@app.post("/api/v1/auth/login", response_model=User, tags=["auth"])
async def login(payload: LoginRequest, request: Request, response: Response) -> User:
    settings: BFFSettings = request.app.state.settings
    user_repo: UserRepository = request.app.state.user_repo
    session_repo: SessionRepository = request.app.state.session_repo
    limiter: SlidingWindowRateLimiter = request.app.state.login_limiter

    # Throttle before doing any credential work: per-username so a targeted
    # account can't be hammered, per-IP so one source can't spray usernames.
    # The IP bucket gets the looser cap — behind the console proxy every
    # browser arrives from the same pod IP, so it is the deployment-global
    # bucket, not a per-user one (see BFFSettings.login_ip_rate_limit_attempts).
    if not limiter.allow(f"u:{payload.username}"):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "too many login attempts — try again shortly"
        )
    if request.client is not None and not limiter.allow(
        f"ip:{request.client.host}", max_attempts=settings.login_ip_rate_limit_attempts
    ):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "too many login attempts — try again shortly"
        )

    resolved = await user_repo.get_by_username_with_hash(payload.username)
    user, password_hash = resolved if resolved is not None else (None, _DUMMY_PASSWORD_HASH)
    # `verify_password` runs on every path — same 401 and same argon2 cost
    # whether the username doesn't exist or the password is wrong;
    # distinguishing them would tell an attacker which usernames are real.
    password_ok = verify_password(payload.password, password_hash)
    if user is None or user.disabled_at is not None or not password_ok:
        await _audit_event(
            request,
            actor=f"unauthenticated:{payload.username}",
            org_path="-",
            resource=f"user:{payload.username}",
            action="auth_login_denied",
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    org_path = await org_path_for_id(request.app.state.pool, user.org_id) or "-"
    # Fail-closed, BEFORE the session row exists: a session minted while the
    # audit log is down is a live credential no record can name, so a failed
    # append is a 500 here — never a logged warning beside a minted session.
    await _audit_auth_event(
        request,
        actor=user.username,
        org_path=org_path,
        resource=f"user:{user.username}",
        action="auth_login",
    )

    session_id, expires_at = await session_repo.create(
        user.id, ttl_hours=settings.session_ttl_hours
    )
    response.set_cookie(
        settings.session_cookie_name,
        session_id,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        expires=expires_at,
    )
    return user


@app.post("/api/v1/auth/logout", tags=["auth"])
async def logout(request: Request, response: Response) -> dict:
    settings: BFFSettings = request.app.state.settings
    session_repo: SessionRepository = request.app.state.session_repo

    session_id = request.cookies.get(settings.session_cookie_name)
    if session_id:
        # Resolve BEFORE revoking — the revoked session no longer resolves.
        # Audit fail-closed before the revoke commits, the same ordering
        # `_audit_mutation` enforces everywhere else: a destroyed session
        # must never exist without a record of who signed out. An
        # unresolvable (already-dead) session writes no row and is still
        # revoked — there is nothing to record, and the cleanup is safe.
        resolved = await session_repo.resolve(session_id)
        if resolved is not None:
            user, org_path = resolved
            await _audit_auth_event(
                request,
                actor=user.username,
                org_path=org_path,
                resource=f"user:{user.username}",
                action="auth_logout",
            )
        await session_repo.revoke(session_id)
    response.delete_cookie(settings.session_cookie_name)
    return {"status": "ok"}


@app.get("/api/v1/auth/me", response_model=Principal, tags=["auth"])
async def me(principal: PrincipalDep) -> Principal:
    return principal


# --- admin: users and API keys ---------------------------------------------
#
# Both actions are scoped the same way: the target org must be at or below
# the calling admin's own org (`in_scope`, the Python-side mirror of the
# `path <@ scope` predicate every other scoped read already applies) — an
# admin at zone-4 cannot create a user or issue a key for a different zone,
# let alone for the state root.


@app.post(
    "/api/v1/auth/users",
    response_model=User,
    status_code=status.HTTP_201_CREATED,
    tags=["auth"],
)
async def create_user(
    payload: UserCreate, principal: AdminDep, audit: AuditDep, request: Request
) -> User:
    await _require_session_admin(principal, audit_action="user_create_denied", audit=audit)
    target_path = await org_path_for_id(request.app.state.pool, payload.org_id)
    if target_path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no org {payload.org_id}")
    if not in_scope(target_path, principal.org_path):
        await _audit_access(
            audit,
            principal,
            purpose_code=_ADMIN_PURPOSE,
            resource=f"user:{payload.username}",
            action="user_create_denied",
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "org is outside your own org subtree")
    return await _audit_mutation(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=f"user:{payload.username}",
        action="user_create",
        mutation=lambda: request.app.state.user_repo.create(payload),
    )


@app.post(
    "/api/v1/auth/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["auth"],
)
async def create_api_key(
    payload: ApiKeyCreate, principal: AdminDep, audit: AuditDep, request: Request
) -> ApiKeyCreated:
    await _require_session_admin(principal, audit_action="api_key_create_denied", audit=audit)
    target_path = await org_path_for_id(request.app.state.pool, payload.org_id)
    if target_path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no org {payload.org_id}")
    if not in_scope(target_path, principal.org_path):
        await _audit_access(
            audit,
            principal,
            purpose_code=_ADMIN_PURPOSE,
            resource=f"api_key:{payload.label}",
            action="api_key_create_denied",
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "org is outside your own org subtree")

    api_key_repo: ApiKeyRepository = request.app.state.api_key_repo
    key, plaintext = await _audit_mutation(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=f"api_key:{payload.label}",
        action="api_key_create",
        mutation=lambda: api_key_repo.create(payload, created_by=principal.id),
    )
    return ApiKeyCreated(**key.model_dump(), plaintext=plaintext)


@app.get("/api/v1/auth/users", response_model=list[User], tags=["auth"])
async def list_users(principal: AdminDep, request: Request) -> list[User]:
    """Every user in the caller's own org subtree. The subtree predicate is
    applied in SQL (`UserRepository.list_users`) against the *caller's*
    `org_path` — the same rule `create_user` applies to a single target org,
    read instead of write."""
    return await request.app.state.user_repo.list_users(principal.org_path)


async def _set_user_disabled(
    user_id: str,
    *,
    disabled: bool,
    action: str,
    principal: Principal,
    audit: AuditLog,
    request: Request,
) -> User:
    """Shared body of the disable/enable pair: resolve the target, scope-
    check its org exactly like `create_user` checks a target org, write,
    audit. Denials and failures are audit entries, not silent responses."""
    user_repo: UserRepository = request.app.state.user_repo
    target = await user_repo.get(user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no user {user_id}")
    target_path = await org_path_for_id(request.app.state.pool, target.org_id)
    if target_path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no org {target.org_id}")
    if not in_scope(target_path, principal.org_path):
        await _audit_access(
            audit,
            principal,
            purpose_code=_ADMIN_PURPOSE,
            resource=f"user:{target.username}",
            action=f"{action}_denied",
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "user is outside your own org subtree")

    async def _write() -> User:
        updated = await user_repo.set_disabled(user_id, disabled=disabled)
        if updated is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no user {user_id}")
        return updated

    return await _audit_mutation(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=f"user:{target.username}",
        action=action,
        mutation=_write,
    )


@app.post("/api/v1/auth/users/{user_id}/disable", response_model=User, tags=["auth"])
async def disable_user(
    user_id: str, principal: AdminDep, audit: AuditDep, request: Request
) -> User:
    """Sets `disabled_at` — sticky, per migrations/006: existing sessions
    stop resolving, not just future logins. Idempotent: re-disabling keeps
    the first `disabled_at`."""
    return await _set_user_disabled(
        user_id,
        disabled=True,
        action="user_disable",
        principal=principal,
        audit=audit,
        request=request,
    )


@app.post("/api/v1/auth/users/{user_id}/enable", response_model=User, tags=["auth"])
async def enable_user(user_id: str, principal: AdminDep, audit: AuditDep, request: Request) -> User:
    return await _set_user_disabled(
        user_id,
        disabled=False,
        action="user_enable",
        principal=principal,
        audit=audit,
        request=request,
    )


@app.get("/api/v1/auth/api-keys", response_model=list[ApiKey], tags=["auth"])
async def list_api_keys(principal: AdminDep, request: Request) -> list[ApiKey]:
    """Metadata for every key in the caller's subtree — `key_hash` is never
    selected, so there is nothing here a response could leak. The plaintext
    is shown once at creation and is unrecoverable by design."""
    return await request.app.state.api_key_repo.list_keys(principal.org_path)


@app.post("/api/v1/auth/api-keys/{key_id}/revoke", response_model=ApiKey, tags=["auth"])
async def revoke_api_key(
    key_id: str, principal: AdminDep, audit: AuditDep, request: Request
) -> ApiKey:
    """Idempotent revoke, scope-checked against the *key's* org the same way
    `create_api_key` scope-checks a target org — a zone admin cannot revoke
    a sibling zone's keys any more than it could mint them."""
    api_key_repo: ApiKeyRepository = request.app.state.api_key_repo
    key = await api_key_repo.get(key_id)
    if key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no api key {key_id}")
    target_path = await org_path_for_id(request.app.state.pool, key.org_id)
    if target_path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no org {key.org_id}")
    if not in_scope(target_path, principal.org_path):
        await _audit_access(
            audit,
            principal,
            purpose_code=_ADMIN_PURPOSE,
            resource=f"api_key:{key.label}",
            action="api_key_revoke_denied",
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "api key is outside your own org subtree")

    async def _revoke() -> ApiKey:
        revoked = await api_key_repo.revoke(key_id)
        if revoked is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no api key {key_id}")
        return revoked

    return await _audit_mutation(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=f"api_key:{key.label}",
        action="api_key_revoke",
        mutation=_revoke,
    )


# --- orgs --------------------------------------------------------------------
#
# The console's board switch needs to know its own org's `kind`
# (state | organization | local_body) and, for the org-admin screen, the
# rest of its subtree — both are the registry's own `/api/v1/orgs`, scoped
# here exactly like any other read. Creating a sub-org reuses
# `_check_target_org` on `parent_id`: the same rule camera writes already
# apply to `org_id` — default to the caller's own org, and anything given
# explicitly must already be within the caller's own subtree.


@app.get("/api/v1/orgs", tags=["orgs"])
async def list_orgs(principal: PrincipalDep, registry: RegistryDep) -> list:
    return _forward_json(await registry.get("/api/v1/orgs", {"scope": principal.org_path}))


@app.post("/api/v1/orgs", status_code=status.HTTP_201_CREATED, tags=["orgs"])
async def create_org(
    principal: AdminDep, registry: RegistryDep, audit: AuditDep, request: Request
) -> dict:
    body = await request.json()
    body["parent_id"] = await _check_target_org(
        request,
        principal,
        body.get("parent_id"),
        audit=audit,
        denied_action="org_create_denied",
        resource=f"org:{body.get('label', 'new')}",
    )

    async def _create() -> dict:
        return _forward_json(await registry.post("/api/v1/orgs", json=body))

    return await _audit_mutation(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=f"org:{body.get('label', 'new')}",
        action="org_create",
        mutation=_create,
    )


def _forward_json(response: httpx.Response):
    if response.status_code >= 400:
        raise HTTPException(response.status_code, _upstream_detail(response))
    return response.json()


def _upstream_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text
    return body.get("detail", response.text) if isinstance(body, dict) else response.text


def _public_camera(camera: dict) -> dict:
    """The browser-facing projection of a registry `Camera`.

    `endpoints` is dropped wholesale: on the registry it now carries
    credential-bearing fan-out URLs meant for inference workers only
    (`StreamEndpoints` docstring), and no response to a browser may contain a
    stream URL — the audited path is `POST /api/v1/media/preview-ticket`. The
    registry's `preview` capability flag stays; it is exactly as much as the
    console needs to decide whether to offer the button.
    """
    if isinstance(camera, dict):
        camera = dict(camera)
        camera.pop("endpoints", None)
    return camera


def _scoped_params(request: Request, principal: Principal) -> dict:
    """Every filter a client sent, minus any `org_scope` it tried to set
    itself — the scope is always the caller's own org, never negotiable. This
    is what lets every registry endpoint below stay unchanged from Stage 1:
    it already takes `org_scope`, the BFF just becomes the only caller
    trusted to supply a truthful one."""
    params = dict(request.query_params)
    params.pop("org_scope", None)
    params["org_scope"] = principal.org_path
    return params


# --- cameras & gaps: scoped proxy to the registry ---------------------------
#
# Browsing (list/summary/geojson/gaps) is not evidence access, so none of
# these take a purpose code — only the camera-detail read below does, since
# it is a specific, identifiable asset rather than a filtered view over many.


@app.get("/api/v1/cameras", tags=["cameras"])
async def list_cameras(principal: PrincipalDep, registry: RegistryDep, request: Request) -> list:
    cameras = _forward_json(
        await registry.get("/api/v1/cameras", _scoped_params(request, principal))
    )
    return [_public_camera(c) for c in cameras]


@app.get("/api/v1/cameras/summary", tags=["cameras"])
async def cameras_summary(principal: PrincipalDep, registry: RegistryDep, request: Request) -> dict:
    return _forward_json(
        await registry.get("/api/v1/cameras/summary", _scoped_params(request, principal))
    )


@app.get("/api/v1/cameras/geojson", tags=["cameras"])
async def cameras_geojson(principal: PrincipalDep, registry: RegistryDep, request: Request) -> dict:
    return _forward_json(
        await registry.get("/api/v1/cameras/geojson", _scoped_params(request, principal))
    )


@app.get("/api/v1/gaps/districts", tags=["gaps"])
async def gaps_districts(principal: PrincipalDep, registry: RegistryDep, request: Request) -> list:
    return _forward_json(
        await registry.get("/api/v1/gaps/districts", _scoped_params(request, principal))
    )


@app.get("/api/v1/gaps/dark-zones", tags=["gaps"])
async def gaps_dark_zones(principal: PrincipalDep, registry: RegistryDep, request: Request) -> list:
    return _forward_json(
        await registry.get("/api/v1/gaps/dark-zones", _scoped_params(request, principal))
    )


@app.get("/api/v1/gaps/nearest", tags=["gaps"])
async def gaps_nearest(principal: PrincipalDep, registry: RegistryDep, request: Request) -> list:
    return _forward_json(
        await registry.get("/api/v1/gaps/nearest", _scoped_params(request, principal))
    )


# `{camera_id}` must be registered after the literal `/summary` and
# `/geojson` paths above — FastAPI matches routes in registration order, and
# a path param would otherwise swallow those literal segments first.
@app.get("/api/v1/cameras/{camera_id}", tags=["cameras"])
async def get_camera(
    camera_id: str,
    principal: PrincipalDep,
    purpose_code: PurposeCodeDep,
    registry: RegistryDep,
    scope_resolver: ScopeResolverDep,
    audit: AuditDep,
) -> dict:
    """Scoped like every other camera read, but with a distinction the
    others don't need: a camera outside the caller's subtree is `403`, not
    the `404` a plain scoped query would return — indistinguishable
    otherwise from "does not exist" (docs/ORG-TIERS-DESIGN.md §4.2's gate
    test 4). That needs the camera's *actual* org, resolved root-scoped, so
    it can be compared against the caller's own scope before deciding which
    of the two responses is honest."""
    org_path = await scope_resolver.org_path_for_camera(camera_id)
    if org_path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no camera {camera_id}")
    if not in_scope(org_path, principal.org_path):
        await _audit_access(
            audit,
            principal,
            purpose_code=purpose_code,
            resource=f"camera:{camera_id}",
            action="denied",
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "camera is outside your org subtree")
    response = await registry.get(f"/api/v1/cameras/{camera_id}", {"org_scope": org_path})
    # Audit before the response is served, with the outcome recorded — a
    # failed append fails closed rather than serving an unlogged read, and
    # an upstream 404 is `read_failed`, not `read`.
    await _audit_access(
        audit,
        principal,
        purpose_code=purpose_code,
        resource=f"camera:{camera_id}",
        action="read" if response.status_code < 400 else "read_failed",
    )
    return _public_camera(_forward_json(response))


@app.get("/api/v1/cameras/{camera_id}/health-history", tags=["cameras"])
async def camera_health_history(
    camera_id: str,
    principal: PrincipalDep,
    registry: RegistryDep,
    request: Request,
) -> list:
    """Operational timeseries (heartbeat-derived health samples), not an
    evidence read — so `PrincipalDep` only, no `X-Purpose-Code` and no audit
    entry, unlike the camera-detail route above which resolves a specific
    identifiable asset. Scope is enforced upstream the same way the list
    endpoints do it: the caller's own `org_path` goes down as `org_scope`
    and an out-of-scope camera reads as the upstream 404, not a filtered
    row."""
    return _forward_json(
        await registry.get(
            f"/api/v1/cameras/{camera_id}/health-history",
            _scoped_params(request, principal),
        )
    )


# --- cameras: registration, edit, decommission -------------------------------
#
# Stage 4b. The registry's own create/update/decommission handlers have no
# principal concept at all (services/registry/src/prahari_registry/app.py) —
# they take org_id/scope from the payload at face value. So org-scoping for
# camera writes lives here, mirroring create_user/create_api_key above: an
# operator may only write cameras at or below their own org, and a reassign
# (org_id in the body) is itself checked against the caller's subtree, not
# just the camera's current org.


async def _check_target_org(
    request: Request,
    principal: Principal,
    org_id: str | None,
    *,
    audit: AuditLog | None = None,
    denied_action: str | None = None,
    resource: str | None = None,
) -> str:
    """Resolve `org_id` (or the caller's own org, when the body omits one) to
    a path, and 403 if it falls outside the caller's own subtree. Shared by
    create (where a missing org_id must default to the caller's own org, not
    to no scope check at all) and update (where org_id is an optional
    reassignment). When `audit`/`denied_action` are given, a refused check is
    itself written to the audit log before the 403."""
    if org_id is None:
        return principal.org_id
    target_path = await org_path_for_id(request.app.state.pool, org_id)
    if target_path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no org {org_id}")
    if not in_scope(target_path, principal.org_path):
        if audit is not None:
            await _audit_access(
                audit,
                principal,
                purpose_code=_ADMIN_PURPOSE,
                resource=resource or f"org:{org_id}",
                action=denied_action or "org_check_denied",
            )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "org is outside your own org subtree")
    return org_id


@app.post(
    "/api/v1/cameras",
    status_code=status.HTTP_201_CREATED,
    tags=["cameras"],
)
async def create_camera(
    principal: OperatorDep, registry: RegistryDep, audit: AuditDep, request: Request
) -> dict:
    body = await request.json()
    resource = f"camera:{body.get('external_id', 'new')}"
    body["org_id"] = await _check_target_org(
        request,
        principal,
        body.get("org_id"),
        audit=audit,
        denied_action="camera_create_denied",
        resource=resource,
    )

    async def _create() -> dict:
        return _public_camera(_forward_json(await registry.post("/api/v1/cameras", json=body)))

    return await _audit_mutation(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=resource,
        action="camera_create",
        mutation=_create,
    )


@app.patch("/api/v1/cameras/{camera_id}", tags=["cameras"])
async def update_camera(
    camera_id: str,
    principal: OperatorDep,
    registry: RegistryDep,
    scope_resolver: ScopeResolverDep,
    audit: AuditDep,
    request: Request,
) -> dict:
    org_path = await scope_resolver.org_path_for_camera(camera_id)
    if org_path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no camera {camera_id}")
    if not in_scope(org_path, principal.org_path):
        await _audit_access(
            audit,
            principal,
            purpose_code=_ADMIN_PURPOSE,
            resource=f"camera:{camera_id}",
            action="camera_update_denied",
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "camera is outside your org subtree")
    body = await request.json()
    if "org_id" in body:
        body["org_id"] = await _check_target_org(
            request,
            principal,
            body["org_id"],
            audit=audit,
            denied_action="camera_update_denied",
            resource=f"camera:{camera_id}",
        )

    async def _update() -> dict:
        return _public_camera(
            _forward_json(await registry.patch(f"/api/v1/cameras/{camera_id}", json=body))
        )

    return await _audit_mutation(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=f"camera:{camera_id}",
        action="camera_update",
        mutation=_update,
    )


@app.delete("/api/v1/cameras/{camera_id}", tags=["cameras"])
async def decommission_camera(
    camera_id: str,
    principal: OperatorDep,
    registry: RegistryDep,
    scope_resolver: ScopeResolverDep,
    audit: AuditDep,
) -> dict:
    org_path = await scope_resolver.org_path_for_camera(camera_id)
    if org_path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no camera {camera_id}")
    if not in_scope(org_path, principal.org_path):
        await _audit_access(
            audit,
            principal,
            purpose_code=_ADMIN_PURPOSE,
            resource=f"camera:{camera_id}",
            action="camera_delete_denied",
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "camera is outside your org subtree")

    async def _delete() -> dict:
        return _public_camera(_forward_json(await registry.delete(f"/api/v1/cameras/{camera_id}")))

    return await _audit_mutation(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=f"camera:{camera_id}",
        action="camera_delete",
        mutation=_delete,
    )


def _redact_url_credentials(url: str) -> str:
    """`url` minus userinfo and query — the audit-log-safe form of an
    operator-supplied stream URL.

    The probe endpoint's audit `resource` names the URL being probed, and
    the audit log is the immutable hash chain: `rtsp://user:pass@host/...`
    written there verbatim would pin operator credentials into a log that
    exists to be kept. Same rule the registry's `redact_url_credentials`
    applies to its own diagnostic surface — reimplemented here rather than
    imported because the BFF does not depend on the registry *package*
    (their contract is HTTP, not code).

    The query string is dropped entirely: some DVR/NVR lines accept
    `?username=&password=` auth. `parts.port` is guarded — a malformed port
    degrades to host-only rather than a 500 from the audit path.
    """
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"  # IPv6 literal — hostname strips the brackets
    try:
        if parts.port is not None:
            netloc += f":{parts.port}"
    except ValueError:
        pass  # malformed port — emit host-only rather than fail the endpoint
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


@app.post("/api/v1/cameras/probe", tags=["cameras"])
async def probe_camera(
    principal: OperatorDep,
    purpose_code: PurposeCodeDep,
    registry: RegistryDep,
    audit: AuditDep,
    request: Request,
) -> dict:
    """Proxies to the registry's SSRF-hardened probe (Stage 4c). Gated and
    audited here, not in the registry: this is a server-side fetch of an
    operator-supplied URL, so it needs the same `operator`-plus-purpose-code
    bar as any other camera write, even though it touches no stored camera or
    org — the registry endpoint itself has no principal concept to enforce
    that with."""
    body = await request.json()
    # The URL goes upstream verbatim for the probe itself, but into the
    # audit resource only redacted — the hash chain must never carry the
    # credential-bearing form (or a query string DVRs accept as auth).
    rtsp_url = body.get("rtsp_url")
    resource = (
        f"camera-probe:{_redact_url_credentials(rtsp_url)}"
        if isinstance(rtsp_url, str)
        else "camera-probe:<unparsable>"
    )

    async def _probe() -> dict:
        return _forward_json(await registry.post("/api/v1/cameras/probe", json=body))

    return await _audit_mutation(
        audit,
        principal,
        purpose_code=purpose_code,
        resource=resource,
        action="probe",
        mutation=_probe,
    )


# --- media: the audited video path -------------------------------------------
#
# "Every video access is written to the hash-chained audit log, with an actor
# and a purpose code." The browser never receives a stream URL — it receives
# a ticket: an Ed25519 JWT scoped to exactly one `cam-<id>` path and ~60s of
# validity, minted only after the scope check passes and the audit entry is
# durable. MediaMTX validates it by asking the registry (`authMethod: http`),
# which verifies the signature against the public key served at /jwks — so a
# stolen or out-of-scope ticket dies at the restreamer, not at the browser.


@app.get("/api/v1/media/jwks", tags=["media"])
async def media_jwks(request: Request) -> dict:
    """The public half of the ticket keypair. Deliberately unauthenticated —
    it is a public key, and the registry fetches it without credentials."""
    issuer: MediaTicketIssuer = request.app.state.media_issuer
    return issuer.jwks()


@app.post("/api/v1/media/preview-ticket", response_model=PreviewTicket, tags=["media"])
async def preview_ticket(
    payload: PreviewTicketRequest,
    principal: PrincipalDep,
    purpose_code: PurposeCodeDep,
    scope_resolver: ScopeResolverDep,
    audit: AuditDep,
    request: Request,
) -> PreviewTicket:
    """Mint a one-camera, ~60s preview ticket.

    Scoped like the camera-detail read (a camera outside the caller's subtree
    is 403, audited, not 404), but heavier: a video access, so the purpose
    code is mandatory and the audit append happens *before* the credential
    exists — a failed append fails closed and no ticket is minted.
    """
    settings: BFFSettings = request.app.state.settings
    issuer: MediaTicketIssuer = request.app.state.media_issuer

    org_path = await scope_resolver.org_path_for_camera(payload.camera_id)
    if org_path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no camera {payload.camera_id}")
    if not in_scope(org_path, principal.org_path):
        await _audit_access(
            audit,
            principal,
            purpose_code=purpose_code,
            resource=f"camera:{payload.camera_id}",
            action="video_preview_denied",
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "camera is outside your org subtree")

    await _audit_access(
        audit,
        principal,
        purpose_code=purpose_code,
        resource=f"camera:{payload.camera_id}",
        action="video_preview",
    )
    ticket = issuer.mint(
        subject=principal.subject,
        camera_id=payload.camera_id,
        ttl_s=settings.media_ticket_ttl_s,
    )
    whep_base = settings.media_whep_base_url.rstrip("/")
    return PreviewTicket(
        camera_id=payload.camera_id,
        ticket=ticket,
        whep_url=f"{whep_base}/cam-{payload.camera_id}/whep",
        expires_in=settings.media_ticket_ttl_s,
    )


# --- cameras: bulk CSV import -------------------------------------------------
#
# Stage 4d. How a ward actually onboards 200 analog cameras behind DVRs —
# one CSV, not 200 individual requests. Every row goes through the exact
# same org-scope check and registry create call as `create_camera` above;
# this is not a second, looser path. Row failures are collected rather than
# aborting the batch, so one bad row does not cost the other 199.

_IMPORT_MAX_BODY_BYTES = 2 * 1024 * 1024
"""Largest CSV body `/api/v1/cameras/import` will accept. 2 MB is ~10x a
generous 200-camera import with every column populated; anything bigger is
not an import, it is a memory attack on a single-worker async service —
`request.body()` buffers the whole thing, so the bound is enforced while
streaming, before the payload can land in memory whole."""


async def _read_bounded_body(request: Request, *, max_bytes: int) -> bytes:
    """The request body, refusing anything over `max_bytes` with 413.

    A declared Content-Length is checked before a single byte is read;
    chunked or absent lengths are bounded while streaming so an oversized
    body never assembles in full — `await request.body()` alone would bound
    the *response*, not the memory."""
    headers = getattr(request, "headers", None) or {}
    declared = headers.get("content-length")
    if declared is not None and declared.strip().isdigit() and int(declared) > max_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"import body exceeds the {max_bytes}-byte limit",
        )
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"import body exceeds the {max_bytes}-byte limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


_IMPORT_STRING_FIELDS = (
    "external_id",
    "site_name",
    "district",
    "department",
    "owner",
    "org_id",
    "camera_type",
    "vendor",
    "vms_platform",
    "codec",
    "rtsp_url",
    "hls_url",
    "whep_url",
    "storage_location",
    "stream_username",
    "stream_password",
)
_IMPORT_INT_FIELDS = ("native_width", "native_height", "retention_days", "stale_after_s")
_IMPORT_FLOAT_FIELDS = ("declared_fps",)


def _row_to_camera_payload(row: dict[str, str]) -> dict:
    payload: dict = {}
    for field in _IMPORT_STRING_FIELDS:
        value = (row.get(field) or "").strip()
        if value:
            payload[field] = value
    for field in _IMPORT_INT_FIELDS:
        value = (row.get(field) or "").strip()
        if value:
            payload[field] = int(value)
    for field in _IMPORT_FLOAT_FIELDS:
        value = (row.get(field) or "").strip()
        if value:
            payload[field] = float(value)
    latitude = (row.get("latitude") or "").strip()
    longitude = (row.get("longitude") or "").strip()
    if latitude and longitude:
        payload["location"] = {"latitude": float(latitude), "longitude": float(longitude)}
    return payload


@app.post("/api/v1/cameras/import", tags=["cameras"])
async def import_cameras(
    principal: OperatorDep, registry: RegistryDep, audit: AuditDep, request: Request
) -> dict:
    raw = (await _read_bounded_body(request, max_bytes=_IMPORT_MAX_BODY_BYTES)).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames or "external_id" not in reader.fieldnames:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "CSV must have an external_id column")

    rows = list(reader)
    # Same audit-before-commit rule as `_audit_mutation`, at batch granularity:
    # the intent row lands before *any* row is created upstream, so committed
    # creates can never exist without a record of who asked for them. One row,
    # not one per CSV line — per-row entries would flood the chain for a
    # 200-camera CSV while adding nothing the per-row results don't carry.
    await _audit_access(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=f"cameras-import:{len(rows)}",
        action="camera_import_requested",
    )

    results: list[dict] = []
    try:
        for line_no, row in enumerate(rows, start=2):  # header occupies line 1
            external_id = (row.get("external_id") or "").strip()
            if not external_id:
                results.append({"row": line_no, "ok": False, "error": "external_id is required"})
                continue
            try:
                payload = _row_to_camera_payload(row)
                # A scope denial here is a row swallowed into a per-row error, not
                # a request failure — without audit kwargs it would leave no trace
                # that an operator probed an out-of-scope org via the import path.
                payload["org_id"] = await _check_target_org(
                    request,
                    principal,
                    payload.get("org_id"),
                    audit=audit,
                    denied_action="camera_import_denied",
                    resource=f"camera:{external_id}",
                )
                response = await registry.post("/api/v1/cameras", json=payload)
            except HTTPException as exc:
                results.append(
                    {"row": line_no, "external_id": external_id, "ok": False, "error": exc.detail}
                )
                continue
            except (ValueError, KeyError) as exc:
                results.append(
                    {"row": line_no, "external_id": external_id, "ok": False, "error": str(exc)}
                )
                continue

            if response.status_code >= 400:
                results.append(
                    {
                        "row": line_no,
                        "external_id": external_id,
                        "ok": False,
                        "error": _upstream_detail(response),
                    }
                )
            else:
                results.append(
                    {
                        "row": line_no,
                        "external_id": external_id,
                        "ok": True,
                        "id": response.json().get("id"),
                    }
                )
    except Exception:
        # Reaching here means the batch itself died mid-import (e.g. the
        # registry connection dropped) — per-row failures are captured in
        # `results` and never reach this handler. Rows that already committed
        # keep their record in the intent row above; this entry says the batch
        # never completed. Best-effort: a failed append must not mask the
        # original error.
        try:
            await audit.append(
                actor=principal.subject,
                org_path=principal.org_path,
                purpose_code=_ADMIN_PURPOSE,
                resource=f"cameras-import:{len(rows)}",
                action="camera_import_failed",
            )
        except Exception as audit_exc:  # noqa: BLE001 - log, never mask the real error
            log.error("audit append failed for camera_import_failed: %s", audit_exc)
        raise

    summary = {
        "total": len(results),
        "succeeded": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
        "rows": results,
    }
    # The outcome row, carrying the row counts. Fail-closed like the intent
    # row — the creates already committed either way and are recorded there.
    await _audit_access(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=f"cameras-import:{summary['succeeded']}/{summary['total']}",
        action="camera_import",
    )
    return summary


# --- catalogue sync -----------------------------------------------------------
#
# Admin-only proxies onto the registry's own sync surface (`POST /api/v1/
# sync`, `GET /api/v1/sync/runs`). Upstream semantics pass straight through
# `_forward_json`: a second trigger while one is running is the registry's
# 409, an unconfigured gateway its 503 — neither is rewritten here.


@app.post("/api/v1/sync", tags=["sync"])
async def trigger_sync(principal: AdminDep, registry: RegistryDep, audit: AuditDep) -> dict:
    async def _trigger() -> dict:
        return _forward_json(await registry.post("/api/v1/sync"))

    return await _audit_mutation(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource="sync:catalogue",
        action="sync_trigger",
        mutation=_trigger,
    )


@app.get("/api/v1/sync/runs", tags=["sync"])
async def list_sync_runs(
    principal: AdminDep,
    registry: RegistryDep,
    limit: int = Query(10, ge=1, le=100),
) -> list:
    return _forward_json(await registry.get("/api/v1/sync/runs", {"limit": limit}))


# --- routes: the mandatory path ----------------------------------------------
#
# Deliberately NOT filtered by org scope, unlike everything above. A route is
# the record of one plate crossing camera (and therefore org) boundaries —
# that is the entire value of statewide integration, and the mandatory test
# case is exactly this: a registration number in, a complete timestamped
# route out. Redacting hops by org would silently break that. Access control
# here is instead: authenticate, require a purpose code, and audit — never
# filter the answer.


@app.get("/api/v1/routes/{plate}", tags=["routes"])
async def get_route(
    plate: str,
    principal: PrincipalDep,
    purpose_code: PurposeCodeDep,
    correlation: CorrelationDep,
    audit: AuditDep,
) -> dict:
    try:
        route = await correlation.get_route(plate)
    except httpx.HTTPStatusError as exc:
        # The attempt is audited with its outcome — a 404 upstream is a
        # `read_failed`, not a `read`, and a failed append fails closed.
        await _audit_access(
            audit,
            principal,
            purpose_code=purpose_code,
            resource=f"route:{plate}",
            action="read_failed",
        )
        raise HTTPException(exc.response.status_code, _upstream_detail(exc.response)) from exc
    except httpx.HTTPError as exc:
        await _audit_access(
            audit,
            principal,
            purpose_code=purpose_code,
            resource=f"route:{plate}",
            action="read_failed",
        )
        detail = f"correlation service error: {exc}"
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail) from exc
    await _audit_access(
        audit,
        principal,
        purpose_code=purpose_code,
        resource=f"route:{plate}",
        action="read",
    )
    return route


@app.get("/api/v1/routes/{plate}/export", tags=["routes"])
async def export_route(
    plate: str,
    principal: PrincipalDep,
    purpose_code: PurposeCodeDep,
    correlation: CorrelationDep,
    audit: AuditDep,
    export_format: Literal["csv", "pdf"] = Query(..., alias="format"),
) -> Response:
    try:
        route = await correlation.get_route(plate)
    except httpx.HTTPStatusError as exc:
        await _audit_access(
            audit,
            principal,
            purpose_code=purpose_code,
            resource=f"route:{plate}",
            action="read_failed",
        )
        raise HTTPException(exc.response.status_code, _upstream_detail(exc.response)) from exc
    except httpx.HTTPError as exc:
        await _audit_access(
            audit,
            principal,
            purpose_code=purpose_code,
            resource=f"route:{plate}",
            action="read_failed",
        )
        detail = f"correlation service error: {exc}"
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail) from exc

    # Audit before the file is rendered and served — the export is the access
    # the log exists to record; an append failure must not still hand it over.
    await _audit_access(
        audit,
        principal,
        purpose_code=purpose_code,
        resource=f"route:{plate}",
        action=f"export:{export_format}",
    )
    if export_format == "csv":
        body, media_type = route_to_csv(route), "text/csv"
    else:
        body, media_type = route_to_pdf(route), "application/pdf"
    # The plate is caller-controlled path material — quote it so a `"` or
    # CR/LF in it can't break the Content-Disposition header or inject a
    # second header.
    filename = f"route-{quote(plate, safe='')}.{export_format}"
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- watchlist admin -----------------------------------------------------------
#
# Admin-only proxies onto the match engine's own surface. The match engine
# has no principal concept, same as the registry's camera writes — the gate
# and the audit entry live here, in the one service a browser can reach.


@app.get("/api/v1/watchlist/summary", tags=["watchlist"])
async def watchlist_summary(principal: AdminDep, match_engine: MatchEngineDep) -> dict:
    return _forward_json(await match_engine.get("/api/v1/watchlist/summary"))


@app.post("/api/v1/watchlist/reload", tags=["watchlist"])
async def watchlist_reload(
    principal: AdminDep, match_engine: MatchEngineDep, audit: AuditDep
) -> dict:
    async def _reload() -> dict:
        return _forward_json(await match_engine.post("/api/v1/watchlist/reload"))

    return await _audit_mutation(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource="watchlist",
        action="watchlist_reload",
        mutation=_reload,
    )


# --- alerts --------------------------------------------------------------------
#
# Two surfaces over the same alert flow: the recent-buffer list (the match
# engine's in-memory ring — a debug/operational view, not the system of
# record) and the live SSE relay off the Redis stream. Both are scoped the
# same way: `Alert.detection.camera_id` is the only per-alert scoping
# signal, resolved through the same `CameraScopeResolver` the relay uses.
# `PrincipalDep`, not `AdminDep` — alerts are operational data every console
# role needs, and the org filter is the actual access boundary.


@app.get("/api/v1/alerts", tags=["alerts"])
async def list_alerts(
    principal: PrincipalDep,
    match_engine: MatchEngineDep,
    scope_resolver: ScopeResolverDep,
    limit: int = Query(50, ge=1, le=500),
    since: Annotated[datetime | None, Query()] = None,
    camera_id: str | None = None,
    plate: str | None = None,
    acknowledged: bool | None = None,
) -> list:
    params: dict = {"limit": limit}
    if since is not None:
        params["since"] = since.isoformat()
    if camera_id:
        params["camera_id"] = camera_id
    if plate:
        params["plate"] = plate
    if acknowledged is not None:
        params["acknowledged"] = str(acknowledged).lower()
    response = await match_engine.get("/api/v1/alerts", params)
    if response.status_code >= 400:
        return _forward_json(response)
    # Same filter as the SSE relay below: resolve the camera's real org
    # (root-scoped — the resolver is trusted to see the whole estate in
    # order to decide whether the *caller* may), drop anything outside the
    # caller's subtree or with no resolvable camera. An alert we cannot
    # attribute to an org is hidden, never leaked.
    visible = []
    for item in response.json():
        camera_id = (item.get("detection") or {}).get("camera_id")
        if camera_id is None:
            continue
        org_path = await scope_resolver.org_path_for_camera(camera_id)
        if org_path is None or not in_scope(org_path, principal.org_path):
            continue
        visible.append(item)
    return visible


@app.post("/api/v1/alerts/{alert_id}/ack", tags=["alerts"])
async def acknowledge_alert(
    alert_id: str,
    principal: PrincipalDep,
    match_engine: MatchEngineDep,
    scope_resolver: ScopeResolverDep,
    audit: AuditDep,
    request: Request,
) -> dict:
    """Acknowledge an alert — the only lifecycle transition that exists, per
    the UX spec's "acknowledge only, no assignment workflow" decision. The
    alert's camera is org-checked before the proxy so an operator cannot ack
    an alert they cannot see; the ack itself is an audit entry."""
    upstream = await match_engine.get(f"/api/v1/alerts/{alert_id}")
    if upstream.status_code == 404:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such alert")
    if upstream.status_code >= 400:
        return _forward_json(upstream)
    camera_id = (upstream.json().get("detection") or {}).get("camera_id")
    org_path = await scope_resolver.org_path_for_camera(camera_id) if camera_id else None
    if org_path is None or not in_scope(org_path, principal.org_path):
        await _audit_access(
            audit,
            principal,
            purpose_code=_ADMIN_PURPOSE,
            resource=f"alert:{alert_id}",
            action="alert_ack_denied",
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "alert is outside your own org subtree")

    async def _ack() -> dict:
        # `_forward_json` inside the mutation so an upstream 4xx/5xx lands
        # `alert_ack_failed` next to `alert_ack_requested` — the same
        # convention every other mutation follows, instead of an `alert_ack`
        # row written before the upstream call was even attempted.
        return _forward_json(
            await match_engine.post(f"/api/v1/alerts/{alert_id}/ack", {"by": principal.subject})
        )

    return await _audit_mutation(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=f"alert:{alert_id}",
        action="alert_ack",
        mutation=_ack,
    )


@app.get("/api/v1/alerts/stream", tags=["alerts"])
async def alerts_stream(principal: PrincipalDep, request: Request) -> StreamingResponse:
    settings: BFFSettings = request.app.state.settings
    scope_resolver: CameraScopeResolver = request.app.state.scope_resolver
    if settings.redis_url is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "alert stream not configured")

    # Each connection holds an open request plus one Redis connection for the
    # life of the tab — cap the fan-out. The check and the increment are
    # await-free, so two handlers can't interleave between them.
    if request.app.state.sse_active >= settings.sse_max_connections:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many concurrent alert streams")
    request.app.state.sse_active += 1

    # One consumer per connection, `start_id="$"` (the class default) — a
    # new browser tab sees alerts from the moment it opened, never a replay
    # of history, and per-connection instances make per-connection scope
    # filtering trivial rather than needing a shared fan-out. The Redis
    # client is injected so the finally-block below can close it: a dropped
    # tab must not leak a connection per poll.
    try:
        redis_client = redis_lib.Redis.from_url(settings.redis_url)
    except Exception:
        request.app.state.sse_active -= 1
        raise
    consumer = RedisStreamConsumer(
        redis_url=settings.redis_url,
        stream_key=settings.alert_stream_key,
        field="alert",
        decode=events_pb2.Alert.FromString,
        client=redis_client,
    )

    async def events():
        try:
            while True:
                if await request.is_disconnected():
                    break
                # poll() blocks up to block_ms; run it off the event loop so
                # it does not stall every other request this worker serves.
                alerts = await asyncio.to_thread(consumer.poll)
                for alert in alerts:
                    camera_id = alert.detection.camera_id
                    org_path = await scope_resolver.org_path_for_camera(camera_id)
                    if org_path is None or not in_scope(org_path, principal.org_path):
                        continue
                    payload = MessageToDict(alert, preserving_proto_field_name=True)
                    yield f"event: alert\ndata: {json.dumps(payload)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            request.app.state.sse_active -= 1
            await asyncio.to_thread(redis_client.close)

    return StreamingResponse(events(), media_type="text/event-stream")


# --- audit ---------------------------------------------------------------
#
# Admin-only reads over the chain itself. `verify` walks every hash;
# `head` is the cheap truncation tripwire — a monitor polling it sees the
# row count only ever grow and the head hash only ever advance, so rows
# deleted off the tail (which `verify` cannot see — the remaining chain is
# still internally consistent) show up as either shrinkage or a rewound
# head.


@app.get("/api/v1/audit", tags=["audit"])
async def list_audit_entries(
    principal: AdminDep,
    audit: AuditDep,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    actor: str | None = None,
    action: str | None = None,
    since: str | None = None,
) -> list[dict]:
    entries = await audit.recent(limit, offset=offset, actor=actor, action=action, since=since)
    return [dataclasses.asdict(entry) for entry in entries]


@app.get("/api/v1/audit/head", tags=["audit"])
async def audit_head(principal: AdminDep, audit: AuditDep) -> dict:
    head_hash, row_count = await audit.head()
    return {"head_hash": head_hash, "row_count": row_count}


@app.get("/api/v1/audit/verify", tags=["audit"])
async def verify_audit(principal: AdminDep, audit: AuditDep) -> dict:
    ok, first_broken_id = await audit.verify()
    head_hash, row_count = await audit.head()
    return {
        "ok": ok,
        "first_broken_entry": first_broken_id,
        "head_hash": head_hash,
        "row_count": row_count,
    }


# --- oidc (Keycloak SSO) -----------------------------------------------------
#
# `auth.kind=keycloak` in the chart (docs/KEYCLOAK.md). All three routes 404
# unless `oidc_enabled` — `builtin` leaves the estate exactly as before and the
# bootstrap admin stays the break-glass path.
#
# The flow: /oidc/login redirects the browser to Keycloak with a PKCE
# challenge; Keycloak returns a code to /oidc/callback, which exchanges it
# server-side (confidential client `prahari-bff`), validates the id_token
# against the realm JWKS, resolves or JIT-provisions the local user, and mints
# the same opaque `prahari_session` cookie builtin login does — so proxy.ts,
# SSE, RBAC and audit downstream are unchanged.


def _get_oidc(request: Request) -> OidcClient:
    """Built lazily on first use and stashed on `app.state` so the JWKS cache
    outlives one request. Tests inject their own (MockTransport-backed)
    client by setting `app.state.oidc` directly."""
    oidc = getattr(request.app.state, "oidc", None)
    if oidc is None:
        oidc = OidcClient(request.app.state.settings)
        request.app.state.oidc = oidc
    return oidc


def _require_oidc(request: Request) -> OidcClient:
    """404 — not 503 — when OIDC is off: the route does not exist in a builtin
    deployment, which is exactly what a caller probing the surface should
    learn."""
    if not request.app.state.settings.oidc_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return _get_oidc(request)


@app.get("/api/v1/auth/mode", tags=["auth"])
async def auth_mode(request: Request) -> dict:
    """Unauthenticated by design: the login page needs to know whether to
    offer the SSO button *before* the user has a session. Leaks only the
    auth kind — the issuer stays on the server."""
    settings: BFFSettings | None = getattr(request.app.state, "settings", None)
    oidc_enabled = settings.oidc_enabled if settings else False
    return {
        "kind": "keycloak" if oidc_enabled else "builtin",
        "sso_login_url": "/api/bff/auth/oidc/login" if oidc_enabled else None,
    }


@app.get("/api/v1/auth/oidc/login", tags=["auth"])
async def oidc_login(request: Request, next: str = "/") -> RedirectResponse:
    settings: BFFSettings = request.app.state.settings
    oidc = _require_oidc(request)
    verifier, challenge = new_pkce_pair()
    nonce = secrets.token_urlsafe(24)
    response = RedirectResponse(
        oidc.authorize_url(state=nonce, challenge=challenge, nonce=nonce),
        status_code=status.HTTP_302_FOUND,
    )
    # The verifier, the CSRF nonce and where to land afterward ride in one
    # signed httponly cookie — nothing server-side to persist, any replica can
    # complete the flow.
    response.set_cookie(
        STATE_COOKIE_NAME,
        oidc.seal_state(nonce=nonce, verifier=verifier, next_path=safe_next(next)),
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=STATE_TTL_S,
    )
    return response


@app.get("/api/v1/auth/oidc/callback", tags=["auth"])
async def oidc_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    settings: BFFSettings = request.app.state.settings
    session_repo: SessionRepository = request.app.state.session_repo
    user_repo: UserRepository = request.app.state.user_repo
    pool = request.app.state.pool
    oidc = _require_oidc(request)

    if not code or not state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing code or state")
    sealed = oidc.open_state(request.cookies.get(STATE_COOKIE_NAME) or "")
    if not hmac.compare_digest(str(sealed.get("n", "")), state):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "oidc state mismatch")

    token = await oidc.exchange_code(code=code, verifier=str(sealed.get("v", "")))
    claims = await oidc.validate_id_token(token["id_token"], expected_nonce=state)

    # `sub` is guaranteed present by validate_id_token (pyjwt `require`), and
    # it is the ONLY claim account resolution may key on: `preferred_username`
    # is mutable realm data, so keying the link on it would let a realm user
    # named `admin` silently inherit the builtin admin account.
    sub = claims["sub"]
    if not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "id_token carries no usable subject")
    username = claims.get("preferred_username") or sub
    org_path_claim = validate_org_path_claim(claims)

    # Resolve or JIT-provision — by `sub` FIRST, and only by `sub`. The
    # decided linking rule (security review L4):
    #
    #   * `sub` already bound to a users row → that account logs in;
    #     Postgres stays authoritative for role and org, and a present
    #     org_path claim is only a consistency check that must agree with
    #     the row.
    #   * `sub` unbound AND a user with the token's `preferred_username`
    #     exists → DENY, audited `oidc_link_denied`. A builtin account is
    #     never silently claimed by a same-named realm user, and an account
    #     bound to a *different* sub is not re-bindable through this path
    #     either. Binding an existing builtin account is an explicit admin
    #     act (a migration/tool writes users.oidc_sub directly), not
    #     something a login attempt can negotiate.
    #   * `sub` unbound AND no username collision → JIT-provision a new
    #     user row carrying this `sub`; the org_path claim is mandatory and
    #     must name an org that exists.
    user = await user_repo.get_by_oidc_sub(sub)
    if user is not None:
        if user.disabled_at is not None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "account is disabled")
        if org_path_claim is not None:
            db_org_path = await org_path_for_id(pool, user.org_id)
            if org_path_claim != db_org_path:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "org_path claim does not match this account's org",
                )
    else:
        collision = await user_repo.get_by_username_with_hash(username)
        if collision is not None:
            await _audit_event(
                request,
                actor=f"oidc:{sub}",
                org_path=org_path_claim or "-",
                resource=f"user:{username}",
                action="oidc_link_denied",
            )
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "an account with this username already exists and is not linked "
                "to this identity — an administrator must link it explicitly",
            )
        if org_path_claim is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "no org_path claim and no existing account — cannot provision",
            )
        org_id = await pool.fetchval("SELECT id FROM orgs WHERE path = $1::ltree", org_path_claim)
        if org_id is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "org_path claim names no known org")
        # Fail-closed before the row exists — a provisioned account with no
        # audit row is a session-granting identity the log cannot name.
        await _audit_auth_event(
            request,
            actor=username,
            org_path=org_path_claim,
            resource=f"user:{username}",
            action="auth_user_provisioned",
        )
        # Random unusable password: an SSO-provisioned row can never satisfy
        # builtin password login, which remains the bootstrap admin's alone.
        user = await user_repo.create(
            UserCreate(
                username=username,
                password=secrets.token_urlsafe(32),
                org_id=str(org_id),
                role=map_realm_role(claims),
            ),
            oidc_sub=sub,
        )

    # Same event builtin login writes, in the same order — fail-closed before
    # the session row exists. A session minted while the audit log is down is
    # a live credential no record can name.
    org_path = org_path_claim or await org_path_for_id(pool, user.org_id) or "-"
    await _audit_auth_event(
        request,
        actor=user.username,
        org_path=org_path,
        resource=f"user:{user.username}",
        action="auth_login",
    )
    session_id, expires_at = await session_repo.create(
        user.id, ttl_hours=settings.session_ttl_hours
    )
    response = RedirectResponse(safe_next(sealed.get("next")), status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        settings.session_cookie_name,
        session_id,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        expires=expires_at,
    )
    # Lets logout tell an OIDC-born session from a builtin one — the sessions
    # table has no auth-via column (006_identity.sql is registry-owned).
    response.set_cookie(
        OIDC_MARKER_COOKIE_NAME,
        "1",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        expires=expires_at,
    )
    response.delete_cookie(STATE_COOKIE_NAME)
    return response


@app.post("/api/v1/auth/oidc/logout", tags=["auth"])
async def oidc_logout(request: Request, response: Response) -> dict:
    """Revoke the local session AND hand back the RP-initiated logout URL.
    Returning the URL rather than 302-ing: the console's fetch follows a
    redirect into Keycloak's HTML page, so it navigates `window.location`
    itself. A non-OIDC session gets plain local logout semantics and no URL."""
    oidc = _require_oidc(request)
    settings: BFFSettings = request.app.state.settings
    session_repo: SessionRepository = request.app.state.session_repo

    session_id = request.cookies.get(settings.session_cookie_name)
    if session_id:
        # Same event and same ordering as builtin `logout`: resolve (the
        # revoked session no longer resolves), audit fail-closed, then
        # revoke. The actor is the session's OWN user — `resolve` returns
        # the users row the session belongs to — never anything read from a
        # token claim, so a forged or stale id_token cannot rename who
        # signed out.
        resolved = await session_repo.resolve(session_id)
        if resolved is not None:
            user, org_path = resolved
            await _audit_auth_event(
                request,
                actor=user.username,
                org_path=org_path,
                resource=f"user:{user.username}",
                action="auth_logout",
            )
        await session_repo.revoke(session_id)
    response.delete_cookie(settings.session_cookie_name)
    response.delete_cookie(OIDC_MARKER_COOKIE_NAME)

    result: dict = {"status": "ok"}
    if request.cookies.get(OIDC_MARKER_COOKIE_NAME) == "1":
        result["end_session_url"] = oidc.end_session_url(
            post_logout_redirect_uri=settings.oidc_redirect_base
        )
    return result


# --- evidence: audited clip-retrieval requests --------------------------------
#
# The last deferred piece of the privacy invariant — "video never leaves the
# edge except as an explicit, audited evidence request". The handlers,
# request/response models and the `evidence_requests` repository live in
# `evidence.py` (docs/EVIDENCE.md); this block only wires the router on. The
# import sits at file end deliberately: this whole feature touches app.py in
# exactly one contiguous place.

from .evidence import router as evidence_router  # noqa: E402

app.include_router(evidence_router)
