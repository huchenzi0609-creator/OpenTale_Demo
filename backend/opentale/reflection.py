"""Finite, source-blind method execution. No model, code evaluation or truth adjudication."""

from __future__ import annotations

from collections import defaultdict

from .domain import GenerationFrame, ReadingFrame, Snapshot
from .kernel import KernelError, fingerprint
from .reflection_types import (
    ChoiceRecord,
    Material,
    MethodRecord,
    ReflectionFrame,
    ReflectionProposal,
)


def position_scope(position_id: str) -> str:
    return f"position:{position_id}"


def reading_scope(reading_id: str) -> str:
    return f"reading:{reading_id}"


def _material_visible(material: Material, scope: str) -> bool:
    return material.role != "主体背景" or scope == position_scope(material.position_id or "")


def objects_for(state: Snapshot, scope: str) -> dict[str, dict]:
    """Expose reusable fiction/methods, never private peer profiles or creator metadata."""
    assert state.reflection is not None
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
        objects[material.id] = {"id": material.id, "kind": "content", **material.model_dump()}
    for expression in state.expressions:
        if expression.created_revision == 0:
            continue  # Initial biographies are only exposed through their own scoped use.
        material = Material(id=expression.id, text=expression.text, expression_id=expression.id,
                            role="已形成的叙述")
        objects[material.id] = {"kind": "content", **material.model_dump()}
    for continuation in state.continuations:
        if continuation.surface:
            material = Material(
                id=f"surface:{continuation.position_id}", text=continuation.surface,
                position_id=continuation.position_id, role="当前生活",
            )
            objects[material.id] = {"kind": "content", **material.model_dump()}
    for choice in sorted(state.reflection.choices, key=lambda item: (item.revision, item.id)):
        # Only adopted world materials are exported, never a reading's private prose history.
        materials = [material for material in choice.materials if _material_visible(material, scope)]
        if not materials:
            continue
        objects[choice.id] = {
            "id": choice.id, "kind": "choice", "text": "\n".join(m.text for m in materials),
            "selected": [material.id for material in materials],
            "materials": [material.model_dump() for material in materials],
            "groups": [[ref for ref in group if ref in {m.id for m in materials}]
                       for group in choice.groups],
            "offered": choice.offered,
            "offered_count": len(choice.offered), "selected_count": len(materials),
        }
    # Prior-version references form a DAG; a method is visible only when all inputs are visible.
    unresolved = list(state.reflection.methods)
    while unresolved:
        newly_visible = [method for method in unresolved if all(ref in objects for ref in method.inputs)]
        if not newly_visible:
            break
        for method in newly_visible:
            objects[method.id] = {
                "id": method.id, "kind": "method", "text": method.instruction or method.title,
                **method.model_dump(exclude={"source_scope", "created_revision"}),
            }
        accepted = {method.id for method in newly_visible}
        unresolved = [method for method in unresolved if method.id not in accepted]
    return objects


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
        "groups": groups, "materials": [m.model_dump() for m in materials],
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
    scopes = {scope["id"] for scope in frame.scopes}
    for method in proposal.methods:
        if not set(method.inputs) <= objects.keys():
            raise KernelError("METHOD_REFERENCE_INVALID", "新方法只能采用本次可见的材料、选择或方法。")
        if method.revises is not None and objects.get(method.revises, {}).get("kind") != "method":
            raise KernelError("METHOD_REFERENCE_INVALID", "方法修订须引用本次可见的已有方法。")
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
            identifier = f"method-{fingerprint([state.run.revision, source_scope, index, draft.model_dump()])[:24]}"
            record = MethodRecord(
                **draft.model_dump(), id=identifier, created_revision=state.run.revision, source_scope=source_scope,
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
    )
    existing = next((choice for choice in state.reflection.choices if choice.id == record.id), None)
    if existing is not None:
        if existing != record:
            raise KernelError("CHOICE_CONFLICT", "同一生成阶段的实际选择记录发生冲突。")
        return existing
    state.reflection.choices.append(record)
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
