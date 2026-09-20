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
import io
import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.parse import quote, urlparse

import httpx
import redis as redis_lib
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from google.protobuf.json_format import MessageToDict
from prahari.v1 import events_pb2
from prahari_common.bus import RedisStreamConsumer

from .audit import AuditLog
from .auth import AdminDep, OperatorDep, PrincipalDep, PurposeCodeDep
from .config import BFFSettings, bff_settings
from .correlation_client import CorrelationClient
from .db import create_pool
from .export import route_to_csv, route_to_pdf
from .models import ApiKeyCreate, ApiKeyCreated, LoginRequest, Principal, User, UserCreate
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


async def _seed_bootstrap_admin(pool, settings: BFFSettings, user_repo: UserRepository) -> None:
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

    await _seed_bootstrap_admin(pool, settings, user_repo)

    registry = RegistryClient(settings)
    correlation = CorrelationClient(settings)
    scope_resolver = CameraScopeResolver(
        registry,
        pool,
        root_scope=settings.state_root_org_path,
        ttl_s=settings.camera_org_cache_ttl_s,
    )
    audit = AuditLog(settings.audit_db_path)

    app.state.pool = pool
    app.state.settings = settings
    app.state.user_repo = user_repo
    app.state.session_repo = session_repo
    app.state.api_key_repo = api_key_repo
    app.state.registry = registry
    app.state.correlation = correlation
    app.state.scope_resolver = scope_resolver
    app.state.audit = audit
    app.state.login_limiter = SlidingWindowRateLimiter(
        settings.login_rate_limit_attempts, settings.login_rate_limit_window_s
    )
    app.state.sse_active = 0
    try:
        yield
    finally:
        await registry.aclose()
        await correlation.aclose()
        audit.close()
        await pool.close()


app = FastAPI(
    title="PRAHARI BFF",
    version="0.1.0",
    summary="Identity, sessions, API keys — the browser's only way into the estate",
    lifespan=lifespan,
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


def get_scope_resolver(request: Request) -> CameraScopeResolver:
    return request.app.state.scope_resolver


def get_audit_log(request: Request) -> AuditLog:
    return request.app.state.audit


RegistryDep = Annotated[RegistryClient, Depends(get_registry_client)]
CorrelationDep = Annotated[CorrelationClient, Depends(get_correlation_client)]
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
        if urlparse(origin).netloc != request.headers.get("host", ""):
            return _with_security_headers(
                JSONResponse(
                    {"detail": "origin does not match request host"},
                    status_code=status.HTTP_403_FORBIDDEN,
                )
            )
    return _with_security_headers(await call_next(request))


# --- audit helper -------------------------------------------------------------
#
# The privacy invariant: every evidence access is written to the hash-chained
# audit log — no exceptions, no "internal" bypass. In code that means the
# append happens *before* the response is served, and a failed append fails
# the request closed (500) rather than serving an unaudited access.

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
    keys = [f"u:{payload.username}"]
    if request.client is not None:
        keys.append(f"ip:{request.client.host}")
    if not all(limiter.allow(key) for key in keys):
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
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

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
    try:
        user = await request.app.state.user_repo.create(payload)
    except Exception:
        await _audit_access(
            audit,
            principal,
            purpose_code=_ADMIN_PURPOSE,
            resource=f"user:{payload.username}",
            action="user_create_failed",
        )
        raise
    await _audit_access(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=f"user:{user.username}",
        action="user_create",
    )
    return user


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
    try:
        key, plaintext = await api_key_repo.create(payload, created_by=principal.id)
    except Exception:
        await _audit_access(
            audit,
            principal,
            purpose_code=_ADMIN_PURPOSE,
            resource=f"api_key:{payload.label}",
            action="api_key_create_failed",
        )
        raise
    await _audit_access(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=f"api_key:{key.label}",
        action="api_key_create",
    )
    return ApiKeyCreated(**key.model_dump(), plaintext=plaintext)


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
    response = await registry.post("/api/v1/orgs", json=body)
    await _audit_access(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=f"org:{body.get('label', 'new')}",
        action="org_create" if response.status_code < 400 else "org_create_failed",
    )
    return _forward_json(response)


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
    return _forward_json(await registry.get("/api/v1/cameras", _scoped_params(request, principal)))


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
    return _forward_json(response)


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
    response = await registry.post("/api/v1/cameras", json=body)
    await _audit_access(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=resource,
        action="camera_create" if response.status_code < 400 else "camera_create_failed",
    )
    return _forward_json(response)


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
    response = await registry.patch(f"/api/v1/cameras/{camera_id}", json=body)
    await _audit_access(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=f"camera:{camera_id}",
        action="camera_update" if response.status_code < 400 else "camera_update_failed",
    )
    return _forward_json(response)


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
    response = await registry.delete(f"/api/v1/cameras/{camera_id}")
    await _audit_access(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=f"camera:{camera_id}",
        action="camera_delete" if response.status_code < 400 else "camera_delete_failed",
    )
    return _forward_json(response)


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
    response = await registry.post("/api/v1/cameras/probe", json=body)
    await _audit_access(
        audit,
        principal,
        purpose_code=purpose_code,
        resource=f"camera-probe:{body.get('rtsp_url', '')}",
        action="probe" if response.status_code < 400 else "probe_failed",
    )
    return _forward_json(response)


# --- cameras: bulk CSV import -------------------------------------------------
#
# Stage 4d. How a ward actually onboards 200 analog cameras behind DVRs —
# one CSV, not 200 individual requests. Every row goes through the exact
# same org-scope check and registry create call as `create_camera` above;
# this is not a second, looser path. Row failures are collected rather than
# aborting the batch, so one bad row does not cost the other 199.

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
    raw = (await request.body()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames or "external_id" not in reader.fieldnames:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "CSV must have an external_id column")

    results: list[dict] = []
    for line_no, row in enumerate(reader, start=2):  # header occupies line 1
        external_id = (row.get("external_id") or "").strip()
        if not external_id:
            results.append({"row": line_no, "ok": False, "error": "external_id is required"})
            continue
        try:
            payload = _row_to_camera_payload(row)
            payload["org_id"] = await _check_target_org(request, principal, payload.get("org_id"))
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

    summary = {
        "total": len(results),
        "succeeded": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
        "rows": results,
    }
    # One entry per import, carrying the row counts — per-row entries would
    # flood the chain for a 200-camera CSV while adding nothing a reviewer
    # can't get from the per-row results.
    await _audit_access(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource=f"cameras-import:{summary['succeeded']}/{summary['total']}",
        action="camera_import",
    )
    return summary


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


# --- alerts: SSE relay --------------------------------------------------------


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
