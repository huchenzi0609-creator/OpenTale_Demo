"""Canonical v2 histories cannot silently disappear during snapshot loading."""

import pytest
from opentale.domain import Snapshot
from pydantic import ValidationError


def test_v2_roundtrips_canonical_storage_and_compatibility_projection(snapshot):
    canonical = snapshot.model_dump(exclude={"story", "reading"})
    assert Snapshot.model_validate(canonical).model_dump() == snapshot.model_dump()
    assert Snapshot.model_validate(snapshot.model_dump()).model_dump() == snapshot.model_dump()


def test_v2_missing_canonical_tracks_is_rejected_even_with_legacy_aliases(snapshot):
    damaged = snapshot.model_dump()
    damaged.pop("reading_tracks")
    with pytest.raises(ValidationError):
        Snapshot.model_validate(damaged)


def test_unsupported_version_does_not_fall_back_to_empty_main_reading(snapshot):
    future = snapshot.model_dump()
    future["schema_version"] = 4
    with pytest.raises(ValidationError):
        Snapshot.model_validate(future)


def test_projection_cannot_override_canonical_primary_history(snapshot):
    damaged = snapshot.model_dump()
    damaged["reading"]["focus_position_id"] = snapshot.positions[0].id
    with pytest.raises(ValidationError):
        Snapshot.model_validate(damaged)
