"""Replaceable evaluators, exported through one stable boundary."""

from opentale.config import Settings

from .base import ModelResult, Provider, ProviderError
from .deepseek import DeepSeekProvider
from .fixture import FixtureProvider


def make_provider(mode: str, settings: Settings) -> Provider:
    if mode == "fixture":
        return FixtureProvider()
    if mode == "deepseek":
        return DeepSeekProvider(settings)
    raise ProviderError("PROVIDER_UNKNOWN", "不支持此生成服务。")


__all__ = [
    "DeepSeekProvider",
    "FixtureProvider",
    "ModelResult",
    "Provider",
    "ProviderError",
    "make_provider",
]
