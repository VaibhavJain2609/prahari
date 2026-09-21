"""config.py: gateway connection settings.

The validators and URL properties are where a misconfigured deploy turns into
a confusing failure far from the cause — a scheme pasted into the host var, a
trailing slash doubling into a path. These tests pin that behaviour plus the
`gateway_settings()` process-wide accessor.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from prahari_common.config import GatewaySettings, gateway_settings


def _settings(**overrides) -> GatewaySettings:
    kwargs: dict = {"host": "cdn.example.test", "password": SecretStr("pw")}
    kwargs.update(overrides)
    return GatewaySettings(**kwargs)


class TestHostValidation:
    @pytest.mark.parametrize("field", ["host", "direct_host"])
    def test_a_scheme_in_the_host_is_rejected(self, field: str) -> None:
        # Pasting the full URL into the host var produces "https://https://..."
        # far from the cause — reject it at parse time.
        with pytest.raises(ValidationError, match="bare hostname without a scheme"):
            _settings(**{field: "https://cdn.example.test"})

    def test_a_trailing_slash_is_stripped(self) -> None:
        assert _settings(host="cdn.example.test/").host == "cdn.example.test"
        assert _settings(direct_host="203.0.113.5/").direct_host == "203.0.113.5"

    def test_direct_host_may_be_absent(self) -> None:
        assert _settings().direct_host is None


class TestUrls:
    def test_base_url(self) -> None:
        assert _settings().base_url == "https://cdn.example.test"
        assert _settings(scheme="http").base_url == "http://cdn.example.test"

    def test_catalogue_url(self) -> None:
        assert _settings().catalogue_url == "https://cdn.example.test/cameras.json"
        assert (
            _settings(catalogue_path="/api/ingest").catalogue_url
            == "https://cdn.example.test/api/ingest"
        )

    def test_login_url(self) -> None:
        assert _settings().login_url == "https://cdn.example.test/auth/login"

    def test_direct_host_or_host_falls_back(self) -> None:
        assert _settings().direct_host_or_host == "cdn.example.test"
        assert _settings(direct_host="203.0.113.5").direct_host_or_host == "203.0.113.5"


class TestGatewaySettingsAccessor:
    def test_gateway_settings_reads_the_env_and_is_cached(self, monkeypatch) -> None:
        monkeypatch.setenv("PRAHARI_GATEWAY_HOST", "env.example.test")
        monkeypatch.setenv("PRAHARI_GATEWAY_PASSWORD", "from-env")
        gateway_settings.cache_clear()
        try:
            settings = gateway_settings()
            assert settings.host == "env.example.test"
            assert settings.password.get_secret_value() == "from-env"
            assert gateway_settings() is settings  # lru_cached process-wide
        finally:
            gateway_settings.cache_clear()
