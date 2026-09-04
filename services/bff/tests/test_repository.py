"""Pure-function pieces of the identity repository layer, tested without a
database: the ltree containment mirror used for admin boundary checks, and
the one read this service makes against a table it does not own.
"""

from __future__ import annotations

from prahari_bff.repository import in_scope, org_path_for_id


def test_in_scope_same_path():
    assert in_scope("gj.ahmedabad_city", "gj.ahmedabad_city")


def test_in_scope_descendant():
    assert in_scope("gj.ahmedabad_city.zone_4", "gj.ahmedabad_city")


def test_in_scope_rejects_ancestor():
    """Being an ancestor of the scope is not being inside it — a zone-4 admin
    must not be able to name the state root as a "target" org."""
    assert not in_scope("gj", "gj.ahmedabad_city")


def test_in_scope_rejects_sibling():
    assert not in_scope("gj.surat_city", "gj.ahmedabad_city")


def test_in_scope_rejects_label_prefix_collision():
    """A naive `str.startswith(scope)` with no dot boundary would wrongly let
    'gj.ahmedabad_city_east' pass as inside scope 'gj.ahmedabad_city' — ltree
    containment is per-label, not per-character."""
    assert not in_scope("gj.ahmedabad_city_east", "gj.ahmedabad_city")


class FakePool:
    def __init__(self, paths: dict[str, str]) -> None:
        self._paths = paths

    async def fetchval(self, query: str, org_id: str) -> str | None:
        assert "FROM orgs WHERE id" in query
        return self._paths.get(org_id)


async def test_org_path_for_id_found():
    pool = FakePool({"org-1": "gj.ahmedabad_city"})
    assert await org_path_for_id(pool, "org-1") == "gj.ahmedabad_city"


async def test_org_path_for_id_missing():
    pool = FakePool({})
    assert await org_path_for_id(pool, "org-x") is None
