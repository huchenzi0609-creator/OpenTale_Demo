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
    if {"inputs", "calls"} & program.keys():
        return _compile_composed(store, state, scope, ref, input_node_ids)
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
    # Preserve the original language-three value/hash for programs without
    # composition. The new optional projection fields have no old semantics.
    saved = store.put(state.run_id, "view", value.model_dump(exclude={"input_refs", "execution"}))
    return SharedView.model_validate(saved)


_INPUT_KINDS = frozenset({"node", "method", "response", "trial", "visit", "passage", "link", "view"})


def _program_refs(value: Any) -> list[ObjectRef]:
    if not isinstance(value, list):
        raise GrowthError("METHOD_PROGRAM", "方法输入必须使用精确引用列表。")
    refs = []
    for item in value:
        try:
            ref = ObjectRef.model_validate(item)
        except (ValueError, TypeError) as exc:
            raise GrowthError("METHOD_PROGRAM", "方法输入引用格式无效。") from exc
        if ref.kind not in _INPUT_KINDS or not ref.object_id or not ref.version:
            raise GrowthError("METHOD_PROGRAM", "方法输入引用的类型或版本无效。")
        refs.append(ref)
    return refs


def _compile_composed(
    store: GrowthStore, state: GraphState, scope: str, root_ref: ObjectRef, input_node_ids: list[str],
) -> SharedView:
    """Execute finite, declarative method composition using an explicit stack.

    A call organizes its exact inputs before its caller finishes organization.
    Non-narrative inputs remain typed references for the next evaluator, not
    invented world nodes. Instructions are referenced and previewed, never
    recursively expanded into a body. Invocation depth describes this program's
    actual structural execution, not a model's cognitive reflection depth.

    Re-entering an exact method on the active path leaves a suspended frame. No
    Python recursion, provider calls, arbitrary expressions or fixed depth cap
    are involved; an evaluator can revise the saved continuation explicitly.
    Identical exact invocations share their completed organization. A dense
    diamond of references therefore stays linear in distinct applications;
    intentional repeated appearances belong in ``occurrences``.
    """
    materials: dict[tuple[str, str, str], ObjectRef] = {}
    members: dict[str, ObjectRef] = {}
    groups: dict[str, list[ObjectRef]] = {}
    relations: list[dict[str, Any]] = []
    occurrences: list[dict[str, Any]] = []
    frames: list[dict[str, Any]] = []
    suspended: list[dict[str, Any]] = []
    instruction_refs: list[dict[str, Any]] = []
    root_outputs: list[ObjectRef] = []
    method_as_data: dict[tuple[str, str, str], ObjectRef] = {}
    active: set[tuple[str, str, str]] = set()
    completed: dict[tuple[tuple[str, str, str], tuple[tuple[str, str, str], ...]], tuple[str, list[ObjectRef]]] = {}
    max_depth = 0

    def key(ref: ObjectRef) -> tuple[str, str, str]:
        return ref.kind, ref.object_id, ref.version

    def visible(ref: ObjectRef) -> dict:
        row = store.get(state.run_id, ref.kind, ref.object_id, version=ref.version,
                        at_commit=state.graph_commit if ref.kind in {"node", "link"} else None,
                        scope_id=scope)
        if row is None:
            raise GrowthError("METHOD_SOURCE_UNAVAILABLE", "方法调用所需的精确材料在此位置不可见。")
        return row

    def node_ref(node_id: str) -> ObjectRef:
        if not isinstance(node_id, str):
            raise GrowthError("METHOD_PROGRAM", "方法选材必须使用节点标识。")
        row = store.get(state.run_id, "node", node_id, at_commit=state.graph_commit, scope_id=scope)
        if row is None:
            raise GrowthError("METHOD_SOURCE_UNAVAILABLE", "方法选中的精确材料不可见。")
        return ObjectRef(kind="node", object_id=node_id, version=row["version"])

    stack: list[dict[str, Any]] = [{"ref": root_ref, "inputs": [node_ref(n) for n in input_node_ids],
                                  "path": "0", "depth": 0, "entered": False}]
    while stack:
        frame = stack[-1]
        if not frame["entered"]:
            method_ref = frame["ref"]
            frame_key = key(method_ref)
            max_depth = max(max_depth, frame["depth"])
            if frame_key in active:
                continuation = {"frame_id": frame["path"], "method_ref": method_ref.model_dump(),
                                "input_refs": [r.model_dump() for r in frame["inputs"]],
                                "depth": frame["depth"], "status": "suspended", "reason": "active_method_cycle"}
                suspended.append(continuation)
                frames.append(continuation)
                stack.pop()
                continue
            method = inspect_method(store, state, scope, method_ref)
            program = method.get("program") or {}
            if not isinstance(program, dict):
                raise GrowthError("METHOD_PROGRAM", "方法组织程序格式无效。")
            actual_inputs = dict.fromkeys(key(r) for r in [*frame["inputs"], *_program_refs(program.get("inputs", []))])
            frame["inputs"] = [ObjectRef(kind=k[0], object_id=k[1], version=k[2]) for k in actual_inputs]
            application_key = (frame_key, tuple(actual_inputs))
            for ref in frame["inputs"]:
                visible(ref)
                materials[key(ref)] = ref
                if ref.kind in {"method", "view"}:
                    method_as_data[key(ref)] = ref
            if application_key in completed:
                previous_id, outputs = completed[application_key]
                frames.append({"frame_id": frame["path"], "method_ref": method_ref.model_dump(),
                               "depth": frame["depth"], "input_refs": [r.model_dump() for r in frame["inputs"]],
                               "output_node_refs": [r.model_dump() for r in outputs],
                               "status": "reused", "reused_frame_id": previous_id})
                stack.pop()
                if stack:
                    stack[-1]["child_output_refs"].update({key(r): r for r in outputs})
                continue
            # Dependencies are validated but remain dependencies. Parent
            # ancestry does not silently execute historical instructions.
            for dependency in method.get("source_refs", []):
                visible(ObjectRef.model_validate(dependency))
            calls = program.get("calls", [])
            if not isinstance(calls, list):
                raise GrowthError("METHOD_PROGRAM", "方法组合调用必须使用列表。")
            validated_calls = []
            for call in calls:
                if (not isinstance(call, dict) or "method_ref" not in call
                        or not call.keys() <= {"method_ref", "input_refs"}):
                    raise GrowthError("METHOD_PROGRAM", "方法组合调用格式无效。")
                callee = _program_refs([call["method_ref"]])[0]
                if callee.kind != "method":
                    raise GrowthError("METHOD_REF_KIND", "组合调用只能使用精确方法版本。")
                call_inputs = _program_refs(call.get("input_refs", []))
                for ref in call_inputs:
                    visible(ref)
                validated_calls.append((callee, call_inputs))
            frame.update(entered=True, method=method, program=program, calls=validated_calls,
                         next_call=0, application_key=application_key, child_output_refs={})
            active.add(frame_key)
            materials[frame_key] = method_ref
        if frame["next_call"] < len(frame["calls"]):
            index = frame["next_call"]
            frame["next_call"] += 1
            callee, call_inputs = frame["calls"][index]
            stack.append({"ref": callee, "inputs": call_inputs, "path": f"{frame['path']}.{index}",
                          "depth": frame["depth"] + 1, "entered": False})
            continue
        program = frame["program"]
        # A completed submethod's node result is an actual input to its caller,
        # not just a diagnostic edge in a version lineage.
        effective_inputs = {key(r): r for r in frame["inputs"]}
        effective_inputs.update(frame["child_output_refs"])
        frame["inputs"] = list(effective_inputs.values())
        selected = program.get("node_ids", [r.object_id for r in frame["inputs"] if r.kind == "node"])
        own_groups, own_relations = program.get("groups", {}), program.get("relations", [])
        if (not isinstance(selected, list) or not isinstance(own_groups, dict)
                or not isinstance(own_relations, list)):
            raise GrowthError("METHOD_PROGRAM", "方法的选材、分组与关系组织格式无效。")
        output_refs: dict[str, ObjectRef] = {}

        def freeze(node_id: str) -> ObjectRef:
            ref = node_ref(node_id)
            members.setdefault(node_id, ref)
            materials[key(ref)] = ref
            output_refs.setdefault(node_id, ref)
            return ref

        for node_id in selected:
            freeze(node_id)
        for label, ids in own_groups.items():
            if not isinstance(label, str) or not isinstance(ids, list):
                raise GrowthError("METHOD_PROGRAM", "方法分组格式无效。")
            group = groups.setdefault(label, [])
            for node_id in ids:
                ref = freeze(node_id)
                if ref not in group:
                    group.append(ref)
        for relation in own_relations:
            if not isinstance(relation, dict):
                raise GrowthError("METHOD_PROGRAM", "方法关系格式无效。")
            source, target = freeze(relation.get("source_id")), freeze(relation.get("target_id"))
            row = {**relation, "source_ref": source.model_dump(), "target_ref": target.model_dump()}
            if row not in relations:
                relations.append(row)
        own_occurrences = program.get("occurrences", [])
        if not isinstance(own_occurrences, list):
            raise GrowthError("METHOD_PROGRAM", "重复出现位置格式无效。")
        for occurrence in own_occurrences:
            if not isinstance(occurrence, dict) or not isinstance(occurrence.get("node_id"), str):
                raise GrowthError("METHOD_PROGRAM", "重复出现位置需要明确的节点引用。")
            occurrences.append({**occurrence, "source_ref": freeze(occurrence["node_id"]).model_dump()})
        method_ref = frame["ref"]
        method = frame["method"]
        instruction_refs.append({"method_ref": method_ref.model_dump(), "instruction_preview": method["instruction"][:180],
                                 "full_length": len(method["instruction"]), "partial": len(method["instruction"]) > 180})
        frames.append({"frame_id": frame["path"], "method_ref": method_ref.model_dump(), "depth": frame["depth"],
                       "input_refs": [r.model_dump() for r in frame["inputs"]],
                       "output_node_refs": [r.model_dump() for r in output_refs.values()], "status": "completed"})
        active.remove(key(method_ref))
        outputs = list(output_refs.values())
        if frame["path"] == "0":
            root_outputs = outputs
        completed[frame["application_key"]] = frame["path"], outputs
        stack.pop()
        if stack:
            stack[-1]["child_output_refs"].update({key(r): r for r in outputs})
    execution = {"root_ref": root_ref.model_dump(), "application_frames": frames, "max_call_depth": max_depth,
                 "root_output_refs": [r.model_dump() for r in root_outputs],
                 "instruction_refs": instruction_refs, "method_as_data_refs": [r.model_dump() for r in method_as_data.values()],
                 "suspended": suspended, "status": "suspended" if suspended else "completed"}
    payload = {"scope": scope, "method": root_ref.model_dump(), "members": [r.model_dump() for r in members.values()],
               "groups": {label: [r.model_dump() for r in refs] for label, refs in groups.items()},
               "relations": relations, "occurrences": occurrences,
               "input_refs": [r.model_dump() for r in materials.values()], "execution": execution}
    value = SharedView(id="view-" + digest(payload)[:24], scope_id=scope, members=list(members.values()), groups=groups,
                       relations=relations, occurrences=occurrences, input_refs=list(materials.values()),
                       execution=execution, private_scope=scope)
    return SharedView.model_validate(store.put(state.run_id, "view", value))


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
