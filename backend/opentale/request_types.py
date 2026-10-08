"""Immutable request evidence and explicit attempts; contains no transport credentials."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ContextPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    total: int = Field(ge=0)
    visible_indices: list[int] = Field(default_factory=list)
    omitted_count: int = Field(ge=0)
    sha256: str


class ContextCompaction(BaseModel):
    """Source-backed projection statistics, not a claim of semantic equivalence."""

    model_config = ConfigDict(extra="forbid")
    algorithm: Literal["source-excerpts-v1", "source-pages-v1"]
    projection_version: Literal["provider-json-v2-context", "provider-json-v3-bounded"]
    target_chars: int = Field(ge=1)
    input_chars: int = Field(ge=0)
    output_chars: int = Field(ge=0)
    lossy: bool
    text_entries: int = Field(ge=0)
    material_occurrences: int = Field(ge=0)
    candidate_objects: int = Field(ge=0)
    original_text_chars: int = Field(ge=0)
    retained_text_chars: int = Field(ge=0)
    omitted_text_chars: int = Field(ge=0)
    history_segments: int = Field(ge=0)
    history_segments_excerpted: int = Field(ge=0)
    within_target: bool
    structure_excerpted: bool = False
    material_occurrences_visible: int | None = Field(default=None, ge=0)
    candidate_objects_visible: int | None = Field(default=None, ge=0)
    history_segments_visible: int | None = Field(default=None, ge=0)
    pages: list[ContextPage] = Field(default_factory=list)


class RequestAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: int = Field(ge=1)
    status: Literal["started", "received", "failed", "interrupted"] = "started"
    repair_category: str | None = None
    request_hash: str
    projection: dict[str, Any] | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    request_chars: int | None = Field(default=None, ge=0)


class RequestEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    epoch_id: str
    stage: Literal["propose", "realize", "present"]
    scope_id: str
    input_world_revision: int
    policy_revision: int
    frame: dict[str, Any]
    frame_hash: str
    request_hash: str
    projection_version: str
    compiler_version: str = "views-v2"
    evidence_level: Literal["exact_projection", "frame_only"]
    attempts: list[RequestAttempt] = Field(default_factory=list)
    accepted_result_hash: str | None = None
    context_compaction: ContextCompaction | None = None


class ScheduleJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    stage: Literal["propose", "realize", "present"]
    scope_id: str
    reason: str
    changed: bool
    dependencies: list[str] = Field(default_factory=list)


class SchedulePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    epoch_id: str
    mode: Literal["shadow"] = "shadow"
    jobs: list[ScheduleJob] = Field(default_factory=list)
