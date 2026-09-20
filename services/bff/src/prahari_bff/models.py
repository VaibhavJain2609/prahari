"""API and domain models for identity.

Mirrors the shape fixed in `docs/ORG-TIERS-DESIGN.md §3` — one `Principal`
regardless of which of the two credential types resolved it, so every
downstream handler depends on the Principal and never on how it was obtained.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"
    """Held at an org node, applies to its whole subtree. The "global board"
    is simply `admin` at the root org — not a special case anywhere in the
    code, which is what keeps three boards one codebase."""


class ApiKeyPurpose(StrEnum):
    LOCAL_BODY_REGISTRATION = "local_body_registration"
    ONVIF_AGENT = "onvif_agent"
    VENDOR_ADAPTER = "vendor_adapter"
    INTERNAL_SERVICE = "internal_service"


class User(BaseModel):
    id: str
    username: str
    org_id: str
    role: Role
    created_at: datetime | None = None
    disabled_at: datetime | None = None


class UserCreate(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=8)
    org_id: str
    role: Role


class LoginRequest(BaseModel):
    username: str
    password: str


class Principal(BaseModel):
    """One shape, two credential types.

    `id` is the underlying user id for a session, or the api_key id for a key
    — kept distinct from `subject` (the human-readable username or key label)
    because `created_by` columns need a real id, not a display string.
    """

    id: str
    subject: str
    org_id: str
    org_path: str
    """The ltree path a scoped read/write uses directly as `scope`, with no
    extra lookup — the same string `CameraRepository` and `gaps.py` already
    take as a required keyword."""
    role: Role
    kind: Literal["session", "api_key"]


class ApiKeyCreate(BaseModel):
    org_id: str
    role: Role
    purpose: ApiKeyPurpose
    label: str = Field(min_length=1)


class ApiKey(BaseModel):
    """Never carries the plaintext or the hash — only `ApiKeyCreated`, once,
    at creation, does."""

    id: str
    org_id: str
    role: Role
    purpose: ApiKeyPurpose
    label: str
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyCreated(ApiKey):
    plaintext: str
    """Shown exactly once, in the response to the creating request. Not
    recoverable afterward — only the hash is stored."""


class PreviewTicketRequest(BaseModel):
    camera_id: str = Field(min_length=1)


class PreviewTicket(BaseModel):
    """The answer to `POST /api/v1/media/preview-ticket`: a scoped, expiring
    credential plus where to spend it. `ticket` is the bearer JWT the client
    presents to MediaMTX (`Authorization: Bearer`, or the `?jwt=` query
    parameter where a header cannot be set); `whep_url` is the restreamer's
    browser-reachable WHEP endpoint for this camera's path."""

    camera_id: str
    ticket: str
    whep_url: str
    expires_in: int
