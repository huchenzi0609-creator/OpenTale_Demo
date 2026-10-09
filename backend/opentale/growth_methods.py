"""Reference-sharing organization for method language three; no arbitrary code execution."""

from __future__ import annotations

from typing import Any

from .growth_kernel import GrowthError
from .growth_store import GrowthStore, digest
from .growth_types import GraphState, MethodVersion, ObjectRef, SharedView, canonical_scope


def inspect_method(
    store: GrowthStore, state: GraphState, scope: str, method_ref: ObjectRef | dict,
) -> dict[str, Any]:
    ref = ObjectRef.model_validate(method_ref)
    scope = canonical_scope(state, scope)
    if ref.kind != "method":
        raise GrowthError("METHOD_REF_KIND", "此引用不是方法版本。")
    method = store.get(state.run_id, "method", ref.object_id, version=ref.version, scope_id=scope)
    if method is None or method.get("language_version", 3) != 3:
        raise GrowthError("METHOD_UNAVAILABLE", "方法精确版本在此位置不可见。")
    return method


def compile_method(
    store: GrowthStore, state: GraphState, scope: str,
    method_ref: ObjectRef | dict, input_node_ids: list[str],
) -> SharedView:
    """Freeze and share selected nodes, groups, and relations without copying bodies.

    Program vocabulary: node_ids (selection/order), groups {label:[node_ids]},
    relations [{source_id,target_id,relation,...}]. Repeated IDs merge by default.
    Relations only add structure. Exact method inputs are separately retained in
    MethodVersion.source_refs and can themselves be method or feedback objects.
    """
    ref = ObjectRef.model_validate(method_ref)
    scope = canonical_scope(state, scope)
    method = inspect_method(store, state, scope, ref)
    program = method.get("program") or {}
    selected = program.get("node_ids", input_node_ids)
    groups = program.get("groups", {})
    relations = program.get("relations", [])
    if not isinstance(selected, list) or not all(isinstance(node, str) for node in selected):
        raise GrowthError("METHOD_PROGRAM", "方法选材必须使用节点标识列表。")
    if not isinstance(groups, dict) or not isinstance(relations, list):
        raise GrowthError("METHOD_PROGRAM", "方法的分组与关系组织格式无效。")
    members: dict[str, ObjectRef] = {}

    def freeze(node_id: str) -> ObjectRef:
        if not isinstance(node_id, str):
            raise GrowthError("METHOD_PROGRAM", "方法选材必须使用节点标识。")
        if node_id not in members:
            node = store.get(state.run_id, "node", node_id, at_commit=state.graph_commit, scope_id=scope)
            if node is None:
                raise GrowthError("METHOD_SOURCE_UNAVAILABLE", "方法选中的精确材料不可见。")
            members[node_id] = ObjectRef(kind="node", object_id=node_id, version=node["version"])
        return members[node_id]

    for node_id in selected:
        freeze(node_id)
    frozen_groups = {}
    for label, ids in groups.items():
        if not isinstance(label, str) or not isinstance(ids, list):
            raise GrowthError("METHOD_PROGRAM", "方法分组格式无效。")
        frozen_groups[label] = [freeze(node_id) for node_id in dict.fromkeys(ids)]
    frozen_relations = []
    for relation in relations:
        if not isinstance(relation, dict):
            raise GrowthError("METHOD_PROGRAM", "方法关系格式无效。")
        source_id, target_id = relation.get("source_id"), relation.get("target_id")
        source, target = freeze(source_id), freeze(target_id)
        row = dict(relation)
        row["source_ref"], row["target_ref"] = source.model_dump(), target.model_dump()
        frozen_relations.append(row)
    frozen_occurrences = []
    for occurrence in program.get("occurrences", []):
        if not isinstance(occurrence, dict) or not isinstance(occurrence.get("node_id"), str):
            raise GrowthError("METHOD_PROGRAM", "重复出现位置需要明确的节点引用。")
        frozen_occurrences.append({**occurrence, "source_ref": freeze(occurrence["node_id"]).model_dump()})
    for source in method.get("source_refs", []):
        source_ref = ObjectRef.model_validate(source)
        if store.get(state.run_id, source_ref.kind, source_ref.object_id, version=source_ref.version,
                     scope_id=scope) is None:
            raise GrowthError("METHOD_SOURCE_UNAVAILABLE", "方法定义依赖的精确对象不可见。")
    value = SharedView(id="view-" + digest({"scope": scope, "method": ref.model_dump(),
                       "members": [member.model_dump() for member in members.values()],
                       "groups": {label: [member.model_dump() for member in rows]
                                  for label, rows in frozen_groups.items()}, "relations": frozen_relations,
                       "occurrences": frozen_occurrences})[:24],
                       scope_id=scope, members=list(members.values()), groups=frozen_groups,
                       relations=frozen_relations, occurrences=frozen_occurrences, private_scope=scope)
    saved = store.put(state.run_id, "view", value)
    return SharedView.model_validate(saved)


def revise_method(
    store: GrowthStore, state: GraphState, scope: str, method_ref: ObjectRef | dict, *,
    instruction: str | None = None, program: dict[str, Any] | None = None,
    source_refs: list[ObjectRef | dict] | None = None,
) -> ObjectRef:
    """Create a new immutable version under the same logical method ID."""
    parent = ObjectRef.model_validate(method_ref)
    scope = canonical_scope(state, scope)
    old = inspect_method(store, state, scope, parent)
    if old["scope_id"] != scope:
        raise GrowthError("METHOD_OWNER", "不能修改其他工作位置的方法。")
    old.pop("version", None)
    old["parent_ref"] = parent.model_dump()
    if instruction is not None:
        old["instruction"] = instruction
    if program is not None:
        old["program"] = program
    if source_refs is not None:
        old["source_refs"] = [ObjectRef.model_validate(ref).model_dump() for ref in source_refs]
    old["created_commit"] = state.graph_commit
    saved = store.put(state.run_id, "method", MethodVersion.model_validate(old))
    return ObjectRef(kind="method", object_id=saved["id"], version=saved["version"])


def adopt_method(
    store: GrowthStore, state: GraphState, scope: str, method_ref: ObjectRef | dict, *,
    expected_revision: int | None = None,
) -> GraphState:
    ref = ObjectRef.model_validate(method_ref)
    with store.transaction():
        current = store.load(state.run_id)
        if current is None:
            raise GrowthError("RUN_NOT_FOUND", "工作记录不存在。")
        inspect_method(store, current, scope, ref)
        scope = canonical_scope(current, scope)
        position = (current.windows.get(scope.removeprefix("window:")) if scope.startswith("window:")
                    else current.reading_tracks.get(scope.removeprefix("reading:")))
        if position is None:
            raise GrowthError("SCOPE_NOT_FOUND", "工作位置不存在。")
        if expected_revision is not None and position.revision != expected_revision:
            raise GrowthError("SCOPE_CONFLICT", "工作位置已改变。")
        position.method_ref = ref
        position.revision += 1
        store.save(current)
        store.record_event(state.run_id, "adopt_method", {"scope_id": scope, "method_ref": ref.model_dump()})
        return current
