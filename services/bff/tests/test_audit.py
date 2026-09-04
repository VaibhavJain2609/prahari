"""The hash-chained audit log: append/verify roundtrip, and the one property
that actually matters for evidentiary use — tampering with any stored row is
detectable, and `verify()` names the first row it breaks.
"""

from __future__ import annotations

import sqlite3

from prahari_bff.audit import AuditLog


def _log(tmp_path) -> AuditLog:
    return AuditLog(str(tmp_path / "audit.db"))


async def test_empty_log_verifies_ok(tmp_path):
    log = _log(tmp_path)
    ok, broken = await log.verify()
    assert ok is True
    assert broken is None


async def test_append_then_verify_ok(tmp_path):
    log = _log(tmp_path)
    for i in range(5):
        await log.append(
            actor="ops.zone4",
            org_path="gj.ahmedabad_city.zone_4",
            purpose_code="case-2026-0001",
            resource=f"camera:cam-{i}",
            action="read",
        )
    ok, broken = await log.verify()
    assert ok is True
    assert broken is None


async def test_first_entry_chains_to_the_genesis_hash(tmp_path):
    log = _log(tmp_path)
    entry = await log.append(
        actor="ops.zone4",
        org_path="gj",
        purpose_code="p",
        resource="camera:cam-1",
        action="read",
    )
    assert entry.prev_hash == "0" * 64
    assert entry.id == 1


async def test_entries_chain_in_order(tmp_path):
    log = _log(tmp_path)
    first = await log.append(
        actor="a", org_path="gj", purpose_code="p", resource="r1", action="read"
    )
    second = await log.append(
        actor="a", org_path="gj", purpose_code="p", resource="r2", action="read"
    )
    assert second.prev_hash == first.hash
    assert second.hash != first.hash


async def test_tampering_a_row_is_detected_at_that_row(tmp_path):
    log = _log(tmp_path)
    await log.append(actor="a", org_path="gj", purpose_code="p", resource="r1", action="read")
    second = await log.append(
        actor="a", org_path="gj", purpose_code="p", resource="r2", action="read"
    )
    await log.append(actor="a", org_path="gj", purpose_code="p", resource="r3", action="read")

    # Tamper directly at the storage layer -- the thing a verifier exists to
    # catch is exactly an edit that did not go through append().
    conn = sqlite3.connect(str(log._db_path))
    conn.execute("UPDATE audit_log SET resource = 'r2-tampered' WHERE id = ?", (second.id,))
    conn.commit()
    conn.close()

    ok, broken = await log.verify()
    assert ok is False
    assert broken == second.id


async def test_denied_action_is_appended_like_any_other_entry(tmp_path):
    log = _log(tmp_path)
    entry = await log.append(
        actor="ops.zone4",
        org_path="gj.ahmedabad_city.zone_4",
        purpose_code="case-2026-0001",
        resource="camera:cam-outside-scope",
        action="denied",
    )
    assert entry.action == "denied"
    recent = await log.recent(limit=10)
    assert recent[0].id == entry.id
    assert recent[0].action == "denied"
