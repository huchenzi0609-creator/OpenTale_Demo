from pathlib import Path

import pytest
from opentale.config import Settings
from opentale.domain import Limits
from opentale.presets import initialize, read_presets

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def snapshot():
    preset = next(p for p in read_presets(ROOT) if p["id"] == "letters")
    return initialize(preset, "fixture", Limits())


@pytest.fixture
def settings():
    return Settings()
