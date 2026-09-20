"""Shared fixtures for the workspace-root integration tests.

Every service's settings getter is `@lru_cache`d once per process (matching
`services/match-engine/tests/conftest.py`, which established this pattern for
`match_settings`). A test that mutates `PRAHARI_*` env vars and then calls a
getter would otherwise leak its settings object into every later test in the
same `pytest` run — the cached instance is built from the env at first call,
not per test. Clear every getter's cache before *and* after each test so the
leak is impossible in either direction.

The getters are resolved lazily (module name + attribute) rather than
imported at conftest load: a service that has not been built yet must not
take down collection of the whole root suite.
"""

from __future__ import annotations

import importlib

import pytest

_SETTINGS_GETTERS = (
    ("prahari_registry.config", "registry_settings"),
    ("prahari_bff.config", "bff_settings"),
    ("prahari_correlation.config", "correlation_settings"),
    ("prahari_match.config", "match_settings"),
    ("prahari_inference.config", "detector_settings"),
    ("prahari_inference.config", "ingest_settings"),
    ("prahari_common.config", "gateway_settings"),
)


def _clear() -> None:
    for module_name, attr in _SETTINGS_GETTERS:
        getter = getattr(importlib.import_module(module_name), attr, None)
        if getter is not None:
            getter.cache_clear()


@pytest.fixture(autouse=True)
def _reset_settings_caches():
    _clear()
    yield
    _clear()
