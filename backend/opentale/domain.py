"""Versioned data contracts; no HTTP or model evaluation belongs here."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .capacity import (
    DEFAULT_TOKEN_BUDGET,
    MAX_BINDING_TEXT_CHARS,
    MAX_CALL_BUDGET,
    MAX_OPERATION_TEXT_CHARS,
    MAX_PARALLEL_POSITIONS,
    MAX_PRESENTATION_TEXT_CHARS,
    MAX_PROPOSAL_OPERATIONS,
    MAX_PROPOSAL_TEXT_CHARS,
    MAX_TOKEN_BUDGET,
    MAX_WINDOW_BUDGET,
)


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
    operations: list[Operation] = Field(min_length=1, max_length=MAX_PROPOSAL_OPERATIONS)


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


class ReadingFrame(Record):
    revision: int
    candidates: list[dict[str, Any]]
    previous_segments: list[dict[str, Any]]
    open_relations: list[dict[str, Any]]
    current_focus: str | None = None


class Presentation(Record):
    text: str = Field(min_length=1, max_length=MAX_PRESENTATION_TEXT_CHARS)
    focus_position_id: str
    expression_ids: list[str] = Field(min_length=1)
    reinterprets: list[str] = Field(default_factory=list)


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


class Run(Record):
    id: str
    title: str
    provider: Literal["fixture", "deepseek"]
    status: str = "idle"
    revision: int = 0
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    limits: Limits = Field(default_factory=Limits)
    usage: Usage = Field(default_factory=Usage)
    last_error: dict[str, Any] | None = None
    phase: str = "idle"
    fork_of: str | None = None


class Snapshot(Record):
    schema_version: Literal[1] = 1
    run: Run
    positions: list[Position]
    expressions: list[Expression]
    uses: list[Use]
    continuations: list[Continuation]
    events: list[Event] = Field(default_factory=list)
    story: list[StorySegment] = Field(default_factory=list)
    reading: Reading = Field(default_factory=Reading)
    pending: Pending | None = None
    audit: dict[str, Any] = Field(default_factory=dict)
