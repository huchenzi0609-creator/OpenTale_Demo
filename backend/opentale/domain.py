"""Versioned data contracts; no HTTP or model evaluation belongs here."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from .capacity import (
    DEFAULT_TOKEN_BUDGET,
    MAX_BINDING_TEXT_CHARS,
    MAX_CALL_BUDGET,
    MAX_OPERATION_TEXT_CHARS,
    MAX_PARALLEL_POSITIONS,
    MAX_PRESENTATION_TEXT_CHARS,
    MAX_PROPOSAL_OPERATIONS,
    MAX_PROPOSAL_TEXT_CHARS,
    MAX_READING_TRACKS,
    MAX_TOKEN_BUDGET,
    MAX_WINDOW_BUDGET,
)
from .reflection_types import ReflectionFrame, ReflectionProposal, ReflectionState


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Limits(Record):
    max_calls: int = Field(default=24, ge=1, le=MAX_CALL_BUDGET)
    max_tokens: int = Field(default=DEFAULT_TOKEN_BUDGET, ge=1, le=MAX_TOKEN_BUDGET)
    max_parallel: int = Field(default=3, ge=1, le=MAX_PARALLEL_POSITIONS)
    max_windows: int = Field(default=8, ge=1, le=MAX_WINDOW_BUDGET)


class Usage(Record):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reserved_tokens: int = 0
    unknown_tokens: int = 0
    failed_calls: int = 0
    windows: int = 0


class Binding(Record):
    source_position_id: str
    source_expression_id: str
    value: str = Field(min_length=1, max_length=MAX_BINDING_TEXT_CHARS)
    anchor: str = Field(min_length=1, max_length=MAX_BINDING_TEXT_CHARS)
    revision: int


class Use(Record):
    id: str
    position_id: str
    expression_id: str
    role: str = Field(min_length=1, max_length=80)
    bindings: list[Binding]
    revision: int


class Expression(Record):
    id: str
    text: str
    created_revision: int
    source_position_id: str | None = None


class Position(Record):
    id: str
    name: str
    seed: str


class Continuation(Record):
    position_id: str
    frame_revision: int
    status: str = "ready"
    seed_text: str = ""
    condition_fingerprint: str = ""
    dependencies: list[str] = Field(default_factory=list)
    surface: str = ""
    resume_count: int = 0


class Operation(Record):
    op: Literal["rebind"]
    target_position_id: str
    expression_id: str
    role: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=MAX_OPERATION_TEXT_CHARS)
    anchor: str = Field(min_length=1, max_length=MAX_OPERATION_TEXT_CHARS)


class Proposal(Record):
    position_id: str
    based_on_revision: int
    text: str = Field(min_length=1, max_length=MAX_PROPOSAL_TEXT_CHARS)
    operations: list[Operation] = Field(min_length=0, max_length=MAX_PROPOSAL_OPERATIONS)
    reflection: ReflectionProposal | None = None


class GenerationFrame(Record):
    position_id: str
    name: str
    revision: int
    seed_text: str = ""
    active_uses: list[dict[str, Any]]
    condition_fingerprint: str
    recent_surface: list[str]
    peers: list[dict[str, str]]
    peer_interfaces: list[dict[str, str]] = Field(default_factory=list)
    reflection: ReflectionFrame | None = None


class ReadingFrame(Record):
    revision: int
    candidates: list[dict[str, Any]]
    previous_segments: list[dict[str, Any]]
    open_relations: list[dict[str, Any]]
    current_focus: str | None = None
    start_position_id: str | None = None
    required_focus_position_id: str | None = None
    reflection: ReflectionFrame | None = None


class Presentation(Record):
    text: str = Field(min_length=1, max_length=MAX_PRESENTATION_TEXT_CHARS)
    focus_position_id: str
    expression_ids: list[str] = Field(min_length=0)
    reinterprets: list[str] = Field(default_factory=list)
    reflection: ReflectionProposal | None = None


class Event(Record):
    id: str
    type: str
    revision: int
    at: str = Field(default_factory=now)
    data: dict[str, Any] = Field(default_factory=dict)


class StorySegment(Record):
    id: str
    revision: int
    text: str
    focus_position_id: str
    expression_ids: list[str]
    reinterprets: list[str]


class Reading(Record):
    focus_position_id: str | None = None
    open_relations: list[dict[str, Any]] = Field(default_factory=list)
    interpretations: list[dict[str, Any]] = Field(default_factory=list)


class Pending(Record):
    base_revision: int
    proposals: dict[str, Proposal] = Field(default_factory=dict)
    realizations: dict[str, str] = Field(default_factory=dict)
    committed: bool = False
    presentations: dict[str, Presentation] = Field(default_factory=dict)
    reading_frames: dict[str, ReadingFrame] = Field(default_factory=dict)
    realization_frames: dict[str, GenerationFrame] = Field(default_factory=dict)


class Run(Record):
    id: str
    title: str
    provider: Literal["fixture", "deepseek"]
    mode: Literal["classic", "reflexive"] = "classic"
    status: str = "idle"
    revision: int = 0
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    limits: Limits = Field(default_factory=Limits)
    usage: Usage = Field(default_factory=Usage)
    last_error: dict[str, Any] | None = None
    phase: str = "idle"
    fork_of: str | None = None


class ReadingTrack(Record):
    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    start_position_id: str | None = None
    created_revision: int = Field(default=0, ge=0)
    enabled: bool = True
    story: list[StorySegment] = Field(default_factory=list)
    reading: Reading = Field(default_factory=Reading)
    last_error: dict[str, Any] | None = None


class Snapshot(Record):
    schema_version: Literal[2, 3] = 2
    run: Run
    positions: list[Position]
    expressions: list[Expression]
    uses: list[Use]
    continuations: list[Continuation]
    events: list[Event] = Field(default_factory=list)
    reading_tracks: list[ReadingTrack] = Field(
        default_factory=lambda: [ReadingTrack(id="main", title="阅读 1")],
        min_length=1, max_length=MAX_READING_TRACKS,
    )
    pending: Pending | None = None
    audit: dict[str, Any] = Field(default_factory=dict)
    reflection: ReflectionState | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_readings(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        version = data.get("schema_version", 1)
        if type(version) is not int or version not in {1, 2, 3}:
            return data  # Literal validation rejects unknown future versions.
        if version in {2, 3} and "reading_tracks" not in data:
            raise ValueError("Version-2 snapshots require canonical reading histories")
        legacy_story = data.pop("story", [])
        legacy_reading = data.pop("reading", {})
        if "reading_tracks" not in data:
            data["reading_tracks"] = [{
                "id": "main", "title": "阅读 1", "story": legacy_story, "reading": legacy_reading,
            }]
        elif legacy_story or legacy_reading:
            # Compatibility projections must never silently override canonical histories.
            main = next((track for track in data["reading_tracks"] if (
                track.id if isinstance(track, ReadingTrack) else track.get("id")
            ) == "main"), None)
            if main is not None:
                main_data = main.model_dump() if isinstance(main, ReadingTrack) else main
                if legacy_story and legacy_story != main_data.get("story", []):
                    raise ValueError("Primary reading projection does not match its history")
                if legacy_reading and legacy_reading != main_data.get("reading", {}):
                    raise ValueError("Primary reading projection does not match its state")
        data["schema_version"] = 3 if version == 3 else 2
        return data

    @model_validator(mode="after")
    def unique_readings(self) -> "Snapshot":
        if self.run.mode == "reflexive" and (self.schema_version != 3 or self.reflection is None):
            raise ValueError("Reflexive snapshots require version 3 and reflection state")
        if self.run.mode == "classic" and (self.schema_version != 2 or self.reflection is not None):
            raise ValueError("Classic snapshots cannot execute reflection state")
        identifiers = [track.id for track in self.reading_tracks]
        if len(identifiers) != len(set(identifiers)) or "main" not in identifiers:
            raise ValueError("Reading tracks need unique IDs and one main compatibility view")
        positions = {position.id for position in self.positions}
        if any(track.start_position_id is not None and track.start_position_id not in positions
               for track in self.reading_tracks):
            raise ValueError("Reading starts must reference current positions")
        return self

    @computed_field
    @property
    def story(self) -> list[StorySegment]:
        """Compatibility projection; every reading has one canonical history."""
        return next(track.story for track in self.reading_tracks if track.id == "main")

    @computed_field
    @property
    def reading(self) -> Reading:
        return next(track.reading for track in self.reading_tracks if track.id == "main")
