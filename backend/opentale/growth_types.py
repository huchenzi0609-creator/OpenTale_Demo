"""Version-six growth and reading records. Bodies and history live outside the run head."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextRef(Record):
    sha256: str
    length: int = Field(ge=0)


class ObjectRef(Record):
    kind: str
    object_id: str
    version: str


class SourceSpan(Record):
    """Unicode code-point half-open offsets into the exact immutable node body."""

    node_id: str
    node_version: str
    text_ref: TextRef
    start: int = Field(default=0, ge=0)
    end: int = Field(ge=0)
    passage_start: int | None = Field(default=None, ge=0)
    passage_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_range(self) -> SourceSpan:
        if self.end < self.start or self.end > self.text_ref.length:
            raise ValueError("Source range exceeds its immutable body")
        if (self.passage_start is None) != (self.passage_end is None):
            raise ValueError("Both passage offsets are required")
        if self.passage_start is not None and self.passage_end < self.passage_start:
            raise ValueError("Invalid passage range")
        return self


class WindowState(Record):
    id: str
    label: str = ""
    revision: int = Field(default=0, ge=0)
    entry_node_ids: list[str] = Field(default_factory=list)
    working_node_id: str | None = None
    frontier_node_ids: list[str] = Field(default_factory=list)
    direction: str = ""
    method_ref: ObjectRef | None = None
    seed_text_ref: TextRef | None = None
    status: str = "ready"


class ReadingTrack(Record):
    id: str
    label: str = ""
    revision: int = Field(default=0, ge=0)
    read_seq: int = Field(default=0, ge=0)
    observed_graph_commit: int = Field(default=0, ge=0)
    entry_node_ids: list[str] = Field(default_factory=list)
    cursor_node_id: str | None = None
    bookmark_node_ids: list[str] = Field(default_factory=list)
    direction: str = ""
    method_ref: ObjectRef | None = None
    status: str = "ready"


class GraphState(Record):
    schema_version: Literal[6] = 6
    engine_version: Literal["growth-reading-v1"] = "growth-reading-v1"
    method_language_version: Literal[3] = 3
    run_id: str
    preset_id: str = "custom"
    title: str = ""
    status: str = "created"
    graph_commit: int = Field(default=0, ge=0)
    windows: dict[str, WindowState] = Field(default_factory=dict)
    reading_tracks: dict[str, ReadingTrack] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


GrowthState = GraphState


def canonical_scope(state: GraphState, scope: str) -> str:
    """Normalize known bare scope IDs without accepting another actor's identity."""
    if scope.startswith("window:") or scope.startswith("reading:"):
        return scope
    window, reading = scope in state.windows, scope in state.reading_tracks
    if window and reading:
        raise ValueError("Ambiguous scope requires its window/reading prefix")
    if window:
        return f"window:{scope}"
    if reading:
        return f"reading:{scope}"
    return scope


class NarrativeNode(Record):
    id: str
    version: str = ""
    text_ref: TextRef
    created_commit: int = Field(ge=0)
    scope_id: str
    source_spans: list[SourceSpan] = Field(default_factory=list)
    private_scope: str | None = None


class NarrativeLink(Record):
    id: str
    version: str = ""
    source_id: str
    target_id: str
    kind: Literal["growth", "association"]
    relation: str = ""
    scope_id: str
    created_commit: int = Field(ge=0)
    source_spans: list[SourceSpan] = Field(default_factory=list)
    private_scope: str | None = None


class NodeDraft(Record):
    local_id: str
    text: str
    source_spans: list[SourceSpan] = Field(default_factory=list)
    private_scope: str | None = None


class LinkDraft(Record):
    source_id: str
    target_id: str
    kind: Literal["growth", "association"] = "growth"
    relation: str = ""
    source_spans: list[SourceSpan] = Field(default_factory=list)


class GrowthPatch(Record):
    id: str
    window_id: str
    author_scope: str | None = None
    base_graph_commit: int = Field(ge=0)
    expected_window_revision: int | None = Field(default=None, ge=0)
    nodes: list[NodeDraft] = Field(default_factory=list)
    links: list[LinkDraft] = Field(default_factory=list)
    input_refs: list[ObjectRef] = Field(default_factory=list)
    working_node_id: str | None = None


class GrowthCommit(Record):
    state: GraphState
    local_ids: dict[str, str]
    node_ids: list[str]
    link_ids: list[str]
    idempotent: bool = False


class ReadVisit(Record):
    id: str
    version: str = ""
    track_id: str
    read_seq: int = Field(ge=1)
    node_id: str
    node_version: str
    observed_graph_commit: int = Field(ge=0)
    from_node_id: str | None = None
    via_link_id: str | None = None
    transition: str = ""
    source_spans: list[SourceSpan] = Field(default_factory=list)
    private_scope: str | None = None


class ReadingPassage(Record):
    id: str
    version: str = ""
    track_id: str
    read_seq: int = Field(ge=1)
    text_ref: TextRef
    visit_ids: list[str]
    source_spans: list[SourceSpan] = Field(default_factory=list)
    private_scope: str | None = None


class MethodVersion(Record):
    id: str
    version: str = ""
    scope_id: str
    instruction: str = ""
    program: dict[str, Any] | None = None
    language_version: Literal[3] = 3
    parent_ref: ObjectRef | None = None
    source_refs: list[ObjectRef] = Field(default_factory=list)
    created_commit: int = Field(default=0, ge=0)
    private_scope: str | None = None


class MethodTrial(Record):
    id: str
    version: str = ""
    scope_id: str
    method_ref: ObjectRef
    source_refs: list[ObjectRef] = Field(default_factory=list)
    result_ref: ObjectRef | None = None
    feedback_refs: list[ObjectRef] = Field(default_factory=list)
    evaluation_method_ref: ObjectRef | None = None
    adopted: bool = False
    private_scope: str | None = None


class WorkSession(Record):
    id: str
    version: str = ""
    scope_id: str
    base_graph_commit: int = Field(ge=0)
    local_seq: int = Field(default=0, ge=0)
    current_action: str = "ready"
    seen_page_refs: list[ObjectRef] = Field(default_factory=list)
    feedback_refs: list[ObjectRef] = Field(default_factory=list)
    pending_candidate_ref: ObjectRef | None = None
    status: str = "ready"
    private_scope: str | None = None


class ActionReceipt(Record):
    id: str
    version: str = ""
    session_id: str
    scope_id: str
    action: str
    input_refs: list[ObjectRef] = Field(default_factory=list)
    visible_spans: list[SourceSpan] = Field(default_factory=list)
    result_refs: list[ObjectRef] = Field(default_factory=list)
    omitted_refs: list[ObjectRef] = Field(default_factory=list)
    created_commit: int = Field(default=0, ge=0)
    details: dict[str, Any] = Field(default_factory=dict)
    private_scope: str | None = None


class FeedbackRecord(Record):
    id: str
    version: str = ""
    scope_id: str
    text_ref: TextRef
    target_ref: ObjectRef | None = None
    method_ref: ObjectRef | None = None
    private_scope: str | None = None


class SharedView(Record):
    """Language-three organization stores references, never expanded recursive values."""

    id: str
    version: str = ""
    scope_id: str
    members: list[ObjectRef] = Field(default_factory=list)
    groups: dict[str, list[ObjectRef]] = Field(default_factory=dict)
    relations: list[dict[str, Any]] = Field(default_factory=list)
    occurrences: list[dict[str, Any]] = Field(default_factory=list)
    private_scope: str | None = None
    input_refs: list[ObjectRef] = Field(default_factory=list)
    execution: dict[str, Any] = Field(default_factory=dict)
