"""Deterministic typed view programs with inspect/apply/revise and no model side effects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generator

from .kernel import KernelError, fingerprint
from .object_store import ObjectResolver
from .policy_types import (
    ApplyStep,
    GroupStep,
    InspectStep,
    ObjectRef,
    QueryStep,
    ReadStep,
    RelateStep,
    ReviseStep,
    View,
    ViewGroup,
    ViewRelation,
)
from .reflection_types import Material, MethodDraft, ObjectVersion
from .retrieval import retrieve


@dataclass
class ViewValue:
    materials: list[Material] = field(default_factory=list)
    view: View = field(default_factory=View)


@dataclass
class MethodValue:
    definition: MethodDraft
    original_id: str


@dataclass
class PolicyExecution:
    materials: list[Material]
    view: View
    instruction: str
    dependencies: list[ObjectRef]
    live_dependencies: list[str]
    generated_methods: list[MethodDraft]
    plan_fingerprint: str
    input_fingerprint: str
    definitions: list[dict]


def material_data(material: Material) -> dict:
    return material.model_dump(exclude={"private_scope"})


def view_data(view: View) -> dict:
    """Canonical structural input independent of opaque execution IDs."""
    aliases: dict[str, str] = {}

    def group_data(group: ViewGroup, path: str) -> dict:
        aliases[group.id] = path
        return {"label": group.label, "material_ids": group.material_ids,
                "children": [group_data(child, f"{path}.{index}") for index, child in enumerate(group.children)]}

    groups = [group_data(group, str(index)) for index, group in enumerate(view.groups)]
    return {"groups": groups, "relations": [
        {"source": aliases.get(edge.source, edge.source), "target": aliases.get(edge.target, edge.target),
         "label": edge.label} for edge in view.relations
    ], "material_ids": view.material_ids}


def _merge(values: list[ViewValue]) -> ViewValue:
    groups, relations = [], []
    for index, value in enumerate(values):
        cloned = value.view.model_copy(deep=True)
        aliases: dict[str, str] = {}
        pending = list(cloned.groups)
        while pending:
            group = pending.pop()
            old_id = group.id
            group.id = f"{index}/{old_id}"
            aliases[old_id] = group.id
            pending.extend(group.children)
        for relation in cloned.relations:
            relation.source = aliases.get(relation.source, relation.source)
            relation.target = aliases.get(relation.target, relation.target)
        groups.extend(cloned.groups)
        relations.extend(cloned.relations)
    return ViewValue(
        materials=[m.model_copy(deep=True) for value in values for m in value.materials],
        view=View(groups=groups, relations=relations,
                  material_ids=[m.id for value in values for m in value.materials]),
    )


def program_references(program) -> list[ObjectRef]:
    """Walk nested revision programs iteratively, including their explicit patch inputs."""
    result: list[ObjectRef] = []
    pending = [program] if program else []
    while pending:
        current = pending.pop()
        for step in current.steps:
            if isinstance(step, ReadStep):
                result.extend(step.refs)
            elif isinstance(step, InspectStep):
                result.append(step.ref)
            elif isinstance(step, QueryStep):
                result.extend(step.related_to)
            elif isinstance(step, ReviseStep):
                result.extend(ObjectRef(object_id=identifier) for identifier in step.patch.inputs or [])
                if step.patch.program:
                    pending.append(step.patch.program)
    return result


class _Executor:
    def __init__(self, resolver: ObjectResolver):
        self.resolver = resolver
        self.dependencies: dict[tuple, ObjectRef] = {}
        self.live_dependencies: set[str] = set()
        self.instructions: list[str] = []
        self.generated: list[MethodDraft] = []
        self.definitions: list[dict] = []

    def resolve(self, ref: ObjectRef) -> dict:
        row, frozen = self.resolver.resolve(ref)
        if ref.mode == "live":
            self.live_dependencies.add(ref.object_id)
        self.dependencies[(frozen.object_id, frozen.version, frozen.start, frozen.end)] = frozen
        return row

    def row_value(self, row: dict, ref: ObjectRef | None = None) -> ViewValue:
        if row["kind"] in {"choice", "view"}:
            materials = [Material.model_validate(item) for item in row.get("materials", [])]
            value = ViewValue(materials, View.model_validate(row["view"]) if row.get("view") else View(
                groups=[ViewGroup(id=f"{row['id']}:group:{i}", material_ids=list(group))
                        for i, group in enumerate(row.get("groups", [[m.id for m in materials]]))],
                material_ids=[m.id for m in materials],
            ))
            if ref and (ref.start is not None or ref.end is not None):
                raise KernelError("METHOD_PROGRAM_INVALID", "结构化视图须完整读取；段落范围仅适用于文本对象。")
            return value
        text = row.get("text", "")
        if ref:
            text = text[ref.start or 0:ref.end]
        role = row.get("role", {"method": "方法定义", "response": "局部回应", "effect": "组织变化记录"}.get(row["kind"], "经历"))
        identifier = row["id"]
        # Ranges are distinct view leaves; dependency still addresses the original immutable object.
        if ref and (ref.start is not None or ref.end is not None):
            identifier = f"{identifier}@{row['version']}[{ref.start or 0}:{ref.end if ref.end is not None else ''}]"
        material = Material(id=identifier, text=text, position_id=row.get("position_id"),
                            expression_id=row.get("expression_id"), role=role,
                            private_scope=row.get("private_scope"))
        return ViewValue([material], View(groups=[ViewGroup(id=identifier, material_ids=[identifier])],
                                          material_ids=[identifier]))

    def read(self, refs: list[ObjectRef]) -> ViewValue:
        return _merge([self.row_value(self.resolve(ref), ref) for ref in refs])

    def definition(self, row: dict) -> MethodDraft:
        if row.get("kind") != "method":
            raise KernelError("METHOD_PROGRAM_INVALID", "检查与应用只能操作方法定义。")
        return MethodDraft.model_validate({key: row[key] for key in MethodDraft.model_fields if key in row})

    def method(self, value: MethodValue) -> Generator[MethodValue, ViewValue, ViewValue]:
        method = value.definition
        self.definitions.append({"object_id": value.original_id, "operation": "apply", **method.model_dump()})
        if method.instruction and method.instruction not in self.instructions:
            self.instructions.append(method.instruction)
        if method.language_version == 1:
            parts = []
            for identifier in method.inputs:
                ref = ObjectRef(object_id=identifier)
                row = self.resolve(ref)
                if row["kind"] == "method":
                    parts.append((yield MethodValue(self.definition(row), row["id"])))
                else:
                    parts.append(self.row_value(row, ref))
            # Explicit v1 adapter: legacy order/take operate on material leaves.
            materials = [m for part in parts for m in part.materials]
            if method.order == "reverse":
                materials.reverse()
            if method.limit is not None:
                materials = materials[:method.limit]
            return ViewValue(materials, View(groups=[ViewGroup(id=value.original_id,
                                                              material_ids=[m.id for m in materials])],
                                             material_ids=[m.id for m in materials]))
        assert method.program is not None
        environment: dict[str, ViewValue | MethodValue] = {}
        for step in method.program.steps:
            if isinstance(step, ReadStep):
                environment[step.id] = self.read(step.refs)
            elif isinstance(step, QueryStep):
                for ref in step.related_to:
                    self.resolve(ref)
                ids = retrieve(self.resolver.objects, step.query, kinds=step.kinds,
                               related_to=step.related_to, limit=step.limit)
                environment[step.id] = self.read([ObjectRef(object_id=identifier) for identifier in ids])
            elif isinstance(step, (GroupStep, RelateStep)):
                values = [environment[identifier] for identifier in step.inputs]
                if not all(isinstance(item, ViewValue) for item in values):
                    raise KernelError("METHOD_PROGRAM_INVALID", "组合操作只能使用视图。")
                merged = _merge(values)
                if isinstance(step, GroupStep):
                    # An empty single-input wrapper has no semantic structural effect.
                    if step.label or len(values) > 1:
                        merged.view.groups = [ViewGroup(id=f"{value.original_id}:{step.id}", label=step.label,
                                                        children=merged.view.groups)]
                else:
                    groups = []
                    offset = 0
                    for identifier, child in zip(step.inputs, values, strict=True):
                        width = len(child.view.groups)
                        groups.append(ViewGroup(id=f"{value.original_id}:{step.id}:{identifier}",
                                                children=merged.view.groups[offset:offset + width]))
                        offset += width
                    merged.view.groups = groups
                    merged.view.relations.extend(ViewRelation(
                        source=f"{value.original_id}:{step.id}:{edge.source}",
                        target=f"{value.original_id}:{step.id}:{edge.target}", label=edge.label,
                    ) for edge in step.relations)
                environment[step.id] = merged
            elif isinstance(step, InspectStep):
                row = self.resolve(step.ref)
                definition = self.definition(row)
                self.definitions.append({"object_id": row["id"], "operation": "inspect", **definition.model_dump()})
                environment[step.id] = MethodValue(definition, row["id"])
            elif isinstance(step, ReviseStep):
                original = environment[step.input]
                if not isinstance(original, MethodValue):
                    raise KernelError("METHOD_PROGRAM_INVALID", "改版需要已检查的方法。")
                patch = {key: item for key, item in step.patch.model_dump(exclude_unset=True).items()
                         if item is not None or key == "limit"}
                data = original.definition.model_dump()
                data.update(patch)
                data["revises"] = original.original_id
                if step.patch.program is not None:
                    data["language_version"] = 2
                draft = MethodDraft.model_validate(data)
                if draft not in self.generated:
                    self.generated.append(draft)
                environment[step.id] = MethodValue(draft, f"derived-{fingerprint(draft.model_dump())[:24]}")
            elif isinstance(step, ApplyStep):
                inspected = environment[step.method]
                if not isinstance(inspected, MethodValue):
                    raise KernelError("METHOD_PROGRAM_INVALID", "应用需要已检查或改版的方法。")
                environment[step.id] = yield inspected
        output = environment[method.program.output]
        assert isinstance(output, ViewValue)
        return output

    def run(self, root: MethodValue) -> ViewValue:
        """Trampoline nested applications: no Python call-stack ceiling or arbitrary depth cap."""
        stack: list[tuple[Generator, str]] = []
        active: set[str] = set()
        current = root
        result = None
        while True:
            if current is not None:
                key = fingerprint(current.definition.model_dump())
                if key in active:
                    raise KernelError("METHOD_CYCLE", "方法应用形成了无法有限求值的循环。")
                active.add(key)
                stack.append((self.method(current), key))
                current = None
                result = None
            generator, key = stack[-1]
            try:
                current = generator.send(result)
                result = None
            except StopIteration as completed:
                result = completed.value
                stack.pop()
                active.remove(key)
                if not stack:
                    return result


def execute_policy(
    objects: dict[str, dict], versions: list[ObjectVersion], scope: str, methods: list[str],
    *, default_ids: list[str] | None = None,
) -> PolicyExecution:
    executor = _Executor(ObjectResolver(objects, versions, scope))
    if methods:
        values = []
        for identifier in sorted(methods):
            row = executor.resolve(ObjectRef(object_id=identifier))
            values.append(executor.run(MethodValue(executor.definition(row), identifier)))
        value = _merge(values)
    else:
        value = executor.read([ObjectRef(object_id=identifier) for identifier in default_ids or []
                               if identifier in objects])
    instruction = "\n".join(executor.instructions)
    dependencies = sorted(executor.dependencies.values(), key=lambda ref: (
        ref.object_id, ref.version or "", ref.start if ref.start is not None else -1,
        ref.end if ref.end is not None else -1,
    ))
    return PolicyExecution(
        materials=value.materials, view=value.view, instruction=instruction, dependencies=dependencies,
        live_dependencies=sorted(executor.live_dependencies),
        generated_methods=executor.generated, definitions=executor.definitions,
        plan_fingerprint=fingerprint({"methods": executor.definitions,
                                      "dependencies": [ref.model_dump() for ref in dependencies]}),
        input_fingerprint=fingerprint({"materials": [material_data(m) for m in value.materials],
                                       "view": view_data(value.view), "instruction": instruction}),
    )
