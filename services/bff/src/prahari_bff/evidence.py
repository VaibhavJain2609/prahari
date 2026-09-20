"""Audited evidence-clip requests — the only sanctioned central pull of video.

Design doc: docs/EVIDENCE.md. The short version:

* `POST /api/v1/evidence/requests` — validates the window
  (`<= evidence_max_range_s`), resolves the camera's org root-scoped and
  enforces the caller's subtree, writes `evidence_requested` to the
  hash-chained audit log BEFORE the row exists, then inserts a
  `pending` `evidence_requests` row carrying a `dvr://<camera>/<start>-<end>`
  locator.
* `POST /api/v1/evidence/requests/{id}/ticket` — re-checks scope against the
  org recorded on the request, writes `evidence_issued`, then mints an
  Ed25519 MediaMTX ticket granting `read` + `playback` on `cam-<id>`. The
  playback grant exists before any recording does, so enabling `record` on
  the reconciled paths later changes nothing on this path.
* `GET /api/v1/evidence/requests/{id}` — scoped read of one request's state
  (`pending` | `issued` | `expired`, the last derived from the ticket's
  `exp`, never a stored transition).
* `GET /api/v1/evidence/requests` — admin-only scoped listing, audit-logged.

What this module does NOT do is move bytes: MediaMTX recording is off
(reconciled paths set no `record`), so clip fulfilment today is the
operator-side DVR pull the `evidence_ref` names. That is documented as the
honest v1 rather than faked.

Handlers live here, on an `APIRouter` that `app.py` includes in one block at
the end of the file; state comes off `request.app.state` (the same places
the lifespan puts it), so this module never imports `app.py`.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from .audit import AuditLog
from .auth import AdminDep, PrincipalDep, PurposeCodeDep
from .config import BFFSettings
from .media import MediaTicketIssuer
from .models import Principal
from .repository import in_scope
from .scope_resolver import CameraScopeResolver

log = logging.getLogger(__name__)

__all__ = ["router", "EvidenceRepository", "EvidenceRequest", "EvidenceRequestCreate"]

router = APIRouter(tags=["evidence"])

_ADMIN_PURPOSE = "admin"
"""Same value as `app._audit_access`'s `_ADMIN_PURPOSE` — admin/config
actions that are not themselves evidence reads. Duplicated rather than
imported because app.py imports THIS module (evidence.py must not import
app.py back)."""


# --- models -----------------------------------------------------------------


class EvidenceRequestCreate(BaseModel):
    """`POST /api/v1/evidence/requests` body. `purpose_code` rides in the
    body (not `X-Purpose-Code`) because it is part of the durable request
    record — stored on the row and mirrored into the audit entry — not just
    a per-call header."""

    camera_id: str = Field(min_length=1)
    start_ts: datetime
    end_ts: datetime
    purpose_code: str = Field(min_length=1)


class EvidenceRequest(BaseModel):
    """One stored request, as the API reports it. `status` is the DERIVED
    lifecycle — `issued` rows whose ticket has expired report `expired`; the
    column itself only ever holds `pending`/`issued` (see migration 008)."""

    id: str
    camera_id: str
    org_path: str
    requested_by: str
    purpose_code: str
    start_ts: datetime
    end_ts: datetime
    evidence_ref: str
    status: Literal["pending", "issued", "expired"]
    created_at: datetime
    issued_at: datetime | None = None
    ticket_expires_at: datetime | None = None


class EvidenceTicket(BaseModel):
    """The answer to `POST .../ticket`: the minted playback credential plus
    the locator the request recorded. `ticket` is a bearer JWT for MediaMTX
    (`Authorization: Bearer` or `?jwt=`); `expires_in` is its TTL in seconds.
    """

    request_id: str
    camera_id: str
    ticket: str
    evidence_ref: str
    expires_in: int


# --- storage ----------------------------------------------------------------
#
# `evidence_requests` is a BFF table in the registry-owned database — the
# same arrangement as the identity tables in 006_identity.sql: one Postgres,
# one checksummed migration runner, and the BFF reaching it through its own
# asyncpg pool rather than the registry's repository (which deliberately
# knows nothing about it).


def _from_row(row: asyncpg.Record) -> EvidenceRequest:
    stored_status = row["status"]
    expires_at = row["ticket_expires_at"]
    # `expired` is derived, not stored: nothing runs when a ticket ages out,
    # so the read is where the answer becomes honest.
    effective = (
        "expired"
        if stored_status == "issued" and expires_at is not None and expires_at <= datetime.now(UTC)
        else stored_status
    )
    return EvidenceRequest(
        id=str(row["id"]),
        camera_id=str(row["camera_id"]),
        org_path=str(row["org_path"]),
        requested_by=row["requested_by"],
        purpose_code=row["purpose_code"],
        start_ts=row["start_ts"],
        end_ts=row["end_ts"],
        evidence_ref=row["evidence_ref"],
        status=effective,
        created_at=row["created_at"],
        issued_at=row["issued_at"],
        ticket_expires_at=expires_at,
    )


class EvidenceRepository:
    """INSERT/SELECT/UPDATE against `evidence_requests` and nothing else."""

    _COLUMNS = (
        "id, camera_id, org_path::text AS org_path, requested_by, purpose_code, "
        "start_ts, end_ts, evidence_ref, status, ticket_jti, ticket_expires_at, "
        "created_at, issued_at"
    )

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(
        self,
        *,
        camera_id: str,
        org_path: str,
        requested_by: str,
        purpose_code: str,
        start_ts: datetime,
        end_ts: datetime,
        evidence_ref: str,
    ) -> EvidenceRequest:
        row = await self._pool.fetchrow(
            f"""
            INSERT INTO evidence_requests
                (camera_id, org_path, requested_by, purpose_code,
                 start_ts, end_ts, evidence_ref)
            VALUES ($1::uuid, $2::ltree, $3, $4, $5, $6, $7)
            RETURNING {self._COLUMNS}
            """,
            camera_id,
            org_path,
            requested_by,
            purpose_code,
            start_ts,
            end_ts,
            evidence_ref,
        )
        return _from_row(row)

    async def get(self, request_id: str) -> EvidenceRequest | None:
        try:
            row = await self._pool.fetchrow(
                f"SELECT {self._COLUMNS} FROM evidence_requests WHERE id = $1::uuid",
                request_id,
            )
        except (ValueError, asyncpg.DataError):
            # A non-uuid id is "no such request", not a 500 — same discipline
            # as the identity repos' `get`.
            return None
        return _from_row(row) if row else None

    async def list(self, scope: str, *, limit: int, offset: int) -> list[EvidenceRequest]:
        """Every request whose camera org sat inside `scope`'s subtree AT
        REQUEST TIME — `org_path` is the denormalised request-time scope, so
        a later camera reassignment cannot hide the request from the admin
        whose subtree was asked for the footage."""
        rows = await self._pool.fetch(
            f"""
            SELECT {self._COLUMNS} FROM evidence_requests
            WHERE org_path <@ $1::ltree
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            scope,
            limit,
            offset,
        )
        return [_from_row(row) for row in rows]

    async def mark_issued(
        self, request_id: str, *, ticket_jti: str, ticket_expires_at: datetime
    ) -> EvidenceRequest | None:
        """pending → issued. `issued_at` keeps the FIRST issuance
        (`COALESCE`) — it is forensic, "when did footage access first go
        out"; `ticket_jti`/`ticket_expires_at` track the LATEST grant, since
        they exist to answer "is this presented ticket one we minted"."""
        try:
            row = await self._pool.fetchrow(
                f"""
                UPDATE evidence_requests
                SET status = 'issued',
                    issued_at = COALESCE(issued_at, now()),
                    ticket_jti = $2,
                    ticket_expires_at = $3
                WHERE id = $1::uuid
                RETURNING {self._COLUMNS}
                """,
                request_id,
                ticket_jti,
                ticket_expires_at,
            )
        except (ValueError, asyncpg.DataError):
            return None
        return _from_row(row) if row else None


# --- internals ----------------------------------------------------------------


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _ticket_claims(ticket: str) -> dict:
    """Decode (not verify) a ticket this process just minted — the `jti` and
    `exp` go onto the request row so "which credential did we issue" is a
    question the database can answer without storing the bearer token."""
    return json.loads(_b64url_decode(ticket.split(".")[1]))


def _evidence_ref(camera_id: str, start_ts: datetime, end_ts: datetime) -> str:
    """The edge-side clip locator the request records. Epoch seconds, not
    ISO: the consumer is an operator/DVR workflow, and an unambiguous pair
    of integers survives being read back off a screen."""
    return f"dvr://{camera_id}/{int(start_ts.timestamp())}-{int(end_ts.timestamp())}"


async def _audit(
    audit: AuditLog,
    principal: Principal,
    *,
    purpose_code: str,
    resource: str,
    action: str,
) -> None:
    """Fail-closed audit append — the same contract as `app._audit_access`,
    duplicated because app.py imports this module. A response served after a
    failed append is an unaudited access, so a raise here is a 500, never a
    quietly unlogged 200."""
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


def _require_tz_aware(payload: EvidenceRequestCreate) -> tuple[datetime, datetime]:
    """Reject naive datetimes: an evidence window with no timezone is an
    ambiguous claim about when footage exists, and ambiguity is the one
    thing an audit trail cannot afford to store."""
    start, end = payload.start_ts, payload.end_ts
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "start_ts and end_ts must carry a timezone"
        )
    return start, end


# --- handlers -----------------------------------------------------------------


@router.post(
    "/api/v1/evidence/requests",
    response_model=EvidenceRequest,
    status_code=status.HTTP_201_CREATED,
)
async def create_evidence_request(
    payload: EvidenceRequestCreate,
    principal: PrincipalDep,
    request: Request,
) -> EvidenceRequest:
    """Register an audited clip-retrieval request for a camera window.

    Ordering is the invariant: validate the window, resolve scope (denials
    audited as `evidence_request_denied`), append `evidence_requested`
    BEFORE the row exists — a stored request with no audit row is the
    failure this ordering exists to prevent — then insert `pending`."""
    settings: BFFSettings = request.app.state.settings
    audit: AuditLog = request.app.state.audit
    resolver: CameraScopeResolver = request.app.state.scope_resolver
    repo = EvidenceRepository(request.app.state.pool)

    start, end = _require_tz_aware(payload)
    if end <= start:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "end_ts must be after start_ts")
    if (end - start).total_seconds() > settings.evidence_max_range_s:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"requested window exceeds the {settings.evidence_max_range_s}s maximum",
        )

    # Same scope contract as the camera-detail read and preview ticket:
    # resolved root-scoped so that out-of-scope is 403-and-audited rather
    # than the 404 of "does not exist".
    org_path = await resolver.org_path_for_camera(payload.camera_id)
    if org_path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no camera {payload.camera_id}")
    if not in_scope(org_path, principal.org_path):
        await _audit(
            audit,
            principal,
            purpose_code=payload.purpose_code,
            resource=f"camera:{payload.camera_id}",
            action="evidence_request_denied",
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "camera is outside your org subtree")

    await _audit(
        audit,
        principal,
        purpose_code=payload.purpose_code,
        resource=f"camera:{payload.camera_id}",
        action="evidence_requested",
    )
    return await repo.create(
        camera_id=payload.camera_id,
        org_path=org_path,
        requested_by=principal.subject,
        purpose_code=payload.purpose_code,
        start_ts=start,
        end_ts=end,
        evidence_ref=_evidence_ref(payload.camera_id, start, end),
    )


@router.get("/api/v1/evidence/requests/{request_id}", response_model=EvidenceRequest)
async def get_evidence_request(
    request_id: str,
    principal: PrincipalDep,
    purpose_code: PurposeCodeDep,
    request: Request,
) -> EvidenceRequest:
    """One request's state. Scoped to the org recorded on the row and
    purpose-coded like every evidence-adjacent read — `evidence_read` on
    success, `evidence_read_denied` across the subtree boundary."""
    audit: AuditLog = request.app.state.audit
    repo = EvidenceRepository(request.app.state.pool)

    evidence = await repo.get(request_id)
    if evidence is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no evidence request {request_id}")
    if not in_scope(evidence.org_path, principal.org_path):
        await _audit(
            audit,
            principal,
            purpose_code=purpose_code,
            resource=f"evidence:{request_id}",
            action="evidence_read_denied",
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "request is outside your org subtree")
    await _audit(
        audit,
        principal,
        purpose_code=purpose_code,
        resource=f"evidence:{request_id}",
        action="evidence_read",
    )
    return evidence


@router.post("/api/v1/evidence/requests/{request_id}/ticket", response_model=EvidenceTicket)
async def mint_evidence_ticket(
    request_id: str,
    principal: PrincipalDep,
    purpose_code: PurposeCodeDep,
    request: Request,
) -> EvidenceTicket:
    """Mint the playback credential a stored request authorises.

    The ticket carries `read` + `playback` on `cam-<id>` — read because the
    same path is the live preview, playback because that is the MediaMTX
    action recordings will be served under once `record: yes` lands. Until
    then the grant is honest dead letter: MediaMTX has nothing to play back
    and the clip itself still arrives via the edge-side process the
    `evidence_ref` names (docs/EVIDENCE.md).

    `evidence_issued` is appended BEFORE the credential exists — fail-closed,
    same ordering as preview-ticket minting. Re-minting is allowed (every
    mint is its own audit row and `ticket_jti` tracks the latest grant).
    """
    settings: BFFSettings = request.app.state.settings
    audit: AuditLog = request.app.state.audit
    issuer: MediaTicketIssuer = request.app.state.media_issuer
    repo = EvidenceRepository(request.app.state.pool)

    evidence = await repo.get(request_id)
    if evidence is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no evidence request {request_id}")
    if not in_scope(evidence.org_path, principal.org_path):
        await _audit(
            audit,
            principal,
            purpose_code=purpose_code,
            resource=f"evidence:{request_id}",
            action="evidence_issue_denied",
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "request is outside your org subtree")

    await _audit(
        audit,
        principal,
        purpose_code=purpose_code,
        resource=f"evidence:{request_id}",
        action="evidence_issued",
    )
    ticket = issuer.mint(
        subject=principal.subject,
        camera_id=evidence.camera_id,
        ttl_s=settings.evidence_ticket_ttl_s,
        actions=("read", "playback"),
    )
    claims = _ticket_claims(ticket)
    await repo.mark_issued(
        request_id,
        ticket_jti=claims["jti"],
        ticket_expires_at=datetime.fromtimestamp(claims["exp"], UTC),
    )
    return EvidenceTicket(
        request_id=evidence.id,
        camera_id=evidence.camera_id,
        ticket=ticket,
        evidence_ref=evidence.evidence_ref,
        expires_in=settings.evidence_ticket_ttl_s,
    )


@router.get("/api/v1/evidence/requests", response_model=list[EvidenceRequest])
async def list_evidence_requests(
    principal: AdminDep,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[EvidenceRequest]:
    """The admin's scoped view over every request in their subtree.

    Audit-logged (`evidence_list`) like the reads it summarises — a listing
    of "who asked for whose footage" is itself evidence-adjacent, so it
    cannot be the one unrecorded access on this path."""
    audit: AuditLog = request.app.state.audit
    repo = EvidenceRepository(request.app.state.pool)

    await _audit(
        audit,
        principal,
        purpose_code=_ADMIN_PURPOSE,
        resource="evidence:requests",
        action="evidence_list",
    )
    return await repo.list(principal.org_path, limit=limit, offset=offset)
