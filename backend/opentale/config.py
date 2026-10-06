"""Backend-only configuration loading with a deliberately small public projection."""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Literal, TypedDict
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator


class ConfigError(Exception):
    """A configuration failure that never includes user-supplied values."""

    code = "CONFIG_INVALID"

    def __init__(self, message: str = "配置无效，请检查本地配置文件中的地址与参数。") -> None:
        self.message = message
        super().__init__(message)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    api_key: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    base_url: str = Field(default="https://api.deepseek.com", repr=False)
    model: str = Field(default="deepseek-flash", repr=False)
    timeout_seconds: float = Field(default=90, ge=5, le=300)
    max_output_tokens: int = Field(default=1600, ge=128)
    port: int = Field(default=8231, ge=1024, le=65535)

    @field_validator("api_key", mode="before")
    @classmethod
    def validate_key(cls, value: object) -> SecretStr:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if (
            not isinstance(raw, str)
            or len(raw) > 8192
            or any(c in raw for c in "\r\n\x00")
            or not raw.isascii()
            or any(ord(c) < 32 or ord(c) > 126 for c in raw)
        ):
            raise ValueError("API credential format is invalid")
        return SecretStr(raw.strip())

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value):
            raise ValueError("Model identifier is invalid")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        if any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("Endpoint format is invalid")
        try:
            parts = urlsplit(value)
            hostname = parts.hostname
            port = parts.port
        except ValueError:
            raise ValueError("Endpoint format is invalid") from None
        if (
            not hostname
            or parts.scheme not in {"https", "http"}
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or (port is not None and not 1 <= port <= 65535)
            or "\\" in value
            or any(segment in {".", ".."} for segment in parts.path.split("/"))
        ):
            raise ValueError("Endpoint format is invalid")
        if parts.scheme == "http":
            loopback = hostname.lower() == "localhost"
            try:
                loopback = loopback or ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                pass
            if not loopback:
                raise ValueError("Remote endpoint requires HTTPS")
        return value.rstrip("/")

    def public(self) -> dict[str, object]:
        """The only configuration representation permitted in APIs and exports."""
        key = self.api_key.get_secret_value()
        return {
            "provider": "deepseek",
            "configured": bool(key),
            "model": "[已隐藏]" if key and key in self.model else self.model,
            "timeout_seconds": self.timeout_seconds,
            "max_output_tokens": self.max_output_tokens,
            "port": self.port,
        }


_ENV_FIELDS = {
    "OPENTALE_API_KEY": "api_key",
    "OPENTALE_BASE_URL": "base_url",
    "OPENTALE_MODEL": "model",
    "OPENTALE_TIMEOUT_SECONDS": "timeout_seconds",
    "OPENTALE_MAX_OUTPUT_TOKENS": "max_output_tokens",
    "OPENTALE_PORT": "port",
}


class ConfigDiagnostics(TypedDict):
    """A fixed allowlist of local configuration metadata, never credential contents."""

    config_file_path: str
    config_file_exists: bool
    file_api_key_present: bool
    environment_api_key_override: bool
    environment_api_key_empty_override: bool
    credential_source: Literal["environment", "file", "none"]


def _read_configuration(root: Path) -> tuple[dict[str, object], ConfigDiagnostics]:
    """Read file and allowlisted environment entries once for a consistent snapshot."""
    try:
        local_file = (root / ".env.local").resolve()
        file_exists = local_file.exists()
        local = (
            dotenv_values(local_file, interpolate=False, encoding="utf-8-sig")
            if file_exists else {}
        )
        environment = {name: os.environ[name] for name in _ENV_FIELDS if name in os.environ}
        values = {
            field: environment[variable] if variable in environment else local[variable]
            for variable, field in _ENV_FIELDS.items()
            if variable in environment or local.get(variable) is not None
        }
        file_key = (local.get("OPENTALE_API_KEY") or "").strip()
        environment_key = (environment.get("OPENTALE_API_KEY") or "").strip()
        file_key_present = bool(file_key)
        environment_override = "OPENTALE_API_KEY" in environment
        public_path = str(local_file)
        for credential in sorted({file_key, environment_key} - {""}, key=len, reverse=True):
            public_path = public_path.replace(credential, "[已隐藏]")
        diagnostics: ConfigDiagnostics = {
            "config_file_path": public_path,
            "config_file_exists": file_exists,
            "file_api_key_present": file_key_present,
            "environment_api_key_override": environment_override,
            "environment_api_key_empty_override": (
                environment_override and not environment_key
            ),
            "credential_source": (
                "environment" if environment_override else "file" if file_key_present else "none"
            ),
        }
        return values, diagnostics
    except (OSError, UnicodeError, ValueError):
        raise ConfigError() from None


def load_settings_with_diagnostics(root: Path) -> tuple[Settings, ConfigDiagnostics]:
    """Load settings and their metadata together; no interpolation or environment mutation."""
    values, diagnostics = _read_configuration(root)
    try:
        return Settings.model_validate(values), diagnostics
    except (ValueError, ValidationError):
        raise ConfigError() from None


def load_settings(root: Path) -> Settings:
    """Load one ignored file without interpolating it or mutating process environment."""
    settings, _ = load_settings_with_diagnostics(root)
    return settings


def config_diagnostics(root: Path) -> ConfigDiagnostics:
    """Inspect only source metadata; use the combined loader when settings are also needed."""
    _, diagnostics = _read_configuration(root)
    return diagnostics
