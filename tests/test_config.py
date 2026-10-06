from __future__ import annotations

import json

import pytest
from opentale.capacity import MAX_MODEL_OUTPUT_TOKENS
from opentale.config import (
    ConfigError,
    Settings,
    config_diagnostics,
    load_settings,
    load_settings_with_diagnostics,
)


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


def test_missing_file_diagnostics_use_the_exact_path_and_only_allowlisted_metadata(tmp_path):
    diagnostics = config_diagnostics(tmp_path)
    assert diagnostics == {
        "config_file_path": str((tmp_path / ".env.local").resolve()),
        "config_file_exists": False,
        "file_api_key_present": False,
        "environment_api_key_override": False,
        "environment_api_key_empty_override": False,
        "credential_source": "none",
    }


def test_bom_configuration_is_loaded_and_diagnostics_never_return_values(tmp_path):
    dummy = "fixture-credential-for-diagnostics"
    (tmp_path / ".env.local").write_text(
        f"OPENTALE_API_KEY={dummy}\n"
        "OPENTALE_BASE_URL=https://example.test/private\n"
        "UNKNOWN_FIELD_WITH_SECRET=private-unknown-content\n",
        encoding="utf-8-sig",
    )
    settings, diagnostics = load_settings_with_diagnostics(tmp_path)
    assert settings.api_key.get_secret_value() == dummy
    assert settings.public()["configured"] is True
    assert diagnostics["config_file_exists"] is True
    assert diagnostics["file_api_key_present"] is True
    assert diagnostics["credential_source"] == "file"
    public = json.dumps(diagnostics)
    for private_text in (
        dummy, "example.test", "private-unknown-content", "UNKNOWN_FIELD_WITH_SECRET", "base_url",
    ):
        assert private_text not in public


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_environment_override_is_diagnosed_without_falling_back(tmp_path, monkeypatch, value):
    (tmp_path / ".env.local").write_text("OPENTALE_API_KEY=file-fixture-key\n", encoding="utf-8")
    monkeypatch.setenv("OPENTALE_API_KEY", value)
    settings, diagnostics = load_settings_with_diagnostics(tmp_path)
    assert settings.public()["configured"] is False
    assert diagnostics["file_api_key_present"] is True
    assert diagnostics["environment_api_key_override"] is True
    assert diagnostics["environment_api_key_empty_override"] is True
    assert diagnostics["credential_source"] == "environment"
    assert "file-fixture-key" not in json.dumps(diagnostics)


def test_nonempty_environment_override_is_diagnosed_without_exposing_credentials(tmp_path, monkeypatch):
    (tmp_path / ".env.local").write_text("OPENTALE_API_KEY=file-fixture-key\n", encoding="utf-8")
    monkeypatch.setenv("OPENTALE_API_KEY", "environment-fixture-key")
    settings, diagnostics = load_settings_with_diagnostics(tmp_path)
    assert settings.api_key.get_secret_value() == "environment-fixture-key"
    assert diagnostics["environment_api_key_override"] is True
    assert diagnostics["environment_api_key_empty_override"] is False
    assert diagnostics["credential_source"] == "environment"
    assert "environment-fixture-key" not in json.dumps(diagnostics)


@pytest.mark.parametrize("file_content", ["OPENTALE_API_KEY=\n", 'OPENTALE_API_KEY="   "\n',
                                           "DEEPSEEK_API_KEY=wrong-field-fixture-key\n"])
def test_blank_or_nonstandard_file_keys_are_not_reported_as_present(tmp_path, file_content):
    (tmp_path / ".env.local").write_text(file_content, encoding="utf-8")
    settings, diagnostics = load_settings_with_diagnostics(tmp_path)
    assert settings.public()["configured"] is False
    assert diagnostics["config_file_exists"] is True
    assert diagnostics["file_api_key_present"] is False
    assert diagnostics["credential_source"] == "none"
    assert "wrong-field-fixture-key" not in json.dumps(diagnostics)


def test_combined_settings_and_diagnostics_load_parse_the_file_once(tmp_path, monkeypatch):
    import opentale.config as configuration

    (tmp_path / ".env.local").write_text("OPENTALE_API_KEY=fixture-key\n", encoding="utf-8")
    real_parser = configuration.dotenv_values
    calls = []

    def counted_parser(*args, **kwargs):
        calls.append((args, kwargs))
        return real_parser(*args, **kwargs)

    monkeypatch.setattr(configuration, "dotenv_values", counted_parser)
    settings, diagnostics = load_settings_with_diagnostics(tmp_path)
    assert settings.public()["configured"] is True
    assert diagnostics["file_api_key_present"] is True
    assert len(calls) == 1


def test_credentials_accidentally_in_directory_names_are_redacted_from_diagnostics(tmp_path, monkeypatch):
    file_dummy = "file-fixture-key"
    environment_dummy = "environment-fixture-key"
    root = tmp_path / file_dummy / environment_dummy
    root.mkdir(parents=True)
    (root / ".env.local").write_text(f"OPENTALE_API_KEY={file_dummy}\n", encoding="utf-8")
    monkeypatch.setenv("OPENTALE_API_KEY", environment_dummy)
    settings, diagnostics = load_settings_with_diagnostics(root)
    assert settings.public()["configured"] is True
    public = json.dumps(diagnostics)
    assert file_dummy not in public
    assert environment_dummy not in public
    assert diagnostics["config_file_path"].endswith("/[已隐藏]/[已隐藏]/.env.local")


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
        "OPENTALE_PORT=1", "OPENTALE_MAX_OUTPUT_TOKENS=127",
        "OPENTALE_TIMEOUT_SECONDS=0", "OPENTALE_MODEL=invalid model credential",
    ],
)
def test_invalid_scalar_configuration_does_not_expose_file_values(tmp_path, line):
    (tmp_path / ".env.local").write_text(line + "\nOPENTALE_API_KEY=fixture-credential\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_settings(tmp_path)
    assert "fixture-credential" not in str(caught.value)
    assert line not in str(caught.value)


def test_output_budget_has_no_software_ceiling_and_keeps_the_default(tmp_path):
    assert Settings().max_output_tokens == 1600
    (tmp_path / ".env.local").write_text(
        f"OPENTALE_MAX_OUTPUT_TOKENS={MAX_MODEL_OUTPUT_TOKENS}\n", encoding="utf-8"
    )
    assert load_settings(tmp_path).max_output_tokens == MAX_MODEL_OUTPUT_TOKENS
    (tmp_path / ".env.local").write_text(
        f"OPENTALE_MAX_OUTPUT_TOKENS={MAX_MODEL_OUTPUT_TOKENS + 1}\n", encoding="utf-8"
    )
    assert load_settings(tmp_path).max_output_tokens == MAX_MODEL_OUTPUT_TOKENS + 1
    (tmp_path / ".env.local").write_text("OPENTALE_MAX_OUTPUT_TOKENS=51200000\n", encoding="utf-8")
    assert load_settings(tmp_path).max_output_tokens == 51200000
