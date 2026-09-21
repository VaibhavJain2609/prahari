"""The audited evidence-clip pull path: `/api/v1/evidence/requests*`.

Handlers are called directly against fakes (same style as test_media.py /
test_audit_ordering.py) — a dict-rowed pool stands in for asyncpg, since the
property under test is ordering and scope, not SQL. The one TestClient pass
at the bottom proves the router is wired onto the app.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException
from fastapi.testclient import TestClient

from prahari_bff.app import app
from prahari_bff.config import BFFSettings
from prahari_bff.evidence import (
    EvidenceRequestCreate,
    create_evidence_request,
    get_evidence_request,
    list_evidence_requests,
    mint_evidence_ticket,
)
from prahari_bff.media import MediaTicketIssuer
from prahari_bff.models import Principal, Role, User
from prahari_bff.repository import in_scope

OPERATOR = Principal(
    id="u1",
    subject="ops.zone4",
    org_id="org-zone4",
    org_path="gj.ahmedabad_city.zone_4",
    role=Role.OPERATOR,
    kind="session",
)
ADMIN = OPERATOR.model_copy(update={"role": Role.ADMIN, "subject": "admin.zone4"})

NOW = datetime.now(UTC)
START = NOW - timedelta(minutes=10)
END = NOW - timedelta(minutes=2)


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _decode_ticket(issuer: MediaTicketIssuer, ticket: str) -> dict:
    """Verify + decode exactly as the registry's TicketVerifier would:
    resolve `kid` from the public JWKS, check the Ed25519 signature, read
    the claims."""
    header_b64, payload_b64, sig_b64 = ticket.split(".")
    header = json.loads(_b64url_decode(header_b64))
    jwk = next(k for k in issuer.jwks()["keys"] if k["kid"] == header["kid"])
    key = Ed25519PublicKey.from_public_bytes(_b64url_decode(jwk["x"]))
    key.verify(_b64url_decode(sig_b64), f"{header_b64}.{payload_b64}".encode())
    return json.loads(_b64url_decode(payload_b64))


class FakeAudit:
    def __init__(self, fail_actions: set[str] | None = None) -> None:
        self.entries: list[dict] = []
        self._fail = fail_actions or set()

    async def append(self, **kwargs):
        if kwargs["action"] in self._fail:
            raise RuntimeError("audit store unavailable")
        self.entries.append(kwargs)
        return kwargs

    def actions(self) -> list[str]:
        return [e["action"] for e in self.entries]


class FakeScopeResolver:
    def __init__(self, camera_orgs: dict[str, str | None]) -> None:
        self._camera_orgs = camera_orgs

    async def org_path_for_camera(self, camera_id: str) -> str | None:
        return self._camera_orgs.get(camera_id)


class FakeEvidencePool:
    """In-memory `evidence_requests` behind the fetchrow/fetch surface the
    repository uses. `on_insert` lets a test observe audit state at the
    moment the row lands — the audit-before-insert assertion."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.on_insert = None

    def _new_row(self, args) -> dict:
        camera_id, org_path, requested_by, purpose_code, start_ts, end_ts, evidence_ref = args
        row = {
            "id": str(uuid.uuid4()),
            "camera_id": camera_id,
            "org_path": org_path,
            "requested_by": requested_by,
            "purpose_code": purpose_code,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "evidence_ref": evidence_ref,
            "status": "pending",
            "ticket_jti": None,
            "ticket_expires_at": None,
            "created_at": datetime.now(UTC),
            "issued_at": None,
        }
        self.rows[row["id"]] = row
        return row

    async def fetchrow(self, query: str, *args):
        if "INSERT INTO evidence_requests" in query:
            if self.on_insert:
                self.on_insert()
            return self._new_row(args)
        if "UPDATE evidence_requests" in query:
            row = self.rows.get(args[0])
            if row is not None:
                row["status"] = "issued"
                row["issued_at"] = row["issued_at"] or datetime.now(UTC)
                row["ticket_jti"] = args[1]
                row["ticket_expires_at"] = args[2]
            return row
        # SELECT ... WHERE id = $1 — a non-uuid id is "no such row".
        try:
            uuid.UUID(str(args[0]))
        except ValueError:
            return None
        return self.rows.get(str(args[0]))

    async def fetch(self, query: str, *args):
        scope, limit, offset = args
        rows = [r for r in self.rows.values() if in_scope(r["org_path"], scope)]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows[offset : offset + limit]


def _request(
    *,
    pool: FakeEvidencePool | None = None,
    audit: FakeAudit | None = None,
    camera_orgs: dict[str, str | None] | None = None,
    issuer: MediaTicketIssuer | None = None,
    settings: BFFSettings | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=settings or BFFSettings(),
                audit=audit or FakeAudit(),
                scope_resolver=FakeScopeResolver(camera_orgs or {}),
                pool=pool or FakeEvidencePool(),
                media_issuer=issuer or MediaTicketIssuer(BFFSettings()),
            )
        )
    )


def _payload(camera_id: str = "cam-7", **overrides) -> EvidenceRequestCreate:
    return EvidenceRequestCreate(
        camera_id=camera_id,
        start_ts=overrides.get("start_ts", START),
        end_ts=overrides.get("end_ts", END),
        purpose_code=overrides.get("purpose_code", "incident-441"),
    )


# --- create --------------------------------------------------------------------


async def test_create_stores_a_pending_request_with_dvr_ref():
    pool, audit = FakeEvidencePool(), FakeAudit()
    result = await create_evidence_request(
        _payload(),
        OPERATOR,
        _request(
            pool=pool,
            audit=audit,
            camera_orgs={"cam-7": "gj.ahmedabad_city.zone_4.ward_9"},
        ),
    )

    assert result.status == "pending"
    assert result.camera_id == "cam-7"
    assert result.org_path == "gj.ahmedabad_city.zone_4.ward_9"
    assert result.requested_by == "ops.zone4"
    assert result.evidence_ref == (f"dvr://cam-7/{int(START.timestamp())}-{int(END.timestamp())}")
    assert audit.actions() == ["evidence_requested"]
    assert audit.entries[0]["purpose_code"] == "incident-441"


async def test_audit_row_precedes_the_insert():
    """The append must be durable BEFORE the row exists — a stored request
    with no audit row is the failure this ordering exists to prevent."""
    audit = FakeAudit()
    pool = FakeEvidencePool()
    seen: list[list[str]] = []
    pool.on_insert = lambda: seen.append(audit.actions())

    await create_evidence_request(
        _payload(),
        OPERATOR,
        _request(pool=pool, audit=audit, camera_orgs={"cam-7": "gj.ahmedabad_city.zone_4"}),
    )
    assert seen == [["evidence_requested"]]


async def test_failed_audit_append_means_no_row_is_stored():
    audit = FakeAudit(fail_actions={"evidence_requested"})
    pool = FakeEvidencePool()
    with pytest.raises(HTTPException) as exc:
        await create_evidence_request(
            _payload(),
            OPERATOR,
            _request(pool=pool, audit=audit, camera_orgs={"cam-7": "gj.ahmedabad_city.zone_4"}),
        )
    assert exc.value.status_code == 500
    assert pool.rows == {}


async def test_out_of_scope_request_is_403_and_audited_denied():
    audit = FakeAudit()
    pool = FakeEvidencePool()
    with pytest.raises(HTTPException) as exc:
        await create_evidence_request(
            _payload("cam-8"),
            OPERATOR,
            _request(pool=pool, audit=audit, camera_orgs={"cam-8": "gj.surat_city"}),
        )
    assert exc.value.status_code == 403
    assert audit.actions() == ["evidence_request_denied"]
    assert pool.rows == {}


async def test_unknown_camera_is_404_with_no_audit_write():
    audit = FakeAudit()
    with pytest.raises(HTTPException) as exc:
        await create_evidence_request(
            _payload("ghost"),
            OPERATOR,
            _request(audit=audit, camera_orgs={}),
        )
    assert exc.value.status_code == 404
    assert audit.entries == []


async def test_window_beyond_the_configured_max_is_rejected():
    pool, audit = FakeEvidencePool(), FakeAudit()
    with pytest.raises(HTTPException) as exc:
        await create_evidence_request(
            _payload(start_ts=START, end_ts=START + timedelta(minutes=16)),
            OPERATOR,
            _request(pool=pool, audit=audit, camera_orgs={"cam-7": "gj.ahmedabad_city.zone_4"}),
        )
    assert exc.value.status_code == 400
    assert pool.rows == {}
    assert audit.entries == []


async def test_reversed_or_naive_windows_are_rejected():
    request = _request(camera_orgs={"cam-7": "gj.ahmedabad_city.zone_4"})
    with pytest.raises(HTTPException):
        await create_evidence_request(_payload(start_ts=END, end_ts=START), OPERATOR, request)
    naive = START.replace(tzinfo=None)
    with pytest.raises(HTTPException):
        await create_evidence_request(_payload(start_ts=naive, end_ts=END), OPERATOR, request)


# --- ticket mint -----------------------------------------------------------------


async def _pending_request(pool: FakeEvidencePool, camera_id: str = "cam-7") -> str:
    row = pool._new_row(
        (
            camera_id,
            "gj.ahmedabad_city.zone_4",
            "ops.zone4",
            "incident-441",
            START,
            END,
            "dvr://cam-7/1-2",
        )
    )
    return row["id"]


async def test_ticket_carries_read_and_playback_on_the_camera_path():
    pool, audit = FakeEvidencePool(), FakeAudit()
    issuer = MediaTicketIssuer(BFFSettings())
    request_id = await _pending_request(pool)

    result = await mint_evidence_ticket(
        request_id,
        OPERATOR,
        "incident-441",
        _request(
            pool=pool,
            audit=audit,
            issuer=issuer,
            camera_orgs={"cam-7": "gj.ahmedabad_city.zone_4"},
        ),
    )

    payload = _decode_ticket(issuer, result.ticket)
    assert payload["mediamtx_permissions"] == [
        {"action": "read", "path": "cam-cam-7"},
        {"action": "playback", "path": "cam-cam-7"},
    ]
    # Expiry is bounded by the configured TTL, not open-ended.
    assert payload["exp"] - payload["iat"] == BFFSettings().evidence_ticket_ttl_s
    assert result.expires_in == BFFSettings().evidence_ticket_ttl_s
    assert result.evidence_ref == "dvr://cam-7/1-2"

    # Second audit row: the request row exists AND the issuance is recorded
    # — and the row now reflects the issued state and the ticket's identity.
    assert audit.actions() == ["evidence_issued"]
    stored = pool.rows[request_id]
    assert stored["status"] == "issued"
    assert stored["ticket_jti"] == payload["jti"]
    assert stored["ticket_expires_at"].timestamp() == pytest.approx(payload["exp"])


async def test_ticket_mint_out_of_scope_is_denied_and_audited():
    pool, audit = FakeEvidencePool(), FakeAudit()
    request_id = await _pending_request(pool)
    outsider = OPERATOR.model_copy(update={"org_path": "gj.surat_city"})

    with pytest.raises(HTTPException) as exc:
        await mint_evidence_ticket(
            request_id,
            outsider,
            "incident-441",
            _request(
                pool=pool,
                audit=audit,
                camera_orgs={"cam-7": "gj.ahmedabad_city.zone_4"},
            ),
        )
    assert exc.value.status_code == 403
    assert audit.actions() == ["evidence_issue_denied"]
    assert pool.rows[request_id]["status"] == "pending"


async def test_ticket_mint_checks_the_cameras_live_org_not_the_stored_one():
    """The security finding: scope frozen at request time. The request was
    made while cam-7 sat inside zone_4 (the stored org_path proves it) — the
    camera has since been reassigned to Surat, so the same caller must NOT
    keep minting tickets. Live re-resolution turns this into a denial."""
    pool, audit = FakeEvidencePool(), FakeAudit()
    request_id = await _pending_request(pool)
    assert pool.rows[request_id]["org_path"] == "gj.ahmedabad_city.zone_4"  # frozen at request

    with pytest.raises(HTTPException) as exc:
        await mint_evidence_ticket(
            request_id,
            OPERATOR,  # still scoped to zone_4 — the camera no longer is
            "incident-441",
            _request(pool=pool, audit=audit, camera_orgs={"cam-7": "gj.surat_city"}),
        )
    assert exc.value.status_code == 403
    assert audit.actions() == ["evidence_issue_denied"]
    assert pool.rows[request_id]["status"] == "pending"


async def test_ticket_mint_for_an_unresolvable_camera_fails_closed():
    """A camera the scope resolver can no longer place is denied, not
    defaulted-in: the ticket is a bearer credential and 'cannot confirm
    scope' is not 'in scope'."""
    pool, audit = FakeEvidencePool(), FakeAudit()
    request_id = await _pending_request(pool)

    with pytest.raises(HTTPException) as exc:
        await mint_evidence_ticket(
            request_id,
            OPERATOR,
            "incident-441",
            _request(pool=pool, audit=audit, camera_orgs={}),  # resolver miss
        )
    assert exc.value.status_code == 403
    assert audit.actions() == ["evidence_issue_denied"]
    assert pool.rows[request_id]["status"] == "pending"


async def test_ticket_mint_for_unknown_request_is_404():
    audit = FakeAudit()
    with pytest.raises(HTTPException) as exc:
        await mint_evidence_ticket(
            str(uuid.uuid4()),
            OPERATOR,
            "incident-441",
            _request(pool=FakeEvidencePool(), audit=audit),
        )
    assert exc.value.status_code == 404
    assert audit.entries == []


# --- read + lifecycle --------------------------------------------------------------


async def test_get_returns_state_and_audits_the_read():
    pool, audit = FakeEvidencePool(), FakeAudit()
    request_id = await _pending_request(pool)

    result = await get_evidence_request(
        request_id,
        OPERATOR,
        "incident-441",
        _request(pool=pool, audit=audit, camera_orgs={"cam-7": "gj.ahmedabad_city.zone_4"}),
    )
    assert result.status == "pending"
    assert audit.actions() == ["evidence_read"]


async def test_get_reports_expired_once_the_ticket_ages_out():
    pool, audit = FakeEvidencePool(), FakeAudit()
    request_id = await _pending_request(pool)
    row = pool.rows[request_id]
    row["status"] = "issued"
    row["issued_at"] = NOW - timedelta(minutes=10)
    row["ticket_expires_at"] = NOW - timedelta(minutes=5)

    result = await get_evidence_request(
        request_id,
        OPERATOR,
        "incident-441",
        _request(pool=pool, audit=audit, camera_orgs={"cam-7": "gj.ahmedabad_city.zone_4"}),
    )
    assert result.status == "expired"  # derived, never a stored transition


async def test_get_out_of_scope_is_denied_and_audited():
    pool, audit = FakeEvidencePool(), FakeAudit()
    request_id = await _pending_request(pool)
    outsider = OPERATOR.model_copy(update={"org_path": "gj.surat_city"})

    with pytest.raises(HTTPException) as exc:
        await get_evidence_request(
            request_id,
            outsider,
            "incident-441",
            _request(pool=pool, audit=audit, camera_orgs={"cam-7": "gj.ahmedabad_city.zone_4"}),
        )
    assert exc.value.status_code == 403
    assert audit.actions() == ["evidence_read_denied"]


async def test_get_follows_the_cameras_live_org_after_a_reassignment():
    """Same live-scope rule as mint: the request was stored while cam-7 was
    in zone_4, but its read now resolves the camera's CURRENT org — a
    reassignment away from the caller's subtree closes the read too."""
    pool, audit = FakeEvidencePool(), FakeAudit()
    request_id = await _pending_request(pool)

    with pytest.raises(HTTPException) as exc:
        await get_evidence_request(
            request_id,
            OPERATOR,
            "incident-441",
            _request(pool=pool, audit=audit, camera_orgs={"cam-7": "gj.surat_city"}),
        )
    assert exc.value.status_code == 403
    assert audit.actions() == ["evidence_read_denied"]


# --- admin list -------------------------------------------------------------------


async def test_admin_list_is_scoped_and_audited():
    pool, audit = FakeEvidencePool(), FakeAudit()
    await _pending_request(pool, "cam-7")
    out_of_scope = pool._new_row(
        ("cam-9", "gj.surat_city", "ops.surat", "x", START, END, "dvr://cam-9/1-2")
    )
    assert out_of_scope["org_path"] == "gj.surat_city"

    results = await list_evidence_requests(
        ADMIN, _request(pool=pool, audit=audit), limit=100, offset=0
    )
    assert [r.camera_id for r in results] == ["cam-7"]
    assert audit.actions() == ["evidence_list"]
    assert audit.entries[0]["purpose_code"] == "admin"


# --- wired through the app ----------------------------------------------------------


def _client() -> TestClient:
    user = User(id="u1", username="ops.zone4", org_id="org-zone4", role=Role.OPERATOR)
    app.state.settings = BFFSettings()
    app.state.media_issuer = MediaTicketIssuer(BFFSettings())
    app.state.scope_resolver = FakeScopeResolver({"cam-7": "gj.ahmedabad_city.zone_4"})
    app.state.audit = FakeAudit()
    app.state.pool = FakeEvidencePool()

    class _Sessions:
        async def resolve(self, session_id):
            return (user, "gj.ahmedabad_city.zone_4") if session_id == "sess-1" else None

    class _Keys:
        async def resolve(self, token):
            return None

    app.state.session_repo = _Sessions()
    app.state.api_key_repo = _Keys()
    return TestClient(app)


def test_evidence_request_end_to_end_over_http():
    client = _client()
    client.cookies.set("prahari_session", "sess-1")
    resp = client.post(
        "/api/v1/evidence/requests",
        json={
            "camera_id": "cam-7",
            "start_ts": START.isoformat(),
            "end_ts": END.isoformat(),
            "purpose_code": "incident-441",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert body["evidence_ref"].startswith("dvr://cam-7/")

    ticket = client.post(
        f"/api/v1/evidence/requests/{body['id']}/ticket",
        headers={"X-Purpose-Code": "incident-441"},
    )
    assert ticket.status_code == 200
    payload = _decode_ticket(app.state.media_issuer, ticket.json()["ticket"])
    assert {p["action"] for p in payload["mediamtx_permissions"]} == {"read", "playback"}


def test_evidence_request_requires_authentication():
    resp = _client().post(
        "/api/v1/evidence/requests",
        json={
            "camera_id": "cam-7",
            "start_ts": START.isoformat(),
            "end_ts": END.isoformat(),
            "purpose_code": "incident-441",
        },
    )
    assert resp.status_code == 401


def test_evidence_list_requires_admin():
    client = _client()  # session resolves to an OPERATOR
    client.cookies.set("prahari_session", "sess-1")
    resp = client.get("/api/v1/evidence/requests")
    assert resp.status_code == 403
