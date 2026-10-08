"""Small, versioned data language for reflective organization (never Python code)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


class PolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ObjectRef(PolicyRecord):
    object_id: str = Field(min_length=1)
    mode: Literal["live", "version"] = "live"
    version: str | None = None
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_range(self):
        if self.mode == "version" and not self.version:
            raise ValueError("A version reference requires its exact version")
        if self.end is not None and self.end < (self.start or 0):
            raise ValueError("Object range end precedes start")
        return self


class ViewGroup(PolicyRecord):
    id: str
    label: str = ""
    material_ids: list[str] = Field(default_factory=list)
    children: list[ViewGroup] = Field(default_factory=list)


class ViewRelation(PolicyRecord):
    source: str
    target: str
    label: str = Field(min_length=1)


class View(PolicyRecord):
    groups: list[ViewGroup] = Field(default_factory=list)
    relations: list[ViewRelation] = Field(default_factory=list)
    material_ids: list[str] = Field(default_factory=list)


class MethodPatch(PolicyRecord):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    instruction: str | None = Field(default=None, max_length=1200)
    inputs: list[str] | None = None
    order: Literal["listed", "reverse"] | None = None
    limit: int | None = Field(default=None, ge=1)
    program: PolicyProgram | None = None

    @model_serializer(mode="wrap")
    def preserve_patch_presence(self, handler):
        # Missing means preserve; explicit limit=null means remove the selection limit.
        # This distinction must survive snapshots and method-definition inspection.
        return {key: value for key, value in handler(self).items() if key in self.model_fields_set}


class ReadStep(PolicyRecord):
    id: str = Field(min_length=1)
    op: Literal["read"] = "read"
    refs: list[ObjectRef] = Field(min_length=1)


class QueryStep(PolicyRecord):
    id: str = Field(min_length=1)
    op: Literal["query"] = "query"
    query: str = Field(min_length=1)
    kinds: list[Literal["content", "method", "choice", "view", "response", "effect"]] = Field(default_factory=list)
    related_to: list[ObjectRef] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1)


class GroupStep(PolicyRecord):
    id: str = Field(min_length=1)
    op: Literal["group"] = "group"
    inputs: list[str] = Field(min_length=1)
    label: str = ""


class RelateStep(PolicyRecord):
    id: str = Field(min_length=1)
    op: Literal["relate"] = "relate"
    inputs: list[str] = Field(min_length=1)
    relations: list[ViewRelation] = Field(min_length=1)


class InspectStep(PolicyRecord):
    id: str = Field(min_length=1)
    op: Literal["inspect"] = "inspect"
    ref: ObjectRef


class ReviseStep(PolicyRecord):
    id: str = Field(min_length=1)
    op: Literal["revise"] = "revise"
    input: str
    patch: MethodPatch


class ApplyStep(PolicyRecord):
    id: str = Field(min_length=1)
    op: Literal["apply"] = "apply"
    method: str


PolicyStep = Annotated[
    ReadStep | QueryStep | GroupStep | RelateStep | InspectStep | ReviseStep | ApplyStep,
    Field(discriminator="op"),
]


class PolicyProgram(PolicyRecord):
    language_version: Literal[2] = 2
    steps: list[PolicyStep] = Field(min_length=1)
    output: str

    @model_validator(mode="after")
    def ordered_typed_steps(self):
        kinds: dict[str, str] = {}
        for step in self.steps:
            if step.id in kinds:
                raise ValueError("Program step identifiers must be unique")
            if isinstance(step, (GroupStep, RelateStep)):
                if any(kinds.get(ref) != "view" for ref in step.inputs):
                    raise ValueError("View composition requires preceding view steps")
                if isinstance(step, RelateStep) and any(
                    relation.source not in step.inputs or relation.target not in step.inputs
                    for relation in step.relations
                ):
                    raise ValueError("Relations must connect this step's input views")
            if isinstance(step, ReviseStep) and kinds.get(step.input) != "method":
                raise ValueError("Revise requires a preceding inspected or revised method")
            if isinstance(step, ApplyStep) and kinds.get(step.method) != "method":
                raise ValueError("Apply requires a preceding inspected or revised method")
            kinds[step.id] = "method" if isinstance(step, (InspectStep, ReviseStep)) else "view"
        if kinds.get(self.output) != "view":
            raise ValueError("An adopted program must return a view")
        return self


MethodPatch.model_rebuild()
