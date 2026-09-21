"""The settings accessor the lifespan uses."""

from __future__ import annotations

from prahari_registry.config import RegistrySettings, registry_settings


def test_registry_settings_returns_a_cached_singleton():
    """`registry_settings()` is the one place env is read — lru_cached so a
    mid-process env edit cannot change the settings other modules captured."""
    first = registry_settings()
    assert first is registry_settings()
    assert isinstance(first, RegistrySettings)
