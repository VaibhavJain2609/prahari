"""`_check_target_org`: the org-scoping check shared by camera create and
update. Registry's own camera handlers have no principal concept
(services/registry/src/prahari_registry/app.py), so this is the only place
that stops an operator writing a camera into an org outside their own
subtree — tested the same way `create_user`/`create_api_key`'s inline checks
are exercised: no database, a fake pool standing in for `request.app.state.
pool`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from prahari_bff.app import _check_target_org
from prahari_bff.models import Principal, Role

PRINCIPAL = Principal(
    id="u1",
    subject="ops.zone4",
    org_id="org-zone4",
    org_path="gj.ahmedabad_city.zone_4",
    role=Role.OPERATOR,
    kind="session",
)


class FakePool:
    def __init__(self, paths: dict[str, str]) -> None:
        self._paths = paths

    async def fetchval(self, query: str, org_id: str) -> str | None:
        return self._paths.get(org_id)


def _request(paths: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=FakePool(paths))))


async def test_no_org_id_defaults_to_the_callers_own_org():
    request = _request({})
    assert await _check_target_org(request, PRINCIPAL, None) == "org-zone4"


async def test_org_id_within_the_callers_subtree_is_accepted():
    request = _request({"org-ward-9": "gj.ahmedabad_city.zone_4.ward_9"})
    assert await _check_target_org(request, PRINCIPAL, "org-ward-9") == "org-ward-9"


async def test_org_id_the_callers_own_org_is_accepted():
    request = _request({"org-zone4": "gj.ahmedabad_city.zone_4"})
    assert await _check_target_org(request, PRINCIPAL, "org-zone4") == "org-zone4"


async def test_org_id_outside_the_callers_subtree_is_403():
    request = _request({"org-zone5": "gj.ahmedabad_city.zone_5"})
    with pytest.raises(HTTPException) as exc:
        await _check_target_org(request, PRINCIPAL, "org-zone5")
    assert exc.value.status_code == 403


async def test_org_id_above_the_callers_subtree_is_403():
    """Also covers the general case: a sibling city's root, or the state
    root itself, is never in-scope for a zone-level operator."""
    request = _request({"org-gj": "gj"})
    with pytest.raises(HTTPException) as exc:
        await _check_target_org(request, PRINCIPAL, "org-gj")
    assert exc.value.status_code == 403


async def test_unknown_org_id_is_404():
    request = _request({})
    with pytest.raises(HTTPException) as exc:
        await _check_target_org(request, PRINCIPAL, "org-does-not-exist")
    assert exc.value.status_code == 404
