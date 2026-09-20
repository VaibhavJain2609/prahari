"""The hash-chained audit log: append/verify roundtrip, and the one property
that actually matters for evidentiary use — tampering with any stored row is
detectable, and `verify()` names the first row it breaks.
"""

from __future__ import annotations

import asyncio
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


async def test_concurrent_appends_form_one_unbroken_chain(tmp_path):
    """`append()` runs on a thread pool — without the append lock, N racing
    appends read the same tail hash and insert N rows claiming the same
    `prev_hash`, forking the chain. Every entry must chain to exactly one
    predecessor."""
    log = _log(tmp_path)
    entries = await asyncio.gather(
        *(
            log.append(
                actor="ops.zone4",
                org_path="gj",
                purpose_code="p",
                resource=f"camera:cam-{i}",
                action="read",
            )
            for i in range(50)
        )
    )
    assert len({entry.id for entry in entries}) == 50
    ok, broken = await log.verify()
    assert ok is True
    assert broken is None


async def test_recent_filters_by_actor_action_and_since(tmp_path):
    log = _log(tmp_path)
    await log.append(actor="alice", org_path="gj", purpose_code="p", resource="r1", action="read")
    await log.append(actor="bob", org_path="gj", purpose_code="p", resource="r2", action="denied")
    third = await log.append(
        actor="alice", org_path="gj", purpose_code="p", resource="r3", action="read"
    )

    by_actor = await log.recent(limit=10, actor="alice")
    assert {e.resource for e in by_actor} == {"r1", "r3"}

    by_action = await log.recent(limit=10, action="denied")
    assert [e.actor for e in by_action] == ["bob"]

    # ISO-8601 strings compare chronologically — `since` at the third
    # entry's own timestamp includes it; a future `since` excludes all.
    by_since = await log.recent(limit=10, since=third.occurred_at)
    assert third.id in {e.id for e in by_since}
    assert await log.recent(limit=10, since="9999-01-01T00:00:00+00:00") == []


async def test_recent_paginates_with_offset(tmp_path):
    log = _log(tmp_path)
    for i in range(5):
        await log.append(
            actor="a", org_path="gj", purpose_code="p", resource=f"r{i}", action="read"
        )
    page1 = await log.recent(limit=2, offset=0)
    page2 = await log.recent(limit=2, offset=2)
    assert [e.resource for e in page1] == ["r4", "r3"]  # newest first
    assert [e.resource for e in page2] == ["r2", "r1"]


async def test_head_reports_the_tail_hash_and_row_count(tmp_path):
    log = _log(tmp_path)
    head_hash, count = await log.head()
    assert head_hash == "0" * 64  # empty log reports the genesis link
    assert count == 0

    last = None
    for i in range(3):
        last = await log.append(
            actor="a", org_path="gj", purpose_code="p", resource=f"r{i}", action="read"
        )
    head_hash, count = await log.head()
    assert head_hash == last.hash
    assert count == 3


async def test_head_exposes_tail_truncation_that_verify_cannot_see(tmp_path):
    """Deleting rows off the tail leaves `verify()` green — the surviving
    chain is internally consistent — but `head()` shrinks the count and
    rewinds the head hash. That pair is what a monitor polls."""
    log = _log(tmp_path)
    entries = [
        await log.append(
            actor="a", org_path="gj", purpose_code="p", resource=f"r{i}", action="read"
        )
        for i in range(4)
    ]

    conn = sqlite3.connect(str(log._db_path))
    conn.execute("DELETE FROM audit_log WHERE id = ?", (entries[-1].id,))
    conn.commit()
    conn.close()

    ok, _ = await log.verify()
    assert ok is True  # the remaining chain is still consistent...
    head_hash, count = await log.head()
    assert count == 3  # ...but the truncation is visible here
    assert head_hash == entries[-2].hash
