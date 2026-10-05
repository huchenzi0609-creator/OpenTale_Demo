from __future__ import annotations

import json

import pytest
from opentale.config import ConfigError, Settings, load_settings


@pytest.fixture(autouse=True)
def clear_config_environment(monkeypatch):
    for name in (
        "OPENTALE_API_KEY", "OPENTALE_BASE_URL", "OPENTALE_MODEL", "OPENTALE_TIMEOUT_SECONDS",
        "OPENTALE_MAX_OUTPUT_TOKENS", "OPENTALE_PORT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_missing_file_is_unconfigured_and_public_projection_is_secret_free(tmp_path):
    settings = load_settings(tmp_path)
    assert settings.public()["configured"] is False
    settings = Settings(api_key="unit-test-only-credential", base_url="https://example.test/private")
    public = json.dumps(settings.public())
    assert "unit-test-only-credential" not in public
    assert "example.test" not in public
    assert "base_url" not in public
    assert "unit-test-only-credential" not in repr(settings)
    assert "api_key" not in settings.model_dump()


def test_accidentally_pasted_credential_in_model_field_is_not_publicly_echoed():
    dummy = "unit-test-only-credential"
    settings = Settings(api_key=dummy, model=dummy, base_url=f"https://example.test/{dummy}")
    assert dummy not in json.dumps(settings.public())
    assert dummy not in repr(settings)


def test_local_file_loaded_without_interpolation_or_process_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIT_SENTINEL", "must-not-expand")
    (tmp_path / ".env.local").write_text(
        'OPENTALE_API_KEY="${UNIT_SENTINEL}"\nOPENTALE_PORT=8240\n', encoding="utf-8"
    )
    settings = load_settings(tmp_path)
    assert settings.api_key.get_secret_value() == "${UNIT_SENTINEL}"
    assert settings.port == 8240
    import os

    assert "OPENTALE_API_KEY" not in os.environ


def test_environment_overrides_file_even_when_key_is_explicitly_empty(tmp_path, monkeypatch):
    (tmp_path / ".env.local").write_text(
        "OPENTALE_API_KEY=fixture-credential\nOPENTALE_MODEL=deepseek-flash\n", encoding="utf-8"
    )
    monkeypatch.setenv("OPENTALE_API_KEY", "")
    monkeypatch.setenv("OPENTALE_MODEL", "unit-model")
    settings = load_settings(tmp_path)
    assert settings.public()["configured"] is False
    assert settings.model == "unit-model"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test", "ftp://example.test", "https://user:credential@example.test",
        "https://example.test?key=credential", "https://example.test/#credential",
        "https://example.test:99999", "https://example.test/../v1",
        "https://example.test\\other.test", "https://example.test\n", "http://127.0.0.1.example.test",
    ],
)
def test_bad_endpoints_fail_with_sanitized_errors(tmp_path, url):
    (tmp_path / ".env.local").write_text(f"OPENTALE_BASE_URL={url!r}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_settings(tmp_path)
    assert caught.value.code == "CONFIG_INVALID"
    assert "credential" not in str(caught.value)
    assert "example.test" not in str(caught.value)


@pytest.mark.parametrize(
    "url", ["https://example.test/v1/", "http://127.0.0.1:9000/v1", "http://[::1]:9000", "http://localhost:9000"]
)
def test_secure_remote_or_explicit_loopback_endpoints_are_accepted(url):
    assert Settings(base_url=url).base_url == url.rstrip("/")


@pytest.mark.parametrize(
    "line",
    [
        "OPENTALE_PORT=1", "OPENTALE_MAX_OUTPUT_TOKENS=999999",
        "OPENTALE_TIMEOUT_SECONDS=0", "OPENTALE_MODEL=invalid model credential",
    ],
)
def test_invalid_scalar_configuration_does_not_expose_file_values(tmp_path, line):
    (tmp_path / ".env.local").write_text(line + "\nOPENTALE_API_KEY=fixture-credential\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_settings(tmp_path)
    assert "fixture-credential" not in str(caught.value)
    assert line not in str(caught.value)
