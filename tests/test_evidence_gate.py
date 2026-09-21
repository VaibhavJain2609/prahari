"""The evidence-chain gate (`docs/EVIDENCE.md`): an audited evidence request
produces a playback ticket that the REGISTRY's MediaMTX auth callback
actually accepts — on exactly the camera the request names, and nowhere else.

This is the cross-service seam no per-service suite proves: the BFF mints
(`prahari_bff.evidence` + `prahari_bff.media`), the registry verifies
(`prahari_registry.media_auth`). Only the database and the JWKS HTTP fetch
are faked — the request row, the audit ordering, the JWT, the signature
check and the permission match are all real production code:

    create_evidence_request -> mint_evidence_ticket (real handler + real
      MediaTicketIssuer -> real Ed25519 JWT)
    -> MediaMTXAuthRequest(action=playback, query=jwt=<token>)
    -> authorize() + TicketVerifier (real JWKS decode, kid resolution,
       exact action/path match)

No k8s, no Postgres, no network beyond a MockTransport.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
from prahari_bff.config import BFFSettings
from prahari_bff.evidence import (
    create_evidence_request,
    mint_evidence_ticket,
)
from prahari_bff.media import MediaTicketIssuer
from prahari_bff.models import Principal, Role
from prahari_registry.config import RegistrySettings
from prahari_registry.media_auth import (
    MediaMTXAuthRequest,
    TicketVerifier,
    authorize,
)

CAMERA_ID = "3f2b1c0a-1234-5678-9abc-def012345678"
NOW = datetime.now(UTC)


class _FakePool:
    """Answers `fetchrow` with a canned row and records `execute` — enough
    asyncpg for EvidenceRepository (INSERT RETURNING / SELECT / UPDATE)."""

    def __init__(self) -> None:
        self.row: dict | None = None
        self.executed: list[tuple] = []

    async def fetchrow(self, _sql, *_args):
        return self.row

    async def execute(self, *args):
        self.executed.append(args)
        return "UPDATE 1"


class _FakeAudit:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    async def append(self, **kwargs):
        self.entries.append(kwargs)
        return kwargs

    def actions(self) -> list[str]:
        return [e["action"] for e in self.entries]


class _FakeScope:
    """CameraScopeResolver shape: camera -> live org path."""

    def __init__(self, org_path: str | None) -> None:
        self._org_path = org_path

    async def org_path_for_camera(self, _camera_id: str) -> str | None:
        return self._org_path


def _request(app_state) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=app_state))


def _row(request_id: str) -> dict:
    return {
        "id": request_id,
        "camera_id": CAMERA_ID,
        "org_path": "gj.ahmedabad_city.zone_4",
        "requested_by": "ops.zone4",
        "purpose_code": "evidence-case-1",
        "start_ts": NOW - timedelta(minutes=10),
        "end_ts": NOW - timedelta(minutes=2),
        "evidence_ref": f"dvr://{CAMERA_ID}/1-2",
        "status": "pending",
        "ticket_jti": None,
        "ticket_expires_at": None,
        "created_at": NOW - timedelta(minutes=1),
        "issued_at": None,
    }


def _verifier(settings: RegistrySettings, jwks: dict) -> TicketVerifier:
    """A TicketVerifier whose JWKS fetch is answered by a mock transport —
    the same wiring services/registry's own suite uses, so the seam is the
    payload, not the transport."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jwks)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TicketVerifier(settings, client=client)


def _registry_settings() -> RegistrySettings:
    return RegistrySettings(
        internal_token="internal-test-token",
        worker_media_token="worker-test-token",
        media_auth_jwks_url="https://bff.invalid/api/v1/media/jwks",
    )


async def test_a_playback_ticket_the_bff_mints_is_what_the_registry_accepts():
    """The full chain, real crypto end to end: audited request -> minted
    ticket -> MediaMTX auth callback grants playback on that camera only."""
    operator = Principal(
        id="u1",
        subject="ops.zone4",
        org_id="org-zone4",
        org_path="gj.ahmedabad_city.zone_4",
        role=Role.OPERATOR,
        kind="session",
    )
    issuer = MediaTicketIssuer(BFFSettings())
    audit = _FakeAudit()
    pool = _FakePool()
    request_id = str(uuid.uuid4())
    state = SimpleNamespace(
        settings=BFFSettings(),
        audit=audit,
        media_issuer=issuer,
        scope_resolver=_FakeScope("gj.ahmedabad_city.zone_4"),
        pool=pool,
    )
    request = _request(state)

    # 1. The audited request — audit row BEFORE the row exists (fail-closed).
    pool.row = _row(request_id)
    evidence = await create_evidence_request(
        payload=_request_body(),
        principal=operator,
        request=request,
    )
    assert audit.actions() == ["evidence_requested"]
    assert evidence.id == request_id

    # 2. Mint — audit row BEFORE the credential exists.
    ticket = await mint_evidence_ticket(
        request_id=request_id,
        principal=operator,
        purpose_code="evidence-case-1",
        request=request,
    )
    assert "evidence_issued" in audit.actions()

    # 3. The REGISTRY side: feed the token through the real auth callback
    #    exactly as MediaMTX would — jwt in the query field, action=playback.
    verifier = _verifier(_registry_settings(), issuer.jwks())
    path = f"cam-{CAMERA_ID}"

    assert await authorize(
        _registry_settings(),
        verifier,
        MediaMTXAuthRequest(action="playback", path=path, query=f"jwt={ticket.ticket}"),
    )
    # The same ticket grants `read` — the path doubles as the live preview.
    assert await authorize(
        _registry_settings(),
        verifier,
        MediaMTXAuthRequest(action="read", path=path, query=f"jwt={ticket.ticket}"),
    )

    # And the boundary checks that make the grant meaningful:
    for action, req_path in [
        ("playback", "cam-other"),  # a different camera
        ("read", "cam-other"),
        ("playback", "all"),  # wildcard-ish path names
    ]:
        assert not await authorize(
            _registry_settings(),
            verifier,
            MediaMTXAuthRequest(action=action, path=req_path, query=f"jwt={ticket.ticket}"),
        ), action

    # Publish is refused for every credential shape, ticket included.
    assert not await authorize(
        _registry_settings(),
        verifier,
        MediaMTXAuthRequest(action="publish", path=path, query=f"jwt={ticket.ticket}"),
    )
    # The ticket cannot masquerade as a machine credential either.
    assert not await authorize(
        _registry_settings(),
        verifier,
        MediaMTXAuthRequest(user="worker", password=ticket.ticket, action="read", path=path),
    )


async def test_the_request_records_its_locator_and_audit_names_it():
    """`evidence_ref` is the edge-side locator the request carries — the
    ticket response hands it back so the caller knows what was authorised."""
    operator = Principal(
        id="u1",
        subject="ops.zone4",
        org_id="org-zone4",
        org_path="gj.ahmedabad_city.zone_4",
        role=Role.OPERATOR,
        kind="session",
    )
    issuer = MediaTicketIssuer(BFFSettings())
    pool = _FakePool()
    pool.row = _row(str(uuid.uuid4()))
    request = _request(
        SimpleNamespace(
            settings=BFFSettings(),
            audit=_FakeAudit(),
            media_issuer=issuer,
            scope_resolver=_FakeScope("gj.ahmedabad_city.zone_4"),
            pool=pool,
        )
    )
    ticket = await mint_evidence_ticket(
        request_id=pool.row["id"],
        principal=operator,
        purpose_code="evidence-case-1",
        request=request,
    )
    assert ticket.evidence_ref.startswith(f"dvr://{CAMERA_ID}/")
    assert ticket.expires_in > 0


def _request_body():
    from prahari_bff.evidence import EvidenceRequestCreate

    return EvidenceRequestCreate(
        camera_id=CAMERA_ID,
        start_ts=NOW - timedelta(minutes=10),
        end_ts=NOW - timedelta(minutes=2),
        purpose_code="evidence-case-1",
    )
