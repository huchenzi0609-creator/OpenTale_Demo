"""Bounded declarative methods, executable choices and request-owned projections."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .policy_types import ObjectRef, PolicyProgram, View


class ReflectionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MethodDraft(ReflectionRecord):
    title: str = Field(min_length=1, max_length=100)
    instruction: str = Field(default="", max_length=1200)
    inputs: list[str] = Field(default_factory=list)
    language_version: Literal[1, 2] = 1
    program: PolicyProgram | None = None
    order: Literal["listed", "reverse"] = "listed"
    limit: int | None = Field(default=16, ge=1)
    revises: str | None = None

    @model_validator(mode="after")
    def language_matches(self):
        if self.language_version == 1 and (not self.inputs or self.program is not None):
            raise ValueError("Version 1 methods require inputs and cannot contain a program")
        if self.language_version == 2 and self.program is None:
            raise ValueError("Version 2 methods require a program")
        return self


class MethodRecord(MethodDraft):
    id: str
    created_revision: int
    source_scope: str


class AdoptionDraft(ReflectionRecord):
    scope_id: str
    method_id: str
    action: Literal["adopt", "release"] = "adopt"


class ResponseDraft(ReflectionRecord):
    text: str = Field(min_length=1)
    about: list[str] = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    visibility: Literal["scope", "shared"] = "scope"


class ResponseRecord(ResponseDraft):
    id: str
    created_revision: int
    source_scope: str


class ObjectVersion(ReflectionRecord):
    object_id: str
    version: str
    value: dict[str, Any]
    private_scope: str | None = None


class ViewRecord(ReflectionRecord):
    id: str
    created_revision: int
    scope_id: str
    view: View
    materials: list["Material"] = Field(default_factory=list)
    dependencies: list[ObjectRef] = Field(default_factory=list)
    live_dependencies: list[str] = Field(default_factory=list)


class EffectRecord(ReflectionRecord):
    id: str
    revision: int
    scope_id: str
    stage: str
    choice_id: str
    previous_choice_id: str | None = None
    method_ids: list[str]
    before_fingerprint: str | None = None
    after_fingerprint: str
    changed: bool
    selected_added: list[str] = Field(default_factory=list)
    selected_removed: list[str] = Field(default_factory=list)
    response_ids: list[str] = Field(default_factory=list)


class ReflectionProposal(ReflectionRecord):
    methods: list[MethodDraft] = Field(default_factory=list)
    adoptions: list[AdoptionDraft] = Field(default_factory=list)
    responses: list[ResponseDraft] = Field(default_factory=list)


class Material(ReflectionRecord):
    id: str
    text: str
    position_id: str | None = None
    expression_id: str | None = None
    role: str = "经历"
    private_scope: str | None = None


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
    view: View | None = None
    plan_fingerprint: str = ""
    dependencies: list[ObjectRef] = Field(default_factory=list)
    live_dependencies: list[str] = Field(default_factory=list)


class ReflectionState(ReflectionRecord):
    methods: list[MethodRecord] = Field(default_factory=list)
    adoptions: dict[str, list[str]] = Field(default_factory=dict)
    choices: list[ChoiceRecord] = Field(default_factory=list)
    object_versions: list[ObjectVersion] = Field(default_factory=list)
    views: list[ViewRecord] = Field(default_factory=list)
    responses: list[ResponseRecord] = Field(default_factory=list)
    effects: list[EffectRecord] = Field(default_factory=list)


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
    language_version: Literal[1, 2] = 1
    view: View | None = None
    plan_fingerprint: str = ""
    dependencies: list[ObjectRef] = Field(default_factory=list)
    live_dependencies: list[str] = Field(default_factory=list)
    full_objects: list[dict[str, Any]] = Field(default_factory=list)
    definitions: list[dict[str, Any]] = Field(default_factory=list)


ViewRecord.model_rebuild()
