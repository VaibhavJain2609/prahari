"""Workspace-root test fixtures — applies to every testpath (services/*/tests,
packages/*/tests, tests/).

The seal: every PRAHARI settings class reads `env_file=".env"`, and the repo
root has a real `.env` (gitignored — live gateway credentials). Any test that
constructs a Settings class and leaves one field unset silently resolves it
from the developer's machine — the same test then asserts different values on
different machines (found: `test_sync.py` asserted a fixture URL while
`.env`'s real `PRAHARI_GATEWAY_DIRECT_HOST` won the precedence chain).

Seal `env_file` on every settings class for the duration of each test so a
constructed settings object contains exactly what the test passed plus real
process env vars — never the repo's secrets file. Class list is resolved
lazily like tests/conftest.py so a not-yet-built service can't break
collection.
"""

from __future__ import annotations

import importlib

import pytest

_SETTINGS_CLASSES = (
    ("prahari_common.config", "GatewaySettings"),
    ("prahari_registry.config", "RegistrySettings"),
    ("prahari_bff.config", "BFFSettings"),
    ("prahari_correlation.config", "CorrelationSettings"),
    ("prahari_match.config", "MatchSettings"),
    ("prahari_inference.config", "IngestSettings"),
)


@pytest.fixture(autouse=True)
def _no_dotenv_leak(monkeypatch):
    for module_name, attr in _SETTINGS_CLASSES:
        try:
            cls = getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError):
            continue
        monkeypatch.setitem(cls.model_config, "env_file", None)
    yield
