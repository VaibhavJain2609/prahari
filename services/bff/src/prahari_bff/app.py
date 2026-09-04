"""The BFF's auth surface: login, logout, `whoami`, and admin's user/API-key
management.

Everything else the browser needs — the scoped camera/gap surface, the audit
chain, SSE alerts, route export — is Stage 3
(docs/ORG-TIERS-DESIGN.md §3–4). This module's job is narrower and is a
prerequisite for all of it: turn a cookie or a bearer token into one
`Principal`, and be the only service a browser can reach directly once the
registry's `PRAHARI_INTERNAL_TOKEN` gate is on.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response, status

from .auth import AdminDep, PrincipalDep
from .config import BFFSettings, bff_settings
from .db import create_pool
from .models import ApiKeyCreate, ApiKeyCreated, LoginRequest, Principal, User, UserCreate
from .repository import (
    ApiKeyRepository,
    SessionRepository,
    UserRepository,
    in_scope,
    org_path_for_id,
)
from .security import verify_password

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

    app.state.pool = pool
    app.state.settings = settings
    app.state.user_repo = user_repo
    app.state.session_repo = session_repo
    app.state.api_key_repo = api_key_repo
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(
    title="PRAHARI BFF",
    version="0.1.0",
    summary="Identity, sessions, API keys — the browser's only way into the estate",
    lifespan=lifespan,
)


# --- probes --------------------------------------------------------------


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict:
    return {"status": "ok", "service": "bff"}


@app.get("/readyz", tags=["ops"])
async def readyz(request: Request, response: Response) -> dict:
    try:
        await request.app.state.pool.fetchval("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - readiness reports any failure, not a chosen subset
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": f"{type(exc).__name__}: {exc}"}
    return {"status": "ready", "database": "ok"}


# --- auth ------------------------------------------------------------------


@app.post("/api/v1/auth/login", response_model=User, tags=["auth"])
async def login(payload: LoginRequest, request: Request, response: Response) -> User:
    settings: BFFSettings = request.app.state.settings
    user_repo: UserRepository = request.app.state.user_repo
    session_repo: SessionRepository = request.app.state.session_repo

    resolved = await user_repo.get_by_username_with_hash(payload.username)
    if resolved is None:
        # Same 401 whether the username doesn't exist or the password is
        # wrong — distinguishing them would tell an attacker which usernames
        # are real.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    user, password_hash = resolved
    if user.disabled_at is not None or not verify_password(payload.password, password_hash):
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
async def create_user(payload: UserCreate, principal: AdminDep, request: Request) -> User:
    target_path = await org_path_for_id(request.app.state.pool, payload.org_id)
    if target_path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no org {payload.org_id}")
    if not in_scope(target_path, principal.org_path):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "org is outside your own org subtree")
    return await request.app.state.user_repo.create(payload)


@app.post(
    "/api/v1/auth/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["auth"],
)
async def create_api_key(
    payload: ApiKeyCreate, principal: AdminDep, request: Request
) -> ApiKeyCreated:
    target_path = await org_path_for_id(request.app.state.pool, payload.org_id)
    if target_path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no org {payload.org_id}")
    if not in_scope(target_path, principal.org_path):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "org is outside your own org subtree")

    api_key_repo: ApiKeyRepository = request.app.state.api_key_repo
    created_by = principal.id if principal.kind == "session" else None
    key, plaintext = await api_key_repo.create(payload, created_by=created_by)
    return ApiKeyCreated(**key.model_dump(), plaintext=plaintext)
