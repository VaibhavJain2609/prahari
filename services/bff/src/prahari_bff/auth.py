"""Resolving one `Principal` from either credential type.

This is the module every other handler depends on instead of depending on
"is there a valid cookie" or "is there a valid bearer token" separately —
see docs/ORG-TIERS-DESIGN.md §3.2. A route that wants an authenticated caller
takes `PrincipalDep`; it never touches `request.cookies` or the
`Authorization` header itself.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from .models import Principal, Role
from .repository import ApiKeyRepository, SessionRepository
from .security import looks_like_api_key


async def get_principal(request: Request) -> Principal:
    settings = request.app.state.settings
    session_repo: SessionRepository = request.app.state.session_repo
    api_key_repo: ApiKeyRepository = request.app.state.api_key_repo

    session_id = request.cookies.get(settings.session_cookie_name)
    if session_id:
        resolved = await session_repo.resolve(session_id)
        if resolved is not None:
            user, org_path = resolved
            return Principal(
                id=user.id,
                subject=user.username,
                org_id=user.org_id,
                org_path=org_path,
                role=user.role,
                kind="session",
            )

    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if looks_like_api_key(token):
            resolved = await api_key_repo.resolve(token)
            if resolved is not None:
                key, org_path = resolved
                return Principal(
                    id=key.id,
                    subject=key.label,
                    org_id=key.org_id,
                    org_path=org_path,
                    role=key.role,
                    kind="api_key",
                )

    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require_admin(principal: PrincipalDep) -> Principal:
    """Route-level gate for admin-only actions (create user, issue API key,
    create sub-org). A plain 403 with no further detail — which of "wrong
    role" vs "wrong org" applied is not this service's business to reveal to
    a caller who failed the first check."""
    if principal.role != Role.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return principal


AdminDep = Annotated[Principal, Depends(require_admin)]


def require_purpose_code(
    x_purpose_code: str | None = Header(default=None, alias="X-Purpose-Code"),
) -> str:
    """Mandatory on every evidence-adjacent read — a route, a camera detail,
    an export (docs/ORG-TIERS-DESIGN.md §4.2 / DAY3-DESIGN.md §4.2). Absent
    is `400`, never a silently-assumed default: a purpose code that the
    caller did not actually choose is not a purpose code, it is decoration on
    an audit entry that looks accountable and is not. Camera *listing* and
    the map/gaps views do not take this dependency — browsing the estate is
    not evidence access."""
    if not x_purpose_code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-Purpose-Code header required")
    return x_purpose_code


PurposeCodeDep = Annotated[str, Depends(require_purpose_code)]
