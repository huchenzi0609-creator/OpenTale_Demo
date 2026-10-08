"""Finite, source-blind method execution. No model, code evaluation or truth adjudication."""

from __future__ import annotations

from collections import defaultdict

from .domain import GenerationFrame, ReadingFrame, Snapshot
from .kernel import KernelError, fingerprint
from .reflection_types import (
    ChoiceRecord,
    EffectRecord,
    Material,
    MethodRecord,
    ReflectionFrame,
    ReflectionProposal,
    ResponseRecord,
    ViewRecord,
)


def position_scope(position_id: str) -> str:
    return f"position:{position_id}"


def reading_scope(reading_id: str) -> str:
    return f"reading:{reading_id}"


def _material_visible(material: Material, scope: str) -> bool:
    if material.private_scope and material.private_scope != scope:
        return False
    return material.role != "主体背景" or scope == position_scope(material.position_id or "")


def legacy_reflection_data(state) -> dict:
    """The exact v1 wire/hash field set; added model defaults are not historical evidence."""
    return {
        "methods": [method.model_dump(exclude={"language_version", "program"}) for method in state.methods],
        "adoptions": state.adoptions,
        "choices": [choice.model_dump(exclude={"view", "plan_fingerprint", "dependencies", "live_dependencies"},
                                      exclude_none=False) | {
            "materials": [material.model_dump(exclude={"private_scope"}) for material in choice.materials],
        } for choice in state.choices],
    }


def _objects_v1(state: Snapshot, scope: str, *, include_methods: bool = True) -> dict[str, dict]:
    """Expose reusable fiction/methods, never private peer profiles or creator metadata."""
    assert state.reflection is not None
    material_exclude = set() if _is_v2(state) else {"private_scope"}
    expressions = {expression.id: expression for expression in state.expressions}
    positions_by_expression: dict[str, set[str]] = defaultdict(set)
    for use in state.uses:
        positions_by_expression[use.expression_id].add(use.position_id)
    objects: dict[str, dict] = {}
    for use in sorted(state.uses, key=lambda item: item.id):
        if (scope != position_scope(use.position_id)
                and (use.role == "主体背景" or len(positions_by_expression[use.expression_id]) < 2)):
            continue
        values = sorted({binding.value for binding in use.bindings})
        text = "\n".join(values)
        if use.role != "主体背景":
            origin = expressions[use.expression_id].text
            text = "\n".join([origin, *(value for value in values if value != origin)])
        material = Material(
            id=use.id, text=text, position_id=use.position_id,
            expression_id=use.expression_id, role=use.role,
        )
        objects[material.id] = {"id": material.id, "kind": "content", **material.model_dump(exclude=material_exclude)}
    for expression in state.expressions:
        if expression.created_revision == 0:
            continue  # Initial biographies are only exposed through their own scoped use.
        material = Material(id=expression.id, text=expression.text, expression_id=expression.id,
                            role="已形成的叙述")
        objects[material.id] = {"kind": "content", **material.model_dump(exclude=material_exclude)}
    for continuation in state.continuations:
        if continuation.surface:
            material = Material(
                id=f"surface:{continuation.position_id}", text=continuation.surface,
                position_id=continuation.position_id, role="当前生活",
            )
            objects[material.id] = {"kind": "content", **material.model_dump(exclude=material_exclude)}
    for choice in sorted(state.reflection.choices, key=lambda item: (item.revision, item.id)):
        # Only adopted world materials are exported, never a reading's private prose history.
        materials = [material for material in choice.materials if _material_visible(material, scope)]
        if not materials:
            continue
        objects[choice.id] = {
            "id": choice.id, "kind": "choice", "text": "\n".join(m.text for m in materials),
            "selected": [material.id for material in materials],
            "materials": [material.model_dump(exclude=material_exclude) for material in materials],
            "groups": [[ref for ref in group if ref in {m.id for m in materials}]
                       for group in choice.groups],
            "offered": choice.offered,
            "offered_count": len(choice.offered), "selected_count": len(materials),
        }
    # Prior-version references form a DAG; a method is visible only when all inputs are visible.
    unresolved = list(state.reflection.methods) if include_methods else []
    while unresolved:
        newly_visible = [method for method in unresolved if all(ref in objects for ref in method.inputs)]
        if not newly_visible:
            break
        for method in newly_visible:
            objects[method.id] = {
                "id": method.id, "kind": "method", "text": method.instruction or method.title,
                **method.model_dump(exclude={"source_scope", "created_revision", "language_version", "program"}),
            }
        accepted = {method.id for method in newly_visible}
        unresolved = [method for method in unresolved if method.id not in accepted]
    return objects



def _is_v2(state: Snapshot) -> bool:
    return state.run.mode == "reflexive_v2"


def _method_refs(method) -> list:
    from .policy_compiler import program_references
    from .policy_types import ObjectRef

    return ([ObjectRef(object_id=ref) for ref in method.inputs]
            + ([ObjectRef(object_id=method.revises)] if method.revises else [])
            + program_references(method.program))


def objects_for(state: Snapshot, scope: str) -> dict[str, dict]:
    if not _is_v2(state):
        return _objects_v1(state, scope)
    from .object_store import ObjectResolver, versioned_object

    assert state.reflection is not None
    objects = _objects_v1(state, scope, include_methods=False)
    # A reading can retrieve its complete earlier prose without publishing another reading's history.
    for track in state.reading_tracks:
        if scope != reading_scope(track.id):
            continue
        for segment in track.story:
            identifier = f"reading-segment:{track.id}:{segment.id}"
            objects[identifier] = {
                "id": identifier, "kind": "content", "text": segment.text,
                "position_id": segment.focus_position_id, "role": "本条阅读",
                "private_scope": scope, "evidence": list(segment.expression_ids),
            }
    uses = {use.id: use for use in state.uses}
    expressions_in_positions: dict[str, set[str]] = defaultdict(set)
    for use in state.uses:
        expressions_in_positions[use.expression_id].add(use.position_id)
    for row in objects.values():
        use = uses.get(row["id"])
        if use and (use.role == "主体背景" or len(expressions_in_positions[use.expression_id]) < 2):
            row["private_scope"] = position_scope(use.position_id)
    # A structured historical view is either wholly visible or withheld; labels/relations can be private.
    choice_resolver = ObjectResolver(objects, state.reflection.object_versions, scope)
    for choice in state.reflection.choices:
        if choice.view is None:
            continue
        if not all(_material_visible(material, scope) for material in choice.materials):
            objects.pop(choice.id, None)
            continue
        if not choice.materials:
            # Empty selected input is still a real choice, with inspect/query dependencies.
            objects[choice.id] = {
                "id": choice.id, "kind": "choice", "text": "", "selected": [], "materials": [],
                "groups": choice.groups, "offered": choice.offered,
                "offered_count": len(choice.offered), "selected_count": 0,
            }
        if choice.id in objects:
            # Inspecting private definitions can shape labels even when output leaves are public.
            # Keep the whole structural view private according to every actual dependency.
            try:
                dependencies = [choice_resolver.resolve(ref)[0] for ref in choice.dependencies]
            except KernelError:
                objects.pop(choice.id, None)
                continue
            objects[choice.id]["view"] = choice.view.model_dump()
            objects[choice.id]["private_scope"] = next(
                (row.get("private_scope") for row in dependencies if row.get("private_scope")),
                next((material.private_scope for material in choice.materials if material.private_scope), None),
            )
    pending = [("method", method) for method in state.reflection.methods]
    pending += [("response", response) for response in state.reflection.responses]
    pending += [("view", view) for view in state.reflection.views]
    pending += [("effect", effect) for effect in state.reflection.effects]
    while pending:
        resolver = ObjectResolver(objects, state.reflection.object_versions, scope)
        accepted = []
        for kind, record in pending:
            if kind == "method":
                refs = _method_refs(record)
            elif kind == "response":
                from .policy_types import ObjectRef

                if record.visibility == "scope" and record.source_scope != scope:
                    continue
                refs = [ObjectRef(object_id=ref) for ref in record.about + record.evidence]
            elif kind == "effect":
                from .policy_types import ObjectRef

                # An old effect lacking its previous choice cannot establish removal privacy.
                if record.before_fingerprint is not None and record.previous_choice_id is None:
                    continue
                choice_ids = [record.choice_id] + ([record.previous_choice_id] if record.previous_choice_id else [])
                if any(objects.get(identifier, {}).get("kind") != "choice"
                       or objects[identifier].get("view") is None for identifier in choice_ids):
                    continue
                refs = [ObjectRef(object_id=identifier) for identifier in choice_ids]
            else:
                if not all(_material_visible(material, scope) for material in record.materials):
                    continue
                refs = record.dependencies
            try:
                resolved = [resolver.resolve(ref)[0] for ref in refs]
            except KernelError:
                continue
            private = next((obj.get("private_scope") for obj in resolved if obj.get("private_scope")), None)
            if kind == "method":
                row = {"id": record.id, "kind": kind, "text": record.instruction or record.title,
                       **record.model_dump(exclude={"source_scope", "created_revision"})}
            elif kind == "response":
                private = record.source_scope if record.visibility == "scope" else private
                row = {"id": record.id, "kind": kind, "text": record.text,
                       "about": record.about, "evidence": record.evidence, "visibility": record.visibility}
            elif kind == "effect":
                outcome = ("这是首次记录的组织输入。" if record.previous_choice_id is None else
                           "实际组织输入发生了变化。" if record.changed else "实际组织输入保持一致。")
                text = (f"{outcome}本次采用{len(objects[record.choice_id].get('materials', []))}项材料；"
                        f"新增{len(record.selected_added)}项材料引用，移出{len(record.selected_removed)}项材料引用。"
                        "这份记录描述组织变化，其叙事意义仍可由各处继续理解。")
                row = {"id": record.id, "kind": kind, "text": text, "changed": record.changed,
                       "stage": record.stage, "choice_id": record.choice_id,
                       "previous_choice_id": record.previous_choice_id, "method_ids": record.method_ids,
                       "selected_added": record.selected_added, "selected_removed": record.selected_removed,
                       "response_ids": record.response_ids, "about": choice_ids, "evidence": choice_ids}
            else:
                row = {"id": record.id, "kind": kind, "text": "\n".join(m.text for m in record.materials),
                       "materials": [m.model_dump() for m in record.materials], "view": record.view.model_dump(),
                       "selected": record.view.material_ids,
                       "dependencies": [ref.model_dump() for ref in record.dependencies]}
            row["private_scope"] = private
            objects[record.id] = row
            accepted.append(record.id)
        if not accepted:
            break
        pending = [(kind, record) for kind, record in pending if record.id not in accepted]
    return {identifier: versioned_object(row) for identifier, row in objects.items()}

def _execute(objects: dict[str, dict], methods: list[str]) -> tuple[list[Material], list[list[str]], str]:
    instructions: list[str] = []
    seen_instructions: set[str] = set()
    memo: dict[str, list[Material]] = {}

    def expand(root: str) -> list[Material]:
        # Iterative postorder avoids a Python recursion ceiling for deep method chains.
        stack = [(root, False)]
        visiting: set[str] = set()
        while stack:
            ref, completed = stack.pop()
            if ref in memo:
                continue
            obj = objects.get(ref)
            if obj is None:
                raise KernelError("METHOD_INPUT_UNAVAILABLE", "当前方法所需材料在此位置不可见。")
            if completed:
                result = [material for child in obj["inputs"] for material in memo[child]]
                if obj["order"] == "reverse":
                    result.reverse()
                memo[ref] = result[:obj["limit"]] if obj["limit"] is not None else result
                visiting.remove(ref)
            elif obj["kind"] == "content":
                memo[ref] = [Material.model_validate({
                    key: obj[key] for key in Material.model_fields if key in obj
                })]
            elif obj["kind"] == "choice":
                memo[ref] = [Material.model_validate(material) for material in obj["materials"]]
            else:
                if ref in visiting:
                    raise KernelError("METHOD_CYCLE", "方法引用形成了无法有限求值的循环。")
                visiting.add(ref)
                instruction = obj.get("instruction", "")
                if instruction and instruction not in seen_instructions:
                    instructions.append(instruction)
                    seen_instructions.add(instruction)
                stack.append((ref, True))
                stack.extend((child, False) for child in reversed(obj["inputs"]))
        return list(memo[root])

    groups, materials = [], []
    for method_id in sorted(methods):
        group = expand(method_id)
        groups.append([material.id for material in group])
        materials.extend(group)
    return materials, groups, "\n".join(instructions)


def reflection_frame(state: Snapshot, scope: str, default_ids: list[str]) -> ReflectionFrame:
    assert state.reflection is not None
    if _is_v2(state):
        return _frame_v2(state, scope, default_ids)
    objects = objects_for(state, scope)
    active = sorted(state.reflection.adoptions.get(scope, []))
    if active:
        materials, groups, instruction = _execute(objects, active)
    else:
        materials = [
            Material.model_validate({key: objects[ref][key] for key in Material.model_fields if key in objects[ref]})
            for ref in default_ids if ref in objects and objects[ref]["kind"] == "content"
        ]
        groups, instruction = [[material.id for material in materials]], ""
    selected = [material.id for material in materials]
    # Request size is bounded, but adopted dependencies are never silently omitted.
    required = set(active) | set(selected)
    frontier = list(active)
    while frontier:
        obj = objects[frontier.pop()]
        for ref in obj.get("inputs", []):
            if ref not in required:
                required.add(ref)
                if objects[ref]["kind"] == "method":
                    frontier.append(ref)
    rows = [obj for ref, obj in objects.items() if ref in required]
    extras = [obj for ref, obj in objects.items() if ref not in required]
    # Offer fresh methods/choices and recent narrative, plus reusable initial carriers.
    rows.extend(extras[-max(0, 96 - len(rows)): ] if len(rows) < 96 else [])
    scopes = [{"id": position_scope(p.id), "name": p.name} for p in state.positions]
    if scope.startswith("reading:"):
        scopes.append({"id": scope, "name": "本条阅读"})
    digest = fingerprint({
        "groups": groups, "materials": [m.model_dump(exclude={"private_scope"}) for m in materials],
        "instruction": instruction,
    })
    previews = []
    for row in rows:
        preview = {**row, "text": row.get("text", "")[:800]}
        preview["preview_truncated"] = len(row.get("text", "")) > 800
        if row["kind"] == "choice":
            preview["materials"] = [{**material, "text": material["text"][:200]}
                                    for material in row["materials"][:8]]
            preview["preview_truncated"] |= (
                len(row["materials"]) > 8 or any(len(m["text"]) > 200 for m in row["materials"])
            )
        previews.append(preview)
    return ReflectionFrame(
        objects=previews, scopes=scopes, active_methods=active, selected_inputs=selected,
        groups=groups, materials=materials, instruction=instruction, input_fingerprint=digest,
    )



def _frame_v2(state: Snapshot, scope: str, default_ids: list[str]) -> ReflectionFrame:
    from .policy_compiler import execute_policy

    assert state.reflection is not None
    objects = objects_for(state, scope)
    active = sorted(state.reflection.adoptions.get(scope, []))
    execution = execute_policy(objects, state.reflection.object_versions, scope, active, default_ids=default_ids)
    scopes = [{"id": position_scope(p.id), "name": p.name} for p in state.positions]
    if scope.startswith("reading:"):
        scopes.append({"id": scope, "name": "本条阅读"})
    previews = []
    for row in objects.values():
        text = row.get("text", "")
        preview = {**row, "text": text[:800], "preview_truncated": len(text) > 800,
                   "full_length": len(text)}
        if "materials" in row:
            preview["materials"] = [{**m, "text": m["text"][:200]} for m in row["materials"][:8]]
            preview["preview_truncated"] |= (len(row["materials"]) > 8
                                              or any(len(m["text"]) > 200 for m in row["materials"]))
        previews.append(preview)
    return ReflectionFrame(
        language_version=2, objects=previews, full_objects=list(objects.values()), scopes=scopes,
        active_methods=active, selected_inputs=[m.id for m in execution.materials],
        groups=[group.material_ids or _group_materials(group) for group in execution.view.groups],
        materials=execution.materials, instruction=execution.instruction, view=execution.view,
        dependencies=execution.dependencies, live_dependencies=execution.live_dependencies,
        plan_fingerprint=execution.plan_fingerprint,
        input_fingerprint=execution.input_fingerprint, definitions=execution.definitions,
    )


def _group_materials(group) -> list[str]:
    result = list(group.material_ids)
    for child in group.children:
        result.extend(_group_materials(child))
    return result

def actor_projection(state: Snapshot, frame: GenerationFrame) -> GenerationFrame:
    default_ids = [use["id"] for use in frame.active_uses]
    scope = position_scope(frame.position_id)
    reflection = reflection_frame(state, scope, default_ids)
    if reflection.active_methods:
        frame.active_uses = [
            {"id": m.id, "expression_id": m.expression_id or m.id, "role": m.role,
             "text": m.text, "values": [m.text]}
            for m in reflection.materials
        ]
        # Old prose cannot quietly undo the method's actual input selection.
        frame.seed_text = "\n".join(material.text for material in reflection.materials)
        frame.recent_surface = []
    frame.reflection = reflection
    frame.condition_fingerprint = fingerprint({
        "active": frame.active_uses, "method_plan": reflection.input_fingerprint,
    })
    return frame


def reading_projection(state: Snapshot, frame: ReadingFrame, reading_id: str) -> ReadingFrame:
    scope = reading_scope(reading_id)
    reflection = reflection_frame(state, scope, [f"surface:{c['position_id']}" for c in frame.candidates])
    if reflection.active_methods:
        selected_positions = {material.position_id for material in reflection.materials if material.position_id}
        if frame.required_focus_position_id:
            selected_positions.add(frame.required_focus_position_id)
        if selected_positions:
            frame.candidates = [c for c in frame.candidates if c["position_id"] in selected_positions]
        # Preserve chronological reading history; method grouping drives the new continuation.
    frame.reflection = reflection
    return frame


def validate_reflection(proposal: ReflectionProposal | None, frame: ReflectionFrame | None) -> None:
    if proposal is None:
        return
    if frame is None:
        raise KernelError("METHOD_MODE_INVALID", "经典实验不执行反身方法。")
    objects = {obj["id"]: obj for obj in frame.objects}
    if frame.language_version == 1 and (proposal.responses or any(m.language_version == 2 for m in proposal.methods)):
        raise KernelError("METHOD_MODE_INVALID", "第二版方法与局部回应只能用于第二版反身实验。")
    scopes = {scope["id"] for scope in frame.scopes}
    for method in proposal.methods:
        if not {ref.object_id for ref in _method_refs(method)} <= objects.keys():
            raise KernelError("METHOD_REFERENCE_INVALID", "新方法只能采用本次可见的材料、选择或方法。")
        for ref in _method_refs(method):
            if ref.mode == "version" and objects[ref.object_id].get("version") != ref.version:
                raise KernelError("METHOD_REFERENCE_INVALID", "精确版本须来自本次可见对象的版本。")
        if method.revises is not None and objects.get(method.revises, {}).get("kind") != "method":
            raise KernelError("METHOD_REFERENCE_INVALID", "方法修订须引用本次可见的已有方法。")
    for response in proposal.responses:
        if not set(response.about + response.evidence) <= objects.keys():
            raise KernelError("RESPONSE_REFERENCE_INVALID", "局部回应只能引用本次可见对象。")

    for adoption in proposal.adoptions:
        if adoption.scope_id not in scopes:
            raise KernelError("METHOD_SCOPE_INVALID", "方法采用不能进入本次未开放的作用范围。")
        if adoption.method_id.startswith("draft:"):
            suffix = adoption.method_id.removeprefix("draft:")
            if (not suffix.isdecimal() or str(int(suffix)) != suffix
                    or int(suffix) >= len(proposal.methods) or adoption.action != "adopt"):
                raise KernelError("METHOD_REFERENCE_INVALID", "新方法的局部编号无效。")
        elif objects.get(adoption.method_id, {}).get("kind") != "method":
            raise KernelError("METHOD_REFERENCE_INVALID", "采用须引用本次可见的方法。")


def install_feedback(
    state: Snapshot, entries: list[tuple[str, ReflectionProposal | None, ReflectionFrame | None]],
) -> None:
    """Validate all, then atomically combine scoped changes; no last-arrival winner."""
    if _is_v2(state):
        _install_v2(state, entries)
        return
    entries = sorted(entries, key=lambda entry: entry[0])
    for _, proposal, frame in entries:
        validate_reflection(proposal, frame)
    if state.reflection is None:
        return
    result = state.reflection.model_copy(deep=True)
    incoming: dict[str, set[str]] = defaultdict(set)
    removed: dict[str, set[str]] = defaultdict(set)
    methods = {method.id: method for method in result.methods}
    for source_scope, proposal, _ in entries:
        if proposal is None:
            continue
        aliases = {}
        for index, draft in enumerate(proposal.methods):
            legacy_draft = draft.model_dump(exclude={"language_version", "program"})
            identifier = f"method-{fingerprint([state.run.revision, source_scope, index, legacy_draft])[:24]}"
            record = MethodRecord(
                **legacy_draft, id=identifier, created_revision=state.run.revision, source_scope=source_scope,
            )
            if identifier in methods and methods[identifier] != record:
                raise KernelError("METHOD_REFERENCE_INVALID", "方法版本记录冲突。")
            methods[identifier] = record
            aliases[f"draft:{index}"] = identifier
        for adoption in proposal.adoptions:
            method_id = aliases.get(adoption.method_id, adoption.method_id)
            if adoption.action == "release":
                removed[adoption.scope_id].add(method_id)
            else:
                incoming[adoption.scope_id].add(method_id)
                if methods[method_id].revises:
                    removed[adoption.scope_id].add(methods[method_id].revises)
    for scope in incoming.keys() | removed.keys():
        adopted = (set(result.adoptions.get(scope, [])) - removed[scope]) | incoming[scope]
        result.adoptions[scope] = sorted(adopted)
    result.methods = sorted(methods.values(), key=lambda method: (method.created_revision, method.id))
    detached = state.model_copy(deep=True)
    detached.reflection = result
    for scope, adopted in result.adoptions.items():
        _execute(objects_for(detached, scope), adopted)
    state.reflection = result


def capture_choice(state: Snapshot, scope: str, stage: str, frame: ReflectionFrame | None) -> ChoiceRecord | None:
    if frame is None or state.reflection is None:
        return None
    record = ChoiceRecord(
        id=f"choice-{fingerprint([state.run.revision, scope, stage])[:24]}",
        revision=state.run.revision, scope_id=scope, stage=stage,
        offered=[obj["id"] for obj in frame.objects], selected=frame.selected_inputs,
        methods=frame.active_methods, groups=frame.groups,
        materials=[m.model_copy(deep=True) for m in frame.materials], input_fingerprint=frame.input_fingerprint,
        view=frame.view.model_copy(deep=True) if frame.view else None,
        plan_fingerprint=frame.plan_fingerprint, dependencies=[ref.model_copy(deep=True) for ref in frame.dependencies],
        live_dependencies=list(frame.live_dependencies),
    )
    existing = next((choice for choice in state.reflection.choices if choice.id == record.id), None)
    if existing is not None:
        if existing != record:
            raise KernelError("CHOICE_CONFLICT", "同一生成阶段的实际选择记录发生冲突。")
        return existing
    state.reflection.choices.append(record)
    if _is_v2(state):
        _capture_v2(state, record, frame)
    return record


def ablate_method(state: Snapshot, scope: str, method_id: str) -> dict:
    if state.reflection is None or method_id not in state.reflection.adoptions.get(scope, []):
        raise KernelError("METHOD_REFERENCE_INVALID", "请选择本作用范围已经采用的方法。")
    detached = state.model_copy(deep=True)
    assert detached.reflection is not None
    detached.reflection.adoptions[scope].remove(method_id)
    if scope.startswith("position:"):
        from .kernel import frame_for

        before = frame_for(state, scope.removeprefix("position:")).reflection
        after = frame_for(detached, scope.removeprefix("position:")).reflection
    else:
        default = [f"surface:{p.id}" for p in state.positions]
        before = reflection_frame(state, scope, default)
        after = reflection_frame(detached, scope, default)
    assert before is not None and after is not None
    return {
        "before_fingerprint": before.input_fingerprint, "after_fingerprint": after.input_fingerprint,
        "before_selected": before.selected_inputs, "after_selected": after.selected_inputs,
        "structural_effect": before.input_fingerprint != after.input_fingerprint,
        "note": "仅在副本中移除方法并重新编译实际输入；不调用模型、不修改原档，不证明文学或意图效果。",
    }


def _install_v2(state: Snapshot, entries) -> None:
    from pydantic import ValidationError

    from .object_store import archive_objects
    from .policy_compiler import execute_policy

    assert state.reflection is not None
    entries = sorted(entries, key=lambda entry: entry[0])
    for _, proposal, frame in entries:
        validate_reflection(proposal, frame)
    detached = state.model_copy(deep=True)
    result = detached.reflection
    assert result is not None
    methods = {method.id: method for method in result.methods}
    responses = {response.id: response for response in result.responses}
    incoming: dict[str, set[str]] = defaultdict(set)
    removed: dict[str, set[str]] = defaultdict(set)
    compile_roots: list[tuple[str, str]] = []
    for source_scope, proposal, frame in entries:
        if proposal is None:
            continue
        assert frame is not None
        # Archive the actual request's complete referenced values, never truncated previews.
        used = {ref.object_id for draft in proposal.methods for ref in _method_refs(draft)}
        used.update(ref for response in proposal.responses for ref in response.about + response.evidence)
        used.update(ref.object_id for ref in frame.dependencies)
        result.object_versions = archive_objects(result.object_versions,
                                                  [row for row in frame.full_objects if row["id"] in used])
        aliases = {}
        for index, draft in enumerate(proposal.methods):
            identifier = f"method-{fingerprint([state.run.revision, source_scope, index, draft.model_dump()])[:24]}"
            record = MethodRecord(**draft.model_dump(), id=identifier, created_revision=state.run.revision,
                                  source_scope=source_scope)
            if identifier in methods and methods[identifier] != record:
                raise KernelError("METHOD_REFERENCE_INVALID", "方法版本记录冲突。")
            methods[identifier] = record
            aliases[f"draft:{index}"] = identifier
            compile_roots.append((source_scope, identifier))
        for index, draft in enumerate(proposal.responses):
            identifier = f"response-{fingerprint([state.run.revision, source_scope, index, draft.model_dump()])[:24]}"
            record = ResponseRecord(**draft.model_dump(), id=identifier, created_revision=state.run.revision,
                                    source_scope=source_scope)
            if identifier in responses and responses[identifier] != record:
                raise KernelError("RESPONSE_REFERENCE_INVALID", "局部回应版本记录冲突。")
            responses[identifier] = record
        for adoption in proposal.adoptions:
            method_id = aliases.get(adoption.method_id, adoption.method_id)
            if adoption.action == "release":
                removed[adoption.scope_id].add(method_id)
            else:
                incoming[adoption.scope_id].add(method_id)
                if methods[method_id].revises:
                    removed[adoption.scope_id].add(methods[method_id].revises)
    result.methods = sorted(methods.values(), key=lambda method: (method.created_revision, method.id))
    result.responses = sorted(responses.values(), key=lambda response: (response.created_revision, response.id))
    # Execute newly submitted definitions at their origin only; generated definitions install atomically.
    try:
        for scope, identifier in compile_roots:
            execution = execute_policy(objects_for(detached, scope), result.object_versions, scope, [identifier])
            for draft in execution.generated_methods:
                derived_id = f"derived-{fingerprint(draft.model_dump())[:24]}"
                if derived_id not in methods:
                    methods[derived_id] = MethodRecord(**draft.model_dump(), id=derived_id,
                                                      created_revision=state.run.revision, source_scope=scope)
            result.methods = sorted(methods.values(), key=lambda method: (method.created_revision, method.id))
        for scope in incoming.keys() | removed.keys():
            result.adoptions[scope] = sorted((set(result.adoptions.get(scope, [])) - removed[scope]) | incoming[scope])
        for scope, adopted in result.adoptions.items():
            execute_policy(objects_for(detached, scope), result.object_versions, scope, adopted)
    except ValidationError as exc:
        raise KernelError("METHOD_PROGRAM_INVALID", "方法改版结果不符合所声明的语言类型。") from exc
    changed = (result.methods, result.adoptions, result.responses) != (
        state.reflection.methods, state.reflection.adoptions, state.reflection.responses,
    )
    state.reflection = result
    if changed:
        state.policy_revision += 1


def _capture_v2(state: Snapshot, record: ChoiceRecord, frame: ReflectionFrame) -> None:
    from .object_store import archive_objects

    assert state.reflection is not None and frame.view is not None
    required = {ref.object_id for ref in frame.dependencies}
    state.reflection.object_versions = archive_objects(
        state.reflection.object_versions, [row for row in frame.full_objects if row["id"] in required],
    )
    view_id = f"view-{fingerprint([record.id, frame.input_fingerprint])[:24]}"
    state.reflection.views.append(ViewRecord(
        id=view_id, created_revision=record.revision, scope_id=record.scope_id,
        view=frame.view.model_copy(deep=True), materials=[m.model_copy(deep=True) for m in frame.materials],
        dependencies=[ref.model_copy(deep=True) for ref in frame.dependencies],
        live_dependencies=list(frame.live_dependencies),
    ))
    previous = next((choice for choice in reversed(state.reflection.choices[:-1])
                     if choice.scope_id == record.scope_id and choice.stage == record.stage), None)
    selected_before = set(previous.selected) if previous else set()
    selected_after = set(record.selected)
    state.reflection.effects.append(EffectRecord(
        id=f"effect-{fingerprint(record.id)[:24]}", revision=record.revision, scope_id=record.scope_id,
        stage=record.stage, choice_id=record.id, previous_choice_id=previous.id if previous else None,
        method_ids=record.methods,
        before_fingerprint=previous.input_fingerprint if previous else None,
        after_fingerprint=record.input_fingerprint,
        changed=previous is None or previous.input_fingerprint != record.input_fingerprint,
        selected_added=sorted(selected_after - selected_before), selected_removed=sorted(selected_before - selected_after),
        response_ids=sorted({ref.object_id for ref in frame.dependencies if ref.object_id.startswith("response-")}),
    ))
