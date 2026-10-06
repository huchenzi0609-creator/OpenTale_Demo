"""Bounded declarative methods, executable choices and request-owned projections."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ReflectionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MethodDraft(ReflectionRecord):
    title: str = Field(min_length=1, max_length=100)
    instruction: str = Field(default="", max_length=1200)
    inputs: list[str] = Field(min_length=1)
    order: Literal["listed", "reverse"] = "listed"
    limit: int | None = Field(default=16, ge=1)
    revises: str | None = None


class MethodRecord(MethodDraft):
    id: str
    created_revision: int
    source_scope: str


class AdoptionDraft(ReflectionRecord):
    scope_id: str
    method_id: str
    action: Literal["adopt", "release"] = "adopt"


class ReflectionProposal(ReflectionRecord):
    methods: list[MethodDraft] = Field(default_factory=list)
    adoptions: list[AdoptionDraft] = Field(default_factory=list)


class Material(ReflectionRecord):
    id: str
    text: str
    position_id: str | None = None
    expression_id: str | None = None
    role: str = "经历"


class ChoiceRecord(ReflectionRecord):
    id: str
    revision: int
    scope_id: str
    stage: Literal["propose", "realize", "present"]
    offered: list[str]
    selected: list[str]
    methods: list[str]
    groups: list[list[str]]
    materials: list[Material]
    input_fingerprint: str


class ReflectionState(ReflectionRecord):
    methods: list[MethodRecord] = Field(default_factory=list)
    adoptions: dict[str, list[str]] = Field(default_factory=dict)
    choices: list[ChoiceRecord] = Field(default_factory=list)


class ReflectionFrame(ReflectionRecord):
    # Objects and scopes are a finite, request-owned capability table. No creator provenance.
    objects: list[dict[str, Any]] = Field(default_factory=list)
    scopes: list[dict[str, str]] = Field(default_factory=list)
    active_methods: list[str] = Field(default_factory=list)
    selected_inputs: list[str] = Field(default_factory=list)
    groups: list[list[str]] = Field(default_factory=list)
    materials: list[Material] = Field(default_factory=list)
    instruction: str = ""
    input_fingerprint: str = ""
