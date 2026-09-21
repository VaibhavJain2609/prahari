"""Audit-before-commit ordering for mutations.

The security-review finding this covers: every mutating endpoint used to run
the upstream/registry/repo write *first* and append the audit row only after
it returned — so a failed append left a committed change with no record,
exactly backwards for an accountability log. Every mutation now lands
`<action>_requested` before the write (fail-closed: a failed intent append
aborts before anything happens), then `<action>` or `<action>_failed` once
the write settles.

Same style as test_admin_endpoints.py: handlers called directly against
fakes, no database — except the chain-integrity check, which uses a real
`AuditLog` on tmp_path because `verify()` is the property that matters.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from prahari_bff.app import (
    create_camera,
    create_user,
    decommission_camera,
    disable_user,
    import_cameras,
    trigger_sync,
)
from prahari_bff.audit import AuditLog
from prahari_bff.models import Principal, Role, User, UserCreate

OPERATOR = Principal(
    id="u1",
    subject="ops.zone4",
    org_id="org-zone4",
    org_path="gj.ahmedabad_city.zone_4",
    role=Role.OPERATOR,
    kind="session",
)
ADMIN = Principal(
    id="u2",
    subject="admin.zone4",
    org_id="org-zone4",
    org_path="gj.ahmedabad_city.zone_4",
    role=Role.ADMIN,
    kind="session",
)

TARGET_USER = User(id="u9", username="viewer.ward9", org_id="org-ward-9", role=Role.VIEWER)


class FakeAudit:
    """Records appends; `fail_actions` names the actions whose append raises,
    simulating an audit store that dies at a chosen point in the sequence."""

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


class FakePool:
    """org_id -> org_path, the one read the scope checks need."""

    def __init__(self, paths: dict[str, str] | None = None) -> None:
        self._paths = paths or {}

    async def fetchval(self, query: str, *args):
        return self._paths.get(args[0]) if args else None


class FakeRegistry:
    """Canned-response client that records calls. `raises` makes every call
    after `fail_after` successful ones raise instead — a registry that dies
    partway through an import, or before the first call with the default 0."""

    def __init__(
        self,
        status_code: int = 200,
        body=None,
        raises: Exception | None = None,
        fail_after: int = 0,
    ) -> None:
        self._status = status_code
        self._body = body if body is not None else {}
        self._raises = raises
        self._fail_after = fail_after
        self.calls: list[tuple] = []

    def _respond(self, call: tuple) -> httpx.Response:
        self.calls.append(call)
        if self._raises is not None and len(self.calls) > self._fail_after:
            raise self._raises
        return httpx.Response(self._status, json=self._body)

    async def get(self, path: str, params: dict | None = None) -> httpx.Response:
        return self._respond(("GET", path, params))

    async def post(self, path: str, json: dict | None = None) -> httpx.Response:
        return self._respond(("POST", path, json))

    async def patch(self, path: str, json: dict | None = None) -> httpx.Response:
        return self._respond(("PATCH", path, json))

    async def delete(self, path: str) -> httpx.Response:
        return self._respond(("DELETE", path))


class FakeScopeResolver:
    def __init__(self, camera_orgs: dict[str, str | None] | None = None) -> None:
        self._camera_orgs = camera_orgs or {}

    async def org_path_for_camera(self, camera_id: str) -> str | None:
        return self._camera_orgs.get(camera_id)


class FakeUserRepo:
    def __init__(self, users: dict[str, User] | None = None, *, raises: bool = False) -> None:
        self._users = users or {}
        self._raises = raises
        self.disabled_calls: list[tuple[str, bool]] = []

    async def get(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    async def set_disabled(self, user_id: str, *, disabled: bool) -> User | None:
        self.disabled_calls.append((user_id, disabled))
        if self._raises:
            raise RuntimeError("database gone")
        user = self._users.get(user_id)
        return None if user is None else user.model_copy(update={"disabled_at": True})


class _Request:
    """The pieces of `Request` the handlers touch: `await .json()` or
    `await .body()`/`.stream()` for payloads, `app.state.pool`/`.user_repo`
    for the org lookups and repo writes."""

    def __init__(self, json_body=None, raw_body: bytes = b"", pool=None, user_repo=None) -> None:
        self._json = json_body
        self._raw = raw_body
        self.headers: dict = {}
        self.app = SimpleNamespace(
            state=SimpleNamespace(pool=pool or FakePool(), user_repo=user_repo or FakeUserRepo())
        )

    async def json(self):
        return self._json

    async def body(self) -> bytes:
        return self._raw

    async def stream(self):
        yield self._raw


# --- the core ordering contract, exercised through create_camera -------------


async def test_intent_row_survives_a_mutation_that_raises():
    """(a) The registry call blows up *after* the intent row landed — the
    attempt and its failure are both in the log, in that order."""
    audit = FakeAudit()
    registry = FakeRegistry(raises=httpx.ConnectError("registry unreachable"))
    with pytest.raises(httpx.ConnectError):
        await create_camera(OPERATOR, registry, audit, _Request({"external_id": "cam-1"}))
    assert registry.calls == [
        ("POST", "/api/v1/cameras", {"external_id": "cam-1", "org_id": "org-zone4"})
    ]
    assert audit.actions() == ["camera_create_requested", "camera_create_failed"]


async def test_upstream_error_response_writes_requested_then_failed():
    audit = FakeAudit()
    registry = FakeRegistry(422, {"detail": "bad camera payload"})
    with pytest.raises(HTTPException) as exc:
        await create_camera(OPERATOR, registry, audit, _Request({"external_id": "cam-1"}))
    assert exc.value.status_code == 422
    assert audit.actions() == ["camera_create_requested", "camera_create_failed"]


async def test_failed_intent_append_means_the_mutation_never_ran():
    """(b) Fail-closed: the intent row can't be written, so the upstream
    write must not happen — nothing occurred, so nothing needs recording."""
    audit = FakeAudit(fail_actions={"camera_create_requested"})
    registry = FakeRegistry(201, {"id": "cam-1"})
    with pytest.raises(HTTPException) as exc:
        await create_camera(OPERATOR, registry, audit, _Request({"external_id": "cam-1"}))
    assert exc.value.status_code == 500
    assert registry.calls == []
    assert audit.entries == []


async def test_success_writes_requested_then_outcome():
    """(c) The success path is two rows: intent, then outcome."""
    audit = FakeAudit()
    registry = FakeRegistry(201, {"id": "cam-1", "external_id": "cam-1"})
    result = await create_camera(OPERATOR, registry, audit, _Request({"external_id": "cam-1"}))
    assert result["id"] == "cam-1"
    assert audit.actions() == ["camera_create_requested", "camera_create"]
    assert all(e["purpose_code"] == "admin" for e in audit.entries)


async def test_failed_outcome_append_500s_but_the_intent_row_records_the_commit():
    """The mutation committed; the outcome append then fails. The request
    500s — the intent row already records who changed what, and a degraded
    audit log must surface rather than return 200 with the outcome silently
    missing. (The decided semantics in `_audit_mutation`'s docstring.)"""
    audit = FakeAudit(fail_actions={"camera_create"})
    registry = FakeRegistry(201, {"id": "cam-1"})
    with pytest.raises(HTTPException) as exc:
        await create_camera(OPERATOR, registry, audit, _Request({"external_id": "cam-1"}))
    assert exc.value.status_code == 500
    assert registry.calls != []  # committed — the intent row is its record
    assert audit.actions() == ["camera_create_requested"]


async def test_denied_scope_check_writes_denied_and_never_reaches_the_mutation():
    """Denials keep their existing single `<action>_denied` row — no
    `_requested` row, no double audit, no upstream call."""
    audit = FakeAudit()
    registry = FakeRegistry(201)
    request = _Request(
        {"external_id": "cam-1", "org_id": "org-zone5"},
        pool=FakePool({"org-zone5": "gj.ahmedabad_city.zone_5"}),
    )
    with pytest.raises(HTTPException) as exc:
        await create_camera(OPERATOR, registry, audit, request)
    assert exc.value.status_code == 403
    assert registry.calls == []
    assert audit.actions() == ["camera_create_denied"]


async def test_two_rows_per_mutation_leave_a_verifying_chain(tmp_path):
    """(d) The `_requested`/`_failed` vocabulary is still a valid hash chain —
    `verify()` only checks hashes, and nothing here breaks them."""
    audit = AuditLog(str(tmp_path / "audit.db"))
    try:
        await create_camera(
            OPERATOR, FakeRegistry(201, {"id": "cam-1"}), audit, _Request({"external_id": "cam-1"})
        )
        with pytest.raises(httpx.ConnectError):
            await create_camera(
                OPERATOR,
                FakeRegistry(raises=httpx.ConnectError("down")),
                audit,
                _Request({"external_id": "cam-2"}),
            )
        ok, broken = await audit.verify()
        assert ok and broken is None
        entries = await audit.recent(10)
        assert [e.action for e in reversed(entries)] == [
            "camera_create_requested",
            "camera_create",
            "camera_create_requested",
            "camera_create_failed",
        ]
    finally:
        audit.close()


# --- the same ordering on the other mutation kinds ----------------------------


async def test_repo_backed_mutation_gets_the_same_ordering():
    """`disable_user` writes to the local user repo, not the registry — the
    ordering rule is identical."""
    audit = FakeAudit()
    repo = FakeUserRepo({"u9": TARGET_USER})
    request = _Request(
        pool=FakePool({"org-ward-9": "gj.ahmedabad_city.zone_4.ward_9"}),
        user_repo=repo,
    )
    user = await disable_user("u9", ADMIN, audit, request)
    assert user.disabled_at is not None
    assert repo.disabled_calls == [("u9", True)]
    assert audit.actions() == ["user_disable_requested", "user_disable"]


async def test_repo_mutation_failure_writes_requested_then_failed():
    audit = FakeAudit()
    repo = FakeUserRepo({"u9": TARGET_USER}, raises=True)
    request = _Request(
        pool=FakePool({"org-ward-9": "gj.ahmedabad_city.zone_4.ward_9"}),
        user_repo=repo,
    )
    with pytest.raises(RuntimeError):
        await disable_user("u9", ADMIN, audit, request)
    assert audit.actions() == ["user_disable_requested", "user_disable_failed"]


async def test_create_user_intent_row_precedes_the_repo_write():
    """A repo that records whether the intent row existed when `create` ran —
    the assertion is on ordering, not just presence."""
    audit = FakeAudit()
    seen_actions_at_write: list[list[str]] = []

    class RecordingRepo(FakeUserRepo):
        async def create(self, payload):
            seen_actions_at_write.append(audit.actions())
            return TARGET_USER

    request = _Request(
        pool=FakePool({"org-ward-9": "gj.ahmedabad_city.zone_4.ward_9"}),
        user_repo=RecordingRepo(),
    )
    payload = UserCreate(
        username="viewer.ward9", password="a-very-long-password", org_id="org-ward-9", role="viewer"
    )
    await create_user(payload, ADMIN, audit, request)
    assert seen_actions_at_write == [["user_create_requested"]]
    assert audit.actions() == ["user_create_requested", "user_create"]


async def test_decommission_writes_requested_then_outcome():
    audit = FakeAudit()
    registry = FakeRegistry(200, {"id": "cam-1"})
    resolver = FakeScopeResolver({"cam-1": "gj.ahmedabad_city.zone_4"})
    await decommission_camera("cam-1", OPERATOR, registry, resolver, audit)
    assert registry.calls == [("DELETE", "/api/v1/cameras/cam-1")]
    assert audit.actions() == ["camera_delete_requested", "camera_delete"]


async def test_sync_trigger_writes_requested_then_outcome():
    audit = FakeAudit()
    registry = FakeRegistry(200, {"run_id": "r1"})
    result = await trigger_sync(ADMIN, registry, audit)
    assert result["run_id"] == "r1"
    assert audit.actions() == ["sync_trigger_requested", "sync_trigger"]


# --- the CSV import's batch-granularity variant --------------------------------


_CSV = b"external_id,site_name\ncam-1,Junction A\ncam-2,Junction B\n"


async def test_import_lands_intent_before_any_row_and_outcome_with_counts():
    audit = FakeAudit()
    registry = FakeRegistry(201, {"id": "x"})
    result = await import_cameras(OPERATOR, registry, audit, _Request(raw_body=_CSV))
    assert result["succeeded"] == 2
    assert len(registry.calls) == 2
    assert audit.actions() == ["camera_import_requested", "camera_import"]
    assert audit.entries[0]["resource"] == "cameras-import:2"
    assert audit.entries[-1]["resource"] == "cameras-import:2/2"


async def test_import_abort_mid_batch_leaves_requested_and_failed():
    """The registry dies on the second row: row 1 committed, and both the
    batch's intent and its failure are recorded — the committed row is never
    without a record."""
    audit = FakeAudit()
    registry = FakeRegistry(201, {"id": "x"}, raises=httpx.ConnectError("down"), fail_after=1)
    with pytest.raises(httpx.ConnectError):
        await import_cameras(OPERATOR, registry, audit, _Request(raw_body=_CSV))
    assert len(registry.calls) == 2  # row 1 created upstream, row 2 never landed
    assert audit.actions() == ["camera_import_requested", "camera_import_failed"]


async def test_import_with_failed_intent_append_never_touches_the_registry():
    audit = FakeAudit(fail_actions={"camera_import_requested"})
    registry = FakeRegistry(201, {"id": "x"})
    with pytest.raises(HTTPException) as exc:
        await import_cameras(OPERATOR, registry, audit, _Request(raw_body=_CSV))
    assert exc.value.status_code == 500
    assert registry.calls == []
